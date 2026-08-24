from __future__ import annotations  # PEP 604 unions (str | None) on Python 3.9

import os
import re
import json
import threading
import time
import logging
import requests   # used at module scope by several handlers; only ever imported inside
                  # functions before, so those call sites would NameError at runtime
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env", override=True)

import functools
import hmac
from flask import Flask, jsonify, render_template, request, redirect, session, url_for
from authlib.integrations.flask_client import OAuth
from zoho_client import ZohoClient, LOCAL_TZ
from ringcx_client import RingCXClient
from billing_report import (build_report as build_billing_report,
                            build_pace_curve, is_connected as _billing_connected)
from v5_report import (build_report as build_v5_report,
                       parse_interaction_csv, CsvShapeError, EmptyReportError,
                       parse_ts as _v5_parse_ts)
from telegram_client import TelegramClient
from books_client import BooksClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", os.urandom(32).hex())
app.config["PREFERRED_URL_SCHEME"] = "https"

# Trust reverse-proxy headers (Render) so url_for generates https:// URLs
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1, x_for=1)

# ────────── Google OAuth SSO ──────────
ALLOWED_DOMAIN = "goalsplasticsurgery.com"
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")

# Machine-to-machine shared secret for the back-office bot's overdue-calls poller.
# Set the SAME value here (Render env: OVERDUE_API_KEY) and in the back-office app.
OVERDUE_API_KEY = os.environ.get("OVERDUE_API_KEY", "")
if not OVERDUE_API_KEY:
    # Fail-closed is right, but silent fail-closed is how this went unnoticed:
    # every /api/overdue-calls request 401s, so the back-office bot never learns
    # about an overdue scheduled call and nobody is ever told why.
    print(
        "[overdue] WARNING: OVERDUE_API_KEY is not set — /api/overdue-calls will "
        "reject every request (401). The back-office bot cannot distribute overdue "
        "scheduled calls until this is set to the same value on both services.",
        flush=True,
    )

def _valid_api_key() -> bool:
    """True if the request carries the shared overdue-calls secret. Used INSTEAD of
    session login for the bot-facing endpoints so they work without a browser."""
    if not OVERDUE_API_KEY:
        return False  # fail closed if the secret isn't configured
    key = request.headers.get("X-API-Key", "") or request.args.get("key", "")
    return key == OVERDUE_API_KEY

GOOGLE_REDIRECT_URI = os.environ.get(
    "GOOGLE_REDIRECT_URI",
    "https://call-tracker-3z6t.onrender.com/auth/callback",
)

oauth = OAuth(app)
_google_reg = {
    "name": "google",
    "client_id": GOOGLE_CLIENT_ID,
    "client_secret": GOOGLE_CLIENT_SECRET,
    "server_metadata_url": "https://accounts.google.com/.well-known/openid-configuration",
    "client_kwargs": {"scope": "openid email profile"},
}
if GOOGLE_REDIRECT_URI:
    _google_reg["redirect_uri"] = GOOGLE_REDIRECT_URI
oauth.register(**_google_reg)


def login_required(f):
    """Decorator: redirect to /login if not authenticated."""
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if not GOOGLE_CLIENT_ID:
            return f(*args, **kwargs)  # SSO disabled if no credentials
        if not session.get("user"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "unauthorized"}), 401
            return redirect("/login")
        return f(*args, **kwargs)
    return wrapper


def login_or_api_key(f):
    """A browser session OR the shared machine key. For endpoints the board renders AND the
    back-office bot polls, so the bot doesn't need a session it can never have."""
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if _valid_api_key():
            return f(*args, **kwargs)
        return login_required(f)(*args, **kwargs)
    return wrapper

REFRESH_INTERVAL_SECONDS = 60

_cache: dict = {"data": None, "last_updated": None, "error": None}
_lock = threading.Lock()

# Shared clients so access tokens are cached across all requests
_zoho = ZohoClient()
_ringcx = RingCXClient()
_telegram = TelegramClient()
_books = BooksClient()

# In-memory cache + lock for api_quotes — prevents concurrent OOM-spiking fetches.
import threading as _threading
_quotes_cache: dict = {}
_QUOTES_CACHE_TTL = 600        # 10 minutes
_quotes_fetch_lock = _threading.Lock()  # only one thread fetches at a time

# ────────── Persistent disk (Render /data mount) ──────────
# Use persistent disk mount if available (Render), fallback to local
_data_dir = Path("/data") if Path("/data").exists() else Path(__file__).parent
if not Path("/data").exists() and os.environ.get("RENDER"):
    # On Render without the disk mounted, this directory is wiped on every deploy and
    # restart. Losing resolved_calls.json means every still-overdue call is distributed to
    # Telegram a second time and every note vanishes. Silence is how that went unnoticed.
    print(
        "[storage] WARNING: /data is not mounted — resolved/distributed flags and notes are "
        "being written to EPHEMERAL container storage and will be lost on the next deploy or "
        "restart, re-distributing every overdue call. Add the `disk:` block to render.yaml.",
        flush=True,
    )
CACHE_PERSIST_PATH = _data_dir / "dashboard_cache.json"

def _persist_cache(data: dict, last_updated: str) -> None:
    """Write dashboard data to disk so it survives restarts."""
    try:
        CACHE_PERSIST_PATH.write_text(json.dumps(
            {"data": data, "last_updated": last_updated}, separators=(",", ":")
        ))
    except Exception as e:
        log.warning("Could not persist cache to disk: %s", e)

def _load_persisted_cache() -> None:
    """Pre-warm in-memory cache from disk on startup (instant first response)."""
    if not CACHE_PERSIST_PATH.exists():
        return
    try:
        blob = json.loads(CACHE_PERSIST_PATH.read_text())
        with _lock:
            _cache["data"] = blob["data"]
            _cache["last_updated"] = blob.get("last_updated")
            _cache["stale"] = True   # flag so UI can show "updating…"
        log.info("Pre-warmed cache from disk (%s)", CACHE_PERSIST_PATH.name)
    except Exception as e:
        log.warning("Could not load persisted cache: %s", e)

# ────────── Resolved-overdue persistence ──────────


RESOLVED_PATH = _data_dir / "resolved_calls.json"
_resolved_lock = threading.Lock()

# ── AI-handled markers, keyed by 10-digit phone ───────────────────────────────
# The back-office bot POSTs here whenever its AI actually replied to a patient. The
# tracker had no idea the bot existed: a call the bot had already rescheduled or booked
# still sat here looking untouched, and coordinators chased patients it had just handled.
AI_HANDLED_PATH = _data_dir / "ai_handled.json"
_ai_lock = threading.Lock()

def _phone10(value) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits[-10:] if len(digits) >= 10 else ""

def _load_ai_handled() -> dict:
    if not AI_HANDLED_PATH.exists():
        return {}
    try:
        return json.loads(AI_HANDLED_PATH.read_text())
    except Exception:
        return {}

def _save_ai_handled(data: dict) -> None:
    tmp = AI_HANDLED_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(AI_HANDLED_PATH)   # atomic: a crash mid-write can't truncate the store

def _prune_ai_handled(data: dict) -> dict:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    return {k: v for k, v in data.items() if (v.get("at") or "") > cutoff}

def ai_handled_data() -> dict:
    with _ai_lock:
        return _prune_ai_handled(_load_ai_handled())

def _load_resolved() -> dict:
    if not RESOLVED_PATH.exists():
        return {}
    try:
        return json.loads(RESOLVED_PATH.read_text())
    except Exception as e:
        # Returning {} here silently discarded every resolved/distributed flag, which
        # re-distributes every overdue call. Preserve the bad file and shout about it.
        try:
            RESOLVED_PATH.replace(RESOLVED_PATH.with_suffix(".corrupt"))
        except Exception:
            pass
        print(f"[storage] ERROR: resolved_calls.json unreadable ({e}); saved as .corrupt and "
              f"starting empty — overdue calls may be re-distributed.", flush=True)
        return {}

def _save_resolved(data: dict) -> None:
    # Atomic: a crash between truncate and write used to leave an empty/partial file,
    # which _load_resolved then read as "nothing is resolved".
    tmp = RESOLVED_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(RESOLVED_PATH)

def _prune_resolved(data: dict) -> dict:
    """Drop entries older than 7 days, but keep any that carry a note so
    manually-entered notes persist indefinitely."""
    cutoff = (datetime.now(timezone.utc) - __import__("datetime").timedelta(days=7)).isoformat()
    return {
        k: v for k, v in data.items()
        if v.get("at", "") > cutoff or (v.get("note") or "").strip()
    }

def resolved_ids() -> set:
    with _resolved_lock:
        data = _prune_resolved(_load_resolved())
        return set(data.keys())

def resolved_data_full() -> dict:
    """Return the full resolved dict (includes notes)."""
    with _resolved_lock:
        return _prune_resolved(_load_resolved())


def _annotate_resolved(annotated: dict, rd: dict) -> None:
    """Annotate scheduled call records with resolved flag, notes, and matched call data.

    If a resolved-manually record later has a call observed in the system
    (via normal refresh), we upgrade it to resolved_with_match and persist
    the match back so it sticks.
    """
    rids = set(rd.keys())
    if not annotated.get("scheduled_calls"):
        return
    ai_map = ai_handled_data()
    newly_matched = {}  # {rec_id: matched_call_data} — to persist back
    for r in annotated["scheduled_calls"]["records"]:
        rid = r.get("id")
        r["resolved"] = rid in rids
        r["sms_sent"] = rid in _sms_sent
        entry = rd.get(rid, {})
        r["note"] = entry.get("note", "")
        r["resolved_by"] = entry.get("resolved_by", "")
        r["assigned_to"] = entry.get("assigned_to", "")
        r["distributed"] = entry.get("distributed", False)
        r["distributed_by"] = entry.get("distributed_by", "")
        r["claimed_by"] = entry.get("claimed_by", "")
        r["claimed_at"] = entry.get("claimed_at", "")
        ai = ai_map.get(_phone10(r.get("phone")))
        r["ai_handled"] = bool(ai)
        r["ai_scenario"] = (ai or {}).get("scenario", "")
        r["ai_at"] = (ai or {}).get("at", "")
        r["ai_reply"] = (ai or {}).get("reply", "")
        r["ai_inbound"] = (ai or {}).get("inbound", "")
        if not r["resolved"]:
            continue
        # Overlay matched call data if this record was resolved with a call match
        matched = entry.get("matched_call")
        if matched:
            if matched.get("actual_call_time"):
                r["actual_call_time"] = matched["actual_call_time"]
            if matched.get("disposition"):
                r["disposition"] = matched["disposition"]
            if matched.get("caller"):
                r["caller"] = matched["caller"]
            if matched.get("offset_minutes") is not None:
                r["offset_minutes"] = matched["offset_minutes"]
            if matched.get("recording_url"):
                r["recording_url"] = matched["recording_url"]
            r["status"] = "completed"
            r["on_time"] = (matched.get("offset_minutes") is not None
                            and -15 <= matched["offset_minutes"] <= 10)
            r["resolved_with_match"] = True
        elif r.get("actual_call_time"):
            # A call appeared in the system after manual resolution — upgrade it
            r["resolved_with_match"] = True
            r["status"] = "completed"
            # Build match data from the refresh-provided fields
            offset = r.get("offset_minutes")
            r["on_time"] = offset is not None and -15 <= offset <= 10
            newly_matched[rid] = {
                "actual_call_time": r["actual_call_time"],
                "disposition": r.get("disposition"),
                "caller": r.get("caller"),
                "offset_minutes": offset,
                "recording_url": r.get("recording_url"),
            }

    # Persist any newly-observed call matches back to resolved_calls.json
    if newly_matched:
        with _resolved_lock:
            data = _load_resolved()
            for rid, match in newly_matched.items():
                if rid in data:
                    data[rid]["matched_call"] = match
                    log.info("Auto-matched call for resolved record %s → %s",
                             rid, match.get("actual_call_time"))
            _save_resolved(data)


def _find_closest_call_for_record(rec_id: str):
    """Find the logged call closest to the scheduled time for a given record.

    Looks up the record from the dashboard cache, gets the contact phone,
    searches Zoho for all outbound calls to that phone, and returns the
    one whose Call_Start_Time is closest to the scheduled time.
    """
    try:
        # Find the record in cache
        with _lock:
            sc_data = (_cache.get("data") or {}).get("scheduled_calls", {})
            records = sc_data.get("records") or []

        rec = next((r for r in records if r.get("id") == rec_id), None)
        if not rec:
            log.warning("resolve: record %s not found in cache", rec_id)
            return None

        phone = rec.get("phone")
        scheduled_time_str = rec.get("scheduled_time") or rec.get("created_time")
        if not phone or not scheduled_time_str:
            log.warning("resolve: no phone or scheduled time for %s", rec_id)
            return None

        # Parse scheduled time
        try:
            scheduled_dt = datetime.fromisoformat(scheduled_time_str.replace("Z", "+00:00"))
        except ValueError:
            return None

        # Fetch all outbound calls for this phone today
        calls_by_phone = _zoho._fetch_all_calls_for_phones([phone])
        from zoho_client import normalize_phone
        norm_phone = normalize_phone(phone)
        calls = calls_by_phone.get(norm_phone, []) if norm_phone else []

        if not calls:
            log.info("resolve: no logged calls found for phone %s", phone)
            return None

        # Find the call closest to the scheduled time
        def time_distance(call):
            t_str = call.get("Call_Start_Time")
            if not t_str:
                return float("inf")
            try:
                t = datetime.fromisoformat(t_str.replace("Z", "+00:00"))
                return abs((t - scheduled_dt).total_seconds())
            except ValueError:
                return float("inf")

        closest = min(calls, key=time_distance)
        dist = time_distance(closest)
        if dist == float("inf"):
            return None

        owner = closest.get("Owner")
        caller_name = (
            owner.get("name") if isinstance(owner, dict)
            else owner if isinstance(owner, str)
            else None
        )

        offset_min = None
        call_dt_str = closest.get("Call_Start_Time")
        if call_dt_str:
            try:
                call_dt = datetime.fromisoformat(call_dt_str.replace("Z", "+00:00"))
                offset_min = round((call_dt - scheduled_dt).total_seconds() / 60, 1)
            except ValueError:
                pass

        matched = {
            "actual_call_time": closest.get("Call_Start_Time"),
            "disposition": closest.get("Outgoing_call_disposition"),
            "caller": caller_name,
            "offset_minutes": offset_min,
            "recording_url": _zoho._extract_recording_url(
                closest.get("Description") or ""
            ),
            "call_id": closest.get("id"),
        }
        log.info("resolve: matched call for %s → %s (offset %.1f min)",
                 rec_id, matched["actual_call_time"], offset_min or 0)
        return matched

    except Exception as e:
        log.error("resolve: error finding closest call for %s: %s", rec_id, e)
        return None


def _refresh():
    log.info("Refreshing dashboard data...")
    try:
        # Fetch RingEX call log as a supplemental source so that ALL dial
        # attempts (including short RingCX campaign calls that Zoho never
        # logs) feed into classification from the start.
        # RingEX log + RingCX dialer CDR — see _fetch_supplemental. The background thread can
        # afford the full fetch; it also primes the request-thread cache.
        supplemental = _fetch_supplemental() or None
        with _sup_lock:
            _sup_cache.update({"at": time.time(), "val": supplemental or {}})
        data = _zoho.get_dashboard_data(supplemental_calls=supplemental)
        ts = datetime.now(timezone.utc).isoformat()
        with _lock:
            _cache["data"] = data
            _cache["last_updated"] = ts
            _cache["error"] = None
            _cache["stale"] = False
        _persist_cache(data, ts)
        log.info("Refresh complete.")
    except BaseException as exc:
        # BaseException, not Exception: a refresh that raised something outside
        # the Exception hierarchy (e.g. a library raising GeneratorExit/SystemExit
        # under a worker recycle) used to slip through here, leaving the cache
        # frozen with error=None — invisible. Record everything.
        log.error("Refresh failed: %r", exc)
        with _lock:
            _cache["error"] = f"{type(exc).__name__}: {exc}"


# ── Self-healing background refresh ────────────────────────────────────────
# The board kept freezing: the loop's refresh would stop completing and the
# cache silently went stale for hours. Design goals now:
#   • the loop can NEVER be blocked by a slow/wedged refresh,
#   • a refresh runs in a plain daemon thread (the same primitive the manual
#     /api/refresh has always used reliably here — NOT a ThreadPoolExecutor),
#   • only one refresh runs at a time, but a refresh that overruns a watchdog
#     is abandoned so a fresh one can supersede it,
#   • if the loop thread ever dies, request traffic still drives refreshes.
REFRESH_WATCHDOG_SECONDS = 150   # a healthy refresh finishes in well under this

_refresh_lock = threading.Lock()
_refresh_running = False
_refresh_started_at = 0.0
_refresh_token = 0


def _cache_age_seconds():
    """Seconds since the cache last updated, or None if never."""
    lu = _cache.get("last_updated")
    if not lu:
        return None
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(lu)).total_seconds()
    except Exception:
        return None


def _kick_refresh() -> bool:
    """Start a refresh in a fresh daemon thread unless a healthy one is already
    running. A refresh that's been running past the watchdog is treated as wedged
    and superseded. Never blocks; returns whether a new refresh was started."""
    global _refresh_running, _refresh_started_at, _refresh_token
    now = time.monotonic()
    with _refresh_lock:
        if _refresh_running and (now - _refresh_started_at) < REFRESH_WATCHDOG_SECONDS:
            return False  # a healthy refresh is already in progress
        _refresh_token += 1
        tok = _refresh_token
        _refresh_running = True
        _refresh_started_at = now

    def _work():
        global _refresh_running
        try:
            _refresh()
        finally:
            with _refresh_lock:
                # Only clear the flag if we're still the current refresh. If the
                # watchdog already superseded us, the newer refresh owns it.
                if _refresh_token == tok:
                    _refresh_running = False

    threading.Thread(target=_work, daemon=True, name="refresh").start()
    return True


def _self_heal_if_stale():
    """Belt-and-suspenders: if the cache is stale past a couple of intervals,
    kick a refresh from whatever thread is serving traffic. The frontend polls
    /api/data constantly, so this keeps the board fresh even if the background
    loop thread has died."""
    age = _cache_age_seconds()
    if age is not None and age > 2 * REFRESH_INTERVAL_SECONDS:
        if _kick_refresh():
            log.warning("Cache %.0fs stale — self-heal refresh kicked from request path", age)


def _background_loop():
    time.sleep(30)  # Let gunicorn fully start + pass health checks before heavy Zoho API calls
    while True:
        try:
            _kick_refresh()
        except BaseException:  # the loop must NEVER die — that's what froze the board
            log.exception("Background refresh dispatcher hiccup")
        time.sleep(REFRESH_INTERVAL_SECONDS)


# ------------------------------------------------------------------ routes


@app.route("/health")
def health():
    """Render health-check probe — must respond quickly."""
    return "ok", 200


@app.route("/auth/debug")
def auth_debug():
    """Show what redirect URI would be generated — for debugging OAuth."""
    generated = url_for("auth_callback", _external=True, _scheme="https")
    env_val = GOOGLE_REDIRECT_URI
    return jsonify({
        "url_for_generated": generated,
        "env_override": env_val or "(not set)",
        "will_use": env_val or generated,
        "google_client_id_set": bool(GOOGLE_CLIENT_ID),
    })


@app.route("/login")
def login():
    if not GOOGLE_CLIENT_ID:
        return redirect("/")
    if session.get("user"):
        return redirect("/")
    return render_template("login.html")


ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "ringcx2026")


@app.route("/auth/password", methods=["POST"])
def auth_password():
    body = request.get_json(silent=True) or {}
    pw = (body.get("password") or "").strip()
    if pw == ADMIN_PASSWORD:
        session["user"] = {
            "email": "admin@goalsplasticsurgery.com",
            "name": "Admin",
            "picture": "",
        }
        log.info("Admin login via password")
        return jsonify({"ok": True})
    log.warning("Failed admin password attempt")
    return jsonify({"ok": False, "error": "Invalid password"}), 401


@app.route("/auth/google")
def auth_google():
    redirect_uri = GOOGLE_REDIRECT_URI or url_for("auth_callback", _external=True, _scheme="https")
    log.info("OAuth redirect_uri: %s", redirect_uri)
    return oauth.google.authorize_redirect(redirect_uri)


@app.route("/auth/callback")
def auth_callback():
    try:
        token = oauth.google.authorize_access_token()
    except Exception as e:
        log.error("OAuth token exchange failed: %s", e)
        return render_template("login.html", error=f"Login failed: {e}"), 500
    userinfo = token.get("userinfo") or oauth.google.userinfo()
    email = (userinfo.get("email") or "").lower()
    if not email.endswith(f"@{ALLOWED_DOMAIN}"):
        log.warning("Login rejected for %s (not @%s)", email, ALLOWED_DOMAIN)
        return render_template("login.html",
                               error=f"Access restricted to @{ALLOWED_DOMAIN} accounts."), 403
    session["user"] = {
        "email": email,
        "name": userinfo.get("name", email.split("@")[0]),
        "picture": userinfo.get("picture", ""),
    }
    log.info("User logged in: %s", email)
    return redirect("/")


@app.route("/logout")
def logout():
    session.pop("user", None)
    return redirect("/login")


@app.route("/")
@login_required
def index():
    user = session.get("user") or {}
    return render_template("index.html", refresh_interval=REFRESH_INTERVAL_SECONDS, current_user=user)


@app.route("/v2")
@login_required
def index_v2():
    """Second version of the dashboard (compact layout, reworked columns).
    Shares the same backend/API as v1."""
    user = session.get("user") or {}
    return render_template("index_v2.html", refresh_interval=REFRESH_INTERVAL_SECONDS, current_user=user)


@app.route("/v3")
@login_required
def index_v3():
    """Snapshot of the v2 dashboard as deployed at 102e0b7 (the "call log" agent
    report cut), preserved verbatim at its own URL while /v2 continues to evolve
    separately. Shares the same backend/API."""
    user = session.get("user") or {}
    return render_template("index_v3.html", refresh_interval=REFRESH_INTERVAL_SECONDS, current_user=user)


@app.route("/v4")
@login_required
def quote_followups_page():
    """Quote Follow-Up tracker: one row per quote (Deal), with the 6-step
    follow-up task cadence synced live from the CRM's 'Quote Follow Up' tasks."""
    user = session.get("user") or {}
    return render_template("quote_followups.html", current_user=user)


# ══════════════════════════════════════════════════════════════
# v5 — talk-time scoreboard, reconciled live across RingEX + RingCX
# ══════════════════════════════════════════════════════════════
# Read-only share link for the sales floor. The board names individuals and their
# performance, so without a token configured /v5/board serves nothing at all.
SCOREBOARD_TOKEN = os.environ.get("SCOREBOARD_TOKEN", "")
if not SCOREBOARD_TOKEN:
    print(
        "[v5] SCOREBOARD_TOKEN is not set — /v5/board returns 404 for every request. "
        "Set it on Render to hand the floor a no-login link. /v5 works regardless, "
        "behind the normal Google login.",
        flush=True,
    )


# Shared passwords for /v5, comma-separated, e.g. "ella,sally,ari,winner,anna".
#
# Read from the environment and NEVER written here: dvoskin/callboard is a PUBLIC
# repository, so a literal in this file is a published password. (The same is true
# of the INGEST_KEY literal in forwarder/gmail_to_ingest.gs -- that one is already
# published and needs rotating.)
#
# These are short dictionary words, so the throttle below is doing real work: five
# 3-6 character words fall to an unthrottled guesser in seconds.
V5_PASSWORDS = tuple(
    p.strip() for p in os.environ.get("V5_PASSWORDS", "").split(",") if p.strip()
)
if not V5_PASSWORDS:
    print("[v5] V5_PASSWORDS is not set — /v5 is Google login only. Set it to a "
          "comma-separated list to hand out word passwords instead.", flush=True)

_V5_PW_MAX_TRIES = 8          # per IP, per window
_V5_PW_WINDOW = 300.0         # 5 minutes
_v5_pw_tries: dict = {}
_v5_pw_lock = threading.Lock()


def _v5_pw_throttled(ip: str) -> bool:
    """True if this IP has burned its guesses. Short word passwords are only as
    safe as the guess rate, so the rate is the control."""
    now = time.time()
    with _v5_pw_lock:
        hits = [t for t in _v5_pw_tries.get(ip, []) if now - t < _V5_PW_WINDOW]
        _v5_pw_tries[ip] = hits
        for k in [k for k, v in list(_v5_pw_tries.items()) if not v]:
            _v5_pw_tries.pop(k, None)
        return len(hits) >= _V5_PW_MAX_TRIES


def _v5_pw_record_failure(ip: str) -> None:
    with _v5_pw_lock:
        _v5_pw_tries.setdefault(ip, []).append(time.time())


def _v5_password_matches(supplied: str) -> bool:
    """Constant-time against every configured password.

    Every candidate is compared even after a match so the reply takes the same
    time whichever password was given -- otherwise the response time says which
    one, and with five words that is most of the secret.
    """
    if not supplied or not V5_PASSWORDS:
        return False
    ok = False
    for p in V5_PASSWORDS:
        if hmac.compare_digest(supplied, p):
            ok = True
    return ok


def _v5_token_ok() -> bool:
    """True if the request carries the read-only scoreboard secret."""
    if not SCOREBOARD_TOKEN:
        return False  # fail closed
    supplied = request.args.get("k", "") or request.headers.get("X-Scoreboard-Token", "")
    return hmac.compare_digest(supplied, SCOREBOARD_TOKEN)


def _v5_allowed() -> bool:
    """Either a signed-in user or the share token. Mirrors login_required's
    SSO-disabled fallback so local dev behaves the same."""
    if not GOOGLE_CLIENT_ID:
        return True
    return (bool(session.get("user")) or bool(session.get("v5_pw"))
            or _v5_token_ok())


@app.route("/v5", methods=["GET", "POST"])
def scoreboard_v5():
    """Talk time and call performance per agent, reconciled live across both phone
    systems. Full view: adds the RingEX/RingCX reconciliation and data warnings.

    Two ways in: the normal Google session, or one of the shared word passwords.
    The password path exists so the floor can be handed a word instead of an
    account; it grants the same read-only view, nothing more.
    """
    ip = (request.headers.get("CF-Connecting-IP")
          or (request.headers.get("X-Forwarded-For", "").split(",")[0].strip())
          or request.remote_addr or "?")
    error = ""

    if request.method == "POST":
        if _v5_pw_throttled(ip):
            error = "Too many tries. Wait five minutes."
        elif _v5_password_matches(request.form.get("password", "").strip()):
            session["v5_pw"] = True
            return redirect(url_for("scoreboard_v5"))
        else:
            _v5_pw_record_failure(ip)
            error = "That is not the password."

    if session.get("user") or session.get("v5_pw") or not GOOGLE_CLIENT_ID:
        return render_template("scoreboard_v5.html",
                               current_user=session.get("user") or {},
                               share_mode=False, share_token="")

    # No session. Offer the password when one is configured; otherwise the only
    # way in is Google, so send them there rather than showing a form that
    # cannot succeed.
    if not V5_PASSWORDS:
        return redirect("/login")
    return render_template("v5_password.html", error=error), (401 if error else 200)


@app.route("/v5/logout")
def scoreboard_v5_logout():
    session.pop("v5_pw", None)
    return redirect(url_for("scoreboard_v5"))


@app.route("/v5/board")
def scoreboard_v5_board():
    """Read-only scoreboard on a share link — no login, for the floor.

    404 rather than 403 on a bad token: this URL gets forwarded around, and a 403
    confirms the endpoint exists and is worth guessing at."""
    if not _v5_token_ok():
        return ("Not Found", 404)
    return render_template("scoreboard_v5.html", current_user={},
                           share_mode=True, share_token=request.args.get("k", ""))


# ══════════════════════════════════════════════════════════════
# v6 — billing team KPI board (RingEX only)
# ══════════════════════════════════════════════════════════════
# Billing does not dial a RingCX campaign -- Danny confirmed they are RingEX
# only, and the RingCX CDR is 403 on this account anyway. So unlike /v5 there is
# nothing to reconcile: every row comes from one seat's own call log.
#
# The roster is CONFIGURABLE on purpose. Two of the six people Danny named could
# not be resolved to an extension (Naomi Dubon, Jasmine Osborne -- checked all
# 181 extensions across every type and status), so the board has to be able to
# take a seat it did not ship with, without a code change.
#
# Precedence: /data/billing_roster.json (editable live on Render's disk)
#             -> BILLING_ROSTER env var (JSON list)
#             -> the four seats confirmed on 2026-08-24.
BILLING_ROSTER_FILE = _data_dir / "billing_roster.json"
# Confirmed by Danny 2026-08-24. Ana Salazar (ext 271) is NOT on this list: her
# line logged its last connected call on 2026-08-11 and he did not name her when
# restating the roster. Restore her with one line here if that was a leave rather
# than a departure.
_BILLING_ROSTER_DEFAULT = [
    {"name": "Vivian Martinez",    "ext_id": 405657034,  "ext": "137"},
    {"name": "Yareth Pavon",       "ext_id": 998743035,  "ext": "220"},
    {"name": "Gabriela Maldonado", "ext_id": 1027587035, "ext": "125"},
    {"name": "Andrea Pleasant",    "ext_id": 388372049,  "ext": "148"},
]
BILLING_TOKEN = os.environ.get("BILLING_TOKEN", "")
if not BILLING_TOKEN:
    print("[v6] BILLING_TOKEN is not set — /v6/board returns 404 for every request. "
          "/v6 still works behind the normal login.", flush=True)

_v6_cache: dict = {}
_v6_lock = threading.Lock()
_V6_TTL_TODAY = 150.0        # today moves; matches the warmer's refresh cadence
_V6_TTL_PAST = 3600.0        # a finished day is final


def _billing_roster():
    """(roster, meta). Never raises: a broken override falls back to the default
    rather than emptying the board, and says so in meta."""
    meta = {"source": "default"}
    raw = None
    try:
        if BILLING_ROSTER_FILE.exists():
            raw = json.loads(BILLING_ROSTER_FILE.read_text())
            meta["source"] = str(BILLING_ROSTER_FILE)
    except Exception as e:  # noqa: BLE001
        meta["error"] = f"{BILLING_ROSTER_FILE} is unreadable ({e}); using the built-in roster."
        raw = None
    if raw is None and os.environ.get("BILLING_ROSTER"):
        try:
            raw = json.loads(os.environ["BILLING_ROSTER"])
            meta["source"] = "BILLING_ROSTER env"
        except Exception as e:  # noqa: BLE001
            meta["error"] = f"BILLING_ROSTER is not valid JSON ({e}); using the built-in roster."
            raw = None
    roster = raw if isinstance(raw, list) and raw else _BILLING_ROSTER_DEFAULT
    clean = []
    for r in roster:
        if not isinstance(r, dict) or not r.get("ext_id"):
            meta.setdefault("skipped", []).append(str(r)[:80])
            continue
        clean.append({"name": (r.get("name") or f"ext {r.get('ext') or r['ext_id']}").strip(),
                      "ext_id": r["ext_id"], "ext": str(r.get("ext") or "")})
    if not clean:
        clean = _BILLING_ROSTER_DEFAULT
        meta["error"] = (meta.get("error", "") + " No usable seats in the override; "
                         "using the built-in roster.").strip()
    meta["size"] = len(clean)
    return clean, meta


# ── v6 day snapshots ───────────────────────────────────────────
# A 90-day window fetched as one call-log query does not work: RingEX rate limits
# it, and the first version of this returned an empty list for three of four
# seats -- which the board then rendered as "logged no calls at all". A 429 read
# as an accusation. It also took 106 seconds, and gunicorn kills a worker stuck
# past 90 (-t 90 in render.yaml), so the 30d and 90d buttons would have taken the
# service down with them.
#
# So the unit of fetching is ONE SEAT, ONE DAY, cached on disk. A finished day
# never changes, so it is fetched once and reused forever; today gets a short
# memory TTL. A long window is then assembled from files, and only the days that
# are genuinely missing cost an API call -- capped per request, with the rest
# reported as missing rather than silently rendered as zero.
V6_SNAP_DIR = _data_dir / "v6_days"
_V6_FETCH_BUDGET = 24        # seat-days fetched per request, ~15s at current pacing
# The deadline is checked BETWEEN fetches, so the real worst case is
# deadline + (one fetch). With a 30s HTTP timeout and a retry that was
# 25 + 63 = 88s, right on gunicorn's 90s kill (render.yaml -t 90) -- and a
# killed worker with -w 1 drops the connection, which is what "the page just
# sits on Loading" looked like. So the per-request HTTP timeout is short, the
# deadline is small, and the two together cannot reach 90s.
_V6_DEADLINE = 8.0           # seconds spent fetching inside a web request
_V6_HTTP_TIMEOUT = 10.0      # per HTTP call on the request path
_V6_MAX_WAIT = 2.0           # never sit on a long Retry-After inside a web request
_V6_MAX_FAILS = 1            # one refusal and this seat waits for the warmer
_v6_today_cache: dict = {}   # (ext_id, day) -> {"at": ts, "rows": [...]}


def _v6_snap_path(ext_id, day_iso):
    return V6_SNAP_DIR / str(ext_id) / f"{day_iso}.json"


def _v6_load_day(ext_id, day_iso):
    p = _v6_snap_path(ext_id, day_iso)
    try:
        if p.exists():
            return json.loads(p.read_text())
    except Exception as e:  # noqa: BLE001
        log.warning("v6 snapshot unreadable %s: %s", p, e)
    return None


def _v6_save_day(ext_id, day_iso, rows):
    p = _v6_snap_path(ext_id, day_iso)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(rows))
        tmp.replace(p)          # atomic: a half-written day must never be read as complete
    except Exception as e:  # noqa: BLE001
        log.warning("v6 snapshot unwritable %s: %s", p, e)


def _v6_fetch_day(ext_id, day_iso, tz_offset_minutes):
    """One seat, one local day. Returns (rows, ok). ok=False means the day could
    not be read -- which is NOT the same as the day being empty."""
    start_dt = _parse_local_date_to_utc(day_iso, 0, 0, 0, tz_offset_minutes)
    end_dt = _parse_local_date_to_utc(day_iso, 23, 59, 59, tz_offset_minutes)
    rows, meta = _ringcx.fetch_extension_calls(ext_id, start_dt, end_dt, max_pages=6,
                                               max_wait=_V6_MAX_WAIT,
                                               timeout=_V6_HTTP_TIMEOUT)
    ok = not meta.get("note") and not meta.get("truncated")
    return rows, ok


def _v6_seat_curve(ext_id, tz_offset_minutes, days_back=45):
    """A seat's own intraday pace curve, from the day snapshots already on disk.

    Reads local files only -- no API calls -- so this costs nothing against the
    10-per-minute RingEX budget. Returns None until the seat has enough history,
    and the caller then falls back to the team curve.
    """
    tz_off = tz_offset_minutes if tz_offset_minutes is not None else \
        -int(os.environ.get("TZ_OFFSET_HOURS", "-4")) * 60
    today = (datetime.now(timezone.utc) - timedelta(minutes=tz_off)).date()
    per_day = []
    for i in range(1, days_back + 1):
        day = (today - timedelta(days=i)).isoformat()
        rows = _v6_load_day(ext_id, day)
        if not rows:
            continue
        hours = {}
        for r in rows:
            if not _billing_connected(r):
                continue
            t = r.get("start_time") or ""
            try:
                dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                continue
            local = dt.astimezone(timezone.utc) - timedelta(minutes=tz_off)
            hours[local.hour] = hours.get(local.hour, 0) + (r.get("duration") or 0)
        if hours:
            per_day.append(hours)
    return build_pace_curve(per_day)


def _v6_build(date_start, date_end, tz_offset_minutes, local_today):
    """Assemble the window from day snapshots, fetching only what is missing."""
    roster, roster_meta = _billing_roster()
    d0 = datetime.strptime(date_start, "%Y-%m-%d").date()
    d1 = datetime.strptime(date_end, "%Y-%m-%d").date()
    days = [(d0 + timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)]

    deadline = time.time() + _V6_DEADLINE
    budget = _V6_FETCH_BUDGET
    rows_by_agent, stats = {}, {"cached": 0, "fetched": 0, "missing": 0}

    for seat in roster:
        eid = seat["ext_id"]
        rows, missing = [], []
        # Per SEAT, not per request. A shared counter meant that when RingEX was
        # busy the first two seats used up the allowance and the last two were
        # never asked at all -- so they came back "unknown" without a single
        # request having been made for them. Every seat gets its own attempts.
        fails = 0
        for day in days:
            is_today = day >= local_today
            if is_today:
                c = _v6_today_cache.get((eid, day))
                if c and time.time() - c["at"] < _V6_TTL_TODAY:
                    rows.extend(c["rows"]); stats["cached"] += 1
                    continue
            else:
                got = _v6_load_day(eid, day)
                if got is not None:
                    rows.extend(got); stats["cached"] += 1
                    continue
            # Once RingEX has refused twice for THIS seat there is no point
            # asking again this request -- the limit is per minute, and each
            # refusal costs a round-trip plus a wait we cannot afford here.
            if budget <= 0 or fails >= _V6_MAX_FAILS or time.time() > deadline:
                missing.append(day); stats["missing"] += 1
                continue
            budget -= 1
            got, ok = _v6_fetch_day(eid, day, tz_offset_minutes)
            if not ok:
                fails += 1
                missing.append(day); stats["missing"] += 1
                continue
            stats["fetched"] += 1
            rows.extend(got)
            if is_today:
                _v6_today_cache[(eid, day)] = {"at": time.time(), "rows": got}
            else:
                _v6_save_day(eid, day, got)   # a finished day never changes
        rows_by_agent[seat["name"]] = {
            "rows": rows, "ext": seat["ext"], "ext_id": eid,
            "complete": not missing, "missing_days": missing,
        }

    offset_east = -(tz_offset_minutes if tz_offset_minutes is not None
                    else -int(os.environ.get("TZ_OFFSET_HOURS", "-4")) * 60)

    # Pace, only when the view is the single day that is still running.
    now_local, curves = None, {}
    if date_start == date_end == local_today:
        now_local = datetime.now(timezone.utc) + timedelta(minutes=offset_east)
        for seat in roster:
            c = _v6_seat_curve(seat["ext_id"], tz_offset_minutes)
            if c:
                curves[seat["name"]] = c

    report = build_billing_report(
        rows_by_agent, tz_offset_minutes=offset_east,
        window={"start": date_start, "end": date_end},
        roster_meta=roster_meta, now_local=now_local, curves=curves,
    )
    if stats["missing"]:
        report["warnings"].append({
            "kind": "incomplete_fetch",
            "message": (f"{stats['missing']} seat-day(s) in this window have not been fetched "
                        f"yet — RingEX only allows so many requests a minute, so long ranges "
                        f"fill in over a few reloads. Every figure below is a floor until they "
                        f"do. Reload in a minute."),
        })
    report["meta"]["generated_utc"] = datetime.now(timezone.utc).isoformat()
    report["meta"]["days"] = stats
    report["meta"]["complete"] = stats["missing"] == 0
    return report


# ── v6 background warmer ───────────────────────────────────────
# A web request cannot wait out RingEX's per-minute limit -- gunicorn kills the
# worker at 90s -- so _v6_build gives up fast and reports the gap. That leaves
# the cache to be filled by whoever happens to reload, which on a rate-limited
# account means it may never fill at all.
#
# This thread has the one thing a request does not: time. It walks the roster
# over the trailing window, fetches the days that are missing one at a time, and
# sleeps generously between them. Nothing waits on it; it only ever makes the
# next page load faster.
# 35 days, not 90. The RingEX call log is a per-ACCOUNT budget and this app's own
# dashboard refresh already spends it every 60 seconds (fetch_todays_outbound_calls,
# same Heavy endpoint) -- so the warmer is not alone on the account and loses the
# race often. 4 seats x 35 days is 140 fetches, which converges in hours instead
# of days and covers every range the board offers except 90d. Raise V6_WARM_DAYS
# once the backfill has settled.
_V6_WARM_DAYS = int(os.environ.get("V6_WARM_DAYS", "35"))
# RingEX's "heavy" group allows 10 requests per 60 SECONDS for the WHOLE account
# (X-Rate-Limit-Group: heavy, Limit 10, Window 60) -- and this app's own dashboard
# refresh spends from the same bucket every minute. A 6s pause is 10/min, i.e. the
# entire account budget, which starves both the dashboard and the live board. 15s
# is 4/min, leaving room for everyone.
_V6_WARM_PAUSE = float(os.environ.get("V6_WARM_PAUSE", "15"))  # seconds between fetches
# How stale today's cached numbers may get before the warmer refreshes them.
# 4 seats every 150s is under 2 requests a minute, which sits alongside the
# backfill and the dashboard inside the account's 10-per-minute ceiling.
_V6_TODAY_WARM_TTL = float(os.environ.get("V6_TODAY_TTL", "150"))
_v6_warm_state = {"running": False, "filled": 0, "missing": None, "last": None}


def _v6_warm_loop():
    """Fill day snapshots in the background, slowly, forever."""
    _v6_warm_state["running"] = True
    tz_off = -int(os.environ.get("TZ_OFFSET_HOURS", "-4")) * 60
    while True:
        try:
            if not _ringcx.configured:
                time.sleep(300)
                continue
            roster, _ = _billing_roster()
            today = (datetime.now(timezone.utc) - timedelta(minutes=tz_off)).date()

            # TODAY FIRST. The board's default view is today, and if the request
            # path has to fetch it live it races the account's 10-per-minute
            # budget against the sales dashboard -- which is how the first live
            # load came back empty. Keeping today warm here means the common case
            # is served from cache and never fetches at all.
            tstr = today.isoformat()
            for seat in roster:
                c = _v6_today_cache.get((seat["ext_id"], tstr))
                if c and time.time() - c["at"] < _V6_TODAY_WARM_TTL:
                    continue
                rows, ok = _v6_fetch_day(seat["ext_id"], tstr, tz_off)
                if ok:
                    _v6_today_cache[(seat["ext_id"], tstr)] = {"at": time.time(), "rows": rows}
                    _v6_warm_state["today_at"] = time.time()
                    time.sleep(_V6_WARM_PAUSE)
                else:
                    time.sleep(65)
            # Drop yesterday's live entries so the dict cannot grow forever.
            for k in [k for k in list(_v6_today_cache) if k[1] != tstr]:
                _v6_today_cache.pop(k, None)

            gaps = []
            for seat in roster:
                for i in range(1, _V6_WARM_DAYS + 1):      # yesterday backwards; today is live
                    day = (today - timedelta(days=i)).isoformat()
                    if _v6_load_day(seat["ext_id"], day) is None:
                        gaps.append((seat["ext_id"], day))
            _v6_warm_state["missing"] = len(gaps)
            if not gaps:
                time.sleep(900)                            # nothing to do; check again later
                continue
            # Newest first: the ranges people actually look at fill in first.
            gaps.sort(key=lambda g: g[1], reverse=True)
            for eid, day in gaps[:40]:
                rows, ok = _v6_fetch_day(eid, day, tz_off)
                if ok:
                    _v6_save_day(eid, day, rows)
                    _v6_warm_state["filled"] += 1
                    _v6_warm_state["last"] = f"{eid} {day}"
                    time.sleep(_V6_WARM_PAUSE)
                else:
                    # Refused. Back off rather than burning the budget the live
                    # board -- and the main dashboard refresh -- also need.
                    # Escalating, so a long outage does not mean a tight retry
                    # loop, but a one-off 429 costs only a few seconds.
                    # RingEX says Retry-After: 60 and means it -- the window is
                    # a flat 60s, so anything shorter just burns a request to be
                    # told the same thing.
                    backoff = min(65 * (1 + _v6_warm_state.get("fails", 0)), 300)
                    _v6_warm_state["fails"] = _v6_warm_state.get("fails", 0) + 1
                    time.sleep(backoff)
                    continue
                _v6_warm_state["fails"] = 0
        except Exception as e:  # noqa: BLE001
            log.warning("v6 warmer error: %s", e)
            time.sleep(120)


@app.route("/api/v6/warm")
def api_v6_warm():
    """What the background warmer has done. Diagnostic — an empty board should
    always be explainable."""
    if not _v6_allowed():
        return jsonify({"error": "unauthorized"}), 401
    return jsonify(dict(_v6_warm_state, warm_days=_V6_WARM_DAYS,
                        pause_seconds=_V6_WARM_PAUSE))


@app.route("/api/v6/report")
def api_v6_report():
    """Billing KPI board for a window. Serves /v6 and /v6/board."""
    if not (_v6_allowed()):
        return jsonify({"error": "unauthorized"}), 401
    if not _ringcx.configured:
        # An empty board and a quiet phone look identical. Say which this is.
        return jsonify({
            "error": "ringcentral_not_configured",
            "detail": "RC_CLIENT_ID / RC_CLIENT_SECRET / RC_JWT_TOKEN are not set on this "
                      "instance, so no calls can be fetched. This is a configuration "
                      "problem, not a quiet day.",
        }), 503
    try:
        tz_param = request.args.get("tz")
        tz_offset_minutes = int(tz_param) if tz_param is not None else None
        _off = tz_offset_minutes if tz_offset_minutes is not None else \
            -int(os.environ.get("TZ_OFFSET_HOURS", "-4")) * 60
        local_today = (datetime.now(timezone.utc) - timedelta(minutes=_off)).date().isoformat()

        date_start = request.args.get("start") or local_today
        date_end = request.args.get("end") or date_start
        if date_end < date_start:
            return jsonify({"error": "bad_range",
                            "detail": "The end date is before the start date."}), 400

        key = (date_start, date_end, _off)
        ttl = _V6_TTL_TODAY if date_end >= local_today else _V6_TTL_PAST
        now = time.time()
        with _v6_lock:
            hit = _v6_cache.get(key)
            if hit and now - hit["at"] < ttl:
                out = dict(hit["report"])
                out["meta"] = dict(out["meta"], cached=True,
                                   age_seconds=round(now - hit["at"]))
                return jsonify(out)

        # Outside the lock would let N concurrent viewers each start their own
        # fetch and collide with RingEX's rate limit. One fetch, everyone waits.
        with _v6_lock:
            hit = _v6_cache.get(key)
            if hit and time.time() - hit["at"] < ttl:
                out = dict(hit["report"])
                out["meta"] = dict(out["meta"], cached=True,
                                   age_seconds=round(time.time() - hit["at"]))
                return jsonify(out)
            report = _v6_build(date_start, date_end, tz_offset_minutes, local_today)
            # An incomplete report must not be cached for an hour -- the whole
            # point is that a reload fills the gap.
            if report["meta"].get("complete"):
                _v6_cache[key] = {"at": time.time(), "report": report}
            for k in [k for k, v in list(_v6_cache.items())
                      if time.time() - v["at"] > _V6_TTL_PAST * 4]:
                _v6_cache.pop(k, None)
        report["meta"]["cached"] = False
        return jsonify(report)
    except Exception as e:  # noqa: BLE001
        log.exception("v6 report failed")
        return jsonify({"error": "report_failed", "detail": str(e)}), 500


def _v6_token_ok() -> bool:
    if not BILLING_TOKEN:
        return False  # fail closed
    supplied = request.args.get("k", "") or request.headers.get("X-Billing-Token", "")
    return hmac.compare_digest(supplied, BILLING_TOKEN)


def _v6_allowed() -> bool:
    """A signed-in user, the v5 word-password session (same staff gate), or the
    billing share token. The billing board names different people than the sales
    board, so it does NOT accept SCOREBOARD_TOKEN."""
    if not GOOGLE_CLIENT_ID:
        return True
    return (bool(session.get("user")) or bool(session.get("v5_pw"))
            or _v6_token_ok())


@app.route("/v6", methods=["GET", "POST"])
def scoreboard_v6():
    """Billing team KPI board — daily performance against floor/target/stretch.

    Same two ways in as /v5: the normal Google session, or a shared word password.
    """
    ip = (request.headers.get("CF-Connecting-IP")
          or (request.headers.get("X-Forwarded-For", "").split(",")[0].strip())
          or request.remote_addr or "?")
    error = ""
    if request.method == "POST":
        if _v5_pw_throttled(ip):
            error = "Too many tries. Wait five minutes."
        elif _v5_password_matches(request.form.get("password", "").strip()):
            session["v5_pw"] = True
            return redirect(url_for("scoreboard_v6"))
        else:
            _v5_pw_record_failure(ip)
            error = "That is not the password."

    if session.get("user") or session.get("v5_pw") or not GOOGLE_CLIENT_ID:
        return render_template("scoreboard_v6.html",
                               current_user=session.get("user") or {},
                               share_mode=False, share_token="")
    if not V5_PASSWORDS:
        return redirect("/login")
    return render_template("v5_password.html", error=error), (401 if error else 200)


@app.route("/v7")
def hub_v7():
    """One page listing every dashboard, so nobody has to remember which number
    is which. Same gate as /v5 and /v6 -- a Google session or the shared word
    password -- because it links straight into them and a hub behind a weaker
    door than its destinations is just a directory of what to guess at.

    Surgery Readiness lives on a different service and opens in a new tab; it
    carries its own sign-in, which the footer says rather than leaving someone
    to discover it.
    """
    if session.get("user") or session.get("v5_pw") or not GOOGLE_CLIENT_ID:
        return render_template("hub_v7.html", current_user=session.get("user") or {})
    if not V5_PASSWORDS:
        return redirect("/login")
    return render_template("v5_password.html", error=""), 200


@app.route("/v6/board")
def scoreboard_v6_board():
    """Read-only billing board on a share link. 404 rather than 403 on a bad
    token: this URL gets forwarded around."""
    if not _v6_token_ok():
        return ("Not Found", 404)
    return render_template("scoreboard_v6.html", current_user={},
                           share_mode=True, share_token=request.args.get("k", ""))


# ── RingCX Interaction Report inbox ────────────────────────────
# RingCX emails an Interaction Report on a schedule. That report -- not the
# GLOBAL_CALL_TYPE_DELIMITED CDR the API pulls -- is the one whose figures were
# reconciled by hand, so when a fresh one has been delivered it wins and the live
# CDR pull becomes the fallback. A forwarder (Gmail Apps Script, Zapier, anything
# that can POST) drops the attachment here.
INGEST_API_KEY = os.environ.get("INGEST_API_KEY", "")
RINGCX_INBOX_DIR = _data_dir / "ringcx_inbox"
try:
    RINGCX_INBOX_DIR.mkdir(parents=True, exist_ok=True)
except Exception as e:  # noqa: BLE001
    log.warning("Could not create ringcx_inbox dir: %s", e)
if not INGEST_API_KEY:
    print(
        "[v5] INGEST_API_KEY is not set — /api/v5/ingest rejects every upload (401), "
        "so the emailed RingCX Interaction Report cannot be delivered and /v5 falls "
        "back to the live CDR pull.",
        flush=True,
    )


def _inbox_path_for(day: str):
    return RINGCX_INBOX_DIR / ("interactions_%s.csv" % day)


# The account call-log is rate limited to roughly 10 requests a minute, and each
# /v5 load can need several pages of it. Without this, every open tab refreshing
# itself every 15 minutes, every manual reload and every /api/v5/diag competes for
# the same budget -- and the loser gets a 429, which the fetch turns into an empty
# list that reads as a quiet phone. One live pull is shared for a few minutes.
_V5_LIVE_TTL = 300.0
_v5_live_cache: dict = {}
_v5_live_lock = threading.Lock()


def _v5_live_fetch(start_dt, end_dt, want_cx: bool):
    """(ringex_rows, ringcx_rows, meta) — served from a short-lived shared cache."""
    key = (start_dt.isoformat(), end_dt.isoformat(), bool(want_cx))
    now = time.time()
    with _v5_live_lock:
        hit = _v5_live_cache.get(key)
        if hit and now - hit["at"] < _V5_LIVE_TTL:
            return hit["ex"], hit["cx"], {"cached": True,
                                          "age_seconds": round(now - hit["at"], 1)}
    from concurrent.futures import ThreadPoolExecutor as _TP
    with _TP(max_workers=2) as pool:
        ex_future = pool.submit(_ringcx._fetch_ringex_agent_calls, start_dt, end_dt)
        cx_future = pool.submit(_ringcx._fetch_ringcx_cdr_rows, start_dt, end_dt) if want_cx else None
        ex_rows = ex_future.result(timeout=120)
        cx_rows = cx_future.result(timeout=120) if cx_future else []
    with _v5_live_lock:
        _v5_live_cache[key] = {"at": time.time(), "ex": ex_rows, "cx": cx_rows}
        for k in [k for k, v in _v5_live_cache.items() if time.time() - v["at"] > 3600]:
            _v5_live_cache.pop(k, None)
    return ex_rows, cx_rows, {"cached": False, "age_seconds": 0}


def _ringex_snap_path(day: str):
    return RINGCX_INBOX_DIR / ("ringex_%s.json" % day)


def _snapshot_ringex(day_iso: str, tz_offset_minutes=None):
    """Capture RingEX for a day and store it beside that day's RingCX report.

    Taken once, when a report arrives -- not on every page load. The board is
    clamped to the RingCX watermark anyway, so pulling live per request spent a
    rate-limited budget on an hour of calls that were then discarded. That is
    what produced the 429. One snapshot per report keeps both halves of the
    board on the same hourly heartbeat and takes page loads off the API entirely.
    """
    try:
        start_dt = _parse_local_date_to_utc(day_iso, 0, 0, 0, tz_offset_minutes)
        end_dt = _parse_local_date_to_utc(day_iso, 23, 59, 59, tz_offset_minutes)
        rows = _ringcx._fetch_ringex_agent_calls(start_dt, end_dt)
        note = getattr(_ringcx, "last_ringex_note", None)
        if not rows:
            # Never let an empty result clobber a snapshot that has calls in it.
            # Checking the note alone was not enough: a fetch that RAISES returns
            # [] with no note, so the guard never fired and an empty snapshot was
            # written over a good one -- the board would then read zero RingEX
            # until the next report. An empty first snapshot of a day is fine; a
            # regression from populated to empty never is.
            # A KNOWN failure never gets written, even as the first snapshot of a
            # day: "the fetch broke" and "nobody called yet" must not share a shape
            # on disk. The note is now always set on the failure path.
            if note:
                log.warning("RingEX snapshot for %s not stored: %s", day_iso, note)
                return {"stored": False, "reason": note}
            prev = _load_ringex_snapshot(day_iso)
            if prev and prev[0]:
                log.warning("RingEX snapshot for %s NOT stored (kept %d existing calls): %s",
                            day_iso, len(prev[0]), note or "empty result, no reason given")
                return {"stored": False, "kept_existing": len(prev[0]),
                        "reason": note or "fetch returned no calls"}
        payload = {"fetched_utc": datetime.now(timezone.utc).isoformat(),
                   "day": day_iso, "calls": rows, "note": note}
        _ringex_snap_path(day_iso).write_text(json.dumps(payload), encoding="utf-8")
        log.info("RingEX snapshot %s: %d calls", day_iso, len(rows))
        return {"stored": True, "calls": len(rows), "note": note}
    except Exception as e:  # noqa: BLE001
        log.exception("RingEX snapshot failed for %s", day_iso)
        return {"stored": False, "reason": str(e)}


# RingEX is a LIVE API; RingCX arrives by email on its own schedule. Taking the
# RingEX snapshot only at ingest tied the live half of the board to the lagging
# half -- when RingCX went quiet for 20 minutes, RingEX froze with it even though
# the API would have answered instantly.
#
# So refresh it on its own clock. The floor below is what keeps us clear of the
# 429 that made this snapshot-only in the first place: one refresh per interval
# for the whole instance, one fetch in flight, and a failed attempt still counts
# against the interval so a broken API cannot be retried on every page load.
_RINGEX_REFRESH_AFTER = 300.0      # refresh a snapshot older than this
_RINGEX_MIN_GAP = 240.0            # never attempt more often than this, per instance
_ringex_last_attempt = {"at": 0.0}
_ringex_refresh_lock = threading.Lock()


def _ringex_refresh_due(age_seconds: float) -> bool:
    """True if this instance may spend an API call refreshing now."""
    if age_seconds < _RINGEX_REFRESH_AFTER:
        return False
    now = time.time()
    with _ringex_refresh_lock:
        if now - _ringex_last_attempt["at"] < _RINGEX_MIN_GAP:
            return False
        _ringex_last_attempt["at"] = now      # claimed whether or not it succeeds
        return True


def _load_ringex_snapshot(day_iso: str, allow_refresh: bool = False,
                          tz_offset_minutes=None):
    p = _ringex_snap_path(day_iso)
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        age = (datetime.now(timezone.utc)
               - datetime.fromisoformat(d["fetched_utc"])).total_seconds()

        # Only today can go stale in a way that matters; a past day is final.
        if allow_refresh and _ringex_refresh_due(age):
            log.info("RingEX snapshot for %s is %.1f min old — refreshing", day_iso, age / 60)
            _snapshot_ringex(day_iso, tz_offset_minutes)
            d = json.loads(p.read_text(encoding="utf-8"))
            age = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(d["fetched_utc"])).total_seconds()

        return d.get("calls") or [], {"source": "snapshot_at_ingest",
                                      "fetched_utc": d.get("fetched_utc"),
                                      "age_minutes": round(age / 60, 1),
                                      "calls": len(d.get("calls") or [])}
    except Exception as e:  # noqa: BLE001
        log.warning("RingEX snapshot %s unreadable: %s", p.name, e)
        return None


_V5_BOOKS_TTL = 300.0
_v5_books_cache: dict = {}
_v5_books_lock = threading.Lock()


def _norm_name(n: str) -> str:
    """Books writes 'Charlotte Mckay'; RingCX writes 'Charlotte McKay'. An exact
    match drops her silently, so join on a normalised key."""
    return " ".join((n or "").split()).lower()


_CREDENTIAL_QS = re.compile(
    r"((?:refresh_token|client_secret|client_id|access_token|code|authtoken)=)[^&\s\"']+",
    re.I,
)


def _redact(text) -> str:
    """Strip credentials out of text bound for a response or a log line.

    Zoho takes its OAuth credentials as query parameters, so any URL-bearing
    error string carries them verbatim. The root fix lives in the clients; this
    is the backstop for every other path that stringifies an exception.
    """
    return _CREDENTIAL_QS.sub(r"\1<redacted>", str(text))


_V5_ROSTER_TTL = 3600.0
_v5_roster_cache = {"at": 0.0, "names": None, "why": {}}
_v5_roster_lock = threading.Lock()

# System accounts that carry estimates but never sell.
_NOT_SALES_NAMES = {"zoho admin", "unassigned", ""}


_V5_CRM_TTL = 300.0
_v5_crm_cache: dict = {}
_v5_crm_lock = threading.Lock()


def _v5_activities_created(start_iso, end_iso, agent_names=None):
    """Per agent, CRM activities they CREATED inside the report's own window.

    Not future-dated activities: the question is what each rep logged or booked
    during the shift being scored, whenever the call itself is scheduled for.

    Attribution prefers the owner id. It falls back to the surname CRM puts in
    Owner.name, because this org's token lacks ZohoCRM.users.READ (401
    OAUTH_SCOPE_MISMATCH) -- but only where exactly one agent on the board has
    that surname. Alexander, Grace and Francisco Rodriguez must not be merged,
    and guessing between them is worse than admitting we cannot tell, so they
    get None (a dash) rather than a number.

    Returns (by_agent, meta); by_agent is None when nothing could be attributed
    at all, because a board of zeros would read as "nobody did anything".
    """
    key = (start_iso, end_iso)
    now = time.time()
    with _v5_crm_lock:
        hit = _v5_crm_cache.get(key)
        if hit and now - hit["at"] < _V5_CRM_TTL:
            return hit["by_agent"], dict(hit["meta"], cached=True)

    meta = {"cached": False}
    try:
        raw = _zoho.count_activities_created(start_iso, end_iso)
        users = _zoho.list_users()
        by_agent, unattributed = {}, 0

        if users:
            meta["attribution"] = "id"
            for uid, slot in raw.items():
                full = users.get(uid)
                if full:
                    k = _norm_name(full)
                    by_agent[k] = (by_agent.get(k) or 0) + slot["n"]
                else:
                    unattributed += slot["n"]
        else:
            meta["attribution"] = "surname"
            board = {}
            for full in (agent_names or []):
                parts = _norm_name(full).split()
                if parts:
                    board.setdefault(parts[-1], []).append(_norm_name(full))
            per_surname = {}
            for slot in raw.values():
                sn = _norm_name(slot["name"]).split()
                sn = sn[-1] if sn else ""
                per_surname[sn] = per_surname.get(sn, 0) + slot["n"]
            shared = []
            for sn, n in per_surname.items():
                who = board.get(sn) or []
                if len(who) == 1:
                    by_agent[who[0]] = (by_agent.get(who[0]) or 0) + n
                else:
                    unattributed += n
                    if len(who) > 1:
                        shared.append(sn)
            for sn in shared:
                for who in board[sn]:
                    by_agent[who] = None      # cannot tell, so do not claim 0
            meta["ambiguous_surnames"] = sorted(set(shared))
            # Both sides of the join, so a zero says WHICH side was empty rather
            # than leaving it to be guessed at from the outside.
            meta["board_surnames"] = len(board)
            meta["owner_surnames"] = sorted(per_surname)[:12]

        for full in (agent_names or []):
            by_agent.setdefault(_norm_name(full), 0)

        meta["attributed"] = sum(v for v in by_agent.values() if v)
        meta["unattributed"] = unattributed
        if not meta["attributed"]:
            meta["error"] = ("none of %d activities could be attributed to an agent"
                             % (unattributed or sum(s["n"] for s in raw.values())))
            return None, meta

        with _v5_crm_lock:
            _v5_crm_cache[key] = {"at": time.time(), "by_agent": by_agent,
                                  "meta": dict(meta)}
            for k in [k for k, v in list(_v5_crm_cache.items())
                      if time.time() - v["at"] > 3600]:
                _v5_crm_cache.pop(k, None)
        return by_agent, meta
    except Exception as e:  # noqa: BLE001
        log.warning("v5 activities created unavailable: %s", _redact(e))
        meta["error"] = _redact(e)[:200]
        return None, meta


def _sales_roster():
    """Who belongs on the sales board: whoever dials a RingCX campaign.

    Sending a quote is NOT a qualifying signal. An earlier version treated it as
    one, on the reading that only sales quote -- and it admitted seven people who
    quote but never dial (Alyan Wasif, Angel Leomis Medina, Charlotte Reyes,
    Genesis Ventura, Henry Marshall, Luisa Perez, Olivia Bennett), none of whom
    are sales. Danny confirmed it: campaign dialling is the test.

    Dropping the quotes half costs nothing real. Maisah Brandon was the worry --
    27 estimates and barely any dialling -- but the campaign half counts
    campaign INTERACTIONS, not campaign talk time, so her silent dials still
    carry her. Measured over 08/17-20, 28 of 29 agents had campaign time.

    The window is every report the inbox holds, not the report's own range:
    Wellington Santiago has 19,375s of campaign over 08/17-20 and 3 attempts
    today, and a same-day test would drop him for one quiet morning.

    Returns (names, meta). names is None when the roster cannot be computed at
    all -- an empty inbox must filter nobody rather than empty the board.
    """
    now = time.time()
    with _v5_roster_lock:
        c = _v5_roster_cache
        if c["names"] is not None and now - c["at"] < _V5_ROSTER_TTL:
            return set(c["names"]), {"cached": True, "size": len(c["names"])}

    why, meta = {}, {"cached": False}

    days = _inbox_days()[:14]
    for day in days:
        try:
            loaded = _load_inbox_csv(day, is_today=False)
        except Exception:  # noqa: BLE001
            continue
        if not loaded:
            continue
        for r in (loaded[0] or []):
            if (r.get("campaign_name") or "").strip():
                n = _norm_name(r.get("agent_name") or "")
                if n:
                    why.setdefault(n, set()).add("campaign")
    meta["campaign_days"] = len(days)

    names = {n for n in why if n not in _NOT_SALES_NAMES}
    if not names:
        # No inbox report parsed, so nothing is known about anyone. Filtering on
        # that would empty the board; absence is not a negative.
        log.warning("v5 roster: no campaign activity found in %d inbox day(s), "
                    "not filtering", len(days))
        meta["error"] = "no campaign activity in the inbox"
        return None, meta
    with _v5_roster_lock:
        _v5_roster_cache.update({"at": time.time(), "names": set(names),
                                 "why": {k: sorted(v) for k, v in why.items()}})
    meta["size"] = len(names)
    return names, meta


def _zero_call_row(display, b):
    """A roster member who quoted in this window but has no calls in it.

    They were invisible before -- the board is built from RingCX, so a rep who
    sent quotes and had not dialled yet simply was not there. Zero dials beside
    real quotes is the honest picture, and it belongs in unranked so it cannot
    drag the floor.
    """
    row = {"name": display, "talk": 0, "campaign": 0, "direct": 0, "attempts": 0,
           "missed": 0, "connected": 0, "wrap": 0, "longest": 0,
           "below": [], "band": "na"}
    for m in (60, 180, 600):
        row["over_%d" % (m // 60)] = 0
    row["books"] = {k: b[k] for k in ("quotes_sent", "quotes_invoiced",
                                      "retainers_sent", "retainers_paid",
                                      "paid_amount")}
    return row


def _v5_books(date_start: str, date_end: str):
    """Per-agent Books figures for the range: quotes sent, retainers sent, and
    retainers PAID (by payment date). Cached briefly -- Books is slow and every
    agent on the board reads from the same fetch."""
    key = (date_start, date_end)
    now = time.time()
    with _v5_books_lock:
        hit = _v5_books_cache.get(key)
        if hit and now - hit["at"] < _V5_BOOKS_TTL:
            return hit["by_agent"], dict(hit["meta"], cached=True)

    by_agent, meta = {}, {"cached": False, "errors": []}

    # Quotes sent is INCLUSIVE: every quote that left the office, invoiced ones
    # included. Invoiced is the SUBSET of those that converted, shown beside it
    # rather than carved out of it.
    #
    # Carving it out was the first attempt and it was wrong in the same direction
    # as the original bug: the headline shrank as quotes succeeded. Adelita on
    # 2026-08-19 sent 16 and closed 4 of them; a "sent" of 12 understated her
    # work by exactly the quotes that worked.
    _QUOTE_CLOSED = frozenset({"invoiced", "signed"})

    def bucket(name):
        return by_agent.setdefault(_norm_name(name), {
            "display": (name or "").strip(), "quotes_sent": 0, "quotes_invoiced": 0,
            "retainers_sent": 0, "retainers_paid": 0, "paid_amount": 0.0})

    def _num(v):
        try:
            return float(v or 0)
        except (TypeError, ValueError):
            return 0.0

    for label, fn, field in (
        ("quotes_sent", lambda: _books.list_sent_estimates(date_start, date_end, 2000), None),
        ("retainers_sent", lambda: _books.list_sent_retainer_invoices(date_start, date_end, 500), None),
        ("retainers_paid", lambda: _books.list_retainer_payments(date_start, date_end), "amount"),
    ):
        try:
            rows = fn() or []
            for r in rows:
                b = bucket(r.get("salesperson_name") or "Unassigned")
                b[label] += 1
                if label == "quotes_sent" and (r.get("status") or "") in _QUOTE_CLOSED:
                    b["quotes_invoiced"] += 1
                if field:
                    b["paid_amount"] += _num(r.get(field))
            meta[label + "_rows"] = len(rows)
        except Exception as e:  # noqa: BLE001
            # Name the failure. A zero that means "Books errored" and a zero that
            # means "no quotes today" must not look the same on the board.
            log.warning("v5 books %s failed: %s", label, _redact(e))
            meta["errors"].append({"metric": label, "detail": _redact(e)[:200]})

    with _v5_books_lock:
        _v5_books_cache[key] = {"at": time.time(), "by_agent": by_agent, "meta": meta}
        for k in [k for k, v in _v5_books_cache.items() if time.time() - v["at"] > 3600]:
            _v5_books_cache.pop(k, None)
    return by_agent, meta


def _inbox_days():
    """Every day the inbox holds a report for, newest first."""
    out = []
    try:
        for p in RINGCX_INBOX_DIR.glob("interactions_*.csv"):
            out.append(p.stem.replace("interactions_", ""))
    except Exception:  # noqa: BLE001
        pass
    return sorted(out, reverse=True)


def _load_inbox_csv(date_start: str, is_today: bool = True, stale_after_hours: float = 3.0):
    """Delivered Interaction Report for a day, or None.

    Staleness only means something for TODAY, where an old file says the hourly
    forwarder has stopped. A past day's report is final and will always be old --
    refusing it there would silently fall back to the live CDR API for every
    historical day, which is a different report with different calls in it.

    Returns (rows, meta) so the report can always say which source it used.
    """
    p = _inbox_path_for(date_start)
    if not p.exists():
        return None
    try:
        age_h = (time.time() - p.stat().st_mtime) / 3600.0
        rows, unit = parse_interaction_csv(p.read_text(encoding="utf-8-sig", errors="replace"))
        covers_to = max(((r.get("start_time") or "").split(" ")[-1] for r in rows), default="")
        meta = {"source": "emailed_interaction_report", "file": p.name,
                "rows": len(rows), "unit": unit, "age_hours": round(age_h, 2),
                "covers_to": covers_to or None}
        if is_today and age_h > stale_after_hours:
            # Still use it -- a stale report beats a different report -- but say so.
            meta["stale"] = True
            meta["stale_detail"] = (
                "The newest delivered report for today is %.1f hours old. The report "
                "is emailed hourly, so the forwarder has probably stopped. Figures "
                "below are correct up to that point, not up to now." % age_h)
        return rows, meta
    except Exception as e:  # noqa: BLE001
        log.warning("ringcx inbox %s unusable: %s", p.name, e)
        return None


@app.route("/api/v5/ingest", methods=["POST"])
def api_v5_ingest():
    """Accept a RingCX Interaction Report CSV from an email forwarder.

    Auth is a shared secret, not a session: the poster is a script, not a browser.
    The file is parsed BEFORE it is stored, so a wrong attachment (a PDF, a summary
    email, an empty report) is rejected loudly instead of quietly replacing a good
    report with an empty day.

        curl -X POST https://<host>/api/v5/ingest \
             -H "X-API-Key: $INGEST_API_KEY" \
             -F "file=@interactions.csv"
    """
    supplied = request.headers.get("X-API-Key", "") or request.args.get("key", "")
    if not INGEST_API_KEY or not hmac.compare_digest(supplied, INGEST_API_KEY):
        return jsonify({"error": "unauthorized"}), 401

    f = request.files.get("file")
    raw = f.read() if (f is not None and f.filename) else request.get_data()
    if not raw:
        return jsonify({"error": "empty_body",
                        "detail": "No CSV received. Send it as multipart 'file' or a raw body."}), 400
    text = raw.decode("utf-8-sig", errors="replace")
    try:
        rows, unit = parse_interaction_csv(text)     # shape check before anything is stored
    except EmptyReportError as e:
        # Every report between midnight and the first dial of the day is this.
        # It is not a wrong file and there is nothing to file: with no dated rows
        # it cannot name a day, so it can never overwrite one -- the risk the
        # blanket refusal existed to prevent does not exist here.
        #
        # Refusing it as 400 meant the forwarder never marked it seen, so ~33
        # empty reports were re-posted every five minutes and every run reported
        # Failed. That made a quiet night and a broken pipeline look identical,
        # and sent the run's own error text chasing an API key that was fine.
        return jsonify({"status": "empty_report", "stored": False,
                        "detail": str(e)}), 200
    except CsvShapeError as e:
        return jsonify({"error": "wrong_file", "detail": str(e)}), 400
    except Exception as e:  # noqa: BLE001
        log.exception("v5 ingest parse failed")
        return jsonify({"error": "parse_failed", "detail": str(e)}), 400

    # The emailed hourly report is a ROLLING export covering more than one day
    # (2,525 rows across 08/19-08/20 in the first live delivery). Filing the whole
    # file under one day put a 3PM report from the 20th into the 19th's slot, and
    # three reports in a row overwrote each other. Split it and file each day
    # separately.
    import csv as _csv
    import io as _io
    rdr = _csv.DictReader(_io.StringIO(text.lstrip("\ufeff")))
    header = rdr.fieldnames or []
    by_day = {}
    for r in rdr:
        d = (r.get("Date") or "").strip()
        if not d or d.lower() == "average":
            continue
        by_day.setdefault(d, []).append(r)
    if not by_day:
        return jsonify({"error": "no_dated_rows",
                        "detail": "No rows carried a Date, so nothing could be filed."}), 400

    written, skipped = [], []
    for day_us, day_rows in sorted(by_day.items()):
        try:
            day_iso = datetime.strptime(day_us, "%m/%d/%Y").date().isoformat()
        except ValueError:
            day_iso = day_us
        path = _inbox_path_for(day_iso)

        # A rolling report can arrive out of order (the 1PM one after the 3PM one),
        # so a day is only replaced by a report that reaches FURTHER INTO it.
        #
        # Row count is the wrong test. It assumes both reports are scoped the same,
        # and they are not -- the emailed report carries 23 agents where a manual
        # export carried 19. A later report covering more of the day can hold fewer
        # rows for it, and a count test would reject it and freeze the day forever.
        # How far a report reaches is knowable: the latest row timestamp in it.
        def _watermark(rs):
            # Only real clock values. RingCX appends an "Average" summary row, and a
            # lexical max puts any word above any time ("A" > "1"), so one leftover
            # summary row makes a day claim it reaches further than it does -- and
            # then refuses every genuinely newer report, forever.
            times = [t for t in ((r.get("Interaction Start Time") or "").split(" ")[0]
                                 for r in rs)
                     if len(t) == 8 and t[2] == ":" and t[5] == ":" and
                     t.replace(":", "").isdigit()]
            return max(times, default="")

        new_wm = _watermark(day_rows)
        have_wm, have_n = "", 0
        if path.exists():
            try:
                prev = [r for r in _csv.DictReader(_io.StringIO(
                    path.read_text(encoding="utf-8-sig", errors="replace")))
                    if (r.get("Date") or "").strip().lower() != "average"]
                have_wm, have_n = _watermark(prev), len(prev)
            except Exception:  # noqa: BLE001
                pass
        # Equal reach -> prefer the fuller file, so a re-send never loses rows.
        if have_wm > new_wm or (have_wm == new_wm and have_n > len(day_rows)):
            skipped.append({"day": day_iso, "incoming": len(day_rows),
                            "incoming_covers_to": new_wm or None,
                            "kept": have_n, "kept_covers_to": have_wm or None,
                            "reason": "stored report already reaches at least this far"})
            continue

        buf = _io.StringIO()
        w = _csv.DictWriter(buf, fieldnames=header, extrasaction="ignore")
        w.writeheader()
        for r in day_rows:
            w.writerow(r)
        path.write_text(buf.getvalue(), encoding="utf-8")
        written.append({"day": day_iso, "rows": len(day_rows), "stored_as": path.name,
                        "covers_to": new_wm or None,
                        "replaced": {"rows": have_n, "covered_to": have_wm} if have_n else None})

    # Only for days that actually moved, and only the newest of them: replaying a
    # backlog should not fire one rate-limited RingEX fetch per report.
    snaps = {}
    if written:
        newest = max(w["day"] for w in written)
        snaps[newest] = _snapshot_ringex(newest, request.args.get("tz") and
                                         int(request.args["tz"]) or None)

    log.info("v5 ingest: %d rows (%s) -> %s written, %s skipped",
             len(rows), unit, [w["day"] for w in written], [k["day"] for k in skipped])
    return jsonify({"status": "ok", "total_rows": len(rows), "unit": unit,
                    "days_written": written, "days_skipped": skipped,
                    "ringex_snapshots": snaps,
                    "agents": len({r["agent_name"] for r in rows})})


@app.route("/api/v5/diag")
@login_required
def api_v5_diag():
    """Why is a source empty? Answers it instead of leaving it to be guessed.

    _fetch_ringex_agent_calls swallows a non-OK response into an empty list, so a
    missing ReadCallLog permission, an expired JWT and a genuinely quiet phone all
    look identical downstream. This makes the same request and reports what came
    back. Status codes and the API's own error text only -- never a credential.
    """
    out = {"configured": _ringcx.configured, "checks": []}

    def add(name, ok, detail, extra=None):
        row = {"check": name, "ok": ok, "detail": detail}
        if extra:
            row.update(extra)
        out["checks"].append(row)

    # Books credential shape -- length and whitespace only, never the value. A
    # refresh token and an access token are both 70 chars with two dots, so the
    # usual failure is grabbing the wrong one, and the usual second failure is a
    # trailing newline from the copy. Neither is visible from the outside.
    try:
        _bt = os.environ.get("ZOHO_BOOKS_REFRESH_TOKEN", "")
        _ct = os.environ.get("ZOHO_REFRESH_TOKEN", "")
        out["books_token"] = {
            "ZOHO_BOOKS_REFRESH_TOKEN_set": bool(_bt),
            "length": len(_bt),
            "expected_length": 70,
            "dots": _bt.count("."),
            "has_surrounding_whitespace": _bt != _bt.strip(),
            "falls_back_to_crm_token": (not _bt) and bool(_ct),
            "note": ("Length is not 70 — likely truncated, or an extra character was copied."
                     if _bt and len(_bt) != 70 else
                     "Whitespace around the value — Zoho will reject it."
                     if _bt != _bt.strip() else
                     "Shape looks right; if Zoho still says invalid_code the value is a "
                     "different (older or access) token." if _bt else
                     "Not set — Books is using the CRM refresh token."),
        }
    except Exception:  # noqa: BLE001
        pass

    # The shape check above cannot tell a correct token from a wrong one, and a
    # wrong one is the entire failure mode here -- reporting on a credential
    # without exercising it is how "not set" and "set but powerless" came to look
    # identical. So exercise it: refresh, then make one real Books call. Reports
    # Zoho's own words at whichever step fails, never a credential.
    try:
        _held = _books.refresh_token or ""
        _env_books = os.environ.get("ZOHO_BOOKS_REFRESH_TOKEN", "")
        probe = {
            # BooksClient is built at import, so a value saved on Render without a
            # restart is live in os.environ and absent from the client. That gap is
            # invisible from outside and looks exactly like "never saved".
            "client_built_with_books_token": bool(_env_books) and _held == _env_books,
            "client_holds_a_token": bool(_held),
            "org_id_set": bool(_books.org_id),
        }
        if not _held:
            probe["result"] = "No refresh token at all - set ZOHO_BOOKS_REFRESH_TOKEN."
        elif not _books.org_id:
            probe["result"] = "ZOHO_BOOKS_ORG_ID is not set - Books cannot be queried."
        else:
            try:
                _books._get_access_token()
                probe["refresh"] = "ok"
            except Exception as e:  # noqa: BLE001
                probe["refresh"] = "failed"
                probe["result"] = _redact(e)[:300]
            if probe.get("refresh") == "ok":
                # Probe every scope the board needs, not just one. Testing only
                # /estimates would have reported "Books works." while retainers
                # paid stayed silently 401 -- the CRM token carries
                # ZohoBooks.estimates.READ and .invoices.READ but not
                # .customerpayments.READ, and that one missing scope is the
                # entire reason the paid column reads zero.
                _scopes, _denied = {}, []
                for _name, _path in (("estimates", "estimates"),
                                     ("invoices", "invoices"),
                                     ("customerpayments", "customerpayments")):
                    try:
                        _r = requests.get(
                            f"{_books.base_url}/{_path}",
                            headers=_books._headers(),
                            params={"organization_id": _books.org_id, "per_page": 1},
                            timeout=20,
                        )
                        _scopes[_name] = _r.status_code
                        if not _r.ok:
                            _denied.append(_name)
                    except Exception as e:  # noqa: BLE001
                        _scopes[_name] = _redact(e)[:120]
                        _denied.append(_name)
                probe["scopes"] = _scopes
                probe["result"] = (
                    "Books works - all three scopes readable."
                    if not _denied else
                    "Token is valid but lacks scope for: %s. Regenerate the Zoho "
                    "token with ZohoBooks.estimates.READ, ZohoBooks.invoices.READ "
                    "and ZohoBooks.customerpayments.READ, then set it as "
                    "ZOHO_BOOKS_REFRESH_TOKEN." % ", ".join(_denied))
        out["books_probe"] = probe
    except Exception as e:  # noqa: BLE001
        out["books_probe"] = {"result": _redact(e)[:300]}

    if not _ringcx.configured:
        add("credentials", False,
            "RC_CLIENT_ID / RC_CLIENT_SECRET / RC_JWT_TOKEN are not all set on this "
            "instance. Nothing can be fetched from RingCentral.")
        return jsonify(out)
    add("credentials", True, "RC_CLIENT_ID, RC_CLIENT_SECRET and RC_JWT_TOKEN are set.")

    # 1. can we get a RingEX token at all?
    try:
        _ringcx._ensure_rc_token()
        add("ringex_token", True, "JWT exchanged for an access token.")
    except Exception as e:  # noqa: BLE001
        add("ringex_token", False,
            "Could not exchange the JWT for an access token: %s. Usually an expired or "
            "revoked JWT, or the app's client id/secret not matching the JWT." % e)
        return jsonify(out)

    tz_param = request.args.get("tz")
    tz_off = int(tz_param) if tz_param is not None else None
    day = request.args.get("start") or datetime.now(timezone.utc).date().isoformat()
    start_dt = _parse_local_date_to_utc(day, 0, 0, 0, tz_off)
    end_dt = _parse_local_date_to_utc(day, 23, 59, 59, tz_off)

    # 2. the exact call the report makes, with the response surfaced
    try:
        import requests as _rq
        resp = _rq.get(
            "%s/restapi/v1.0/account/%s/call-log" % (_ringcx.server_url, _ringcx.account_id),
            headers=_ringcx._rc_headers(),
            params={"dateFrom": start_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                    "dateTo": end_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                    "view": "Simple", "perPage": 250, "page": 1},
            timeout=25)
        if resp.ok:
            recs = (resp.json() or {}).get("records", [])
            named = sum(1 for r in recs
                        if ((r.get("from") or {}).get("name") or
                            (r.get("to") or {}).get("name")))
            add("ringex_call_log", True,
                "HTTP 200. %d call(s) on page 1 for %s, %d carrying an agent name."
                % (len(recs), day, named),
                {"records_page_1": len(recs), "with_agent_name": named})
            if recs and not named:
                add("ringex_attribution", False,
                    "Calls came back but none carry a name on either leg, so every one "
                    "is dropped as unattributed. The report will read zero.")
        else:
            body = (resp.text or "")[:400]
            hint = ""
            if resp.status_code in (401, 403):
                hint = (" The app most likely lacks the ReadCallLog permission, or the "
                        "JWT is not for an account-level admin. Account-wide call log "
                        "needs both.")
            add("ringex_call_log", False,
                "HTTP %d from the account call-log.%s API said: %s"
                % (resp.status_code, hint, body),
                {"status": resp.status_code})
    except Exception as e:  # noqa: BLE001
        add("ringex_call_log", False, "Request raised: %s" % e)

    # 3. the RingCX side, for contrast
    try:
        cx = _ringcx._fetch_ringcx_cdr_rows(start_dt, end_dt)
        add("ringcx_cdr", bool(cx), "%d CDR row(s) for %s." % (len(cx), day),
            {"rows": len(cx)})
    except Exception as e:  # noqa: BLE001
        add("ringcx_cdr", False, "Request raised: %s" % e)

    # 4. what the inbox holds
    try:
        out["inbox"] = _inbox_days()[:10]
    except Exception:  # noqa: BLE001
        out["inbox"] = []
    bad = [c["check"] for c in out["checks"] if not c["ok"]]
    out["verdict"] = ("RingEX is returning attributed calls."
                      if not bad else
                      "RingEX will report zero — failing: %s" % ", ".join(bad))
    return jsonify(out)


@app.route("/api/v5/ingest/status")
@login_required
def api_v5_ingest_status():
    """What the inbox currently holds, so a silent forwarder failure is visible."""
    out = []
    try:
        import csv as _csv
        import io as _io
        for p in sorted(RINGCX_INBOX_DIR.glob("interactions_*.csv"), reverse=True)[:14]:
            st = p.stat()
            rows_n, covers_to = 0, None
            try:
                rs = list(_csv.DictReader(_io.StringIO(
                    p.read_text(encoding="utf-8-sig", errors="replace"))))
                rows_n = len(rs)
                _t = [t for t in ((r.get("Interaction Start Time") or "").split(" ")[0]
                                  for r in rs)
                      if len(t) == 8 and t[2] == ":" and t[5] == ":" and
                      t.replace(":", "").isdigit()]
                covers_to = max(_t, default=None)
            except Exception:  # noqa: BLE001
                pass
            out.append({"file": p.name, "rows": rows_n, "covers_to": covers_to,
                        "bytes": st.st_size,
                        "modified_utc": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
                        "age_hours": round((time.time() - st.st_mtime) / 3600.0, 2)})
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500
    return jsonify({"configured": bool(INGEST_API_KEY), "dir": str(RINGCX_INBOX_DIR),
                    "files": out})


@app.route("/api/v5/report")
def api_v5_report():
    """Merged ledger + reconciliation for a window. Serves /v5 and /v5/board."""
    if not _v5_allowed():
        return jsonify({"error": "unauthorized"}), 401
    try:
        today = datetime.now(timezone.utc).date()
        date_start = request.args.get("start") or today.isoformat()
        date_end = request.args.get("end") or date_start
        tz_param = request.args.get("tz")
        tz_offset_minutes = int(tz_param) if tz_param is not None else None

        start_dt = _parse_local_date_to_utc(date_start, 0, 0, 0, tz_offset_minutes)
        end_dt = _parse_local_date_to_utc(date_end, 23, 59, 59, tz_offset_minutes)

        if not _ringcx.configured:
            # An empty report and a quiet phone look identical. Say which this is —
            # that ambiguity is exactly what hid the RingEX fetch bug for 41 days.
            return jsonify({
                "error": "ringcentral_not_configured",
                "detail": "RC_CLIENT_ID / RC_CLIENT_SECRET / RC_JWT_TOKEN are not set on "
                          "this instance, so no calls can be fetched. This is a "
                          "configuration problem, not a quiet day.",
            }), 503

        # RingCX: prefer a freshly delivered Interaction Report over the live CDR
        # pull. They are different reports and do not contain the same calls, so
        # the choice is always reported in meta rather than made silently.
        # ?source=api forces the live pull, for comparing the two.
        # "today" in the viewer's own timezone, not UTC -- at 8pm Eastern it is
        # already tomorrow in UTC, and the freshness rule would misfire.
        _off = tz_offset_minutes if tz_offset_minutes is not None else \
            -int(os.environ.get("TZ_OFFSET_HOURS", "-4")) * 60
        local_today = (datetime.now(timezone.utc) - timedelta(minutes=_off)).date().isoformat()
        inbox = (None if request.args.get("source") == "api"
                 else _load_inbox_csv(date_start, is_today=(date_start == local_today)))

        # Prefer the snapshot taken when this day's report arrived: it is already
        # the right vintage, and it costs no API budget. ?source=api forces live.
        # Today's snapshot may refresh itself here; a past day's is final, so it
        # never spends an API call.
        snap = (None if request.args.get("source") == "api"
                else _load_ringex_snapshot(date_start,
                                           allow_refresh=(date_start == local_today),
                                           tz_offset_minutes=tz_offset_minutes))
        need_live = (snap is None) or (not inbox)   # nothing to fetch if both are on disk
        if need_live:
            live_ex, live_cx, live_meta = _v5_live_fetch(
                start_dt, end_dt, want_cx=not inbox)
        else:
            live_ex, live_cx, live_meta = [], [], {"cached": True, "age_seconds": 0}

        if snap:
            ex_rows, ex_meta = snap
        else:
            ex_rows, ex_meta = live_ex, dict(live_meta, source="live_pull")
        cx_rows = inbox[0] if inbox else live_cx
        cx_source = inbox[1] if inbox else {"source": "live_cdr_api", "rows": len(cx_rows)}

        # RingEX is pulled live and is current to this second; the emailed RingCX
        # report lags ~20-25 minutes behind its own send time. Left unaligned, a
        # rep on their direct line is credited for an hour that a campaign rep is
        # not -- on a board that ranks people and names the bottom three, that
        # penalises whoever sits on the slower source. Clamp both to the window
        # they BOTH cover.
        # _parse_local_date_to_utc takes minutes WEST of UTC (getTimezoneOffset);
        # v5_report takes a signed offset EAST. Negate.
        offset_east = -(tz_offset_minutes if tz_offset_minutes is not None
                        else -int(os.environ.get("TZ_OFFSET_HOURS", "-4")) * 60)

        ex_clamped = 0
        cov = cx_source.get("covers_to")
        if cov and len(cov) == 8:
            try:
                hh, mm, ss = (int(x) for x in cov.split(":"))
                cutoff = _parse_local_date_to_utc(date_start, hh, mm, ss, tz_offset_minutes)
                keep = []
                for e in ex_rows:
                    # ISO values carry their own zone; a naive one is local, so
                    # reading it as UTC would shift it by hours and clamp nothing.
                    t = _v5_parse_ts(e.get("start_time"), offset_east)
                    if t is None or t <= cutoff:
                        keep.append(e)
                ex_clamped = len(ex_rows) - len(keep)
                ex_rows = keep
            except (ValueError, TypeError):
                pass

        roster, roster_meta = _sales_roster()
        report = build_v5_report(ex_rows, cx_rows, tz_offset_minutes=offset_east,
                                 window={"start": date_start, "end": date_end},
                                 roster=roster)
        report["meta"]["roster"] = roster_meta
        report["meta"]["generated_utc"] = datetime.now(timezone.utc).isoformat()
        # Books figures, joined onto whoever is already on the board.
        try:
            books, books_meta = _v5_books(date_start, date_end)
            seen = set()
            for a in report.get("ranked", []) + report.get("unranked", []):
                b = books.get(_norm_name(a["name"]))
                if b:
                    seen.add(_norm_name(a["name"]))
                # Every counter bucket() defines must be listed here. This
                # projection sits between the bucketing and the template, and
                # tests on either side of it both stayed green while it silently
                # dropped quotes_invoiced -- the panel read undefined.
                a["books"] = {k: b[k] for k in
                              ("quotes_sent", "quotes_invoiced", "retainers_sent",
                               "retainers_paid", "paid_amount")} if b else {
                    "quotes_sent": 0, "quotes_invoiced": 0, "retainers_sent": 0,
                    "retainers_paid": 0, "paid_amount": 0.0}
            # A roster member who quoted today but has not dialled yet was simply
            # absent: the board is built from RingCX, so no calls meant no row.
            # Give them a row with real quotes and zero dials, in unranked so it
            # cannot drag the floor.
            added = []
            for k, v in books.items():
                if k in seen or k in _NOT_SALES_NAMES:
                    continue
                if roster is not None and k not in roster:
                    continue
                if not (v["quotes_sent"] or v["quotes_invoiced"]):
                    continue
                report.setdefault("unranked", []).append(_zero_call_row(v["display"], v))
                added.append(v["display"])
                seen.add(k)
            books_meta["added_without_calls"] = sorted(added)

            # Anyone left with Books activity and still no row is worth naming
            # rather than dropping -- it usually means a name mismatch.
            orphans = [v["display"] for k, v in books.items()
                       if k not in seen and k != "unassigned"
                       and (v["quotes_sent"] or v["quotes_invoiced"]
                            or v["retainers_sent"] or v["retainers_paid"])]
            books_meta["not_on_board"] = sorted(orphans)[:12]

            # CRM activities booked ahead. None means "could not fetch" -- the
            # template shows a dash for that rather than a confident 0.
            _fmt = "%Y-%m-%dT%H:%M:%S+00:00"
            crm, crm_meta = _v5_activities_created(
                start_dt.strftime(_fmt), end_dt.strftime(_fmt),
                [a["name"] for a in report.get("ranked", []) + report.get("unranked", [])])
            report["meta"]["crm"] = crm_meta
            for a in report.get("ranked", []) + report.get("unranked", []):
                a["followups"] = (None if crm is None
                                  else crm.get(_norm_name(a["name"]), 0))
            report["meta"]["books"] = books_meta
            if books_meta.get("errors"):
                report.setdefault("warnings", []).append({
                    "kind": "books_unavailable",
                    # Carry the reason, not just the metric name. "retainers paid: 0"
                    # and "retainers paid: we were refused" must not read the same.
                    "detail": "These Books figures read zero because they could not be "
                              "fetched, not because there were none — " +
                              "; ".join("%s: %s" % (e["metric"].replace("_", " "), e["detail"])
                                        for e in books_meta["errors"])})
        except Exception as e:  # noqa: BLE001
            log.warning("v5 books join failed: %s", e)

        report["meta"]["ringcx_source"] = cx_source
        report["meta"]["ringex_source"] = dict(ex_meta, calls=len(ex_rows),
                                              clamped_to_ringcx=ex_clamped or None)
        report["meta"]["covers_to"] = cov
        _note = getattr(_ringcx, "last_ringex_note", None)
        if _note and not live_meta.get("cached"):
            report.setdefault("warnings", []).append(
                {"kind": "ringex_incomplete", "detail": _note})
        report["meta"]["available_days"] = _inbox_days()[:60]
        if cx_source.get("stale"):
            report.setdefault("warnings", []).append(
                {"kind": "report_stale", "detail": cx_source["stale_detail"]})
        if cx_source["source"] == "live_cdr_api":
            report.setdefault("warnings", []).append({
                "kind": "ringcx_source_fallback",
                "detail": "No Interaction Report has been delivered for this day, so RingCX "
                          "figures come from the live CDR pull. That is a different report "
                          "and may not contain every interaction.",
            })
        report["meta"]["share_mode"] = bool(_v5_token_ok() and not session.get("user"))
        return jsonify(report)
    except Exception as e:
        log.exception("/api/v5/report error")
        return jsonify({"error": "report_failed", "detail": str(e)}), 500


@app.route("/v2/agents")
@login_required
def agents_report_v2():
    """Per-agent productivity report (talk/handle time, calls, calls >3m) for a
    day. Consumes /api/agent-analytics (RingCentral Business Analytics, covering
    both RingEX and RingCX)."""
    user = session.get("user") or {}
    return render_template("agents_report.html", current_user=user)


@app.route("/v2/interactions")
@login_required
def interactions_report_v2():
    """Automatic per-agent interactions report — pulls calls live from RingEX +
    RingCX for any date range. The CSV-upload version lives at /v2/interactions/csv."""
    user = session.get("user") or {}
    return render_template("interactions_report.html", current_user=user)


@app.route("/v2/interactions/csv")
@login_required
def interactions_report_csv():
    """Upload a RingCX Interactions CSV export and get the full per-agent report
    (talk time, calls >3/>10 min, connect rate, campaign vs. personal queue,
    dispositions). Self-contained: parses the file, no live RingCX/WEM access.
    Kept alongside the automatic report because the RingCX CSV carries WEM/CDR
    detail (true dialer talk time, dispositions) the live pull can't always see."""
    user = session.get("user") or {}
    return render_template("interactions_report_csv.html", current_user=user)


@app.route("/api/interactions/analyze", methods=["POST"])
@login_required
def api_interactions_analyze():
    """Parse an uploaded interactions CSV into stats. Returns the same shape the
    front-end renders (totals, agents, channels, daily, dispositions)."""
    from interactions_report import analyze_csv, ReportError

    f = request.files.get("file")
    if f is None or not f.filename:
        return jsonify({"status": "error", "message": "No file uploaded."}), 400
    try:
        raw = f.read()
        # utf-8-sig strips the BOM RingCX puts on the header row
        text = raw.decode("utf-8-sig", errors="replace")
        result = analyze_csv(text)
        result["filename"] = f.filename
        return jsonify(result)
    except ReportError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    except Exception as e:  # noqa: BLE001 — surface parse failures to the UI
        log.exception("interactions analyze failed")
        return jsonify({"status": "error", "message": f"Couldn't parse the file: {e}"}), 500


# ────────── Saved interactions reports (shared record) ──────────
# Stored as JSON under the same _data_dir the dashboard cache uses, so they land
# on the Render /data persistent disk when it's mounted.
SAVED_REPORTS_DIR = _data_dir / "saved_reports"
try:
    SAVED_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
except Exception as e:  # noqa: BLE001
    log.warning("Could not create saved_reports dir: %s", e)


def _saved_report_path(rid: str):
    """Resolve an id to its file path, rejecting anything non-slug (path safety)."""
    import re
    if not rid or not re.fullmatch(r"[0-9A-Za-z_-]{1,64}", rid):
        return None
    return SAVED_REPORTS_DIR / f"{rid}.json"


def _saved_report_meta(blob: dict) -> dict:
    """The light record shown in the saved-reports list (no agent arrays)."""
    t = (blob.get("report") or {}).get("totals") or {}
    return {
        "id": blob.get("id"),
        "name": blob.get("name"),
        "saved_at": blob.get("saved_at"),
        "saved_by": blob.get("saved_by"),
        "filename": blob.get("filename"),
        "date_start": t.get("date_start"),
        "date_end": t.get("date_end"),
        "agents": t.get("agents"),
        "outbound": t.get("outbound"),
        "talk_secs": t.get("talk_secs"),
        "interactions": t.get("interactions"),
    }


@app.route("/api/interactions/saved", methods=["GET"])
@login_required
def api_interactions_saved_list():
    items = []
    for p in SAVED_REPORTS_DIR.glob("*.json"):
        try:
            items.append(_saved_report_meta(json.loads(p.read_text())))
        except Exception:  # noqa: BLE001 — skip a corrupt file, don't fail the list
            continue
    items.sort(key=lambda x: x.get("saved_at") or "", reverse=True)
    return jsonify({"status": "ok", "reports": items})


@app.route("/api/interactions/saved/<rid>", methods=["GET"])
@login_required
def api_interactions_saved_get(rid):
    p = _saved_report_path(rid)
    if p is None or not p.exists():
        return jsonify({"status": "error", "message": "Report not found."}), 404
    try:
        return jsonify({"status": "ok", **json.loads(p.read_text())})
    except Exception as e:  # noqa: BLE001
        return jsonify({"status": "error", "message": f"Couldn't read report: {e}"}), 500


@app.route("/api/interactions/saved/<rid>/delete", methods=["POST"])
@login_required
def api_interactions_saved_delete(rid):
    p = _saved_report_path(rid)
    if p is None or not p.exists():
        return jsonify({"status": "error", "message": "Report not found."}), 404
    try:
        p.unlink()
        return jsonify({"status": "ok"})
    except Exception as e:  # noqa: BLE001
        return jsonify({"status": "error", "message": f"Couldn't delete: {e}"}), 500


@app.route("/api/interactions/save", methods=["POST"])
@login_required
def api_interactions_save():
    """Re-parse the uploaded file server-side and store it with who/when metadata
    so any signed-in user can open it later."""
    from interactions_report import analyze_csv, ReportError
    import uuid

    f = request.files.get("file")
    if f is None or not f.filename:
        return jsonify({"status": "error", "message": "No file uploaded."}), 400
    try:
        text = f.read().decode("utf-8-sig", errors="replace")
        report = analyze_csv(text)
    except ReportError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    except Exception as e:  # noqa: BLE001
        log.exception("interactions save-parse failed")
        return jsonify({"status": "error", "message": f"Couldn't parse the file: {e}"}), 500

    now = datetime.now(timezone.utc)
    rid = now.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
    user = session.get("user") or {}
    saved_by = user.get("email") or user.get("name") or "Unknown"
    t = report.get("totals") or {}
    name = (request.form.get("name") or "").strip() or \
        f"{t.get('date_start', '?')} – {t.get('date_end', '?')}"
    blob = {
        "id": rid,
        "name": name,
        "saved_at": now.isoformat().replace("+00:00", "Z"),
        "saved_by": saved_by,
        "filename": f.filename,
        "report": report,
    }
    p = _saved_report_path(rid)
    try:
        p.write_text(json.dumps(blob, separators=(",", ":")))
    except Exception as e:  # noqa: BLE001
        log.exception("saving report failed")
        return jsonify({"status": "error", "message": f"Couldn't save: {e}"}), 500
    return jsonify({"status": "ok", "report": _saved_report_meta(blob)})


def _parse_local_date_to_utc(
    date_str: str, hour: int, minute: int, second: int,
    tz_offset_minutes=None,
) -> datetime:
    """Convert a YYYY-MM-DD local-time date string to a UTC-aware datetime.

    tz_offset_minutes: browser's getTimezoneOffset() value (minutes WEST of UTC,
    e.g. CDT = 300, CST = 360).  When provided it takes precedence over the env var
    so that DST is handled correctly.
    """
    if tz_offset_minutes is not None:
        # getTimezoneOffset() is positive for zones behind UTC (CDT=300 → UTC-5)
        tz_offset_hours = -tz_offset_minutes / 60.0
    else:
        # Fallback when the browser didn't send tz: the zone's CURRENT offset (DST-aware),
        # not a fixed env constant that is an hour wrong half the year.
        tz_offset_hours = LOCAL_TZ.utcoffset(datetime.now(timezone.utc)).total_seconds() / 3600.0
    d = datetime.strptime(date_str, "%Y-%m-%d").replace(
        hour=hour, minute=minute, second=second, tzinfo=timezone.utc
    )
    return d - timedelta(hours=tz_offset_hours)


@app.route("/api/data")
@login_required
def api_data():
    start_param = request.args.get("start")   # YYYY-MM-DD local date
    end_param   = request.args.get("end")     # YYYY-MM-DD local date
    tz_param    = request.args.get("tz")      # browser getTimezoneOffset() in minutes
    tz_offset_minutes = int(tz_param) if tz_param is not None else None

    if start_param:
        # Custom date range: live fetch, bypass cache.
        # Run in a thread with a hard timeout so a slow Zoho response never
        # blocks a gunicorn thread indefinitely (which kills the worker).
        try:
            effective_end = end_param or start_param
            start_dt = _parse_local_date_to_utc(start_param, 0, 0, 0, tz_offset_minutes)
            end_dt   = _parse_local_date_to_utc(effective_end, 23, 59, 59, tz_offset_minutes)
            sup = None
            if _ringcx.configured:
                try:
                    sup = _fetch_supplemental_cached()
                except Exception:
                    pass
            with ThreadPoolExecutor(max_workers=1) as ex:
                future = ex.submit(_zoho.get_dashboard_data, start_dt=start_dt, end_dt=end_dt, supplemental_calls=sup)
                data = future.result(timeout=85)  # hard cap — gunicorn worker timeout is 90s
            rd = resolved_data_full()
            annotated = json.loads(json.dumps(data))
            _annotate_resolved(annotated, rd)
            return jsonify({
                "status": "ok",
                "last_updated": datetime.now(timezone.utc).isoformat(),
                "error": None,
                "data": annotated,
                "date_range": {"start": start_param, "end": effective_end},
            })
        except FutureTimeoutError:
            log.error("Custom date range fetch timed out for %s→%s", start_param, end_param)
            return jsonify({"status": "error", "message": "Zoho API is taking too long — try again in a moment."}), 504
        except Exception as exc:
            log.error("Custom date range fetch failed: %s", exc)
            return jsonify({"status": "error", "message": str(exc)}), 500

    # Default: today's cached data
    _self_heal_if_stale()  # keeps the board fresh even if the bg loop has died
    with _lock:
        if _cache["data"] is None and _cache["error"] is None:
            return jsonify({"status": "loading"}), 202
        if _cache["error"] and _cache["data"] is None:
            return jsonify({"status": "error", "message": _cache["error"]}), 500
        data = _cache["data"]
        # Annotate scheduled call records with resolved flag + notes (does not mutate cache)
        is_stale = _cache.get("stale", False)
        rd = resolved_data_full()
        annotated = json.loads(json.dumps(data))  # cheap deep copy
        _annotate_resolved(annotated, rd)
        return jsonify(
            {
                "status": "ok",
                "last_updated": _cache["last_updated"],
                "stale": is_stale,
                "error": _cache["error"],
                "data": annotated,
            }
        )


_callnow_cache: dict = {}
_callnow_lock = threading.Lock()

@app.route("/api/call-now-deals")
@login_or_api_key
def call_now_deals():
    """Call Now deals (Best Contact Time empty or before deal creation) for a day.
    Cached 120s per (start,end): the back-office bot polls this every few minutes and
    each uncached compute holds a request thread for up to 85s — a few overlapping
    callers starved the 8-thread pool and failed /health."""
    tz_param = request.args.get("tz")
    tz_offset_minutes = int(tz_param) if tz_param is not None else None
    start_param = request.args.get("start")
    end_param = request.args.get("end")
    cache_key = (start_param or "", end_param or "")
    with _callnow_lock:
        hit = _callnow_cache.get(cache_key)
        if hit and time.time() - hit["at"] < 120:
            return jsonify(hit["payload"])
    try:
        if start_param:
            eff_end = end_param or start_param
            start_dt = _parse_local_date_to_utc(start_param, 0, 0, 0, tz_offset_minutes)
            end_dt   = _parse_local_date_to_utc(eff_end, 23, 59, 59, tz_offset_minutes)
        else:
            start_dt = end_dt = None
        sup = None
        if _ringcx.configured:
            try:
                sup = _fetch_supplemental_cached()
            except Exception:
                pass
        with ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(_zoho.get_call_now_deals, start_dt=start_dt, end_dt=end_dt,
                            supplemental_calls=sup)
            data = fut.result(timeout=85)
        payload = {"status": "ok", "last_updated": datetime.now(timezone.utc).isoformat(),
                   "data": data}
        with _callnow_lock:
            _callnow_cache[cache_key] = {"at": time.time(), "payload": payload}
            # keep the cache tiny — it only ever holds a handful of day windows
            if len(_callnow_cache) > 8:
                oldest = min(_callnow_cache, key=lambda k: _callnow_cache[k]["at"])
                _callnow_cache.pop(oldest, None)
        return jsonify(payload)
    except Exception as e:
        log.exception("/api/call-now-deals error")
        return jsonify({"status": "error", "message": str(e)}), 500


_qfu_cache: dict = {}
_qfu_lock = threading.Lock()

@app.route("/api/quote-followups")
@login_required
def quote_followups():
    """Quote follow-up tasks grouped by quote (Deal). Cached 120s per (start,end)."""
    tz_param = request.args.get("tz")
    tz_offset_minutes = int(tz_param) if tz_param is not None else None
    start_param = request.args.get("start")
    end_param = request.args.get("end")
    cache_key = (start_param or "", end_param or "")
    with _qfu_lock:
        hit = _qfu_cache.get(cache_key)
        if hit and time.time() - hit["at"] < 120:
            return jsonify(hit["payload"])
    try:
        start_dt = end_dt = None
        if start_param:
            eff_end = end_param or start_param
            start_dt = _parse_local_date_to_utc(start_param, 0, 0, 0, tz_offset_minutes)
            end_dt   = _parse_local_date_to_utc(eff_end, 23, 59, 59, tz_offset_minutes)
        with ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(_zoho.get_quote_followups, start_dt=start_dt, end_dt=end_dt)
            data = fut.result(timeout=60)
        payload = {"status": "ok", "last_updated": datetime.now(timezone.utc).isoformat(),
                   "data": data}
        with _qfu_lock:
            _qfu_cache[cache_key] = {"at": time.time(), "payload": payload}
            if len(_qfu_cache) > 8:
                oldest = min(_qfu_cache, key=lambda k: _qfu_cache[k]["at"])
                _qfu_cache.pop(oldest, None)
        return jsonify(payload)
    except FutureTimeoutError:
        return jsonify({"status": "error", "message": "Zoho is taking too long — try again."}), 504
    except Exception as e:
        log.exception("/api/quote-followups error")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/scheduled-call/<rec_id>/resolve", methods=["POST"])
@login_required
def resolve_call(rec_id):
    body = request.get_json(silent=True) or {}
    resolved_by = (body.get("resolved_by") or "").strip()

    # Accept an observed call from the live monitor (already validated on client)
    observed_call = body.get("observed_call")

    # Try to find the closest logged call for this scheduled record
    matched_call = _find_closest_call_for_record(rec_id)

    # Prefer a Zoho-logged call, fall back to the live-observed call
    final_match = matched_call or observed_call

    with _resolved_lock:
        data = _prune_resolved(_load_resolved())
        entry = data.get(rec_id, {})
        entry["at"] = datetime.now(timezone.utc).isoformat()
        if resolved_by:
            entry["resolved_by"] = resolved_by
        if final_match:
            entry["matched_call"] = final_match
        data[rec_id] = entry
        _save_resolved(data)
    return jsonify({"status": "resolved", "id": rec_id, "matched_call": final_match})


@app.route("/api/scheduled-calls/resolve-bulk", methods=["POST"])
@login_required
def resolve_calls_bulk():
    """Resolve many scheduled/overdue calls at once. Body: {ids: [...], resolved_by}."""
    body = request.get_json(silent=True) or {}
    ids = body.get("ids") or []
    resolved_by = (body.get("resolved_by") or "").strip()
    if not isinstance(ids, list) or not ids:
        return jsonify({"error": "ids required"}), 400
    ids = [str(i) for i in ids][:1000]   # bound the batch
    now_iso = datetime.now(timezone.utc).isoformat()
    with _resolved_lock:
        data = _prune_resolved(_load_resolved())
        for rec_id in ids:
            entry = data.get(rec_id, {})
            entry["at"] = now_iso
            if resolved_by:
                entry["resolved_by"] = resolved_by
            match = _find_closest_call_for_record(rec_id)
            if match:
                entry["matched_call"] = match
            data[rec_id] = entry
        _save_resolved(data)
    return jsonify({"status": "resolved", "count": len(ids), "resolved_by": resolved_by})


@app.route("/api/scheduled-call/<rec_id>/unresolve", methods=["POST"])
@login_required
def unresolve_call(rec_id):
    with _resolved_lock:
        data = _prune_resolved(_load_resolved())
        data.pop(rec_id, None)
        _save_resolved(data)
    return jsonify({"status": "unresolved", "id": rec_id})


@app.route("/api/scheduled-call/<rec_id>/note", methods=["POST"])
@login_required
def save_note(rec_id):
    """Save a note for a scheduled call record. Persists across deploys."""
    body = request.get_json(silent=True) or {}
    note_text = (body.get("note") or "").strip()
    with _resolved_lock:
        data = _prune_resolved(_load_resolved())
        if rec_id not in data:
            # Create entry even if not resolved — just for notes
            data[rec_id] = {"at": datetime.now(timezone.utc).isoformat()}
        data[rec_id]["note"] = note_text
        _save_resolved(data)
    return jsonify({"status": "ok", "id": rec_id, "note": note_text})


@app.route("/api/scheduled-call/<rec_id>/resolved-by", methods=["POST"])
@login_required
def save_resolved_by(rec_id):
    """Save the resolved-by name for an overdue call record."""
    body = request.get_json(silent=True) or {}
    name = (body.get("resolved_by") or "").strip()
    with _resolved_lock:
        data = _prune_resolved(_load_resolved())
        if rec_id not in data:
            data[rec_id] = {"at": datetime.now(timezone.utc).isoformat()}
        data[rec_id]["resolved_by"] = name
        _save_resolved(data)
    return jsonify({"status": "ok", "id": rec_id, "resolved_by": name})


@app.route("/api/scheduled-call/<rec_id>/assigned-to", methods=["POST"])
@login_required
def save_assigned_to(rec_id):
    """Save the assigned-to name for an overdue call record."""
    body = request.get_json(silent=True) or {}
    name = (body.get("assigned_to") or "").strip()
    with _resolved_lock:
        data = _prune_resolved(_load_resolved())
        if rec_id not in data:
            data[rec_id] = {"at": datetime.now(timezone.utc).isoformat()}
        data[rec_id]["assigned_to"] = name
        _save_resolved(data)
    return jsonify({"status": "ok", "id": rec_id, "assigned_to": name})


@app.route("/api/ai-handled", methods=["POST"])
def api_ai_handled():
    """The back-office AI replied to a patient. Record it against their phone number so the
    dashboard can show "Resolved by bot" instead of a call that looks untouched.

    Machine-to-machine: authenticated with the shared OVERDUE_API_KEY, not a browser session.
    Keyed by phone rather than record id because the bot knows the patient, not our record.
    """
    if not _valid_api_key():
        return jsonify({"error": "unauthorized"}), 401
    body = request.get_json(silent=True) or {}
    phone = _phone10(body.get("phone"))
    if not phone:
        return jsonify({"error": "phone required"}), 400

    entry = {
        "at": (body.get("at") or datetime.now(timezone.utc).isoformat()),
        "scenario": (body.get("scenario") or "")[:60],
        "contact_name": (body.get("contact_name") or "")[:80],
        "inbound": (body.get("inbound") or "")[:200],
        "reply": (body.get("reply") or "")[:300],
    }
    with _ai_lock:
        data = _prune_ai_handled(_load_ai_handled())
        prev = data.get(phone) or {}
        entry["count"] = int(prev.get("count", 0)) + 1   # how many times the bot answered them
        data[phone] = entry
        _save_ai_handled(data)
    return jsonify({"ok": True, "phone": phone, "count": entry["count"]})


@app.route("/api/scheduled-call/<rec_id>/claimed", methods=["POST"])
def api_scheduled_call_claimed(rec_id):
    """A coordinator claimed this call's Telegram card in the back-office bot.

    "Distributed" only ever meant "a card was posted"; it never said who picked it up, so a
    claimed call looked identical to one sitting unclaimed in the chat. Machine-to-machine
    (shared OVERDUE_API_KEY), keyed by our record id — which the bot carries as externalId.
    """
    if not _valid_api_key():
        return jsonify({"error": "unauthorized"}), 401
    body = request.get_json(silent=True) or {}
    claimed_by = (body.get("claimed_by") or "").strip()[:80]
    if not claimed_by:
        return jsonify({"error": "claimed_by required"}), 400
    with _resolved_lock:
        data = _prune_resolved(_load_resolved())
        entry = data.setdefault(rec_id, {"at": datetime.now(timezone.utc).isoformat()})
        entry["claimed_by"] = claimed_by
        entry["claimed_at"] = (body.get("at") or datetime.now(timezone.utc).isoformat())
        # A claimed card is by definition distributed; keep the two consistent.
        entry["distributed"] = True
        entry.setdefault("distributed_by", BOT_DISTRIBUTED_BY)
        _save_resolved(data)
    return jsonify({"status": "ok", "id": rec_id, "claimed_by": claimed_by})


# ── Bot distributions (proxied from the back-office app) ─────────────────────
# The back-office owns distribution; we own scheduled calls. It answers "what did the bot
# hand out today, to whom, and how did it end". Cached briefly so a dozen open boards don't
# hammer it — the underlying data changes on a human timescale.
BACKOFFICE_URL = os.environ.get("BACKOFFICE_URL", "").rstrip("/")
_botdist_cache = {"at": 0.0, "date": "", "payload": None}
_botdist_lock = threading.Lock()
_BOTDIST_TTL = 30.0


def _fetch_backoffice_call_stats(start_dt: datetime, end_dt: datetime):
    """Per-agent call stats from the back office's RingCentral account call-log (this
    tracker's own RingEX/RingCX access returns nothing). ALWAYS returns a dict with a
    'diag' block so a 0 result explains itself: {"agents": {...}, "meta": {...},
    "diag": {"reached": bool, "http": int|None, "error": str|None, "url": str}}."""
    url = f"{BACKOFFICE_URL}/api/agent-call-stats" if BACKOFFICE_URL else ""
    diag = {"reached": False, "http": None, "error": None, "url": url}
    if not BACKOFFICE_URL or not OVERDUE_API_KEY:
        diag["error"] = "BACKOFFICE_URL or OVERDUE_API_KEY not set on the tracker"
        return {"agents": {}, "meta": {}, "diag": diag}
    try:
        r = requests.get(
            url,
            params={
                "start": start_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "end": end_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            },
            headers={"X-API-Key": OVERDUE_API_KEY},
            timeout=60, allow_redirects=False,
        )
        diag["http"] = r.status_code
        if r.status_code in (301, 302, 303, 307, 308):
            diag["error"] = "redirected to login — the back office did not accept the API key"
            return {"agents": {}, "meta": {}, "diag": diag}
        if r.status_code != 200:
            diag["error"] = f"HTTP {r.status_code}: {r.text[:140]}"
            return {"agents": {}, "meta": {}, "diag": diag}
        if "application/json" not in r.headers.get("Content-Type", ""):
            diag["error"] = f"non-JSON response ({r.headers.get('Content-Type', '?')})"
            return {"agents": {}, "meta": {}, "diag": diag}
        diag["reached"] = True
        data = r.json() or {}
        agents = {}
        for a in data.get("agents", []):
            nm = (a.get("name") or "").strip()
            if not nm:
                continue
            calls = int(a.get("calls", 0) or 0)
            u3 = int(a.get("calls_under_3m", 0) or 0)
            agents[nm] = {
                "calls": calls,
                "talk_seconds": int(a.get("talk_seconds", 0) or 0),
                "calls_under_3m": u3,
                "calls_under_15m": int(a.get("calls_under_15m", 0) or 0),
                "calls_over_3m": max(0, calls - u3),
                "handle_seconds": 0,
            }
        return {"agents": agents, "diag": diag, "meta": {
            "calls_fetched": data.get("calls_fetched"),
            "calls_in_window": data.get("calls_in_window"),
            "via_leg": data.get("via_leg"),
            "unattributed": data.get("unattributed"),
        }}
    except Exception as ex:
        diag["error"] = f"{type(ex).__name__}: {ex}"
        log.warning("back-office agent-call-stats fetch failed: %s", ex)
        return {"agents": {}, "meta": {}, "diag": diag}


def _fetch_zoho_agent_calls(start_dt: datetime, end_dt: datetime):
    """Per-agent OUTBOUND call stats from Zoho CRM Calls — the CRM system of record, where
    BOTH platforms log: regular/manual RingEX calls AND RingCX dialer calls. So this is the
    one source that already includes the dialer (no RingCX reporting permission needed).
    Returns {"agents": {name: {...}}, "meta": {...}} or None."""
    if not getattr(_zoho, "configured", True):
        return None
    try:
        start_str = start_dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        end_str = end_dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        rows = _zoho._fetch_calls_full_dump(start_str, end_str)
    except Exception as ex:
        log.warning("zoho agent calls fetch failed: %s", ex)
        return None

    agents = {}
    unattributed = 0
    for c in rows or []:
        owner = c.get("Owner") or {}
        name = (owner.get("name") or owner.get("full_name") or "").strip() if isinstance(owner, dict) else ""
        if not name:
            unattributed += 1
            continue
        try:
            dur = int(float(c.get("Call_Duration_in_seconds") or 0))
        except (TypeError, ValueError):
            dur = 0
        if dur < 0:
            dur = 0
        a = agents.setdefault(name, {"calls": 0, "talk_seconds": 0, "calls_under_3m": 0,
                                     "calls_under_15m": 0, "calls_over_3m": 0, "handle_seconds": 0})
        a["calls"] += 1
        a["talk_seconds"] += dur
        if dur < 180:
            a["calls_under_3m"] += 1
        else:
            a["calls_over_3m"] += 1
        if dur < 900:
            a["calls_under_15m"] += 1
    return {"agents": agents, "meta": {
        "calls_fetched": len(rows or []),
        "calls_in_window": len(rows or []),
        "unattributed": unattributed,
        "reached": True,
    }}


def _merge_duplicate_agents(agents: dict) -> dict:
    """Same person, two names: the call source uses formal RingCentral names ("Gregory
    Beltran", "Charlotte McKay") while bot claims use roster/nicknames ("Gregorys Beltran",
    "Charlotte", "Rothmel (Roe)", "Maisah ."). Merge them conservatively so each agent is one
    row. Only merges when confident:
      1) both full names, same last name (prefix-tolerant) + first-name prefix, or
      2) a first-name-only name that matches EXACTLY ONE full name's first name.
    The fuller name is kept as the display name."""
    def toks(n):
        s = (n or "").lower()
        s = re.sub(r"\(.*?\)", " ", s)          # drop "(roe)"-style aliases, not a last name
        s = re.sub(r"[^a-z\s]", " ", s)
        return [t for t in s.split() if t]

    STAT_KEYS = ("calls", "calls_under_3m", "calls_under_15m", "calls_over_3m",
                 "talk_seconds", "handle_seconds", "quotes", "closings", "deals_claimed")
    names = [r["name"] for r in agents.values()]
    tk = {n: toks(n) for n in names}
    parent = {}

    def find(x):
        while parent.get(x, x) != x:
            x = parent[x]
        return x

    def pfx(a, b):
        return a == b or (len(a) >= 3 and len(b) >= 3 and (a.startswith(b) or b.startswith(a)))

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        keep, drop = (ra, rb) if len(tk[ra]) >= len(tk[rb]) else (rb, ra)
        parent[drop] = keep

    # Pass 1 — full-name pairs: last name matches + first name prefix.
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = names[i], names[j]
            ta, tb = tk[a], tk[b]
            if len(ta) >= 2 and len(tb) >= 2 and pfx(ta[-1], tb[-1]) and pfx(ta[0], tb[0]):
                union(a, b)

    # Pass 2 — first-name-only → the unique full name sharing that first name.
    first_to_full = {}
    for n in names:
        if len(tk[n]) >= 2:
            first_to_full.setdefault(tk[n][0], []).append(n)
    for n in names:
        if len(tk[n]) == 1:
            cands = first_to_full.get(tk[n][0], [])
            if cands and len({find(c) for c in cands}) == 1:
                union(n, cands[0])

    merged = {}
    for r in agents.values():
        canon = find(r["name"])
        m = merged.get(canon)
        if m is None:
            m = {"name": canon, **{k: 0 for k in STAT_KEYS}}
            merged[canon] = m
        for k in STAT_KEYS:
            m[k] += int(r.get(k, 0) or 0)
    return merged


def _fetch_bot_claim_counts(start_date_str: str, end_date_str: str) -> dict:
    """Deals claimed per coordinator in the bot (Telegram) over an inclusive ET-day
    range ('YYYY-MM-DD'..'YYYY-MM-DD'). Reads the back-office /api/bot-distributions
    once per day with the shared key. Returns {claimed_by: count}; empty if the
    back-office isn't configured or reachable."""
    out: dict = {}
    if not BACKOFFICE_URL or not OVERDUE_API_KEY:
        return out
    try:
        d0 = datetime.strptime(start_date_str, "%Y-%m-%d").date()
        d1 = datetime.strptime(end_date_str, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return out
    if d1 < d0:
        d0, d1 = d1, d0
    if (d1 - d0).days > 62:          # bound the fan-out — one HTTP call per day
        d0 = d1 - timedelta(days=62)
    day = d0
    while day <= d1:
        ds = day.isoformat()
        try:
            r = requests.get(
                f"{BACKOFFICE_URL}/api/bot-distributions",
                params={"date": ds},
                headers={"X-API-Key": OVERDUE_API_KEY},
                timeout=12, allow_redirects=False,
            )
            if r.status_code == 200 and "application/json" in r.headers.get("Content-Type", ""):
                for row in ((r.json() or {}).get("distributions") or []):
                    name = (row.get("claimed_by") or "").strip()
                    if name:
                        out[name] = out.get(name, 0) + 1
        except Exception as ex:
            log.warning("bot-claim counts for %s failed: %s", ds, ex)
        day += timedelta(days=1)
    return out

@app.route("/api/bot-distributions")
@login_required
def api_bot_distributions():
    if not BACKOFFICE_URL or not OVERDUE_API_KEY:
        return jsonify({
            "error": "not configured",
            "detail": "Set BACKOFFICE_URL and OVERDUE_API_KEY on this service.",
            "distributions": [], "count": 0,
        }), 200      # 200, not 500: the card renders an explanation rather than a dead spinner

    date = (request.args.get("date") or "").strip()
    with _botdist_lock:
        fresh = (
            _botdist_cache["payload"] is not None
            and _botdist_cache["date"] == date
            and time.time() - _botdist_cache["at"] < _BOTDIST_TTL
        )
        if fresh:
            return jsonify(_botdist_cache["payload"])

    url = f"{BACKOFFICE_URL}/api/bot-distributions"
    try:
        # allow_redirects=False on purpose. The back-office bounces unauthenticated requests
        # to /login, and following that redirect returned an HTML page that json() choked on
        # with "Expecting value: line 1 column 1" — a parse error that says nothing about the
        # actual problem (a rejected key). Read the status instead, and say what it means.
        r = requests.get(url, params={"date": date} if date else None,
                         headers={"X-API-Key": OVERDUE_API_KEY}, timeout=12,
                         allow_redirects=False)
        if r.status_code in (301, 302, 303, 307, 308):
            raise RuntimeError(
                "back-office redirected to login — it did not accept the API key. "
                "Check OVERDUE_API_KEY matches on both services.")
        if r.status_code == 401:
            raise RuntimeError("back-office rejected the API key (401). "
                               "Check OVERDUE_API_KEY matches on both services.")
        r.raise_for_status()
        ctype = r.headers.get("Content-Type", "")
        if "application/json" not in ctype:
            raise RuntimeError(f"back-office returned {ctype or 'no content-type'}, not JSON "
                               f"(status {r.status_code})")
        payload = r.json()
    except Exception as e:
        # Never 500 the board because the other service is slow, down, or misconfigured.
        print(f"[bot-dist] fetch failed: {e}", flush=True)
        return jsonify({"error": "upstream unavailable", "detail": str(e)[:220],
                        "distributions": [], "count": 0}), 200

    with _botdist_lock:
        _botdist_cache.update({"at": time.time(), "date": date, "payload": payload})
    return jsonify(payload)


# ── Dialer CDR feed (for the back-office bot) ─────────────────────────────────
# This app has WORKING RingCX credentials (the reportsStreaming CDR behind the agent
# report and call history). The back-office needs the same rows to log dialer calls
# against distributed deals, but its own RINGCX_ACCESS_TOKEN is often unset — so it
# reads them from here instead of needing its own. Cached 60s: EV report generation
# is heavy, and the bot polls every few minutes.
_dialer_cdr_cache = {"at": 0.0, "hours": 0, "rows": None}
_dialer_cdr_lock = threading.Lock()

def _get_dialer_cdr_rows(hours: int = 24) -> list:
    """RingCX CDR rows for the last N hours, 60s-cached (EV report generation is heavy)."""
    if not _ringcx.configured:
        return []
    with _dialer_cdr_lock:
        fresh = (_dialer_cdr_cache["rows"] is not None
                 and _dialer_cdr_cache["hours"] == hours
                 and time.time() - _dialer_cdr_cache["at"] < 60)
        if fresh:
            return _dialer_cdr_cache["rows"]
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=hours)
        try:
            rows = _ringcx._fetch_ringcx_cdr_rows(start, end)
        except Exception as e:
            log.warning("dialer CDR fetch failed: %s", e)
            rows = []
        _dialer_cdr_cache.update({"at": time.time(), "hours": hours, "rows": rows})
        return rows


_sup_cache = {"at": 0.0, "val": {}}
_sup_lock = threading.Lock()
_sup_refreshing = {"v": False}

def _fetch_supplemental_cached() -> dict:
    """Request-thread-safe supplemental: returns the last known value instantly and kicks a
    background refresh when stale. The full fetch (RingEX pages + the CDR) can take minutes —
    on a request thread that blows gunicorn's 90s worker timeout, the worker gets killed, and
    /health starts failing. Request paths use THIS; only the background _refresh fetches inline.
    """
    with _sup_lock:
        fresh = time.time() - _sup_cache["at"] < 60
        val = _sup_cache["val"]
        if fresh or _sup_refreshing["v"]:
            return val
        _sup_refreshing["v"] = True

    def _work():
        try:
            new_val = _fetch_supplemental()
            with _sup_lock:
                _sup_cache.update({"at": time.time(), "val": new_val})
        except Exception as e:
            log.warning("supplemental background refresh failed: %s", e)
        finally:
            with _sup_lock:
                _sup_refreshing["v"] = False
    threading.Thread(target=_work, daemon=True, name="sup-refresh").start()
    return val


def _fetch_supplemental() -> dict:
    """Every call the CRM might not know about, keyed by last-10-digit patient phone:
    the RingEX account log (coordinators dialing from RingCentral) PLUS the RingCX
    dialer CDR. The board's classification only ever merged the RingEX log, so campaign
    calls the dialer placed — which never appear in that log — showed as "not dialed"
    unless a browser happened to observe them live. This is why calls looked unlogged.
    """
    sup: dict = {}
    if not _ringcx.configured:
        return sup
    try:
        sup = _ringcx.fetch_todays_outbound_calls() or {}
    except Exception as e:
        log.warning("RingEX supplemental fetch failed: %s", e)
        sup = {}
    for r in _get_dialer_cdr_rows(24):
        direction = (r.get("direction") or "").upper()
        raw = r.get("dnis") if "OUT" in direction else r.get("ani")
        digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
        phone = digits[-10:] if len(digits) >= 10 else ""
        if not phone or not r.get("start_time"):
            continue
        sup.setdefault(phone, []).append({
            "id": r.get("uii") or "",
            "start_time": r.get("start_time"),
            "duration": r.get("duration", 0),
            "result": r.get("result", ""),
            "direction": direction or "Outbound",
            "to_number": raw or phone,
            "agent": r.get("agent_name", ""),
            "source": "ringcx",
        })
    return sup


@app.route("/api/dialer-calls")
def api_dialer_calls():
    if not _valid_api_key():
        return jsonify({"error": "unauthorized"}), 401
    if not _ringcx.configured:
        return jsonify({"status": "ok", "count": 0, "calls": [],
                        "note": "RingCX not configured on the tracker"})
    try:
        hours = max(1, min(int(request.args.get("hours", "24") or 24), 72))
    except ValueError:
        hours = 24
    rows = _get_dialer_cdr_rows(hours)
    return jsonify({"status": "ok", "count": len(rows), "calls": rows})


@app.route("/api/ai-handled", methods=["GET"])
@login_required
def api_ai_handled_list():
    """Everything the bot has handled in the last 7 days, keyed by phone."""
    return jsonify({"ai_handled": ai_handled_data()})


@app.route("/api/scheduled-call/<rec_id>/distributed", methods=["POST"])
@login_required
def save_distributed(rec_id):
    """Toggle the manually-distributed flag for a scheduled call."""
    body = request.get_json(silent=True) or {}
    distributed = bool(body.get("distributed", False))
    distributed_by = (body.get("distributed_by") or "").strip()
    with _resolved_lock:
        data = _prune_resolved(_load_resolved())
        if rec_id not in data:
            data[rec_id] = {"at": datetime.now(timezone.utc).isoformat()}
        data[rec_id]["distributed"] = distributed
        if distributed and distributed_by:
            data[rec_id]["distributed_by"] = distributed_by
        elif not distributed:
            data[rec_id].pop("distributed_by", None)
        _save_resolved(data)
    return jsonify({"status": "ok", "id": rec_id, "distributed": distributed})


# ────────── Overdue calls API (for the back-office Telegram bot) ──────────
#
# The back-office bot polls this to distribute overdue scheduled calls into the
# sales Telegram chat as P1 "Claim" cards. Auth is the shared OVERDUE_API_KEY
# (header X-API-Key or ?key=), NOT session login, so it works machine-to-machine.

# The bot's `distributed_by` value. Three spellings existed: the endpoint defaulted to
# "bot", the reset endpoint only cleared "backoffice-bot", and the template only recognised
# "bot". A bot POST that omitted the field was hidden from /api/overdue-calls forever AND
# unreachable by the cleanup — the call was silently lost.
BOT_DISTRIBUTED_BY = "backoffice-bot"
BOT_ALIASES = {"bot", "backoffice-bot"}

# Handing a call to the bot on GET, then waiting for its POST, leaves a window in which a
# second poll sees the same call and distributes it twice. Claim it at read time for a few
# minutes; if the bot never confirms, the claim lapses and the call comes back.
_CLAIM_TTL_SECONDS = 300
_overdue_claims: dict[str, float] = {}
_claims_lock = threading.RLock()   # re-entrant: the compute-and-claim block holds it while _is_claimed re-acquires

def _claim_overdue(ids: list[str]) -> None:
    now = time.time()
    with _claims_lock:
        for stale in [k for k, t in _overdue_claims.items() if now - t > _CLAIM_TTL_SECONDS]:
            _overdue_claims.pop(stale, None)
        for i in ids:
            _overdue_claims[i] = now

def _is_claimed(rec_id: str) -> bool:
    with _claims_lock:
        t = _overdue_claims.get(rec_id)
        return bool(t and time.time() - t <= _CLAIM_TTL_SECONDS)


def _compute_overdue_calls() -> list[dict]:
    """The same set the dashboard shows under 'Overdue', computed server-side:
    a scheduled call is overdue when it's >15 min past its scheduled time, has no
    logged call, and hasn't been resolved, completed via workflow, or already
    distributed. Returns lightweight cards: id, name, phone, language, scheduled_time.

    These land in the back-office bot as tier P3 — worked after P1 (a patient who asked
    for a call in the last 20 min) and P2 (a call booked in Zoho, at its time). The
    most-overdue-first ordering below matches how the bot ranks within P3, so what this
    endpoint hands over is already in the order it will be sent."""
    # Bot polling counts as traffic — keep the cache fresh even with no browser open.
    _self_heal_if_stale()
    with _lock:
        data = _cache["data"]
    if not data:
        return []
    rd = resolved_data_full()
    annotated = json.loads(json.dumps(data))  # deep copy — don't mutate cache
    _annotate_resolved(annotated, rd)          # sets resolved / distributed / status
    records = (annotated.get("scheduled_calls") or {}).get("records") or []

    now = datetime.now(timezone.utc)
    out = []
    for r in records:
        # Already handled or already sent to the bot → skip.
        if r.get("resolved") or r.get("distributed") or r.get("completed_via_workflow"):
            continue
        if r.get("actual_call_time"):  # a call was logged → not overdue
            continue
        if _is_claimed(r.get("id") or ""):
            continue                      # handed to the bot moments ago; awaiting its POST
        sched = r.get("scheduled_time")
        dt = _zoho._parse_dt(sched)
        if not dt:
            continue
        # The board floors pre-9 AM calls to 9 AM (_effective_scheduled) but this did not,
        # so a 6:00 AM call was "20 min overdue" at 6:20 and got distributed while the
        # dashboard still showed it as upcoming. Measure from the same effective time.
        try:
            dt = _zoho._effective_scheduled(dt)
        except Exception:
            pass
        minutes_overdue = (now - dt).total_seconds() / 60
        if minutes_overdue <= 15:  # matches the dashboard's 15-min pending window
            continue
        out.append({
            "id": r.get("id"),
            "name": r.get("name") or "",
            "phone": r.get("phone") or "",
            "language": r.get("language") or "",
            "scheduled_time": sched,
            "minutes_overdue": round(minutes_overdue, 1),
            "owner": r.get("owner") or "",
        })
    out.sort(key=lambda x: x["minutes_overdue"], reverse=True)  # most overdue first
    return out


@app.route("/api/overdue-calls")
def api_overdue_calls():
    if not _valid_api_key():
        return jsonify({"error": "unauthorized"}), 401
    with _lock:
        loading = _cache["data"] is None
    if loading:
        return jsonify({"status": "loading", "count": 0, "overdue": []})
    # Compute-and-claim under ONE lock. Split, two overlapping GETs (8 gthreads) could both
    # compute the same unclaimed call before either claimed it → double distribution. The
    # in-memory claim map itself assumes -w 1 (single process), which render.yaml pins.
    with _claims_lock:
        overdue = _compute_overdue_calls()
        now_ts = time.time()
        for stale in [k for k, t in _overdue_claims.items() if now_ts - t > _CLAIM_TTL_SECONDS]:
            _overdue_claims.pop(stale, None)
        for o in overdue:
            if o.get("id"):
                _overdue_claims[o["id"]] = now_ts
    return jsonify({"status": "ok", "count": len(overdue), "overdue": overdue})


@app.route("/api/overdue-calls/<rec_id>/distributed", methods=["POST"])
def api_overdue_mark_distributed(rec_id):
    """The bot calls this after posting an overdue call to Telegram so it isn't
    surfaced (or re-distributed) again. Same store/flag the dashboard uses."""
    if not _valid_api_key():
        return jsonify({"error": "unauthorized"}), 401
    body = request.get_json(silent=True) or {}
    distributed_by = (body.get("distributed_by") or BOT_DISTRIBUTED_BY).strip()
    with _resolved_lock:
        data = _prune_resolved(_load_resolved())
        if rec_id not in data:
            data[rec_id] = {"at": datetime.now(timezone.utc).isoformat()}
        data[rec_id]["distributed"] = True
        data[rec_id]["distributed_by"] = distributed_by
        _save_resolved(data)
    return jsonify({"status": "ok", "id": rec_id, "distributed": True})


@app.route("/api/overdue-calls/reset-bot-distributed", methods=["POST"])
def api_overdue_reset_bot_distributed():
    """Clear the `distributed` flag on calls the BOT marked but never actually sent.

    The back-office bot used to POST /distributed the moment it queued a call, not
    when the Telegram card was sent. Its queue posts one card at a time and refuses
    while anything sits unclaimed, so most queued calls expired unseen — yet this
    endpoint's `distributed` flag had already hidden them from /api/overdue-calls
    forever. They still show ⚠️ Overdue on the dashboard, which is why they look
    stuck.

    Scope is deliberately narrow:
      • only entries with distributed_by == "backoffice-bot" — a human's ✋ manual
        distribution is never touched;
      • `resolved` entries are left alone (someone already handled the call).

    Idempotent. Returns how many flags were cleared.
    """
    if not _valid_api_key():
        return jsonify({"error": "unauthorized"}), 401
    dry_run = str(request.args.get("dry_run", "")).lower() in ("1", "true", "yes")
    cleared = []
    with _resolved_lock:
        data = _prune_resolved(_load_resolved())
        for rec_id, entry in data.items():
            if not entry.get("distributed"):
                continue
            if str(entry.get("distributed_by", "")).lower() not in BOT_ALIASES:
                continue  # manual ✋ distribution — leave it
            if entry.get("resolved") or entry.get("resolved_by"):
                continue  # already handled by a human
            cleared.append(rec_id)
            if not dry_run:
                entry.pop("distributed", None)
                entry.pop("distributed_by", None)
        if not dry_run and cleared:
            _save_resolved(data)
    return jsonify({
        "status": "ok",
        "dry_run": dry_run,
        "cleared": len(cleared),
        "ids": cleared[:50],
    })


@app.route("/api/scheduled-call/<rec_id>/update", methods=["POST"])
@login_required
def update_scheduled_call(rec_id):
    """Reschedule and/or reassign the owner of an existing scheduled call."""
    body = request.get_json(silent=True) or {}
    call_time = (body.get("call_time") or "").strip() or None
    owner_id = (body.get("owner_id") or "").strip() or None
    if not call_time and not owner_id:
        return jsonify({"error": "call_time or owner_id required"}), 400
    try:
        result = _zoho.update_scheduled_call(
            call_id=rec_id, call_time=call_time, owner_id=owner_id)
        threading.Thread(target=_refresh, daemon=True).start()
        return jsonify(result)
    except Exception as e:
        log.error("Update scheduled call error: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/scheduled-call/<rec_id>/delete", methods=["POST"])
@login_required
def delete_scheduled_call(rec_id):
    """Delete a scheduled call record (moves to Zoho Recycle Bin)."""
    try:
        result = _zoho.delete_scheduled_call(call_id=rec_id)
        threading.Thread(target=_refresh, daemon=True).start()
        return jsonify(result)
    except Exception as e:
        log.error("Delete scheduled call error: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/refresh", methods=["POST"])
@login_required
def api_refresh():
    threading.Thread(target=_refresh, daemon=True).start()
    return jsonify({"status": "refreshing"})


@app.route("/api/_diag")
def api_diag():
    """Diagnostic — reveals current cache state, no auth (read-only)."""
    with _lock:
        data = _cache.get("data")
        sc = (data or {}).get("scheduled_calls") or {}
        records = sc.get("records") or []
        return jsonify({
            "cache_data_is_none": data is None,
            "cache_error": _cache.get("error"),
            "cache_last_updated": _cache.get("last_updated"),
            "cache_age_seconds": _cache_age_seconds(),
            "refresh_running": _refresh_running,
            "record_count": len(records),
            "bg_started": _bg_started,
            "id_cache": id(_cache),
            "id_lock": id(_lock),
            "pid": os.getpid(),
        })


@app.route("/api/contact/<contact_id>/summary")
@login_required
def contact_summary(contact_id):
    import anthropic
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return jsonify({"error": "ANTHROPIC_API_KEY not configured"}), 503

    try:
        data = _zoho.get_contact_summary_data(contact_id)

        # Build concise prompt
        calls_text = "\n".join(
            f"- [{c.get('time','?')}] {c.get('type','?')} | {c.get('subject','')} | "
            f"Disposition: {c.get('disposition') or 'none'} | Rep: {c.get('owner','')} | "
            f"Notes: {c.get('description','')[:150]}"
            for c in data["calls"][:20]
        ) or "No calls on record."

        deals_text = "\n".join(
            f"- {d.get('name')} | Stage: {d.get('stage')} | Modified: {d.get('modified','?')} | "
            f"Notes: {d.get('description','')[:100]}"
            for d in data["deals"]
        ) or "No deals on record."

        tasks_text = "\n".join(
            f"- [{t.get('status')}] {t.get('subject')} | Due: {t.get('due','?')} | {t.get('description','')[:100]}"
            for t in data["tasks"]
        ) or "No tasks on record."

        contact = data["contact"]
        prompt = f"""You are a concise sales assistant for a medical aesthetics/plastic surgery practice.
Analyze this CRM contact and write a SHORT summary (4–6 sentences) for a sales rep who is about to call them.

Cover: current deal stage, what was discussed in past calls, any objections or interest signals, pending tasks, and a suggested approach.

Contact: {contact.get('name')} | Phone: {contact.get('phone')} | Source: {contact.get('lead_source')}
Title/Notes: {contact.get('description') or 'none'}

DEALS:
{deals_text}

CALL HISTORY (most recent first):
{calls_text}

OPEN TASKS:
{tasks_text}

Write your response as plain prose — no bullet points, no headers. Be direct and actionable."""

        claude = anthropic.Anthropic(api_key=api_key)
        message = claude.messages.create(
            model="claude-haiku-4-5",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        summary_text = message.content[0].text

        return jsonify({
            "summary": summary_text,
            "contact": contact,
            "stats": data["stats"],
        })

    except Exception as e:
        log.error("Contact summary error: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/contact/<contact_id>/insights")
@login_required
def contact_insights(contact_id):
    """Direct, digestible AI analysis with live record stats for inline expansion."""
    import anthropic
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return jsonify({"insights": "AI not configured (set ANTHROPIC_API_KEY)."}), 200

    try:
        data = _zoho.get_contact_summary_data(contact_id)
        calls = data["calls"]
        deals = data["deals"]
        sms = data.get("sms", [])
        stats = data.get("stats", {})

        # ── Build structured data for AI and frontend ──
        now = datetime.now(timezone.utc)

        # Only show actual dial attempts: MVP calls ("outgoing call to") or
        # calls with an outgoing disposition logged (RingCX completed dials).
        def is_dial_attempt(c):
            subj = (c.get("subject") or "").lower()
            return (subj.startswith("outgoing call to")
                    or bool(c.get("disposition")))

        dialed_calls = [c for c in calls if is_dial_attempt(c)]
        recent_calls = dialed_calls[:4]

        # Detect campaign from call descriptions (e.g. "OUT - Scheduled Calls - English")
        def extract_campaign(c):
            desc = c.get("description") or c.get("subject") or ""
            if desc.upper().startswith("OUT"):
                # "OUT - Scheduled Calls - English" → "Scheduled Calls - English"
                parts = desc.split(" - ", 1)
                if len(parts) > 1:
                    return parts[1].strip()
            return None

        campaigns = {}
        for c in dialed_calls:
            camp = extract_campaign(c)
            if camp:
                campaigns[camp] = campaigns.get(camp, 0) + 1

        recent_calls_text = "\n".join(
            f"  {i+1}. {(c.get('time') or '?')[:16]} | {c.get('type','?')} | "
            f"Disposition: {c.get('disposition') or 'none'} | "
            f"Duration: {c.get('duration_sec') or 0}s | "
            f"Rep: {c.get('owner') or '?'}"
            + (f" | Campaign: {extract_campaign(c)}" if extract_campaign(c) else "")
            + (f" | Notes: {c.get('description','')[:120]}" if c.get('description') else "")
            for i, c in enumerate(recent_calls)
        ) or "  (no calls logged)"

        # Activity in last 24h and 72h
        def hours_ago(iso_str):
            try:
                t = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
                return (now - t).total_seconds() / 3600
            except Exception:
                return 9999

        calls_24h = [c for c in dialed_calls if hours_ago(c.get("time","")) <= 24]
        calls_72h = [c for c in dialed_calls if hours_ago(c.get("time","")) <= 72]
        sms_24h   = [s for s in sms if hours_ago(s.get("time","")) <= 24]
        sms_72h   = [s for s in sms if hours_ago(s.get("time","")) <= 72]

        recency_text = (
            f"Last 24h: {len(calls_24h)} calls, {len(sms_24h)} SMS. "
            f"Last 72h: {len(calls_72h)} calls, {len(sms_72h)} SMS."
        )

        # Deal info
        deal_text = "\n".join(
            f"  - {d.get('name','?')} | Stage: {d.get('stage','?')} | "
            f"Modified: {d.get('modified','?')[:10]}"
            + (f" | Notes: {d.get('description','')[:100]}" if d.get('description') else "")
            for d in deals
        ) or "  (no deals)"

        # Recent SMS (last 5)
        recent_sms_text = "\n".join(
            f"  - {(s.get('time') or '?')[:16]} | {s.get('direction','?')}: "
            f"{(s.get('message') or '')[:120]}"
            for s in sms[:5]
        ) or "  (no SMS)"

        contact = data["contact"]
        prompt = f"""You are a sales analyst for a medical aesthetics practice. A rep is about to call this contact. Give a DIRECT, actionable briefing.

RULES:
- Plain text only. NO markdown, NO asterisks, NO bold, NO bullet points, NO headers.
- 3-4 short, punchy sentences. Be specific with dates and numbers.
- State what happened, what the contact wants, and what the rep should do.
- If there's little data, say "Limited history" and state what IS known.

CONTACT: {contact.get('name')} | Phone: {contact.get('phone')} | Source: {contact.get('lead_source') or 'unknown'}

STATS: {len(dialed_calls)} actual dial attempts out of {stats.get('total_calls',0)} call records, {stats.get('total_sms',0)} SMS ({stats.get('sms_outbound',0)} sent, {stats.get('sms_inbound',0)} received)
{recency_text}

DEAL:
{deal_text}

LAST {len(recent_calls)} DIAL ATTEMPTS (outbound calls only):
{recent_calls_text}

RECENT SMS:
{recent_sms_text}

Write your briefing now. Be direct — "Called 3x on May 13, no answer" not "Multiple outreach attempts were made"."""

        claude = anthropic.Anthropic(api_key=api_key)
        msg = claude.messages.create(
            model="claude-haiku-4-5",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )

        # Strip any stray markdown
        text = msg.content[0].text.strip()
        text = text.replace("**", "").replace("__", "")
        lines = [ln for ln in text.split("\n") if not ln.strip().endswith(":") or len(ln.strip()) > 35]
        text = " ".join(ln.strip() for ln in lines if ln.strip())

        # Build recent attempts list for frontend display
        recent_attempts = [
            {
                "time": c.get("time"),
                "disposition": c.get("disposition"),
                "duration": c.get("duration_sec", 0),
                "rep": c.get("owner"),
            }
            for c in recent_calls
        ]

        return jsonify({
            "insights": text,
            "stats": stats,
            "call_count": len(calls),
            "dial_count": len(dialed_calls),
            "sms_count": len(sms),
            "recent_attempts": recent_attempts,
            "activity_24h": {"calls": len(calls_24h), "sms": len(sms_24h)},
            "activity_72h": {"calls": len(calls_72h), "sms": len(sms_72h)},
            "last_call_time": dialed_calls[0]["time"] if dialed_calls else None,
            "last_call_disposition": dialed_calls[0].get("disposition") if dialed_calls else None,
            "last_sms_time": sms[0]["time"] if sms else None,
            "last_sms_direction": sms[0].get("direction") if sms else None,
            "campaigns": campaigns,  # e.g. {"Scheduled Calls - English": 3}
        })
    except Exception as e:
        log.error("Contact insights error: %s", e)
        return jsonify({"insights": f"Error: {e}"}), 500


# ------------------------------------------------------------------ Schedule call

@app.route("/api/zoho/owners")
@login_required
def zoho_owners():
    """List CRM users for call owner dropdown."""
    try:
        owners = _zoho.get_crm_owners()
        return jsonify({"owners": owners})
    except Exception as e:
        log.error("Owners fetch error: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/zoho/contacts/search")
@login_required
def zoho_contact_search():
    """Search contacts by name or phone."""
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify({"contacts": []})
    try:
        contacts = _zoho.search_contacts(q)
        return jsonify({"contacts": contacts})
    except Exception as e:
        log.error("Contact search error: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/zoho/contacts/<contact_id>/deals")
@login_required
def zoho_contact_deals(contact_id):
    """Get deals linked to a contact."""
    try:
        deals = _zoho.get_deals_for_contact(contact_id)
        return jsonify({"deals": deals})
    except Exception as e:
        log.error("Contact deals error: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/zoho/schedule-call", methods=["POST"])
@login_required
def zoho_schedule_call():
    """Create a scheduled call record in Zoho CRM."""
    body = request.get_json(silent=True) or {}
    contact_id = body.get("contact_id", "").strip()
    contact_name = body.get("contact_name", "").strip()
    call_time = body.get("call_time", "").strip()
    deal_id = body.get("deal_id", "").strip()

    if not call_time:
        return jsonify({"error": "call_time is required"}), 400

    # Call Now rows can lack a linked contact (Who_Id null) — resolve the
    # deal's Contact_Name so we can still create the scheduled call.
    if not contact_id and deal_id:
        try:
            dc = _zoho.get_deal_contact(deal_id)
            contact_id = (dc.get("contact_id") or "").strip()
            contact_name = contact_name or dc.get("contact_name") or ""
        except Exception as e:
            log.warning("schedule-call deal-contact resolve failed: %s", e)

    if not contact_id:
        return jsonify({"error": "contact_id (or a deal with a linked contact) is required"}), 400

    try:
        result = _zoho.create_scheduled_call(
            contact_id=contact_id,
            contact_name=contact_name or "Unknown",
            call_time=call_time,
            deal_id=deal_id or None,
            owner_id=body.get("owner_id", "").strip() or None,
        )
        # Trigger a cache refresh so the new call shows up
        threading.Thread(target=_refresh, daemon=True).start()
        return jsonify(result)
    except Exception as e:
        log.error("Schedule call error: %s", e)
        return jsonify({"error": str(e)}), 500


# ------------------------------------------------------------------ Pipeline stage counts

def _books_count_by_salesperson(items: list) -> dict:
    """Group Books items by salesperson_name → {total, by_agent: [{name, count}]}."""
    counts: dict = {}
    for it in items:
        owner = (it.get("salesperson_name") or "").strip() or "Unassigned"
        counts[owner] = counts.get(owner, 0) + 1
    by_agent = [{"name": n, "count": c}
                for n, c in sorted(counts.items(), key=lambda x: -x[1])]
    return {"total": len(items), "by_agent": by_agent}


@app.route("/api/pipeline")
@login_required
def api_pipeline():
    """Pipeline stage counts.

    For Quote Sent and Retainer Invoice Sent, the source of truth is Zoho Books
    (a document was actually issued in the date range). For everything else we
    fall back to CRM stage counts. The old CRM-only path over-counted because
    Modified_Time matches any field touch, not just stage transitions.
    """
    start_param = request.args.get("start")
    end_param   = request.args.get("end")
    tz_param    = request.args.get("tz")
    tz_offset_minutes = int(tz_param) if tz_param is not None else None
    try:
        start_dt = end_dt = None
        if start_param:
            effective_end = end_param or start_param
            start_dt = _parse_local_date_to_utc(start_param, 0, 0, 0, tz_offset_minutes)
            end_dt   = _parse_local_date_to_utc(effective_end, 23, 59, 59, tz_offset_minutes)
        counts = _zoho.get_pipeline_counts(start_dt=start_dt, end_dt=end_dt)

        # Override the document-issuance stages with Books data — actual sends, not edits.
        if _books.configured:
            # Books filters on its own document date by LOCAL calendar day, so
            # feed it the user's local date strings (the same ones the calls
            # table uses) — not a UTC date derived from the converted datetime,
            # which drifts to the wrong day near midnight Central.
            tz_hours = (-tz_offset_minutes / 60.0) if tz_offset_minutes is not None \
                else LOCAL_TZ.utcoffset(datetime.now(timezone.utc)).total_seconds() / 3600.0
            local_today = (datetime.now(timezone.utc) + timedelta(hours=tz_hours)).date().isoformat()
            date_start = start_param or local_today
            date_end   = end_param or start_param or local_today
            try:
                estimates = _books.list_sent_estimates(date_start, date_end, max_records=500)
                counts["Quote Sent"] = _books_count_by_salesperson(estimates)
            except Exception as ex:
                log.warning("Books estimates fetch for pipeline failed: %s", ex)
            try:
                retainers = _books.list_sent_retainer_invoices(date_start, date_end, max_records=500)
                counts["Retainer Invoice Sent"] = _books_count_by_salesperson(retainers)
            except Exception as ex:
                log.warning("Books retainers fetch for pipeline failed: %s", ex)
            # "Retainers Paid" = real paid retainer invoices (a payment fact),
            # replacing the old proxy that counted the CRM Closed-Won stage.
            try:
                paid = _books.list_paid_retainer_invoices(date_start, date_end, max_records=500)
                counts["Retainers Paid"] = _books_count_by_salesperson(paid)
            except Exception as ex:
                log.warning("Books paid-retainers fetch for pipeline failed: %s", ex)
        return jsonify(counts)
    except Exception as e:
        log.exception("Pipeline counts error")
        return jsonify({"error": str(e)}), 500



# ------------------------------------------------------------------ RingCX live monitoring

@app.route("/api/ringcx/status")
@login_required
def ringcx_status():
    """Check if RingCX integration is configured."""
    return jsonify({"configured": _ringcx.configured})


@app.route("/api/ringcx/cdr-debug")
@login_required
def ringcx_cdr_debug():
    """Diagnostic: probe the RingCX reportsStreaming CDR endpoint and return the
    raw status + response body per token, to pinpoint the 403 permission."""
    if not _ringcx.configured:
        return jsonify({"error": "RingCX not configured"}), 503
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=6)
    try:
        return jsonify(_ringcx.cdr_diagnostics(start, now))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/ringcx/live")
@login_required
def ringcx_live():
    """Full live snapshot: active calls + agent statuses."""
    if not _ringcx.configured:
        return jsonify({
            "error": "RingCX not configured",
            "help": "Set RC_CLIENT_ID, RC_CLIENT_SECRET, RC_JWT_TOKEN in .env",
        }), 503
    try:
        snapshot = _ringcx.get_live_snapshot()
        return jsonify(snapshot)
    except Exception as e:
        log.error("RingCX live snapshot error: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/ringcx/active-calls")
@login_required
def ringcx_active_calls():
    """Active calls with scheduled-call cross-reference."""
    if not _ringcx.configured:
        return jsonify({"error": "RingCX not configured"}), 503
    try:
        calls = _ringcx.get_active_calls()

        # Cross-reference with today's scheduled calls if available
        with _lock:
            sc_data = (_cache.get("data") or {}).get("scheduled_calls", {})
            sc_records = sc_data.get("records") or []

        # Build phone → scheduled record lookup
        phone_map = {}
        for rec in sc_records:
            phone = (rec.get("phone") or "").replace("+", "").replace("-", "").replace(" ", "").replace("(", "").replace(")", "")
            if len(phone) >= 10:
                phone_map[phone[-10:]] = {
                    "contact_name": rec.get("contact_name") or rec.get("name"),
                    "scheduled_time": rec.get("scheduled_time"),
                    "status": rec.get("status"),
                    "deal_stage": rec.get("deal_stage"),
                    "id": rec.get("id"),
                }

        # Annotate active calls with scheduled-call matches
        for call in calls:
            ani = (call.get("ani") or "").replace("+", "").replace("-", "").replace(" ", "")
            dnis = (call.get("dnis") or "").replace("+", "").replace("-", "").replace(" ", "")
            match = None
            if len(ani) >= 10:
                match = phone_map.get(ani[-10:])
            if not match and len(dnis) >= 10:
                match = phone_map.get(dnis[-10:])
            call["scheduled_match"] = match

        return jsonify({"calls": calls, "count": len(calls)})
    except Exception as e:
        log.error("RingCX active calls error: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/ringcx/agents")
@login_required
def ringcx_agents():
    """Agent presence statuses."""
    if not _ringcx.configured:
        return jsonify({"error": "RingCX not configured"}), 503
    try:
        agents = _ringcx.get_agent_statuses()
        return jsonify({"agents": agents, "count": len(agents)})
    except Exception as e:
        log.error("RingCX agents error: %s", e)
        return jsonify({"error": str(e)}), 500



_extensions_cache = {"data": None, "expires": 0}

@app.route("/api/ringcx/extensions")
@login_required
def ringcx_extensions():
    """List RingCentral extensions for monitoring phone picker. Cached 10 min."""
    if not _ringcx.configured:
        return jsonify({"error": "RingCX not configured"}), 503

    now = time.time()
    if _extensions_cache["data"] and now < _extensions_cache["expires"]:
        return jsonify(_extensions_cache["data"])

    try:
        import requests as req
        headers = _ringcx._rc_headers()
        ext_names = _ringcx._fetch_extension_names()

        # Fetch ALL account phone numbers in one call and map by extension
        ext_phones = {}
        page = 1
        while page <= 10:
            resp = req.get(
                f"{_ringcx.server_url}/restapi/v1.0/account/{_ringcx.account_id}/phone-number",
                headers=headers,
                params={"perPage": 250, "page": page, "usageType": "DirectNumber"},
                timeout=15,
            )
            if not resp.ok:
                break
            data = resp.json()
            for pn in data.get("records", []):
                ext_obj = pn.get("extension") or {}
                eid = str(ext_obj.get("id", ""))
                phone = pn.get("phoneNumber", "")
                if eid and phone and eid in ext_names:
                    if eid not in ext_phones:
                        ext_phones[eid] = phone
            nav = data.get("paging") or data.get("navigation") or {}
            total = nav.get("totalPages", 1)
            if page < total:
                page += 1
            else:
                break

        exts = []
        for eid, info in ext_names.items():
            if not info.get("name"):
                continue
            phone = ext_phones.get(eid, "")
            exts.append({
                "name": info["name"],
                "ext": info["extensionNumber"],
                "phones": [phone] if phone else [],
            })
        exts.sort(key=lambda x: x["name"])
        result = {"extensions": exts, "count": len(exts)}
        _extensions_cache["data"] = result
        _extensions_cache["expires"] = now + 600
        return jsonify(result)
    except Exception as e:
        log.error("Extensions list error: %s", e)
        return jsonify({"error": str(e)}), 500


_sms_numbers_cache = {"data": None, "expires": 0}

@app.route("/api/sms/numbers")
@login_required
def sms_numbers():
    """List RingEX phone numbers that can send SMS (have the SmsSender feature).

    Returns {"numbers": [{phoneNumber, label}], "default": <env default>}.
    Cached 10 min.
    """
    if not _ringcx.configured:
        return jsonify({"error": "RingCX not configured"}), 503

    now = time.time()
    if _sms_numbers_cache["data"] and now < _sms_numbers_cache["expires"]:
        return jsonify(_sms_numbers_cache["data"])

    try:
        import requests as req
        headers = _ringcx._rc_headers()
        numbers = []
        seen = set()
        page = 1
        while page <= 10:
            resp = req.get(
                f"{_ringcx.server_url}/restapi/v1.0/account/{_ringcx.account_id}/phone-number",
                headers=headers,
                params={"perPage": 250, "page": page},
                timeout=15,
            )
            if not resp.ok:
                break
            data = resp.json()
            for pn in data.get("records", []):
                phone = pn.get("phoneNumber", "")
                features = pn.get("features") or []
                if not phone or phone in seen or "SmsSender" not in features:
                    continue
                seen.add(phone)
                ext_obj = pn.get("extension") or {}
                label = pn.get("label") or ext_obj.get("name") or pn.get("usageType") or ""
                numbers.append({"phoneNumber": phone, "label": label})
            nav = data.get("paging") or data.get("navigation") or {}
            if page < nav.get("totalPages", 1):
                page += 1
            else:
                break

        numbers.sort(key=lambda x: x["label"] or x["phoneNumber"])
        result = {
            "numbers": numbers,
            "default": os.getenv("RC_SMS_FROM_NUMBER", ""),
        }
        _sms_numbers_cache["data"] = result
        _sms_numbers_cache["expires"] = now + 600
        return jsonify(result)
    except Exception as e:
        log.error("SMS numbers list error: %s", e)
        return jsonify({"error": str(e)}), 500


# ------------------------------------------------------------------ Call Monitoring

@app.route("/api/ringcx/monitor", methods=["POST"])
@login_required
def ringcx_monitor():
    """Initiate a supervisor monitoring session on an active RingCX call.

    Body JSON:
      uii          — Unique Interaction ID of the active call
      destination   — Phone number to ring the supervisor on
      session_type  — "MONITOR", "COACHING", or "BARGE_IN" (default: MONITOR)
    """
    if not _ringcx.configured:
        return jsonify({"error": "RingCX not configured"}), 503
    body = request.get_json(silent=True) or {}
    uii = (body.get("uii") or "").strip()
    destination = (body.get("destination") or "").strip()
    session_type = (body.get("session_type") or "MONITOR").upper().strip()

    if not uii:
        return jsonify({"error": "uii is required"}), 400
    if not destination:
        return jsonify({"error": "destination phone number is required"}), 400

    result = _ringcx.monitor_call(uii, destination, session_type)
    if result.get("error"):
        return jsonify(result), 400
    return jsonify(result)


# ------------------------------------------------------------------ Call & SMS History Search

@app.route("/api/call-history/search")
@login_required
def call_history_search():
    """Search call history across RingEX and RingCX by phone number or contact name.

    Query params:
      q    — phone number or contact name
      days — how many days back (default 30, max 180)
    """
    import re
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify({"error": "Query too short (min 2 chars)"}), 400

    days = min(int(request.args.get("days", 30)), 180)

    # Determine if query is a phone number or a name
    digits = re.sub(r"\D", "", q)
    phones = []
    contact = None

    if len(digits) >= 7:
        # Looks like a phone number — use directly
        phones = [digits[-10:] if len(digits) >= 10 else digits]
    else:
        # Looks like a name — search Zoho contacts to resolve phone(s)
        try:
            contacts = _zoho.search_contacts(q)
            if contacts:
                contact = contacts[0]  # primary match
                for c in contacts[:3]:  # check top 3 matches
                    p = re.sub(r"\D", "", c.get("phone") or "")
                    if len(p) >= 7:
                        phones.append(p[-10:] if len(p) >= 10 else p)
        except Exception as e:
            log.error("Contact search for call history failed: %s", e)

    if not phones:
        return jsonify({
            "calls": [],
            "contact": contact,
            "query": q,
            "message": "No phone number found for this query",
        })

    if not _ringcx.configured:
        return jsonify({"error": "RingCX/RingEX not configured"}), 503

    from concurrent.futures import ThreadPoolExecutor
    primary_phone = phones[0]
    debug = {}

    # Run all searches in parallel
    def _search_ringex():
        try:
            r = _ringcx.search_call_history(primary_phone, days=days)
            return r, f"ok: {len(r)} calls"
        except Exception as e:
            return [], f"error: {e}"

    def _search_ringcx():
        try:
            r = _ringcx.search_ringcx_call_history(primary_phone, days=days)
            return r, f"ok: {len(r)} calls"
        except Exception as e:
            return [], f"error: {e}"

    def _search_zoho_calls():
        try:
            from zoho_client import normalize_phone
            phone_map = _zoho._fetch_all_calls_for_phones([primary_phone])
            norm = normalize_phone(primary_phone)
            raw = phone_map.get(norm, []) if norm else []
            result = []
            for c in raw:
                owner = c.get("Owner")
                caller = (owner.get("name") if isinstance(owner, dict)
                          else owner if isinstance(owner, str) else None)
                result.append({
                    "id": c.get("id"),
                    "start_time": c.get("Call_Start_Time"),
                    "subject": c.get("Subject"),
                    "disposition": c.get("Outgoing_call_disposition"),
                    "caller": caller,
                    "description": (c.get("Description") or "")[:200],
                    "recording_url": _zoho._extract_recording_url(c.get("Description") or ""),
                    "source": "zoho",
                })
            return result, None
        except Exception as e:
            return [], str(e)

    def _search_sms():
        try:
            r = _zoho.search_sms_history(primary_phone)
            return r, f"ok: {len(r)} messages"
        except Exception as e:
            return [], f"error: {e}"

    with ThreadPoolExecutor(max_workers=4) as pool:
        f_rex = pool.submit(_search_ringex)
        f_rcx = pool.submit(_search_ringcx)
        f_zoho = pool.submit(_search_zoho_calls)
        f_sms = pool.submit(_search_sms)

        ringex_calls, debug["ringex"] = f_rex.result(timeout=30)
        ringcx_calls, debug["ringcx"] = f_rcx.result(timeout=30)
        zoho_calls, zoho_err = f_zoho.result(timeout=30)
        if zoho_err:
            log.error("Zoho call fetch for history failed: %s", zoho_err)
        sms_messages, debug["sms"] = f_sms.result(timeout=30)

    # Merge and deduplicate by session_id / start_time
    seen = set()
    merged = []
    for call in ringcx_calls:
        key = call.get("session_id") or call.get("start_time", "")
        if key and key not in seen:
            seen.add(key)
            merged.append(call)
    for call in ringex_calls:
        key = call.get("session_id") or call.get("start_time", "")
        if key and key not in seen:
            seen.add(key)
            merged.append(call)
    merged.sort(key=lambda c: c.get("start_time") or "", reverse=True)

    return jsonify({
        "calls": merged,
        "zoho_calls": zoho_calls,
        "sms": sms_messages,
        "contact": contact,
        "phone": primary_phone,
        "query": q,
        "days": days,
        "count": len(merged),
        "zoho_count": len(zoho_calls),
        "sms_count": len(sms_messages),
        "_debug": debug,
    })


# ------------------------------------------------------------------ Quotes panel

@app.route("/api/quotes")
@login_required
def api_quotes():
    """List Zoho Books "sent" estimates in a date range, each enriched with
    CRM activity (calls / notes / tasks / stage changes) logged against the
    linked Deal after the estimate's last_modified_time.
    """
    try:
        return _api_quotes_inner()
    except Exception as e:
        # Last-chance JSON error envelope so the panel never receives an HTML
        # 500 page (which would parse as "Unexpected token '<'" in the client).
        log.exception("/api/quotes top-level exception")
        return jsonify({
            "status": "error",
            "message": f"{type(e).__name__}: {e}",
            "quotes": [], "count": 0,
        }), 500


def _api_quotes_inner():
    if not _books.configured:
        return jsonify({
            "status": "not_configured",
            "message": "Set ZOHO_BOOKS_ORG_ID and a Books-scoped refresh token "
                       "(ZOHO_BOOKS_REFRESH_TOKEN, or add Books scope to the existing token).",
            "quotes": [],
        }), 200

    today = datetime.now(timezone.utc).date()
    default_start = (today - timedelta(days=30)).isoformat()
    default_end = today.isoformat()
    date_start = request.args.get("start") or default_start
    date_end = request.args.get("end") or default_end
    include_activity = request.args.get("activity", "1") != "0"
    # Cap N to keep responses under Render's gunicorn 90s worker timeout.
    # 3 parallel workers × 3 related-list calls per item; at ~500ms per CRM
    # call when Zoho is slow, 100 items finishes in ~50s — leaves headroom.
    # If 100 still isn't enough rows for the team, the right next step is
    # lazy CRM enrichment (return rows fast, fetch activity on row expand).
    max_records = min(int(request.args.get("limit", "100")), 300)

    # Serve from cache when the same date range was fetched recently.
    import time as _time
    _cache_key = (date_start, date_end)
    _cached = _quotes_cache.get(_cache_key)
    if _cached and (_time.monotonic() - _cached["ts"]) < _QUOTES_CACHE_TTL:
        return jsonify(_cached["payload"])

    # Only one thread does the expensive Zoho fetch at a time; the lock is
    # always released in the finally block, even if an exception or early
    # return fires inside (cache hit, Books error, etc.).
    if not _quotes_fetch_lock.acquire(timeout=90):
        return jsonify({"status": "error", "message": "Server busy, try again.", "quotes": []}), 503
    try:
        # Re-check cache after acquiring — another thread may have filled it.
        _cached = _quotes_cache.get(_cache_key)
        if _cached and (_time.monotonic() - _cached["ts"]) < _QUOTES_CACHE_TTL:
            return jsonify(_cached["payload"])

        try:
            estimates = _books.list_sent_estimates(date_start, date_end, max_records=max_records)
            retainers = _books.list_sent_retainer_invoices(date_start, date_end, max_records=max_records)
        except Exception as e:
            log.error("Books fetch failed: %s", e)
            return jsonify({"status": "error", "message": str(e), "quotes": []}), 500

        def _sent_ts(doc):
            return doc.get("last_modified_time") or doc.get("created_time") or ""
        best_by_deal: dict = {}
        orphans: list = []
        for r in retainers:
            did = r.get("zcrm_potential_id")
            if not did:
                orphans.append(("retainer", r)); continue
            prev = best_by_deal.get(did)
            if not prev or _sent_ts(r) > _sent_ts(prev[1]):
                best_by_deal[did] = ("retainer", r)
        for e in estimates:
            did = e.get("zcrm_potential_id")
            if not did:
                orphans.append(("quote", e)); continue
            prev = best_by_deal.get(did)
            if prev and prev[0] == "retainer":
                continue
            if not prev or _sent_ts(e) > _sent_ts(prev[1]):
                best_by_deal[did] = ("quote", e)
        merged = list(best_by_deal.values()) + orphans
        # Earliest first — surfaces the most-at-risk quotes/retainers at the top.
        merged.sort(key=lambda x: (x[1].get("date") or "",
                                    x[1].get("last_modified_time") or ""))
        merged = merged[:max_records]

        def _fetch_signals_for(item):
            kind, doc = item
            deal_id = doc.get("zcrm_potential_id") or ""
            sent_at = doc.get("created_time") or doc.get("date") or ""
            if not (include_activity and deal_id and sent_at):
                return item, [], {"status": "forgotten", "when": None, "by": None,
                                  "summary": None, "kind": None, "source": None}
            try:
                sig = _zoho.get_deal_post_quote_signals(deal_id, sent_at)
                return item, sig["activities"], sig["next_followup"]
            except Exception as ex:
                log.warning("Activity fetch failed for deal %s: %s", deal_id, ex)
                return item, [], {"status": "forgotten", "when": None, "by": None,
                                  "summary": None, "kind": None, "source": None}

        results_by_id = {}
        if merged:
            with ThreadPoolExecutor(max_workers=3) as pool:
                for item, activities, next_followup in pool.map(_fetch_signals_for, merged):
                    _, doc = item
                    doc_id = doc.get("estimate_id") or doc.get("invoice_id")
                    results_by_id[doc_id] = (activities, next_followup)

        import re as _re
        # Rows where the most recent note signals the deal is already handled
        # (rep booked the surgery, internal-ops note from booking team, etc.)
        # bypass the Books `paid` filter because the invoice status hasn't
        # caught up yet. Drop them so the tracker stays focused on real chases.
        _EXCLUDED_NOTE_AUTHORS = {"alanis castillo", "oscar caballero"}
        _BOOKING_PHRASES = (
            "booked the appointment", "booked the appointments",
            "booked appointment", "booked appointments",
            "appointments were booked", "appointment was booked",
            "appointments are booked", "appointment is booked",
            "appointments have been booked", "appointment has been booked",
            "i booked the appointment", "i booked the appointments",
            "booking the appointment", "appointments booked",
        )
        def _note_excludes(note_text: str, note_by: str) -> bool:
            """True if this single note signals the deal is already handled."""
            if (note_by or "").strip().lower() in _EXCLUDED_NOTE_AUTHORS:
                return True
            text = (note_text or "").lower()
            return any(p in text for p in _BOOKING_PHRASES)

        def _any_note_excludes(activities: list) -> bool:
            """Scan ALL post-send notes, not just the latest. Otherwise a
            booking-team note gets buried by a later "no answer" call note
            and the row sneaks through. We strip HTML before matching."""
            for a in activities:
                if a.get("kind") != "Notes":
                    continue
                raw = (a.get("detail") or "") + " " + (a.get("summary") or "")
                clean = _re.sub(r"<[^>]+>", " ", raw)
                if _note_excludes(clean, a.get("by")):
                    return True
            return False

        quotes = []
        excluded_count = 0
        for kind, doc in merged:
            deal_id = doc.get("zcrm_potential_id") or ""
            sent_at = doc.get("created_time") or doc.get("date") or ""
            doc_id = doc.get("estimate_id") or doc.get("invoice_id")
            activities, next_followup = results_by_id.get(doc_id,
                ([], {"status": "forgotten", "when": None, "by": None,
                      "summary": None, "kind": None, "source": None}))
            if _any_note_excludes(activities):
                excluded_count += 1
                continue
            latest_note = next((a for a in activities if a.get("kind") == "Notes"), None)
            latest_note_summary = ""
            latest_note_by = latest_note.get("by") if latest_note else None
            if latest_note:
                latest_note_summary = (latest_note.get("detail") or
                                        latest_note.get("summary") or "").strip()
                latest_note_summary = _re.sub(r"<[^>]+>", "", latest_note_summary)
                latest_note_summary = " ".join(latest_note_summary.split())
            quotes.append({
                "kind": kind,
                "estimate_id": doc_id,
                "estimate_number": doc.get("estimate_number") or doc.get("invoice_number"),
                "deal_id": deal_id,
                "deal_name": doc.get("zcrm_potential_name"),
                "customer_name": doc.get("customer_name"),
                "salesperson": doc.get("salesperson_name"),
                "date": doc.get("date"),
                "sent_at": sent_at,
                "total": doc.get("total"),
                "balance": doc.get("balance"),
                "currency": doc.get("currency_code"),
                "is_viewed_by_client": doc.get("is_viewed_by_client"),
                "mail_first_viewed_time": doc.get("mail_first_viewed_time") or "",
                "expiry_date": doc.get("expiry_date") or doc.get("due_date"),
                "status": doc.get("status"),
                "activity_count": len(activities),
                # Cap activities to the 5 most recent — that's what the row
                # expansion actually shows. Holding the full history per row
                # in the 10-min cache was the biggest contributor to OOM.
                "activities": activities[:5],
                "next_followup": next_followup,
                "latest_note": latest_note_summary,
                "latest_note_ts": latest_note.get("ts") if latest_note else None,
                "latest_note_by": latest_note_by,
            })

        if excluded_count:
            log.info("/api/quotes: excluded %d rows by note signal", excluded_count)

        payload = {
            "status": "ok",
            "date_range": {"start": date_start, "end": date_end},
            "count": len(quotes),
            "quotes": quotes,
            "source": "cache" if _books.last_source_was_cache else "live",
        }
        # Evict stale entries AND keep the cache to a single date range at a
        # time — managers usually look at one window per session, so caching
        # multiple windows is mostly memory waste.
        for k in list(_quotes_cache.keys()):
            v = _quotes_cache.get(k)
            if not v or (_time.monotonic() - v["ts"]) > _QUOTES_CACHE_TTL or k != _cache_key:
                _quotes_cache.pop(k, None)
        _quotes_cache[_cache_key] = {"ts": _time.monotonic(), "payload": payload}
        return jsonify(payload)
    finally:
        _quotes_fetch_lock.release()


# ------------------------------------------------------------------ Deal notes (lightweight, no AI)

@app.route("/api/deal-latest-note/<deal_id>")
@login_required
def api_deal_latest_note(deal_id):
    """Return the most recent CRM note for a deal (fast, no AI, no Books call)."""
    try:
        resp = requests.get(
            f"{_zoho.base_url}/crm/v6/Deals/{deal_id}/Notes",
            headers=_zoho._headers(),
            params={"fields": "id,Note_Title,Note_Content,Created_Time,Owner",
                    "per_page": 1, "sort_by": "Created_Time", "sort_order": "desc"},
            timeout=12,
        )
        if resp.status_code == 204 or not resp.ok:
            return jsonify({"note": "", "by": "", "ts": ""})
        rows = (resp.json() or {}).get("data", []) or []
        if not rows:
            return jsonify({"note": "", "by": "", "ts": ""})
        row = rows[0]
        import re as _re
        content = ((row.get("Note_Title") or "") + " " + (row.get("Note_Content") or "")).strip()
        content = _re.sub(r"<[^>]+>", "", content)
        content = " ".join(content.split())
        owner = row.get("Owner") or {}
        by = owner.get("name") or "" if isinstance(owner, dict) else str(owner)
        return jsonify({"note": content[:500], "by": by, "ts": row.get("Created_Time") or ""})
    except Exception as e:
        log.error("deal-latest-note %s: %s", deal_id, e)
        return jsonify({"error": str(e)}), 500


# ------------------------------------------------------------------ AI notes summary

@app.route("/api/deal-notes-summary/<deal_id>")
@login_required
def api_deal_notes_summary(deal_id):
    """Fetch CRM notes/calls for a deal and return an AI summary via Claude."""
    try:
        import anthropic as _anthropic
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            return jsonify({"error": "ANTHROPIC_API_KEY not configured"}), 503

        sig = _zoho.get_deal_post_quote_signals(deal_id, "2000-01-01T00:00:00+00:00")
        activities = sig.get("activities", [])

        if not activities:
            return jsonify({"summary": "No CRM notes or call activity found for this deal."})

        lines = []
        for a in activities[:15]:
            ts = (a.get("ts") or "")[:10]
            kind = a.get("kind", "")
            by = a.get("by") or ""
            summary = a.get("summary") or ""
            detail = a.get("detail") or ""
            lines.append(f"[{ts}] {kind} by {by}: {summary}. {detail}".strip(". "))

        activity_text = "\n".join(lines)

        client = _anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": (
                    "Summarize the following CRM activity for a plastic surgery patient lead "
                    "in 2-3 sentences. Focus on where the patient is in the sales process, "
                    "any concerns or next steps mentioned, and the most recent status.\n\n"
                    f"{activity_text}"
                )
            }]
        )
        summary = msg.content[0].text.strip()
        return jsonify({"summary": summary})
    except Exception as e:
        log.error("deal-notes-summary %s: %s", deal_id, e)
        return jsonify({"error": str(e)}), 500


# ------------------------------------------------------------------ Follow-up call endpoints

@app.route("/api/deal-info/<deal_id>")
@login_required
def api_deal_info(deal_id):
    """Return contact_id and owner_id for a CRM deal (used by follow-up modal)."""
    try:
        info = _zoho.get_deal_contact(deal_id)
        return jsonify(info)
    except Exception as e:
        log.error("deal-info %s: %s", deal_id, e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/followup-call", methods=["POST"])
@login_required
def api_create_followup_call():
    """Create a scheduled follow-up call in Zoho CRM."""
    body = request.get_json(force=True) or {}
    contact_id   = body.get("contact_id", "")
    contact_name = body.get("contact_name", "")
    deal_id      = body.get("deal_id", "")
    owner_id     = body.get("owner_id", "")
    call_time    = body.get("call_time", "")
    notes        = body.get("notes", "")
    subject      = body.get("subject") or f"Follow-up: {contact_name}"

    if not call_time:
        return jsonify({"error": "call_time is required"}), 400

    try:
        result = _zoho.create_scheduled_call(
            contact_id=contact_id,
            contact_name=contact_name,
            call_time=call_time,
            deal_id=deal_id,
            owner_id=owner_id,
        )
        # If notes or a custom subject were provided, Zoho's create_scheduled_call
        # doesn't accept them — patch the record via update.
        if result.get("id") and (notes or subject):
            patch_payload = {}
            if notes:
                patch_payload["Description"] = notes
            if subject:
                patch_payload["Subject"] = subject
            if patch_payload:
                try:
                    import requests as _req
                    _req.put(
                        f"{_zoho.base_url}/crm/v6/Calls/{result['id']}",
                        headers=_zoho._headers(),
                        json={"data": [patch_payload]},
                        timeout=10,
                    )
                except Exception as pe:
                    log.warning("followup-call patch failed: %s", pe)
        return jsonify(result)
    except Exception as e:
        log.error("create_followup_call: %s", e)
        return jsonify({"error": str(e)}), 500


_AUTO_FU_SLOT_MINUTES = 15
_AUTO_FU_CALLS_PER_SLOT = 2
_AUTO_FU_START_HOUR = 9
_AUTO_FU_START_MINUTE = 30   # first slot at 9:30 AM ET
_AUTO_FU_END_HOUR = 20       # last slot before 8:00 PM ET
_AUTO_FU_TZ = timezone(timedelta(hours=-4))  # EDT (Miami)
_auto_fu_slots: dict[str, dict[str, int]] = {}  # {"2026-06-16": {"09:30": 1, "09:45": 2, ...}} in ET

def _auto_fu_load_slots(target_date: str) -> dict[str, int]:
    """Load Anna's existing call counts per 15-min slot for target_date from Zoho.

    Zoho stores Call_Start_Time in UTC. We convert to ET before bucketing
    so the slot keys match the business-hours grid (9:30 AM – 8 PM ET).
    The COQL date range is widened by ±1 day to catch calls that straddle
    the UTC midnight boundary.
    """
    if target_date in _auto_fu_slots:
        return _auto_fu_slots[target_date]
    slot_counts: dict[str, int] = {}
    anna_id = _resolve_anna_id()
    if not anna_id:
        _auto_fu_slots[target_date] = slot_counts
        return slot_counts
    try:
        # Widen to catch ET times that cross UTC midnight
        d = datetime.strptime(target_date, "%Y-%m-%d")
        q_start = (d - timedelta(days=1)).strftime("%Y-%m-%d")
        q_end = (d + timedelta(days=1)).strftime("%Y-%m-%d")
        resp = requests.post(
            f"{_zoho.base_url}/crm/v6/coql",
            headers={**_zoho._headers(), "Content-Type": "application/json"},
            json={"select_query": (
                f"select id, Call_Start_Time from Calls "
                f"where Call_Start_Time between '{q_start}T00:00:00+00:00' and '{q_end}T23:59:59+00:00' "
                f"and Subject like 'AUTO FU:%' "
                f"limit 200"
            )},
            timeout=20,
        )
        if resp.ok:
            for c in resp.json().get("data", []):
                cst = c.get("Call_Start_Time") or ""
                try:
                    cdt = datetime.fromisoformat(cst).astimezone(_AUTO_FU_TZ)
                    if cdt.strftime("%Y-%m-%d") != target_date:
                        continue
                    slot_h = cdt.hour
                    slot_m = (cdt.minute // _AUTO_FU_SLOT_MINUTES) * _AUTO_FU_SLOT_MINUTES
                    key = f"{slot_h:02d}:{slot_m:02d}"
                    slot_counts[key] = slot_counts.get(key, 0) + 1
                except (ValueError, TypeError):
                    pass
    except Exception as e:
        log.warning("auto-fu slot load failed: %s", e)
    _auto_fu_slots[target_date] = slot_counts
    return slot_counts

def _auto_fu_next_slot(target_date: str) -> str | None:
    """Return the next available HH:MM (ET) slot on target_date, or None if full."""
    counts = _auto_fu_load_slots(target_date)
    h = _AUTO_FU_START_HOUR
    m = _AUTO_FU_START_MINUTE
    while h < _AUTO_FU_END_HOUR:
        key = f"{h:02d}:{m:02d}"
        if counts.get(key, 0) < _AUTO_FU_CALLS_PER_SLOT:
            return key
        m += _AUTO_FU_SLOT_MINUTES
        if m >= 60:
            m = 0
            h += 1
    return None

def _auto_fu_claim_slot(target_date: str, slot_key: str):
    """Increment the slot count after successfully creating a call."""
    counts = _auto_fu_slots.get(target_date, {})
    counts[slot_key] = counts.get(slot_key, 0) + 1
    _auto_fu_slots[target_date] = counts


@app.route("/api/auto-schedule-followup", methods=["POST"])
@login_required
def api_auto_schedule_followup():
    """Auto-schedule a follow-up call, spread across 15-min slots (max 2/slot).

    Assigned to Anna Parizher, prefixed with AUTO FU for calendar visibility.
    Accepts days_offset (default 1 = tomorrow) to schedule further out.
    """
    body = request.get_json(force=True) or {}
    deal_id = body.get("deal_id", "")
    customer_name = body.get("customer_name", "")
    sent_at = body.get("sent_at", "")
    kind = body.get("kind", "quote")
    days_offset = int(body.get("days_offset", 1))

    if not deal_id:
        return jsonify({"error": "deal_id is required"}), 400
    if not sent_at:
        return jsonify({"error": "sent_at is required"}), 400

    try:
        # --- dedup: check for existing future AUTO FU call on this deal ---
        try:
            existing = requests.get(
                f"{_zoho.base_url}/crm/v6/Deals/{deal_id}/Calls",
                headers=_zoho._headers(),
                params={"fields": "id,Subject,Call_Start_Time,Outgoing_call_disposition",
                        "per_page": 20, "sort_by": "Modified_Time", "sort_order": "desc"},
                timeout=15,
            )
            if existing.ok:
                now_utc = datetime.now(timezone.utc)
                for c in (existing.json() or {}).get("data", []):
                    subj = (c.get("Subject") or "")
                    if not subj.startswith("AUTO FU:"):
                        continue
                    if c.get("Outgoing_call_disposition"):
                        continue
                    cst = c.get("Call_Start_Time") or ""
                    try:
                        cdt = datetime.fromisoformat(cst)
                        if cdt > now_utc:
                            return jsonify({
                                "call_time": cst,
                                "owner": "Anna Parizher",
                                "id": c.get("id"),
                                "status": "already_scheduled",
                            })
                    except (ValueError, TypeError):
                        pass
        except Exception as dup_err:
            log.warning("auto-schedule dedup check failed: %s", dup_err)

        # --- find next available slot ---
        target = datetime.now(timezone.utc).date() + timedelta(days=days_offset)
        target_str = target.isoformat()
        slot_key = _auto_fu_next_slot(target_str)
        if slot_key is None:
            return jsonify({
                "error": "day_full",
                "message": f"All slots on {target_str} are full.",
                "target_date": target_str,
                "days_offset": days_offset,
            }), 409

        slot_h, slot_m = (int(x) for x in slot_key.split(":"))
        # Slot hours are in ET — convert to UTC for Zoho
        call_dt_et = datetime(target.year, target.month, target.day,
                              slot_h, slot_m, 0, tzinfo=_AUTO_FU_TZ)
        call_dt = call_dt_et.astimezone(timezone.utc)
        call_iso = call_dt.isoformat()

        info = _zoho.get_deal_contact(deal_id)
        contact_id = info.get("contact_id", "")

        anna_id = _resolve_anna_id()
        if not anna_id:
            return jsonify({"error": "Could not find Anna Parizher in CRM owners"}), 500

        kind_label = "Retainer Sent" if kind == "retainer" else "Quote Sent"
        subject = f"AUTO FU: {customer_name} - {kind_label}"
        result = _zoho.create_scheduled_call(
            contact_id=contact_id,
            contact_name=customer_name,
            call_time=call_iso,
            deal_id=deal_id,
            owner_id=anna_id,
        )
        if result.get("id"):
            _auto_fu_claim_slot(target_str, slot_key)
            try:
                requests.put(
                    f"{_zoho.base_url}/crm/v6/Calls/{result['id']}",
                    headers=_zoho._headers(),
                    json={"data": [{"Subject": subject}]},
                    timeout=10,
                )
            except Exception as pe:
                log.warning("auto-schedule patch subject failed: %s", pe)
            if deal_id:
                try:
                    requests.post(
                        f"{_zoho.base_url}/crm/v6/Deals/{deal_id}/Notes",
                        headers={**_zoho._headers(), "Content-Type": "application/json"},
                        json={"data": [{"Note_Content": f"AUTO FU: {customer_name} - {kind_label} — call set for {call_dt_et.strftime('%m/%d/%Y %I:%M %p')} ET, assigned to Anna Parizher"}]},
                        timeout=10,
                    )
                except Exception as ne:
                    log.warning("auto-schedule note failed: %s", ne)

        result["call_time"] = call_iso
        result["owner"] = "Anna Parizher"
        result["target_date"] = target_str
        return jsonify(result)
    except Exception as e:
        log.error("auto_schedule_followup: %s", e)
        return jsonify({"error": str(e)}), 500


# ── Auto-SMS (one-time follow-up after 5+ dials) ──────────────
_sms_sent: set[str] = set()  # call IDs that have already received an auto-SMS

_AUTO_SMS_TEMPLATE = (
    "Hi {name}, this is Goals Plastic Surgery following up on your recent consultation. "
    "We've been trying to reach you — please call us back at your earliest convenience "
    "or reply to this message. We're happy to help!"
)

@app.route("/api/send-auto-sms", methods=["POST"])
@login_required
def api_send_auto_sms():
    body = request.get_json(force=True)
    call_id = body.get("call_id", "").strip()
    if not call_id:
        return jsonify({"error": "call_id required"}), 400
    if call_id in _sms_sent:
        return jsonify({"error": "SMS already sent for this call"}), 409

    # Look up the call record from cache to get contact phone + name + dial count
    with _lock:
        sc_data = (_cache.get("data") or {}).get("scheduled_calls", {})
        records = sc_data.get("records") or []
    rec = next((r for r in records if r.get("id") == call_id), None)
    if not rec:
        return jsonify({"error": "Call record not found"}), 404

    dials = rec.get("dial_attempts") or 0
    if dials < 5:
        return jsonify({"error": f"Only {dials} dial attempts — need at least 5"}), 400

    phone = rec.get("phone")
    if not phone:
        return jsonify({"error": "No phone number on record"}), 400

    name = (rec.get("name") or "").split()[0] or "there"
    text = _AUTO_SMS_TEMPLATE.format(name=name)

    try:
        result = _ringcx.send_sms(to_number=phone, text=text)
        _sms_sent.add(call_id)
        log.info("Auto-SMS sent for call %s to %s", call_id, phone)
        return jsonify({"ok": True, "message_id": result.get("id")})
    except Exception as e:
        log.error("Auto-SMS failed for call %s: %s", call_id, e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/send-sms", methods=["POST"])
@login_required
def api_send_sms():
    """Send a custom SMS to a scheduled-call contact from a chosen RingEX number."""
    body = request.get_json(force=True) or {}
    call_id = (body.get("call_id") or "").strip()
    from_number = (body.get("from_number") or "").strip()
    text = (body.get("text") or "").strip()
    if not text:
        return jsonify({"error": "Message text is required"}), 400

    # Resolve recipient phone from the cached record (fall back to body.to_number)
    phone = (body.get("to_number") or "").strip()
    if call_id and not phone:
        with _lock:
            sc_data = (_cache.get("data") or {}).get("scheduled_calls", {})
            records = sc_data.get("records") or []
        rec = next((r for r in records if r.get("id") == call_id), None)
        if rec:
            phone = rec.get("phone") or ""
    if not phone:
        return jsonify({"error": "No phone number for this contact"}), 400

    try:
        result = _ringcx.send_sms(to_number=phone, text=text, from_number=from_number)
        if call_id:
            _sms_sent.add(call_id)
        log.info("SMS sent for call %s to %s from %s", call_id or "-", phone, from_number or "default")
        return jsonify({"ok": True, "message_id": result.get("id")})
    except Exception as e:
        log.error("SMS send failed (call %s): %s", call_id or "-", e)
        return jsonify({"error": str(e)}), 500


_ANNA_ID = "5212466000169239093"

def _resolve_anna_id() -> str:
    return _ANNA_ID


@app.route("/api/fu-activity")
@login_required
def api_fu_activity():
    """Team-lead view of every agent's CRM call activity in a date range.

    Returns each call's owner, status, customer, deal, and (when present)
    the next chained call/task on the same deal — so a manager can see at
    a glance who's keeping their calendar in motion.
    """
    try:
        today = datetime.now(timezone.utc).date()
        date_start = request.args.get("start") or today.isoformat()
        date_end   = request.args.get("end")   or date_start
        owner_filter = (request.args.get("owner") or "").strip().lower()
        start_iso = f"{date_start}T00:00:00+00:00"
        end_iso   = f"{date_end}T23:59:59+00:00"

        rows = _zoho.get_followup_activities(start_iso, end_iso, limit=500)

        # Build "next action" links: for each call, find the next call on the
        # same deal whose Call_Start_Time is after this one's. Cheap O(N) on
        # an already-time-sorted list grouped by deal.
        by_deal: dict = {}
        for r in rows:
            did = r.get("deal_id") or ""
            if not did:
                continue
            by_deal.setdefault(did, []).append(r)
        for _did, group in by_deal.items():
            group.sort(key=lambda r: r.get("call_time") or "")
            for i, r in enumerate(group):
                if i + 1 < len(group):
                    nxt = group[i + 1]
                    r["next_action"] = {
                        "id": nxt.get("id"),
                        "kind": "Call",
                        "when": nxt.get("call_time"),
                        "status": nxt.get("status"),
                        "subject": nxt.get("subject"),
                        "owner_name": nxt.get("owner_name"),
                    }

        if owner_filter:
            rows = [r for r in rows
                    if (r.get("owner_name") or "").lower() == owner_filter]

        # Group counts by owner for the dropdown badge
        owners: dict = {}
        for r in rows:
            n = r.get("owner_name") or "Unassigned"
            owners[n] = owners.get(n, 0) + 1
        owners_list = [{"name": n, "count": c}
                       for n, c in sorted(owners.items(), key=lambda x: -x[1])]

        return jsonify({
            "status": "ok",
            "date_range": {"start": date_start, "end": date_end},
            "count": len(rows),
            "rows": rows,
            "owners": owners_list,
        })
    except Exception as e:
        log.exception("/api/fu-activity error")
        return jsonify({
            "status": "error",
            "message": f"{type(e).__name__}: {e}",
            "rows": [], "owners": [], "count": 0,
        }), 500


# Talk-time bucket boundary: calls at/over this many seconds count as "over 3 min".
AGENT_ANALYTICS_LONG_CALL_SECONDS = 180


@app.route("/api/ringcx/raw-interactions")
@login_required
def ringcx_raw_interactions():
    """Raw interaction-level data from RingCX CDR + RingEX call-log for today.

    Returns every single interaction from both platforms, grouped by agent,
    with no summarization — the ground-truth debug view.
    """
    if not _ringcx.configured:
        return jsonify({"error": "RingCX not configured"}), 503

    try:
        today = datetime.now(timezone.utc).date()
        date_start = request.args.get("start") or today.isoformat()
        date_end = request.args.get("end") or date_start
        tz_param = request.args.get("tz")
        tz_offset_minutes = int(tz_param) if tz_param is not None else None

        start_dt = _parse_local_date_to_utc(date_start, 0, 0, 0, tz_offset_minutes)
        end_dt = _parse_local_date_to_utc(date_end, 23, 59, 59, tz_offset_minutes)

        from concurrent.futures import ThreadPoolExecutor as _TP

        with _TP(max_workers=2) as ex:
            cx_future = ex.submit(_ringcx._fetch_ringcx_cdr_rows, start_dt, end_dt)
            ex_future = ex.submit(_ringcx._fetch_ringex_agent_calls, start_dt, end_dt)
            cx_rows = cx_future.result(timeout=90)
            ex_rows = ex_future.result(timeout=90)

        # Group by agent (case-insensitive merge)
        canon = {}  # lowercase → display name
        cx_by_agent = {}
        for row in cx_rows:
            name = (row.get("agent_name") or "").strip() or "Unattributed"
            low = name.lower()
            if low not in canon:
                canon[low] = name
            key = canon[low]
            cx_by_agent.setdefault(key, []).append(row)

        ex_by_agent = {}
        for row in ex_rows:
            name = (row.get("agent_name") or "").strip() or "Unattributed"
            low = name.lower()
            if low not in canon:
                canon[low] = name
            key = canon[low]
            ex_by_agent.setdefault(key, []).append(row)

        all_agents = sorted(set(list(cx_by_agent.keys()) + list(ex_by_agent.keys())) - {"Unattributed"})

        agents = []
        for agent in all_agents:
            cx = cx_by_agent.get(agent, [])
            ex = ex_by_agent.get(agent, [])
            cx_total_dur = sum(c.get("duration", 0) for c in cx)
            ex_total_dur = sum(c.get("duration", 0) for c in ex)

            agents.append({
                "agent": agent,
                "ringcx_count": len(cx),
                "ringcx_total_duration_sec": cx_total_dur,
                "ringcx_interactions": cx,
                "ringex_count": len(ex),
                "ringex_total_duration_sec": ex_total_dur,
                "ringex_calls": ex,
            })

        cx_unattr = cx_by_agent.get("Unattributed", [])
        ex_unattr = ex_by_agent.get("Unattributed", [])

        return jsonify({
            "date_range": {"start": date_start, "end": date_end},
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "totals": {
                "ringcx_interactions": len(cx_rows),
                "ringex_calls": len(ex_rows),
                "agents_identified": len(all_agents),
                "ringcx_unattributed": len(cx_unattr),
                "ringex_unattributed": len(ex_unattr),
            },
            "agents": agents,
            "unattributed": {
                "ringcx": cx_unattr,
                "ringex": ex_unattr,
            },
        })
    except Exception as e:
        log.exception("/api/ringcx/raw-interactions error")
        return jsonify({"error": str(e)}), 500


@app.route("/api/agent-analytics")
@login_required
def api_agent_analytics():
    """Per-sales-agent performance dashboard for a date range (defaults to today).

    Combines three sources, keyed by agent name:
      • Calls made + duration buckets + total talk time — Zoho CRM Calls
        (a call counts as "made" once it carries a disposition).
      • Quotes sent — Zoho Books estimates grouped by salesperson.
      • Closings — CRM "Closed Won - Surgery Scheduled" deals grouped by owner.

    Agent identity is reconciled by exact name match across systems. A Books
    salesperson name that doesn't match a CRM owner simply shows as its own row.
    """
    try:
        today = datetime.now(timezone.utc).date()
        date_start = request.args.get("start") or today.isoformat()
        date_end   = request.args.get("end")   or date_start
        tz_param   = request.args.get("tz")
        tz_offset_minutes = int(tz_param) if tz_param is not None else None

        # Accept either a plain date ('YYYY-MM-DD') or a datetime-local value
        # ('YYYY-MM-DDTHH:MM[:SS]'), so the report can be filtered to the hour,
        # not just the day. Plain dates keep the old full-day window.
        def _split_dt(s, dh, dm, ds):
            if "T" in s:
                d, t = s.split("T", 1)
                p = t.split(":")
                h = int(p[0]) if p and p[0] != "" else dh
                m = int(p[1]) if len(p) > 1 and p[1] != "" else dm
                sec = int(p[2]) if len(p) > 2 and p[2] != "" else ds
                return d, h, m, sec
            return s, dh, dm, ds

        sd, sh, sm, ss = _split_dt(date_start, 0, 0, 0)
        ed, eh, em, es = _split_dt(date_end, 23, 59, 59)

        # Convert the selected local range to a UTC window using the browser's tz
        # (falling back to TZ_OFFSET_HOURS). Used for BOTH the calls query and
        # the closings query so every metric covers the same local range.
        start_dt = _parse_local_date_to_utc(sd, sh, sm, ss, tz_offset_minutes)
        end_dt   = _parse_local_date_to_utc(ed, eh, em, es, tz_offset_minutes)
        threshold = AGENT_ANALYTICS_LONG_CALL_SECONDS

        # Interactions Report mode: show ONLY people who actually call (telephony) or
        # claim deals in the bot — not every Books salesperson / CRM deal owner. Those
        # extra sources are what dragged ex-employees and unrelated names onto the report.
        calls_only = request.args.get("calls_only") == "1"
        bot_claims = request.args.get("bot_claims") == "1"

        agents: dict = {}
        _canon: dict = {}  # lowercase → canonical display name

        def _agent(name):
            raw = (name or "").strip() or "Unassigned"
            low = raw.lower()
            if low not in _canon:
                _canon[low] = raw
            key = _canon[low]
            if key not in agents:
                agents[key] = {
                    "name": key,
                    "calls": 0,
                    "calls_under_3m": 0,
                    "calls_under_15m": 0,
                    "calls_over_3m": 0,
                    "talk_seconds": 0,
                    "handle_seconds": 0,
                    "quotes": 0,
                    "closings": 0,
                    "deals_claimed": 0,
                }
            return agents[key]

        # ── Calls: per-agent stats from the real telephony sources (RingCX
        #    Engage Voice CDR + RingEX platform call-log), not Zoho-logged
        #    calls. Calls with no identifiable agent are already excluded by
        #    get_agent_call_stats, so no "Unassigned" call bucket appears.
        call_source = "none"
        unattributed_calls = 0
        call_meta = {}

        def _add_call_stats(src_agents):
            nonlocal unattributed_calls
            for nm, s in (src_agents or {}).items():
                a = _agent(nm)
                a["calls"]          += s.get("calls", 0)
                a["calls_under_3m"] += s.get("calls_under_3m", 0)
                a["calls_under_15m"]+= s.get("calls_under_15m", 0)
                a["calls_over_3m"]  += s.get("calls_over_3m", 0)
                a["talk_seconds"]   += s.get("talk_seconds", 0)
                a["handle_seconds"] += s.get("handle_seconds", 0)

        # Interactions Report (calls_only): Zoho CRM Calls FIRST — it's the system of record
        # where BOTH platforms log (manual/regular RingEX AND the RingCX dialer), so it's the
        # one source that already includes the dialer, no RingCX reporting permission needed.
        zoho_stats = _fetch_zoho_agent_calls(start_dt, end_dt) if calls_only else None
        if zoho_stats and zoho_stats.get("agents"):
            call_source = "zoho-crm-calls"
            call_meta = zoho_stats.get("meta") or {}
            unattributed_calls = int(call_meta.get("unattributed", 0) or 0)
            _add_call_stats(zoho_stats["agents"])
        else:
            # Fallback: the back office's RC account call-log (RingEX only, no dialer).
            bo_stats = _fetch_backoffice_call_stats(start_dt, end_dt) if calls_only else None
            if bo_stats:
                call_meta = {**(bo_stats.get("meta") or {}), **(bo_stats.get("diag") or {})}
            if bo_stats and bo_stats.get("agents"):
                call_source = "backoffice-rc"
                unattributed_calls = int((bo_stats.get("meta") or {}).get("unattributed", 0) or 0)
                _add_call_stats(bo_stats["agents"])
            elif _ringcx.configured:
                try:
                    stats = _ringcx.get_agent_call_stats(start_dt, end_dt, long_call_seconds=threshold)
                    call_source = stats.get("source", "none")
                    unattributed_calls = int(stats.get("unattributed", 0) or 0)
                    _add_call_stats(stats.get("agents"))
                except Exception as ex:
                    log.warning("agent-analytics call stats failed: %s", ex)

        # ── Quotes sent: Books estimates by salesperson (skipped in calls-only mode) ──
        if not calls_only and _books.configured:
            try:
                estimates = _books.list_sent_estimates(date_start, date_end, max_records=500)
                for e in estimates:
                    _agent(e.get("salesperson_name"))["quotes"] += 1
            except Exception as ex:
                log.warning("agent-analytics quotes fetch failed: %s", ex)

        # ── Closings: CRM Closed Won - Surgery Scheduled by owner (skipped in calls-only) ──
        if not calls_only:
            try:
                counts = _zoho.get_pipeline_counts(start_dt=start_dt, end_dt=end_dt)
                closed = counts.get("Closed Won - Surgery Scheduled") or {}
                for row in closed.get("by_agent", []):
                    _agent(row.get("name"))["closings"] += int(row.get("count") or 0)
            except Exception as ex:
                log.warning("agent-analytics closings fetch failed: %s", ex)

        # ── Deals claimed in the bot (Telegram) — who's actually working leads ──
        if bot_claims:
            try:
                for name, cnt in _fetch_bot_claim_counts(sd, ed).items():
                    _agent(name)["deals_claimed"] += cnt
            except Exception as ex:
                log.warning("agent-analytics bot-claims fetch failed: %s", ex)

        # Drop the catch-all "Unassigned" bucket. In calls-only mode, also drop anyone
        # with no real activity (no calls AND no bot claims) so only people who actually
        # called on the tracker or claimed in the bot remain — no stale/unrelated names.
        # Merge the same person appearing under a call name and a claim name (report only).
        if calls_only:
            agents = _merge_duplicate_agents(agents)
        def _keep(r):
            if r["name"] == "Unassigned":
                return False
            if calls_only:
                return (r["calls"] > 0) or (r.get("deals_claimed", 0) > 0)
            return True
        rows = sorted(
            (r for r in agents.values() if _keep(r)),
            key=lambda r: (-r["calls"], -r.get("deals_claimed", 0), r["name"].lower()),
        )
        for r in rows:
            r["talk_minutes"] = round(r["talk_seconds"] / 60.0, 1)
            r["handle_minutes"] = round(r.get("handle_seconds", 0) / 60.0, 1)
            r["avg_talk_seconds"] = round(r["talk_seconds"] / r["calls"]) if r["calls"] else 0

        handle_total = sum(r.get("handle_seconds", 0) for r in rows)
        totals = {
            "agents":         len(rows),
            "calls":          sum(r["calls"] for r in rows),
            "calls_under_3m": sum(r["calls_under_3m"] for r in rows),
            "calls_under_15m":sum(r["calls_under_15m"] for r in rows),
            "calls_over_3m":  sum(r["calls_over_3m"] for r in rows),
            "talk_seconds":   sum(r["talk_seconds"] for r in rows),
            "talk_minutes":   round(sum(r["talk_seconds"] for r in rows) / 60.0, 1),
            "handle_seconds": handle_total,
            "handle_minutes": round(handle_total / 60.0, 1),
            "has_handle_time": handle_total > 0,
            "quotes":         sum(r["quotes"] for r in rows),
            "closings":       sum(r["closings"] for r in rows),
            "deals_claimed":  sum(r.get("deals_claimed", 0) for r in rows),
            "unattributed_calls": unattributed_calls,
            "call_source":    call_source,
            "call_meta":      call_meta,
        }

        return jsonify({
            "status": "ok",
            "date_range": {"start": date_start, "end": date_end},
            "rows": rows,
            "totals": totals,
        })
    except Exception as e:
        log.exception("/api/agent-analytics error")
        return jsonify({
            "status": "error",
            "message": f"{type(e).__name__}: {e}",
            "rows": [], "totals": {},
        }), 500


@app.route("/api/followup-calls")
@login_required
def api_followup_calls():
    """List Zoho CRM scheduled follow-up calls for a date range."""
    today = datetime.now(timezone.utc).date()
    date_start = request.args.get("start") or today.isoformat()
    date_end   = request.args.get("end") or date_start

    # Build ISO range (full day, UTC)
    start_iso = f"{date_start}T00:00:00+00:00"
    end_iso   = f"{date_end}T23:59:59+00:00"

    try:
        calls = _zoho.get_scheduled_followup_calls(start_iso, end_iso)
        return jsonify({"status": "ok", "calls": calls, "count": len(calls)})
    except Exception as e:
        log.error("followup-calls: %s", e)
        return jsonify({"status": "error", "error": str(e), "calls": []}), 500


# ------------------------------------------------------------------ Telegram chat

@app.route("/api/telegram/status")
@login_required
def telegram_status():
    """Check if Telegram integration is configured."""
    if not _telegram.configured:
        return jsonify({"configured": False})
    info = _telegram.get_chat_info()
    return jsonify({
        "configured": True,
        "chat_title": info.get("title", ""),
        "chat_type": info.get("type", ""),
        "allow_send": _telegram.allow_send,
    })


@app.route("/api/telegram/messages")
@login_required
def telegram_messages():
    """Fetch recent messages from the team group chat."""
    if not _telegram.configured:
        return jsonify({"error": "Telegram not configured"}), 503
    limit = min(int(request.args.get("limit", 50)), 100)
    messages = _telegram.get_recent_messages(limit=limit)
    return jsonify({"messages": messages, "count": len(messages)})


@app.route("/api/telegram/send", methods=["POST"])
@login_required
def telegram_send():
    """Send a message to the team group chat."""
    if not _telegram.configured:
        return jsonify({"error": "Telegram not configured"}), 503
    if not _telegram.allow_send:
        return jsonify({"error": "Sending disabled"}), 403
    body = request.get_json(silent=True) or {}
    text = (body.get("text") or "").strip()
    if not text:
        return jsonify({"error": "Empty message"}), 400
    sender = (body.get("sender_name") or "").strip()
    try:
        msg = _telegram.send_message(text, sender_name=sender, reply_to_message_id=body.get("reply_to"))
        return jsonify({"message": msg})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ------------------------------------------------------------------ startup

_bg_started = False
_bg_lock = threading.Lock()

def _ensure_background_thread():
    """Start the refresh thread exactly once, regardless of whether we're
    running under `python app.py` or gunicorn."""
    global _bg_started
    with _bg_lock:
        if _bg_started:
            return
        t = threading.Thread(target=_background_loop, daemon=True)
        t.start()
        _bg_started = True

# Load persisted cache from disk so first request is instant, then start background loop.
_load_persisted_cache()
_ensure_background_thread()

# The billing board's day-snapshot warmer. Same once-only guard, same reason:
# gunicorn imports this module in the worker, `python app.py` runs it directly.
if not _v6_warm_state["running"]:
    threading.Thread(target=_v6_warm_loop, daemon=True, name="v6-warm").start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, port=port, use_reloader=False, threaded=True)
 
