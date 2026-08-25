"""Two RingCX reports for the same day must not overwrite each other.

RingCX mails more than one Interaction Report from the same address: the sales
one, and since 2026-08-25 one scoped to Inbound & Scheduling. They cover
different agents. The inbox was keyed by DAY alone, and a day is replaced by
whichever report reaches further into it -- so the second arrival would win the
watermark test and replace the first wholesale, emptying whichever board lost.
Every fifteen minutes.

Every distinct subject now gets its own slot, so a third report added later
cannot collide with either of the first two -- it would, silently, because both
files parse fine and the boards would simply show fewer people.

This posts three reports for the same day and asserts all three survive. The
negative half matters more than the positive: a check that only proved "the new
report stored" would pass just as happily while the sales report was being
destroyed.
"""
import io
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ["INGEST_API_KEY"] = "test-ingest-key"
os.environ.setdefault("GOOGLE_CLIENT_ID", "test-client-id")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")

import app as appmod  # noqa: E402

DAY_US, DAY_ISO = "07/15/2026", "2026-07-15"
HDR = ("Date,Agent Full Name,Call Type,Interaction Start Time,Lead Phone,Caller ID,"
       "Channel,Agent Disposition,Call Result,Term Party,Recording URL,"
       "Screen Recording URL,Channel Type,Voicemail Recording URL,RNA,"
       "Queue Time (min),Ring Time (min),Outbound Ring Time (min),"
       "Handle Time (min),Talk Time (min),Wrap Time (min),"
       "Sum of Interaction Duration,Interaction Time (min)")


def _row(agent, hhmmss, talk=90):
    return (f"{DAY_US},{agent},OUTBOUND,{hhmmss},+15551234567,+15557654321,Camp,"
            f"Disp,Outbound Answered,AGENT,,N/A,Voice,N/A,,0,0,0,{talk},{talk},0,{talk},{talk}")


def _csv(agents_times):
    return "\n".join([HDR] + [_row(a, t) for a, t in agents_times]) + "\n"


def _post(client, text, subject):
    return client.post(
        "/api/v5/ingest",
        headers={"X-API-Key": "test-ingest-key", "X-Report-Scope": subject},
        data={"file": (io.BytesIO(text.encode()), "report.csv")},
        content_type="multipart/form-data",
    )


def _agents_in(path):
    if not path.exists():
        return set()
    rows, _u = appmod.parse_interaction_csv(
        path.read_text(encoding="utf-8-sig", errors="replace"))
    return {(r.get("agent_name") or "").strip() for r in rows}


def run():
    appmod.app.config["TESTING"] = True
    c = appmod.app.test_client()
    for p in appmod.RINGCX_INBOX_DIR.glob("interactions_%s*.csv" % DAY_ISO):
        p.unlink()

    fails = 0
    # Sales first, reaching to 10:00. Then Inbound & Scheduling reaching LATER --
    # the ordering that used to destroy the first file.
    r1 = _post(c, _csv([("Gregory Beltran", "10:00:00")]), "Daily Interaction Report (Sales)")
    r2 = _post(c, _csv([("Ariel Ramirez", "17:00:00"), ("Sarahi Rivera", "17:05:00")]),
               "Interaction Report (Inbound & Scheduling)")
    # A THIRD report must get its own slot too, not collide with either.
    r3 = _post(c, _csv([("Someone Else", "18:00:00")]), "Interaction Report (Retention)")

    sales = _agents_in(appmod._inbox_path_for(DAY_ISO))
    cx = _agents_in(appmod._inbox_path_for(DAY_ISO, "interaction_report_inbound_scheduling"))

    for label, got, want in [
        ("sales accepted", r1.status_code, 200),
        ("cx accepted", r2.status_code, 200),
        ("sales SURVIVED", "Gregory Beltran" in sales, True),
        ("cx stored separately", cx, {"Ariel Ramirez", "Sarahi Rivera"}),
        ("no cross-contamination", sales & cx, set()),
        ("third report accepted", r3.status_code, 200),
        ("third report own slot",
         _agents_in(appmod._inbox_path_for(DAY_ISO, "interaction_report_retention")),
         {"Someone Else"}),
    ]:
        ok = got == want
        print("  %-24s want %-28s got %-28s %s"
              % (label, want, got, "OK" if ok else "<<< FAIL"))
        fails += 0 if ok else 1

    # And /v6 must see the CX agents even though they are not in the default slot.
    roster = [{"name": "Ariel Ramirez", "ext": "145", "ext_id": 1},
              {"name": "Sarahi Rivera", "ext": "153", "ext_id": 2}]
    by_agent, days, _cov = appmod._v6_cx_rows_for_team("inbound", [DAY_ISO], roster)
    for label, got, want in [
        ("v6 reads the cx scope", days, 1),
        ("v6 found both agents", set(by_agent), {"Ariel Ramirez", "Sarahi Rivera"}),
    ]:
        ok = got == want
        print("  %-24s want %-28s got %-28s %s"
              % (label, want, got, "OK" if ok else "<<< FAIL"))
        fails += 0 if ok else 1

    # SAFETY NET: an un-updated forwarder sends no subject at all. Two reports
    # that share no agents must still not destroy each other.
    for p in appmod.RINGCX_INBOX_DIR.glob("interactions_%s*.csv" % DAY_ISO):
        p.unlink()
    c.post("/api/v5/ingest", headers={"X-API-Key": "test-ingest-key"},
           data={"file": (io.BytesIO(_csv([("Gregory Beltran", "10:00:00")]).encode()), "r.csv")},
           content_type="multipart/form-data")
    c.post("/api/v5/ingest", headers={"X-API-Key": "test-ingest-key"},
           data={"file": (io.BytesIO(_csv([("Ariel Ramirez", "17:00:00")]).encode()), "r.csv")},
           content_type="multipart/form-data")
    survived = _agents_in(appmod._inbox_path_for(DAY_ISO))
    all_agents = set().union(*[_agents_in(p) for p in
                               appmod._inbox_paths_all_scopes(DAY_ISO)] or [set()])
    for label, got, want_ in [
        ("no-subject: first kept", "Gregory Beltran" in survived, True),
        ("no-subject: second kept", "Ariel Ramirez" in all_agents, True),
    ]:
        ok = got == want_
        print("  %-24s want %-28s got %-28s %s"
              % (label, want_, got, "OK" if ok else "<<< FAIL"))
        fails += 0 if ok else 1

    # A day where ONLY the sales report arrived must not mark a CX team's window
    # read. Otherwise every one of them renders as "logged no calls at all" --
    # an accusation assembled from another team's report.
    for p in appmod.RINGCX_INBOX_DIR.glob("interactions_%s*.csv" % DAY_ISO):
        p.unlink()
    _post(c, _csv([("Gregory Beltran", "10:00:00")]), "Daily Interaction Report (Sales)")
    by_agent2, days2, _c2 = appmod._v6_cx_rows_for_team("inbound", [DAY_ISO], roster)
    for label, got, want_ in [
        ("sales-only day not read", days2, 0),
        ("no agents invented", set(by_agent2), set()),
    ]:
        ok = got == want_
        print("  %-24s want %-28s got %-28s %s"
              % (label, want_, got, "OK" if ok else "<<< FAIL"))
        fails += 0 if ok else 1

    # REGRESSION: scoped filenames must not leak into the day list. /v5 builds
    # its date buttons from _inbox_days(), and a scope glued to the date rendered
    # every one of them as "Invalid Date" on the live Sales board.
    for p in appmod.RINGCX_INBOX_DIR.glob("interactions_%s*.csv" % DAY_ISO):
        p.unlink()
    _post(c, _csv([("Gregory Beltran", "10:00:00")]), "Daily Interaction Report (Sales)")
    _post(c, _csv([("Ariel Ramirez", "17:00:00")]), "Interaction Report (Inbound & Scheduling)")
    days = appmod._inbox_days()
    malformed = [d for d in days if len(d) != 10 or d[4] != "-" or d[7] != "-"]
    for label, got, want_ in [
        ("day list has the day", DAY_ISO in days, True),
        ("no scoped junk in days", malformed, []),
        ("one entry per day", days.count(DAY_ISO), 1),
    ]:
        ok = got == want_
        print("  %-24s want %-28s got %-28s %s"
              % (label, want_, got, "OK" if ok else "<<< FAIL"))
        fails += 0 if ok else 1

    # /v5 must read EVERY slot, not just the default one. It read only the
    # unscoped file, which was right when RingCX mailed one report. It now mails
    # more than one under different subjects, each filed separately so they stop
    # overwriting each other -- so the day the sales report lands in a scoped
    # slot, /v5 showed whatever else was in the default one. Which slot a report
    # lands in is decided by an email SUBJECT; a board's figures must not be.
    for p in appmod.RINGCX_INBOX_DIR.glob("interactions_%s*.csv" % DAY_ISO):
        p.unlink()
    _post(c, _csv([("Gregory Beltran", "10:00:00")]), "Daily Interaction Report (Sales)")
    _post(c, _csv([("Ariel Ramirez", "17:00:00")]), "Interaction Report (Inbound & Scheduling)")
    loaded = appmod._load_inbox_csv(DAY_ISO, is_today=False)
    names = {(r.get("agent_name") or "").strip() for r in (loaded[0] if loaded else [])}
    meta = loaded[1] if loaded else {}
    for label, got, want_ in [
        ("v5 sees both reports", names, {"Gregory Beltran", "Ariel Ramirez"}),
        ("...and counts both files", meta.get("reports"), 2),
        ("...covers_to is the later", meta.get("covers_to"), "17:00:00"),
    ]:
        ok = got == want_
        print("  %-24s want %-28s got %-28s %s"
              % (label, want_, got, "OK" if ok else "<<< FAIL"))
        fails += 0 if ok else 1

    # And the SAME interaction in both reports is counted once, even when the
    # two exports format the phone numbers differently -- which is the shape of
    # double counting that reads as somebody doing twice the work.
    for p in appmod.RINGCX_INBOX_DIR.glob("interactions_%s*.csv" % DAY_ISO):
        p.unlink()
    plain = HDR + "\n" + _row("Gregory Beltran", "10:00:00") + "\n"
    unformatted = plain.replace("+15551234567", "15551234567").replace(
        "+15557654321", "(555) 765-4321")
    _post(c, plain, "Daily Interaction Report (Sales)")
    _post(c, unformatted, "Interaction Report (Inbound & Scheduling)")
    loaded2 = appmod._load_inbox_csv(DAY_ISO, is_today=False)
    for label, got, want_ in [
        ("same call counted once", len(loaded2[0] if loaded2 else []), 1),
    ]:
        ok = got == want_
        print("  %-24s want %-28s got %-28s %s"
              % (label, want_, got, "OK" if ok else "<<< FAIL"))
        fails += 0 if ok else 1

    # A stopped forwarder must still be visible once several reports feed one
    # board. Taking the FRESHEST file's age would let an Inbound & Scheduling
    # report arriving every fifteen minutes mask a sales report that died hours
    # ago -- the board would rank people on stale numbers and say nothing.
    import os as _os
    for p in appmod.RINGCX_INBOX_DIR.glob("interactions_%s*.csv" % DAY_ISO):
        p.unlink()
    _post(c, _csv([("Gregory Beltran", "10:00:00")]), "Daily Interaction Report (Sales)")
    _post(c, _csv([("Ariel Ramirez", "17:00:00")]), "Interaction Report (Inbound & Scheduling)")
    old_p = appmod._inbox_path_for(DAY_ISO)          # the sales slot
    long_ago = time.time() - 6 * 3600
    _os.utime(old_p, (long_ago, long_ago))
    loaded3 = appmod._load_inbox_csv(DAY_ISO, is_today=True, stale_after_hours=3.0)
    m3 = loaded3[1] if loaded3 else {}
    for label, got, want_ in [
        ("stale report is flagged", m3.get("stale"), True),
        ("...and named", old_p.name in (m3.get("stale_detail") or ""), True),
        ("...age is the oldest", round(m3.get("age_hours") or 0) >= 6, True),
        ("...rows still returned", len(loaded3[0]) if loaded3 else 0, 2),
    ]:
        ok = got == want_
        print("  %-24s want %-28s got %-28s %s"
              % (label, want_, got, "OK" if ok else "<<< FAIL"))
        fails += 0 if ok else 1

    for p in appmod.RINGCX_INBOX_DIR.glob("interactions_%s*.csv" % DAY_ISO):
        p.unlink()
    print("\n%d mismatched" % fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
