import os
import json
import threading
import time
import logging
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env", override=True)

from flask import Flask, jsonify, render_template
from zoho_client import ZohoClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)

REFRESH_INTERVAL_SECONDS = 120

_cache: dict = {"data": None, "last_updated": None, "error": None}
_lock = threading.Lock()

# Shared client so the access token is cached across all requests
_zoho = ZohoClient()

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


@app.route("/api/data")
def api_data():
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
    """50-word AI summary of deal notes + RingCX call notes for inline expansion."""
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

        # Build context strings
        deal_notes = "\n".join(
            f"- {d.get('name','?')} [{d.get('stage','?')}] {d.get('modified','')[:10]}: {d.get('description','')}"
            for d in deals if d.get("description") or d.get("stage")
        )
        call_log = "\n".join(
            f"- {(c.get('time') or '?')[:16]} | {c.get('type','?')} | "
            f"{c.get('disposition') or 'no disposition'} | "
            f"{c.get('duration_sec') or 0}s | "
            f"Rep: {c.get('owner') or '?'} | "
            f"Notes: {(c.get('description') or '')[:200]}"
            for c in calls[:15]
        )
        sms_log = "\n".join(
            f"- {(s.get('time') or '?')[:16]} | {s.get('direction','?')} | "
            f"{s.get('status','?')}: {(s.get('message') or '')[:160]}"
            for s in sms[:15]
        )

        # Aggregated call attempt stats
        calls_summary = (
            f"{stats.get('total_calls',0)} total calls "
            f"({stats.get('outbound_calls',0)} outbound, "
            f"{stats.get('calls_with_disposition',0)} with dispositions logged)"
        )
        sms_summary = (
            f"{stats.get('total_sms',0)} SMS messages "
            f"({stats.get('sms_outbound',0)} sent, {stats.get('sms_inbound',0)} received)"
        )

        contact = data["contact"]
        prompt = f"""You analyze CRM contact history for a sales rep about to call them.

Write a 50-word summary as flowing prose. CRITICAL formatting rules:
- NO markdown, NO asterisks, NO bullet points, NO headers like "Interest Level:" — just plain prose sentences
- Cover the contact's funnel position, last meaningful interaction, any objections or interest signals, and what the rep should know going in
- If there is little info, say so plainly. Do not invent details.

CONTACT: {contact.get('name')} | Source: {contact.get('lead_source') or 'unknown'}
SUMMARY: {calls_summary}; {sms_summary}

DEALS:
{deal_notes or '(no deals on record)'}

CALL HISTORY (most recent first, up to 15):
{call_log or '(no calls logged)'}

SMS HISTORY (most recent first, up to 15):
{sms_log or '(no SMS messages)'}

Write 3-4 plain sentences. No formatting."""

        claude = anthropic.Anthropic(api_key=api_key)
        msg = claude.messages.create(
            model="claude-haiku-4-5",
            max_tokens=180,
            messages=[{"role": "user", "content": prompt}],
        )

        # Strip any stray markdown
        text = msg.content[0].text.strip()
        text = text.replace("**", "").replace("__", "")
        # Strip lines that are just labels (defensive)
        lines = [ln for ln in text.split("\n") if not ln.strip().endswith(":") or len(ln.strip()) > 35]
        text = " ".join(ln.strip() for ln in lines if ln.strip())

        return jsonify({
            "insights": text,
            "stats": stats,
            "call_count": len(calls),
            "sms_count": len(sms),
            "last_call_time": calls[0]["time"] if calls else None,
            "last_call_disposition": calls[0].get("disposition") if calls else None,
            "last_sms_time": sms[0]["time"] if sms else None,
            "last_sms_direction": sms[0].get("direction") if sms else None,
        })
    except Exception as e:
        log.error("Contact insights error: %s", e)
        return jsonify({"insights": f"Error: {e}"}), 500


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
    app.run(debug=False, port=port, use_reloader=False)
