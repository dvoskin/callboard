import os
import json
import threading
import time
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env", override=True)

from flask import Flask, jsonify, render_template, request
from zoho_client import ZohoClient
from ringcx_client import RingCXClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)

REFRESH_INTERVAL_SECONDS = 120

_cache: dict = {"data": None, "last_updated": None, "error": None}
_lock = threading.Lock()

# Shared clients so access tokens are cached across all requests
_zoho = ZohoClient()
_ringcx = RingCXClient()

# ────────── Resolved-overdue persistence ──────────
RESOLVED_PATH = Path(__file__).parent / "resolved_calls.json"
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


def _refresh():
    log.info("Refreshing dashboard data...")
    try:
        data = _zoho.get_dashboard_data()
        with _lock:
            _cache["data"] = data
            _cache["last_updated"] = datetime.now(timezone.utc).isoformat()
            _cache["error"] = None
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


@app.route("/")
def index():
    return render_template("index.html", refresh_interval=REFRESH_INTERVAL_SECONDS)


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
def api_data():
    start_param = request.args.get("start")   # YYYY-MM-DD local date
    end_param   = request.args.get("end")     # YYYY-MM-DD local date
    tz_param    = request.args.get("tz")      # browser getTimezoneOffset() in minutes
    tz_offset_minutes = int(tz_param) if tz_param is not None else None

    if start_param:
        # Custom date range: live fetch, bypass cache
        try:
            effective_end = end_param or start_param
            start_dt = _parse_local_date_to_utc(start_param, 0, 0, 0, tz_offset_minutes)
            end_dt   = _parse_local_date_to_utc(effective_end, 23, 59, 59, tz_offset_minutes)
            data = _zoho.get_dashboard_data(start_dt=start_dt, end_dt=end_dt)
            rids = resolved_ids()
            annotated = json.loads(json.dumps(data))
            if annotated.get("scheduled_calls"):
                for r in annotated["scheduled_calls"]["records"]:
                    r["resolved"] = r.get("id") in rids
            return jsonify({
                "status": "ok",
                "last_updated": datetime.now(timezone.utc).isoformat(),
                "error": None,
                "data": annotated,
                "date_range": {"start": start_param, "end": effective_end},
            })
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
        # Annotate scheduled call records with resolved flag (does not mutate cache)
        rids = resolved_ids()
        annotated = json.loads(json.dumps(data))  # cheap deep copy
        if annotated.get("scheduled_calls"):
            for r in annotated["scheduled_calls"]["records"]:
                r["resolved"] = r.get("id") in rids
        return jsonify(
            {
                "status": "ok",
                "last_updated": _cache["last_updated"],
                "error": _cache["error"],
                "data": annotated,
            }
        )


@app.route("/api/scheduled-call/<rec_id>/resolve", methods=["POST"])
def resolve_call(rec_id):
    with _resolved_lock:
        data = _prune_resolved(_load_resolved())
        data[rec_id] = {"at": datetime.now(timezone.utc).isoformat()}
        _save_resolved(data)
    return jsonify({"status": "resolved", "id": rec_id})


@app.route("/api/scheduled-call/<rec_id>/unresolve", methods=["POST"])
def unresolve_call(rec_id):
    with _resolved_lock:
        data = _prune_resolved(_load_resolved())
        data.pop(rec_id, None)
        _save_resolved(data)
    return jsonify({"status": "unresolved", "id": rec_id})


@app.route("/api/refresh", methods=["POST"])
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
        recent_calls_text = "\n".join(
            f"  {i+1}. {(c.get('time') or '?')[:16]} | {c.get('type','?')} | "
            f"Disposition: {c.get('disposition') or 'none'} | "
            f"Duration: {c.get('duration_sec') or 0}s | "
            f"Rep: {c.get('owner') or '?'}"
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
        })
    except Exception as e:
        log.error("Contact insights error: %s", e)
        return jsonify({"insights": f"Error: {e}"}), 500


# ------------------------------------------------------------------ RingCX live monitoring

@app.route("/api/ringcx/status")
def ringcx_status():
    """Check if RingCX integration is configured."""
    return jsonify({"configured": _ringcx.configured})


@app.route("/api/ringcx/live")
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
def ringcx_active_calls():
    """Just the active calls (lightweight poll)."""
    if not _ringcx.configured:
        return jsonify({"error": "RingCX not configured"}), 503
    try:
        calls = _ringcx.get_active_calls()
        return jsonify({"calls": calls, "count": len(calls)})
    except Exception as e:
        log.error("RingCX active calls error: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/ringcx/agents")
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

# Start on import (gunicorn workers, flask reloader, etc.)
_ensure_background_thread()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, port=port, use_reloader=False, threaded=True)
 
