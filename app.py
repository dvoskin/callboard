import os
import json
import threading
import time
import logging
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

# ────────── Persistent disk (Render /data mount) ──────────
# Use persistent disk mount if available (Render), fallback to local
_data_dir = Path("/data") if Path("/data").exists() else Path(__file__).parent
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

def _load_resolved() -> dict:
    if not RESOLVED_PATH.exists():
        return {}
    try:
        return json.loads(RESOLVED_PATH.read_text())
    except Exception:
        return {}

def _save_resolved(data: dict) -> None:
    RESOLVED_PATH.write_text(json.dumps(data, indent=2))

def _prune_resolved(data: dict) -> dict:
    """Drop entries older than 7 days."""
    cutoff = (datetime.now(timezone.utc) - __import__("datetime").timedelta(days=7)).isoformat()
    return {k: v for k, v in data.items() if v.get("at", "") > cutoff}

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
    newly_matched = {}  # {rec_id: matched_call_data} — to persist back
    for r in annotated["scheduled_calls"]["records"]:
        rid = r.get("id")
        r["resolved"] = rid in rids
        entry = rd.get(rid, {})
        r["note"] = entry.get("note", "")
        r["resolved_by"] = entry.get("resolved_by", "")
        r["assigned_to"] = entry.get("assigned_to", "")
        r["distributed"] = entry.get("distributed", False)
        r["distributed_by"] = entry.get("distributed_by", "")
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
        data = _zoho.get_dashboard_data()
        ts = datetime.now(timezone.utc).isoformat()
        with _lock:
            _cache["data"] = data
            _cache["last_updated"] = ts
            _cache["error"] = None
            _cache["stale"] = False
        _persist_cache(data, ts)
        log.info("Refresh complete.")
    except Exception as exc:
        log.error("Refresh failed: %s", exc)
        with _lock:
            _cache["error"] = str(exc)


def _background_loop():
    consecutive_failures = 0
    while True:
        _refresh()
        with _lock:
            had_error = _cache["error"] is not None
        if had_error:
            consecutive_failures += 1
            # Exponential backoff: 30s, 60s, 120s, 240s … capped at 10 min
            wait = min(30 * (2 ** (consecutive_failures - 1)), 600)
            log.warning("Refresh failed (attempt %d). Retrying in %ds.", consecutive_failures, wait)
        else:
            consecutive_failures = 0
            wait = REFRESH_INTERVAL_SECONDS
        time.sleep(wait)


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
            with ThreadPoolExecutor(max_workers=1) as ex:
                future = ex.submit(_zoho.get_dashboard_data, start_dt=start_dt, end_dt=end_dt)
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

    if not contact_id or not call_time:
        return jsonify({"error": "contact_id and call_time are required"}), 400

    try:
        result = _zoho.create_scheduled_call(
            contact_id=contact_id,
            contact_name=contact_name or "Unknown",
            call_time=call_time,
            deal_id=body.get("deal_id", "").strip() or None,
            owner_id=body.get("owner_id", "").strip() or None,
        )
        # Trigger a cache refresh so the new call shows up
        threading.Thread(target=_refresh, daemon=True).start()
        return jsonify(result)
    except Exception as e:
        log.error("Schedule call error: %s", e)
        return jsonify({"error": str(e)}), 500


# ------------------------------------------------------------------ Pipeline stage counts

@app.route("/api/pipeline")
@login_required
def api_pipeline():
    """Deal pipeline stage counts broken down by owner. Defaults to today
    if no start/end query params are passed (same shape as /api/data)."""
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
    # Cap N to keep first prototype responsive — 4 related-list calls per quote.
    max_records = min(int(request.args.get("limit", "50")), 200)

    try:
        estimates = _books.list_sent_estimates(date_start, date_end, max_records=max_records)
        retainers = _books.list_sent_retainer_invoices(date_start, date_end, max_records=max_records)
    except Exception as e:
        log.error("Books fetch failed: %s", e)
        return jsonify({"status": "error", "message": str(e), "quotes": []}), 500

    # Dedupe by CRM deal id. Rules:
    #  1. A retainer always wins over a quote on the same deal (later in funnel).
    #  2. Multiple records of the same kind on one deal collapse to the most
    #     recently modified one (rep often sends multiple quote variants per deal).
    # Records without a deal_id are kept as-is (no way to dedupe them safely).
    def _sent_ts(doc):
        return doc.get("last_modified_time") or doc.get("created_time") or ""
    best_by_deal: dict = {}  # deal_id → (kind, doc)
    orphans: list = []        # records with no deal_id
    # Process retainers first so they win ties at the same timestamp.
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
        # Skip if a retainer already claimed this deal (retainer always wins).
        if prev and prev[0] == "retainer":
            continue
        if not prev or _sent_ts(e) > _sent_ts(prev[1]):
            best_by_deal[did] = ("quote", e)
    merged = list(best_by_deal.values()) + orphans
    merged.sort(key=lambda x: x[1].get("date") or "", reverse=True)
    merged = merged[:max_records]

    # Fetch CRM signals in parallel — 3 related-list calls per item, so 8 workers
    # ≈ 24 concurrent HTTP requests.
    def _fetch_signals_for(item):
        kind, doc = item
        deal_id = doc.get("zcrm_potential_id") or ""
        # Use created_time as the cutoff — last_modified_time drifts forward on
        # re-sends/status changes and would filter out legitimate post-quote notes.
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
        with ThreadPoolExecutor(max_workers=8) as pool:
            for item, activities, next_followup in pool.map(_fetch_signals_for, merged):
                _, doc = item
                doc_id = doc.get("estimate_id") or doc.get("invoice_id")
                results_by_id[doc_id] = (activities, next_followup)

    quotes = []
    for kind, doc in merged:
        deal_id = doc.get("zcrm_potential_id") or ""
        sent_at = doc.get("last_modified_time") or doc.get("created_time") or ""
        doc_id = doc.get("estimate_id") or doc.get("invoice_id")
        activities, next_followup = results_by_id.get(doc_id,
                                                       ([], {"status": "forgotten",
                                                             "when": None, "by": None,
                                                             "summary": None,
                                                             "kind": None, "source": None}))
        # Surface the most recent post-send note inline so the panel can show
        # it without expanding the row. Activities are time-sorted desc.
        latest_note = next((a for a in activities if a.get("kind") == "Notes"), None)
        latest_note_summary = ""
        if latest_note:
            # Content (detail) is more informative than the title; fall back to title.
            latest_note_summary = (latest_note.get("detail") or
                                    latest_note.get("summary") or "").strip()
            # Strip residual HTML from rich-text notes
            import re as _re
            latest_note_summary = _re.sub(r"<[^>]+>", "", latest_note_summary)
            latest_note_summary = " ".join(latest_note_summary.split())

        quotes.append({
            "kind": kind,  # "quote" | "retainer"
            "estimate_id": doc_id,  # keeps the existing API field name for stable client keying
            "estimate_number": doc.get("estimate_number") or doc.get("invoice_number"),
            "deal_id": deal_id,
            "deal_name": doc.get("zcrm_potential_name"),
            "customer_name": doc.get("customer_name"),
            "salesperson": doc.get("salesperson_name"),
            "date": doc.get("date"),
            "sent_at": sent_at,
            "total": doc.get("total"),
            "balance": doc.get("balance"),  # only present on retainer invoices
            "currency": doc.get("currency_code"),
            "is_viewed_by_client": doc.get("is_viewed_by_client"),
            "mail_first_viewed_time": doc.get("mail_first_viewed_time") or "",
            "expiry_date": doc.get("expiry_date") or doc.get("due_date"),
            "status": doc.get("status"),
            "activity_count": len(activities),
            "activities": activities,
            "next_followup": next_followup,
            "latest_note": latest_note_summary,
            "latest_note_ts": latest_note.get("ts") if latest_note else None,
            "latest_note_by": latest_note.get("by") if latest_note else None,
        })

    return jsonify({
        "status": "ok",
        "date_range": {"start": date_start, "end": date_end},
        "count": len(quotes),
        "quotes": quotes,
        "source": "cache" if _books.last_source_was_cache else "live",
    })


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
 
