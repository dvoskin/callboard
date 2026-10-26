"""Staff-only pages behind a word password: /team/<slug>/ (2026-10-26).

First use: the coordinator onboarding test. Danny did not want it on the patient-facing
website, and it must never be indexed anywhere.

NOTHING about a page lives in this file or anywhere in git. dvoskin/callboard is a PUBLIC
repository, so the questions and the password would be published the moment they were
committed. Each page is uploaded to the Render disk with the machine key:

    PUT /api/staff-pages/<slug>   X-API-Key: <OVERDUE_API_KEY>
    {"page_html": "...", "thanks_html": "...", "password": "..."}

and sits under /data/staff_pages/<slug>/ as page.html, thanks.html and config.json. Only a
PBKDF2 hash of the password is stored. A slug with no folder is a plain 404, the same as any
unknown URL, so the code alone gives nothing away.

    GET  /team/<slug>/          the lock (a password box) until the word is typed, then the page
    POST /team/<slug>/          the password; 8 wrong tries per address per 10 minutes, then 429
    POST /team/<slug>/submit    the page's form; one submission per unlocked browser
    GET  /team/<slug>/thanks
    GET  /team/<slug>/results   every submission; a real Google sign-in only (not the shared
                                admin word, which signs in as admin@)
    GET  /api/staff-pages/<slug>/submissions   the same as JSON, machine key only

Every response under /team/ carries noindex/nofollow/noarchive, no-store, no-referrer and
DENY framing.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, abort, jsonify, redirect, request, session

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{5,63}$")
ITERATIONS = 200_000
MAX_FAILS, FAIL_WINDOW = 8, 600
MAX_FIELD = 20_000
ADMIN_PASSWORD_EMAIL = "admin@goalsplasticsurgery.com"   # what the shared admin word signs in as
STAFF_DOMAIN = "@goalsplasticsurgery.com"

bp = Blueprint("staff_pages", __name__)
_cfg: dict = {"root": None, "api_key_ok": lambda: False}
_fails: dict[str, list[float]] = {}
_lock = threading.Lock()


def init_staff_pages(app, data_dir: Path, api_key_ok) -> None:
    _cfg["root"] = Path(data_dir) / "staff_pages"
    _cfg["api_key_ok"] = api_key_ok
    app.register_blueprint(bp)


def _dir(slug: str) -> Path | None:
    if not SLUG_RE.match(slug or ""):
        return None
    d = _cfg["root"] / slug
    return d if (d / "config.json").is_file() else None


def _config(d: Path) -> dict:
    return json.loads((d / "config.json").read_text())


def _hash(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.strip().lower().encode(), salt, ITERATIONS).hex()


def _unlocked(slug: str) -> bool:
    return bool(session.get("staff_pages", {}).get(slug))


def _real_google_user() -> bool:
    email = ((session.get("user") or {}).get("email") or "").lower()
    return email.endswith(STAFF_DOMAIN) and email != ADMIN_PASSWORD_EMAIL


def _throttled(ip: str) -> bool:
    now = time.time()
    with _lock:
        recent = [t for t in _fails.get(ip, []) if now - t < FAIL_WINDOW]
        _fails[ip] = recent
        return len(recent) >= MAX_FAILS


def _note_fail(ip: str) -> None:
    with _lock:
        _fails.setdefault(ip, []).append(time.time())


@bp.after_request
def _private_headers(resp):
    if request.path.startswith("/team/") or request.path.startswith("/api/staff-pages/"):
        resp.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive, nosnippet, noimageindex"
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["X-Frame-Options"] = "DENY"
    return resp


LOCK_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow,noarchive,nosnippet">
<title>Private</title>
<style>
:root{--bg:#f6f8f8;--surface:#fff;--ink:#15292b;--muted:#56696b;--line:#d7e0e0;--accent:#0d6b70;--bad:#b3261e}
@media (prefers-color-scheme:dark){:root{--bg:#0f1819;--surface:#162324;--ink:#e4eeee;--muted:#9db0b1;--line:#2a3b3c;--accent:#5cc4c8;--bad:#f28b82;color-scheme:dark}}
body{margin:0;background:var(--bg);color:var(--ink);font:17px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:420px;margin:12vh auto;padding:0 16px}
form{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:24px;display:grid;gap:14px}
h1{margin:0;font-size:1.5rem}p{margin:0}.muted{color:var(--muted)}.err{color:var(--bad);font-weight:600}
input{font:inherit;color:var(--ink);background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:10px 12px;width:100%;box-sizing:border-box}
button{font:inherit;font-weight:700;border-radius:8px;padding:10px 18px;border:1px solid var(--accent);background:var(--accent);color:var(--surface);cursor:pointer}
input:focus-visible,button:focus-visible{outline:3px solid var(--accent);outline-offset:2px}
</style></head><body><main><form method="post">
<h1>This page is private.</h1><p class="muted">Enter the password to continue.</p>
{{MESSAGE}}
<input type="password" name="password" aria-label="Password" placeholder="Password" autocomplete="off" autocapitalize="none" spellcheck="false" required autofocus>
<button type="submit">Continue</button></form></main></body></html>"""


def _lock_page(error: str = "", status: int = 200):
    msg = '<p class="err" role="alert">%s</p>' % html.escape(error) if error else ""
    return LOCK_PAGE.replace("{{MESSAGE}}", msg), status, {"Content-Type": "text/html; charset=utf-8"}


@bp.route("/team/<slug>/", methods=["GET", "POST"])
def page(slug):
    d = _dir(slug)
    if not d:
        abort(404)
    if request.method == "POST":
        ip = request.remote_addr or "?"
        if _throttled(ip):
            return _lock_page("Too many tries. Wait 10 minutes and try again.", 429)
        cfg = _config(d)
        typed = request.form.get("password", "")
        if typed and hmac.compare_digest(_hash(typed, bytes.fromhex(cfg["salt"])), cfg["hash"]):
            unlocked = dict(session.get("staff_pages", {}))
            unlocked[slug] = True
            session["staff_pages"] = unlocked
            return redirect("/team/%s/" % slug, 303)
        _note_fail(ip)
        return _lock_page("That password is not right. Try again.", 401)
    if not _unlocked(slug):
        return _lock_page()
    if session.get("staff_pages_sent", {}).get(slug):
        return redirect("/team/%s/thanks" % slug, 303)
    return (d / "page.html").read_text(), 200, {"Content-Type": "text/html; charset=utf-8"}


@bp.route("/team/<slug>/submit", methods=["POST"])
def submit(slug):
    d = _dir(slug)
    if not d:
        abort(404)
    if not _unlocked(slug):
        return redirect("/team/%s/" % slug, 303)
    if session.get("staff_pages_sent", {}).get(slug):
        return redirect("/team/%s/thanks" % slug, 303)
    form = request.form
    if form.get("company"):          # honeypot: a person never fills it
        return redirect("/team/%s/thanks" % slug, 303)
    fields = {k: v[:MAX_FIELD] for k, v in form.items(multi=False)
              if k not in ("company", "form-name", "password")}
    now = datetime.now(timezone.utc)
    record = {"submitted_at": now.isoformat(timespec="seconds"),
              "ip": request.remote_addr, "user_agent": request.headers.get("User-Agent", "")[:300],
              "fields": fields}
    out = d / "submissions"
    out.mkdir(parents=True, exist_ok=True)
    name = "%s-%s.json" % (now.strftime("%Y%m%dT%H%M%S"), secrets.token_hex(4))
    tmp = out / (name + ".tmp")
    tmp.write_text(json.dumps(record, indent=1))
    os.replace(tmp, out / name)
    sent = dict(session.get("staff_pages_sent", {}))
    sent[slug] = True
    session["staff_pages_sent"] = sent
    return redirect("/team/%s/thanks" % slug, 303)


@bp.route("/team/<slug>/thanks")
def thanks(slug):
    d = _dir(slug)
    if not d or not (d / "thanks.html").is_file():
        abort(404)
    return (d / "thanks.html").read_text(), 200, {"Content-Type": "text/html; charset=utf-8"}


def _submissions(d: Path) -> list[dict]:
    out = d / "submissions"
    if not out.is_dir():
        return []
    rows = []
    for f in sorted(out.glob("*.json"), reverse=True):
        try:
            rows.append(json.loads(f.read_text()))
        except (OSError, ValueError):
            continue
    return rows


@bp.route("/team/<slug>/results")
def results(slug):
    d = _dir(slug)
    if not d:
        abort(404)
    if not _real_google_user():
        return redirect("/login?next=/team/%s/results" % slug)
    rows = _submissions(d)
    esc = html.escape
    parts = []
    for r in rows:
        f = r.get("fields", {})
        items = "".join("<tr><th>%s</th><td>%s</td></tr>" % (esc(k), esc(v).replace("\n", "<br>"))
                        for k, v in f.items() if k not in ("name", "email"))
        parts.append("<section><h2>%s <span>%s</span></h2><p>%s UTC</p><table>%s</table></section>"
                     % (esc(f.get("name", "?")), esc(f.get("email", "")),
                        esc(r.get("submitted_at", "")), items))
    body = "".join(parts) or "<p>No submissions yet.</p>"
    doc = ("<!doctype html><html lang='en'><head><meta charset='utf-8'>"
           "<meta name='viewport' content='width=device-width,initial-scale=1'>"
           "<meta name='robots' content='noindex,nofollow,noarchive'><title>Results</title><style>"
           "body{margin:0;font:16px/1.5 system-ui,sans-serif;background:#f6f8f8;color:#15292b}"
           "main{max-width:900px;margin:0 auto;padding:24px 16px;display:grid;gap:20px}"
           "section{background:#fff;border:1px solid #d7e0e0;border-radius:12px;padding:16px;overflow-x:auto}"
           "h2{margin:0;font-size:1.2rem}h2 span{font-weight:400;color:#56696b;font-size:1rem}"
           "p{margin:4px 0 12px;color:#56696b}table{border-collapse:collapse;width:100%%}"
           "th{text-align:left;vertical-align:top;padding:6px 12px 6px 0;white-space:nowrap}"
           "td{padding:6px 0;border-top:1px solid #eef2f2}"
           "</style></head><body><main><h1>Results: %s (%d)</h1>%s</main></body></html>"
           % (esc(slug), len(rows), body))
    return doc, 200, {"Content-Type": "text/html; charset=utf-8"}


@bp.route("/api/staff-pages/<slug>", methods=["PUT"])
def upload(slug):
    if not _cfg["api_key_ok"]():
        return jsonify({"error": "unauthorized"}), 401
    if not SLUG_RE.match(slug or ""):
        return jsonify({"error": "bad slug"}), 400
    body = request.get_json(silent=True) or {}
    page_html, thanks_html = body.get("page_html") or "", body.get("thanks_html") or ""
    password = (body.get("password") or "").strip()
    if not page_html or not password:
        return jsonify({"error": "page_html and password are required"}), 400
    d = _cfg["root"] / slug
    d.mkdir(parents=True, exist_ok=True)
    (d / "page.html").write_text(page_html)
    if thanks_html:
        (d / "thanks.html").write_text(thanks_html)
    salt = secrets.token_bytes(16)
    (d / "config.json").write_text(json.dumps({"salt": salt.hex(), "hash": _hash(password, salt),
                                               "updated_at": datetime.now(timezone.utc).isoformat()}))
    return jsonify({"ok": True, "slug": slug, "submissions": len(_submissions(d))})


@bp.route("/api/staff-pages/<slug>/submissions")
def submissions_json(slug):
    if not _cfg["api_key_ok"]():
        return jsonify({"error": "unauthorized"}), 401
    d = _dir(slug)
    if not d:
        return jsonify({"error": "not found"}), 404
    return jsonify({"slug": slug, "submissions": _submissions(d)})
