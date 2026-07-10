from __future__ import annotations  # PEP 604 unions (str | None) on Python 3.9

import os
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
from flask import Flask, jsonify, render_template, request, redirect, session, url_for
from authlib.integrations.flask_client import OAuth
from zoho_client import ZohoClient
from ringcx_client import RingCXClient
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
        supplemental = None
        if _ringcx.configured:
            try:
                supplemental = _ringcx.fetch_todays_outbound_calls()
            except Exception as e:
                log.warning("RingEX supplemental fetch failed: %s", e)
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
    """Upload a RingCX Interactions CSV export and get the full per-agent report
    (talk time, calls >3/>10 min, connect rate, campaign vs. personal queue,
    dispositions). Self-contained: parses the file, no live RingCX/WEM access."""
    user = session.get("user") or {}
    return render_template("interactions_report.html", current_user=user)


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
        tz_offset_hours = float(os.environ.get("TZ_OFFSET_HOURS", "-6"))
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
                    sup = _ringcx.fetch_todays_outbound_calls()
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


@app.route("/api/call-now-deals")
@login_required
def call_now_deals():
    """Call Now deals (Best Contact Time empty or before deal creation) for a
    day. Always live (uncached). Loaded separately by the v2 board."""
    tz_param = request.args.get("tz")
    tz_offset_minutes = int(tz_param) if tz_param is not None else None
    start_param = request.args.get("start")
    end_param = request.args.get("end")
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
                sup = _ringcx.fetch_todays_outbound_calls()
            except Exception:
                pass
        with ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(_zoho.get_call_now_deals, start_dt=start_dt, end_dt=end_dt,
                            supplemental_calls=sup)
            data = fut.result(timeout=85)
        return jsonify({"status": "ok", "last_updated": datetime.now(timezone.utc).isoformat(),
                        "data": data})
    except Exception as e:
        log.exception("/api/call-now-deals error")
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
_claims_lock = threading.Lock()

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
    distributed. Returns lightweight cards: id, name, phone, language, scheduled_time."""
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
    overdue = _compute_overdue_calls()
    _claim_overdue([o["id"] for o in overdue if o.get("id")])
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
                else float(os.environ.get("TZ_OFFSET_HOURS", "-6"))
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

        # Convert the selected local day to a UTC window using the browser's tz
        # (falling back to TZ_OFFSET_HOURS). Used for BOTH the calls query and
        # the closings query so every metric covers the same local day.
        start_dt = _parse_local_date_to_utc(date_start, 0, 0, 0, tz_offset_minutes)
        end_dt   = _parse_local_date_to_utc(date_end, 23, 59, 59, tz_offset_minutes)
        threshold = AGENT_ANALYTICS_LONG_CALL_SECONDS

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
                    "calls_over_3m": 0,
                    "talk_seconds": 0,
                    "handle_seconds": 0,
                    "quotes": 0,
                    "closings": 0,
                }
            return agents[key]

        # ── Calls: per-agent stats from the real telephony sources (RingCX
        #    Engage Voice CDR + RingEX platform call-log), not Zoho-logged
        #    calls. Calls with no identifiable agent are already excluded by
        #    get_agent_call_stats, so no "Unassigned" call bucket appears.
        call_source = "none"
        unattributed_calls = 0
        if _ringcx.configured:
            try:
                stats = _ringcx.get_agent_call_stats(
                    start_dt, end_dt, long_call_seconds=threshold)
                call_source = stats.get("source", "none")
                unattributed_calls = int(stats.get("unattributed", 0) or 0)
                for name, s in (stats.get("agents") or {}).items():
                    a = _agent(name)
                    a["calls"]          += s.get("calls", 0)
                    a["calls_under_3m"] += s.get("calls_under_3m", 0)
                    a["calls_over_3m"]  += s.get("calls_over_3m", 0)
                    a["talk_seconds"]   += s.get("talk_seconds", 0)
                    a["handle_seconds"] += s.get("handle_seconds", 0)
            except Exception as ex:
                log.warning("agent-analytics call stats failed: %s", ex)

        # ── Quotes sent: Books estimates by salesperson ──
        if _books.configured:
            try:
                estimates = _books.list_sent_estimates(date_start, date_end, max_records=500)
                for e in estimates:
                    _agent(e.get("salesperson_name"))["quotes"] += 1
            except Exception as ex:
                log.warning("agent-analytics quotes fetch failed: %s", ex)

        # ── Closings: CRM Closed Won - Surgery Scheduled by owner ──
        try:
            counts = _zoho.get_pipeline_counts(start_dt=start_dt, end_dt=end_dt)
            closed = counts.get("Closed Won - Surgery Scheduled") or {}
            for row in closed.get("by_agent", []):
                _agent(row.get("name"))["closings"] += int(row.get("count") or 0)
        except Exception as ex:
            log.warning("agent-analytics closings fetch failed: %s", ex)

        # Drop the catch-all "Unassigned" bucket entirely — only real, named
        # agents belong on the performance board.
        rows = sorted(
            (r for r in agents.values() if r["name"] != "Unassigned"),
            key=lambda r: (-r["calls"], r["name"].lower()),
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
            "calls_over_3m":  sum(r["calls_over_3m"] for r in rows),
            "talk_seconds":   sum(r["talk_seconds"] for r in rows),
            "talk_minutes":   round(sum(r["talk_seconds"] for r in rows) / 60.0, 1),
            "handle_seconds": handle_total,
            "handle_minutes": round(handle_total / 60.0, 1),
            "has_handle_time": handle_total > 0,
            "quotes":         sum(r["quotes"] for r in rows),
            "closings":       sum(r["closings"] for r in rows),
            "unattributed_calls": unattributed_calls,
            "call_source":    call_source,
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

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, port=port, use_reloader=False, threaded=True)
 
