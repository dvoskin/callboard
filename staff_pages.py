"""Staff-only pages behind the company Google sign-in: /team/<slug>/ (2026-10-26).

First use: the coordinator onboarding test. Danny did not want it on the patient-facing
website, and it must never be indexed anywhere. Trainees sign in with their own
@goalsplasticsurgery.com Google account ("they can just login via google"); the shared
admin word does not count, because it is one account for everybody.

NOTHING about a page lives in this file or anywhere in git. dvoskin/callboard is a PUBLIC
repository, so the questions would be published the moment they were committed. Each page
is uploaded to the Render disk with the machine key:

    PUT /api/staff-pages/<slug>   X-API-Key: <OVERDUE_API_KEY>
    {"page_html": "...", "thanks_html": "...", "managers": ["someone@goalsplasticsurgery.com"],
     "title": "...", "login_text": "..."}

and sits under /data/staff_pages/<slug>/ as page.html, thanks.html and config.json. A slug
with no folder is a plain 404, the same as any unknown URL, so the code alone gives nothing
away.

    GET  /team/<slug>/          Google sign-in, then the page (or thanks, once submitted)
    POST /team/<slug>/submit    the page's form; one submission per Google account
    GET  /team/<slug>/thanks
    GET  /team/<slug>/results   every submission; the page's managers only, since the
                                trainees are signed in too
    GET  /api/staff-pages/<slug>/submissions   the same as JSON, machine key only

/login?next=/team/<slug>/... shows the page's title instead of "Call Tracker", hides the
shared admin password box (these pages refuse it anyway) and is noindex too; see
login_purpose().

Every response under /team/ carries noindex/nofollow/noarchive, no-store, no-referrer and
DENY framing.
"""
from __future__ import annotations

import html
import json
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from flask import Blueprint, abort, jsonify, redirect, request, session

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{5,63}$")
MAX_FIELD = 20_000
ADMIN_WORD_EMAIL = "admin@goalsplasticsurgery.com"   # what the shared admin word signs in as
STAFF_DOMAIN = "@goalsplasticsurgery.com"

bp = Blueprint("staff_pages", __name__)
_cfg: dict = {"root": None, "api_key_ok": lambda: False}


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


LOGIN_NEXT_RE = re.compile(r"^/team/([a-z0-9][a-z0-9-]{5,63})/")


def login_purpose(next_path: str) -> dict | None:
    """What the sign-in page says when it was reached from a staff page, or None.
    The title comes from the page's /data config, never from the URL."""
    m = LOGIN_NEXT_RE.match(next_path or "")
    d = _dir(m.group(1)) if m else None
    if not d:
        return None
    cfg = _config(d)
    if (next_path or "").split("?")[0].rstrip("/").endswith("/results"):
        text = "Sign in with your Goals Google account to see the results."
    else:
        text = cfg.get("login_text") or "Sign in with your Goals Google account to continue."
    return {"title": cfg.get("title") or "Goals staff page", "text": text}


def _email() -> str:
    return ((session.get("user") or {}).get("email") or "").lower()


def _staff_email() -> str:
    """The signed-in person's own company Google account, or ''."""
    e = _email()
    return e if e.endswith(STAFF_DOMAIN) and e != ADMIN_WORD_EMAIL else ""


@bp.after_request
def _private_headers(resp):
    if request.path.startswith("/team/") or request.path.startswith("/api/staff-pages/"):
        resp.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive, nosnippet, noimageindex"
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["X-Frame-Options"] = "DENY"
    return resp


NOTE_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow,noarchive,nosnippet">
<title>Private</title>
<style>
:root{--bg:#f6f8f8;--surface:#fff;--ink:#15292b;--muted:#56696b;--line:#d7e0e0;--accent:#0d6b70}
@media (prefers-color-scheme:dark){:root{--bg:#0f1819;--surface:#162324;--ink:#e4eeee;--muted:#9db0b1;--line:#2a3b3c;--accent:#5cc4c8;color-scheme:dark}}
body{margin:0;background:var(--bg);color:var(--ink);font:17px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:440px;margin:12vh auto;padding:0 16px}
div{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:24px;display:grid;gap:14px}
h1{margin:0;font-size:1.5rem}p{margin:0}.muted{color:var(--muted)}
a.btn{justify-self:start;font-weight:700;border-radius:8px;padding:10px 18px;background:var(--accent);color:var(--surface);text-decoration:none}
a:focus-visible{outline:3px solid var(--accent);outline-offset:2px}
</style></head><body><main><div><h1>{{TITLE}}</h1><p class="muted">{{TEXT}}</p>{{ACTION}}</div></main></body></html>"""


def _note(title: str, text: str, action: str = "", status: int = 200):
    doc = (NOTE_PAGE.replace("{{TITLE}}", html.escape(title))
           .replace("{{TEXT}}", html.escape(text)).replace("{{ACTION}}", action))
    return doc, status, {"Content-Type": "text/html; charset=utf-8"}


def _gate(slug: str, path: str):
    """None when a company Google account is signed in; otherwise the response to send.
    Someone signed in some other way (the shared admin word) is told to switch accounts,
    not bounced to /login, which would send them straight back here in a loop."""
    if _staff_email():
        return None
    if not _email():
        return redirect("/login?next=" + quote(path, safe=""))
    return _note("Use your Goals Google account",
                 "This page opens only for your own @goalsplasticsurgery.com Google account.",
                 '<a class="btn" href="/logout">Sign out and switch account</a>', 403)


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


def _already_sent(d: Path, email: str) -> bool:
    return any(r.get("google_email") == email for r in _submissions(d))


@bp.route("/team/<slug>/")
def page(slug):
    d = _dir(slug)
    if not d:
        abort(404)
    gate = _gate(slug, "/team/%s/" % slug)
    if gate is not None:
        return gate
    if _already_sent(d, _staff_email()):
        return redirect("/team/%s/thanks" % slug, 303)
    return (d / "page.html").read_text(), 200, {"Content-Type": "text/html; charset=utf-8"}


@bp.route("/team/<slug>/submit", methods=["POST"])
def submit(slug):
    d = _dir(slug)
    if not d:
        abort(404)
    gate = _gate(slug, "/team/%s/" % slug)
    if gate is not None:
        return gate
    email = _staff_email()
    if request.form.get("company") or _already_sent(d, email):   # honeypot, or a second try
        return redirect("/team/%s/thanks" % slug, 303)
    fields = {k: v[:MAX_FIELD] for k, v in request.form.items(multi=False)
              if k not in ("company", "form-name")}
    now = datetime.now(timezone.utc)
    record = {"submitted_at": now.isoformat(timespec="seconds"), "google_email": email,
              "google_name": (session.get("user") or {}).get("name", ""),
              "user_agent": request.headers.get("User-Agent", "")[:300], "fields": fields}
    out = d / "submissions"
    out.mkdir(parents=True, exist_ok=True)
    name = "%s-%s.json" % (now.strftime("%Y%m%dT%H%M%S"), secrets.token_hex(4))
    tmp = out / (name + ".tmp")
    tmp.write_text(json.dumps(record, indent=1))
    os.replace(tmp, out / name)
    return redirect("/team/%s/thanks" % slug, 303)


@bp.route("/team/<slug>/thanks")
def thanks(slug):
    d = _dir(slug)
    if not d or not (d / "thanks.html").is_file():
        abort(404)
    gate = _gate(slug, "/team/%s/thanks" % slug)
    if gate is not None:
        return gate
    return (d / "thanks.html").read_text(), 200, {"Content-Type": "text/html; charset=utf-8"}


@bp.route("/team/<slug>/results")
def results(slug):
    d = _dir(slug)
    if not d:
        abort(404)
    gate = _gate(slug, "/team/%s/results" % slug)
    if gate is not None:
        return gate
    managers = [m.lower() for m in _config(d).get("managers", [])]
    if _staff_email() not in managers:
        return _note("Managers only", "The results of this page are open to its managers only.", "", 403)
    rows = _submissions(d)
    esc = html.escape
    parts = []
    for r in rows:
        f = r.get("fields", {})
        items = "".join("<tr><th>%s</th><td>%s</td></tr>" % (esc(k), esc(v).replace("\n", "<br>"))
                        for k, v in f.items() if k not in ("name", "email"))
        parts.append("<section><h2>%s <span>%s</span></h2><p>%s UTC · signed in as %s</p><table>%s</table></section>"
                     % (esc(f.get("name", "?")), esc(f.get("email", "")), esc(r.get("submitted_at", "")),
                        esc(r.get("google_email", "")), items))
    body = "".join(parts) or "<p>No submissions yet.</p>"
    doc = ("<!doctype html><html lang='en'><head><meta charset='utf-8'>"
           "<meta name='viewport' content='width=device-width,initial-scale=1'>"
           "<meta name='robots' content='noindex,nofollow,noarchive'><title>Results</title><style>"
           "body{margin:0;font:16px/1.5 system-ui,sans-serif;background:#f6f8f8;color:#15292b}"
           "main{max-width:900px;margin:0 auto;padding:24px 16px;display:grid;gap:20px}"
           "section{background:#fff;border:1px solid #d7e0e0;border-radius:12px;padding:16px;overflow-x:auto}"
           "h2{margin:0;font-size:1.2rem}h2 span{font-weight:400;color:#56696b;font-size:1rem}"
           "p{margin:4px 0 12px;color:#56696b}table{border-collapse:collapse;width:100%}"
           "th{text-align:left;vertical-align:top;padding:6px 12px 6px 0;white-space:nowrap}"
           "td{padding:6px 0;border-top:1px solid #eef2f2}"
           "</style></head><body><main><h1>Results: " + esc(slug) + " (" + str(len(rows)) + ")</h1>"
           + body + "</main></body></html>")
    return doc, 200, {"Content-Type": "text/html; charset=utf-8"}


@bp.route("/api/staff-pages/<slug>", methods=["PUT"])
def upload(slug):
    if not _cfg["api_key_ok"]():
        return jsonify({"error": "unauthorized"}), 401
    if not SLUG_RE.match(slug or ""):
        return jsonify({"error": "bad slug"}), 400
    body = request.get_json(silent=True) or {}
    page_html, thanks_html = body.get("page_html") or "", body.get("thanks_html") or ""
    managers = [m.strip().lower() for m in body.get("managers") or [] if m.strip().lower().endswith(STAFF_DOMAIN)]
    if not page_html or not managers:
        return jsonify({"error": "page_html and at least one @goalsplasticsurgery.com manager are required"}), 400
    d = _cfg["root"] / slug
    d.mkdir(parents=True, exist_ok=True)
    (d / "page.html").write_text(page_html)
    if thanks_html:
        (d / "thanks.html").write_text(thanks_html)
    title = (body.get("title") or "").strip()[:120]
    login_text = (body.get("login_text") or "").strip()[:300]
    (d / "config.json").write_text(json.dumps({"managers": managers, "title": title, "login_text": login_text,
                                               "updated_at": datetime.now(timezone.utc).isoformat()}))
    return jsonify({"ok": True, "slug": slug, "managers": managers, "title": title,
                    "submissions": len(_submissions(d))})


@bp.route("/api/staff-pages/<slug>/submissions")
def submissions_json(slug):
    if not _cfg["api_key_ok"]():
        return jsonify({"error": "unauthorized"}), 401
    d = _dir(slug)
    if not d:
        return jsonify({"error": "not found"}), 404
    return jsonify({"slug": slug, "submissions": _submissions(d)})
