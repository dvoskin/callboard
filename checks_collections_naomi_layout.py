"""Naomi Dubon's tab (2026-10-27): the board read $0 for a day she had logged $1,000.

Her tab holds two layouts. The old rows (to Aug 7) keep their dates in column B
and their money in column H; the block she is working today matches her header,
with the date in column A as "27/10" (day/month, NO year) and "Amount
Collected" in column G as "500$". Two defects, both fixed here:

  1. parse_date refuses a year-less date unless a row above carried a year, and
     in this tab column A never states one -- so all of today's rows were
     "untrusted" and nothing was counted. A year-less date with no year to carry
     is now placed by the calendar: the year nearest today, when exactly one
     reading of it falls within 45 days.
  2. The live column is chosen by which of "Amount Collected" and the column to
     its right held money most recently, and parse_amount reads a callback note
     ("friday CB 500$") as $500 -- so her notes column would have won. Only a
     money-shaped cell now counts as evidence of which column is live.

Reads nothing from the network. Run: .venv/bin/python checks_collections_naomi_layout.py
"""
import os, sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import collections_client as cc  # noqa: E402

passed, errors = 0, []


def ck(label, cond, got=None):
    global passed
    if cond:
        passed += 1
    else:
        errors.append("%s -- got %r" % (label, got))


def tab(lines, today=None):
    real, real_near = cc._fetch_lines, cc.parse_date_near_today
    cc._fetch_lines = lambda url, timeout=120: iter([l + "\n" for l in lines])
    if today:
        cc.parse_date_near_today = lambda raw, t=None: real_near(raw, today)
    try:
        return cc.read_tab("sheet", "gid")
    finally:
        cc._fetch_lines, cc.parse_date_near_today = real, real_near


TODAY = date(2026, 10, 27)

# ---- the date ----
n = cc.parse_date_near_today
ck('"27/10" is 27 Oct, the only reading near today', n("27/10", TODAY) == date(2026, 10, 27), n("27/10", TODAY))
ck('"28/10" tomorrow', n("28/10", TODAY) == date(2026, 10, 28), n("28/10", TODAY))
ck('"5/11" is 5 Nov (11 May is 6 months away)', n("5/11", TODAY) == date(2026, 11, 5), n("5/11", TODAY))
ck('"10/11" is Oct 11 OR Nov 10, both near: ambiguous, stays untrusted', n("10/11", TODAY) is None, n("10/11", TODAY))
ck('"8/7" in October is Aug 7 (80 days) or 8 Jul: nothing within 45 days, stays untrusted', n("8/7", TODAY) is None, n("8/7", TODAY))
ck("a year-end date finds next year ('3/1' is Jan 3; Mar 1 is two months off)", n("3/1", date(2026, 12, 28)) == date(2027, 1, 3), n("3/1", date(2026, 12, 28)))
ck("...and '2/1' at year end is Jan 2 OR Feb 1, both near: ambiguous, untrusted", n("2/1", date(2026, 12, 28)) is None, n("2/1", date(2026, 12, 28)))
ck("not a date, not placed", n("cb on friday", TODAY) is None and n("", TODAY) is None and n("27/10/2026", TODAY) is None, "text")
ck("a stated year still wins in parse_date (unchanged)", cc.parse_date("8/26/2026") == date(2026, 8, 26), cc.parse_date("8/26/2026"))

# ---- money shape ----
for t in ("500$", "$500", "1,500$", "$325.00", "-$50", "(250)", "800", "$0.00"):
    ck("money: %r" % t, cc.looks_like_money(t) is True, t)
for t in ("friday CB 500$", "tomorrow/CB", "9/21", "4/25/2025", "payment link 250$", "-", "", "CARD", "lunch 1;40-2"):
    ck("not money: %r" % t, cc.looks_like_money(t) is False, t)

# ---- her tab, shaped as it is: an old-layout block, then today's ----
HDR = "Date,Patient Name,Patient DOB,Surgery Date,Location/Center,Dr. Name,Amount Collected,,Payment Method ,Medical Clearance,Blood Work"
OLD = [
    ",4/11/2025,Candace Ford,\"1,500$\",,,4/25/2025,,,,",
    ",4/11/2025,Natalie Range,,,,4/26/2025,\"$325.00\",,,",
    "cb on friday,8/7,Maria Suarez,,,,,\"10,400$\",,,",
]
NEW = [
    "27/10,Tracy Simon,(09/01/1982),11/2,New York Aesthetic,,500$,,CARD,,",
    "27/10,Kierra Raymond,04/09/1984),11/2,Texas,,,,,,",
    "27/10,Brittany Gilburt,(11/26/1988),11/3,Atlanta,,-,,,,YES",
    "27/10,Darrentasia Wilson,(10/09/1997),11/4,Texas,,,tomorrow/CB,,,",
    "27/10,Justinique Greene,(01/01/1990),11/4,Texas,,500$,,,,",
    "27/10,Tiffanie Taylor,(03/19/1978),11/5,Texas,,,friday CB 500$,,,",
]
per, st = tab([HDR] + OLD + NEW, TODAY)
ck("today's takings are the two real $500s -- not 0, not the callback note", per == {date(2026, 10, 27): 1000.0}, per)
ck("the amount column is G (the header's), not the notes column beside it", st["amount_col"] == 6, st)
ck("every one of today's six rows was dated", st["dated"] == 6, st)

# the same tab read the day before it was worked: the old block alone stays uncounted, no guess
per0, st0 = tab([HDR] + OLD, TODAY)
ck("the old layout alone is still not counted (it never was readable, and is not guessed)", per0 == {}, per0)

# a tab that carries a year is untouched by the fallback
HDR2 = "Patient,Date,Amount Collected,Note"
per2, _ = tab([HDR2, "A,08/24/2026,\"$6,350\",x", "B,,$100,same day", "C,08/25/2026,$800,y"], TODAY)
ck("a tab that states its years reads exactly as before", per2 == {date(2026, 8, 24): 6450.0, date(2026, 8, 25): 800.0}, per2)

for e in errors:
    print("  FAIL " + e)
print("%d passed, %d failed" % (passed, len(errors)))
sys.exit(1 if errors else 0)
