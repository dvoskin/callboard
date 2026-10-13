"""Outbound SMS split into TYPED by the person vs AUTOMATED through their number.

Zoho automations (HelloSend) send the Journey and billing texts through the
coordinators' own RingEX numbers, so the message store files them as that
person's outbound SMS. On 2026-10-13 Judith Merlo read 71 sent = 43 typed + 28
automated. The message store has no "sent by API" flag; the body is the only
separator, and every automated template ends with a STOP line or the
coordinator signature.

What this defends:
  1. The classifier: the real templates (EN and ES) are auto, a hand-typed
     text is typed, and a missing body is typed -- never guessed.
  2. The reader keeps the VERDICT and drops the body: rows are written to disk
     as day snapshots and the body is patient text.
  3. A snapshot read before the split (no `sent_by`) still loads and its total
     still counts, but its split is None -- the board shows the total alone
     rather than a typed figure short by an unknown amount.
  4. The warmer recognises such a snapshot so it can re-read it.

Run with no arguments. Reads nothing from the network.
"""
import os
import sys
import types
from datetime import datetime, timezone

os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
os.environ["TZ_OFFSET_HOURS"] = "-4"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ringcx_client as rc  # noqa: E402
from billing_report import build_report  # noqa: E402


def _eq(label, got, want, fails):
    ok = got == want
    print("  %-58s %s (got %r)" % (label, "ok" if ok else "MISMATCH", got))
    return fails + (0 if ok else 1)


# The real templates, as sent.
JOHANA_EN = ("Hi Shemekia, I just tried calling about your surgery on Jan 29. Please call "
             "or text me back when you have a minute.\n- Johana, GOALS Coordinator "
             "(929) 415-8477\nReply STOP to opt out.")
ARIEL_ES = ("Hola Rocio, te llamé...\n- Ariel, Coordinación GOALS (929) 265-5966\n"
            "Responde STOP para cancelar.")
ZELLE = "Hi, here are the Zelle details: ..."


def case_classifier():
    f = 0
    c = rc.classify_outbound_sms
    f = _eq("EN Journey template is auto", c(JOHANA_EN), "auto", f)
    f = _eq("ES Journey template is auto", c(ARIEL_ES), "auto", f)
    f = _eq("Zelle details typed by hand is typed", c(ZELLE), "typed", f)
    f = _eq("signature alone (no STOP line) is auto",
            c("Your balance is $500.\n- Surgical Coordinator at GOALS"), "auto", f)
    f = _eq("STOP line alone (no signature) is auto",
            c("Your appointment is tomorrow at 10am. Reply STOP to opt out."), "auto", f)
    f = _eq("case-insensitive", c("see you then. reply stop to opt out"), "auto", f)
    f = _eq("decomposed accent still matches Coordinación",
            c("Hola\n- Ana, Coordinación GOALS"), "auto", f)
    f = _eq("line break inside a marker still matches",
            c("Hi\n- Ana, GOALS\nCoordinator"), "auto", f)
    f = _eq("'stop' in ordinary words is typed",
            c("Please stop by the office before 5, the coordinator will see you"), "typed", f)
    f = _eq("missing body is typed (not guessed)", c(None), "typed", f)
    f = _eq("empty body is typed (not guessed)", c("   "), "typed", f)
    return f


def _client(records):
    class R:
        status_code, ok = 200, True
        headers = {}
        def json(self): return {"records": records}
    # The fetch reads `requests` from its module; the stub replaces it there
    # only, and case_reader_keeps_verdict_not_body puts the real one back.
    rc.requests = types.SimpleNamespace(get=lambda *a, **k: R(), post=lambda *a, **k: R())
    c = rc.RingCXClient()
    c._ensure_rc_token = lambda: "tok"
    c._rc_headers = lambda: {}
    c.server_url, c.account_id = "https://x", "~"
    return c


def _rec(i, direction, subject):
    r = {"id": str(i), "direction": direction, "type": "SMS",
         "creationTime": "2026-10-13T15:00:0%dZ" % i,
         "from": {"phoneNumber": "+19294158477"}, "to": [{"phoneNumber": "+15550000000"}],
         "messageStatus": "Delivered"}
    if subject is not None:
        r["subject"] = subject
    return r


def case_reader_keeps_verdict_not_body():
    f = 0
    recs = [_rec(1, "Outbound", JOHANA_EN), _rec(2, "Outbound", ZELLE),
            _rec(3, "Outbound", None), _rec(4, "Inbound", "Reply STOP? ok")]
    real = rc.requests
    try:
        rows, meta = _client(recs).fetch_extension_messages(
            "1", datetime(2026, 10, 13, tzinfo=timezone.utc),
            datetime(2026, 10, 13, 23, 59, tzinfo=timezone.utc))
    finally:
        rc.requests = real
    by_id = {r["id"]: r for r in rows}
    f = _eq("read cleanly", meta.get("note"), None, f)
    f = _eq("template outbound -> auto", by_id["1"].get("sent_by"), "auto", f)
    f = _eq("hand-typed outbound -> typed", by_id["2"].get("sent_by"), "typed", f)
    f = _eq("outbound with no body -> typed", by_id["3"].get("sent_by"), "typed", f)
    f = _eq("inbound carries no sent_by", "sent_by" in by_id["4"], False, f)
    f = _eq("the body is NOT kept on any row",
            any(v in (JOHANA_EN, ZELLE, "Reply STOP? ok")
                for r in rows for v in r.values()), False, f)
    return f


def _call(day):
    return {"direction": "Outbound", "result": "Call connected", "duration": 120,
            "start_time": f"{day}T15:00:00.000Z"}


def _sms(day, direction="Outbound", sent_by=None):
    r = {"direction": direction, "type": "SMS", "start_time": f"{day}T15:00:00.000Z"}
    if sent_by:
        r["sent_by"] = sent_by
    return r


def _agent(rep):
    return (rep["ranked"] + rep["silent"] + rep["stalled"] + rep["unknown"])[0]


def _report(srows):
    calls = {"Judith": {"rows": [_call("2026-10-13")], "ext": "301", "ext_id": "1",
                        "complete": True, "missing_days": []}}
    sms = {"Judith": {"rows": srows, "complete": True, "missing_days": []}}
    return build_report(calls, window={"start": "2026-10-13", "end": "2026-10-13"},
                        sms_by_agent=sms)


def case_split_on_the_board():
    f = 0
    rows = ([_sms("2026-10-13", sent_by="typed") for _ in range(43)]
            + [_sms("2026-10-13", sent_by="auto") for _ in range(28)]
            + [_sms("2026-10-13", "Inbound") for _ in range(53)])
    rep = _report(rows)
    s = _agent(rep)["sms"]
    f = _eq("sent is still the total (71)", s["sent"], 71, f)
    f = _eq("sent_typed 43", s["sent_typed"], 43, f)
    f = _eq("sent_auto 28", s["sent_auto"], 28, f)
    f = _eq("received unchanged (53)", s["received"], 53, f)
    f = _eq("nothing unsplit", s["sent_unsplit"], 0, f)
    f = _eq("team carries the split too",
            (rep["team"]["sms"]["sent_typed"], rep["team"]["sms"]["sent_auto"]), (43, 28), f)
    return f


def case_old_snapshot_is_total_only():
    """A snapshot written before the split: total counts, split unknown."""
    f = 0
    old = [_sms("2026-10-13") for _ in range(5)] + [_sms("2026-10-13", "Inbound")]
    s = _agent(_report(old))["sms"]
    f = _eq("old snapshot: sent total still 5", s["sent"], 5, f)
    f = _eq("old snapshot: sent_typed is None, not 0", s["sent_typed"], None, f)
    f = _eq("old snapshot: sent_auto is None, not 0", s["sent_auto"], None, f)
    f = _eq("old snapshot: sent_unsplit says how many", s["sent_unsplit"], 5, f)
    # Mixed window (one old day, one new): no partial split either.
    mixed = old + [_sms("2026-10-13", sent_by="auto") for _ in range(3)]
    rep = _report(mixed)
    s2 = _agent(rep)["sms"]
    f = _eq("mixed window: sent total 8", s2["sent"], 8, f)
    f = _eq("mixed window: no partial typed figure", s2["sent_typed"], None, f)
    f = _eq("mixed window: team split None too", rep["team"]["sms"]["sent_typed"], None, f)
    return f


def case_warmer_recognises_old_snapshots():
    f = 0
    import app as A
    f = _eq("old outbound row -> needs re-read",
            A._v6_sms_unsplit([_sms("2026-10-13")]), True, f)
    f = _eq("split outbound rows -> no re-read",
            A._v6_sms_unsplit([_sms("2026-10-13", sent_by="typed"),
                               _sms("2026-10-13", sent_by="auto")]), False, f)
    f = _eq("inbound-only day -> no re-read (nothing to split)",
            A._v6_sms_unsplit([_sms("2026-10-13", "Inbound")]), False, f)
    f = _eq("empty day -> no re-read", A._v6_sms_unsplit([]), False, f)
    return f


def case_every_fetch_sms_day_caller_unpacks_four():
    """_v6_fetch_sms_day returns (rows, ok, group, why) since fd50852. The
    warmer's backfill still unpacked three, so every pass died on "too many
    values to unpack" and no finished SMS day -- and no pre-split snapshot --
    was ever re-read in the background. Read from the source: the warmer is an
    endless loop and cannot be run here."""
    import ast
    f = 0
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py")).read()
    widths = []
    for node in ast.walk(ast.parse(src)):
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                and getattr(node.value.func, "id", None) == "_v6_fetch_sms_day"):
            t = node.targets[0]
            widths.append(len(t.elts) if isinstance(t, ast.Tuple) else None)
    f = _eq("callers found (request path, today warm, backfill)", len(widths) >= 3, True, f)
    f = _eq("every caller unpacks four values", sorted(set(widths)), [4], f)
    return f


def run():
    total = 0
    for title, fn in [
        ("classifier: templates auto, hand-typed typed", case_classifier),
        ("the reader keeps the verdict, not the body", case_reader_keeps_verdict_not_body),
        ("the split reaches the board, total kept", case_split_on_the_board),
        ("a pre-split snapshot shows the total alone", case_old_snapshot_is_total_only),
        ("the warmer can tell a pre-split snapshot", case_warmer_recognises_old_snapshots),
        ("the warmer's backfill unpacks what it is given",
         case_every_fetch_sms_day_caller_unpacks_four),
    ]:
        print("\n== %s" % title)
        total += fn()
    print("\n%d mismatched" % total)
    return total


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
