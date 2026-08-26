"""The sheet writes dates four ways and moves its money column. Read all of it.

Today's collections read as $0 for everyone while the sheet plainly held them.
Three separate defects, each verified here against a synthetic tab shaped like
the real one:

  1. YEAR-LESS DATES. Andrea's tab dates today's row "8/26" and Vivian's writes
     "August 26" -- no year in either. The parser demanded M/D/Y and returned
     None, so both rows were skipped. The year carries forward from the last row
     that stated one, which is the rule the sheet already relies on for dates
     themselves; guessing the CURRENT year would drag every undated row of an
     old section into this year.

  2. MONTH NAMES. "August 26", "Aug 26, 2026", "Sept 7" were all unreadable.

  3. A MOVING MONEY COLUMN. Yareth's layout changed partway down: "Amount
     Collected" holds nothing in the first 400 rows and 389 values in the last
     400, while the column beside it is the mirror image. Choosing from the top
     picked the DEAD column, so every recent figure was zero. The choice is made
     from the LIVE end now, because what the board reports is recent.

Verified against the real sheet at the time of writing: Vivian $13,943, Andrea
$1,781, Gabriela $0 -- three of three that had entries, to the dollar.

Run with no arguments. Reads nothing from the network.
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import collections_client as cc  # noqa: E402


def _tab(lines):
    """read_tab over a synthetic tab."""
    real = cc._fetch_lines
    cc._fetch_lines = lambda url, timeout=120: iter([l + "\n" for l in lines])
    try:
        return cc.read_tab("sheet", "gid")
    finally:
        cc._fetch_lines = real


def run():
    fails = 0
    cases = []

    # 1 + 2: the four date shapes, with the year carried forward.
    per, st = _tab([
        "Date,Patient Name,Amount Collected",
        "8/24/2026,A,$100",
        "8/25,B,$200",             # no year -> carries 2026
        "August 26,C,$300",        # month name, no year
        ",D,$50",                  # inherits August 26
        "Sept 7,E,$400",           # still 2026
    ])
    cases += [
        ("explicit M/D/Y", per.get(date(2026, 8, 24)), 100.0),
        ("year-less M/D", per.get(date(2026, 8, 25)), 200.0),
        ("month name, no year", per.get(date(2026, 8, 26)), 350.0),
        ("month name later in year", per.get(date(2026, 9, 7)), 400.0),
        ("nothing invented", len(per), 4),
    ]

    # A year-less date BEFORE any year is stated cannot be placed, and must not
    # be guessed into the current year.
    per2, _ = _tab([
        "Date,Patient Name,Amount Collected",
        "8/26,A,$100",
        "8/27/2026,B,$200",
    ])
    cases += [
        ("no year to carry -> skipped", per2.get(date(2026, 8, 26)), None),
        ("...the dated row still counts", per2.get(date(2026, 8, 27)), 200.0),
    ]

    # 3a: the header sits one column LEFT of the money and always has.
    #
    # This is the case the old step-right heuristic existed for, and the one a
    # "just trust the header" rule gets wrong -- so it is what stops the choice
    # collapsing back to the header column, which the moving-column case below
    # cannot catch on its own because there the header happens to be right.
    per3a, st3a = _tab(["Date,Patient Name,Amount Collected,"]
                       + ["8/26/2026,A,,$250"] * 8)
    cases += [
        ("header left of the money", st3a.get("amount_col"), 3),
        ("...and the total follows it", per3a.get(date(2026, 8, 26)), 2000.0),
    ]

    # 3b: the money column moves partway down. The header names col 2; the first
    # rows have money in col 3; the live section has it in col 2.
    # The live section is deliberately SHORT -- a tab that changed columns
    # yesterday. A rule that counted values inside a fixed window would still
    # read the dead column here, and did.
    old_block = ["8/0%d/2026,A,,$%d" % (i, 100) for i in range(1, 10)]
    new_block = ["8/26/2026,B,$500,"] * 3
    # Padding in the DEAD column, which is how the real tabs end. If a $0.00
    # counted as money the dead column would look the most recently active and
    # win, so this is what makes the non-zero rule testable.
    tail_pad = ["8/26/2026,,,$0.00"] * 200
    per3, st3 = _tab(["Date,Patient Name,Amount Collected,Payment Method"]
                     + old_block * 60 + new_block + tail_pad)
    cases += [
        ("live column wins", st3.get("amount_col"), 2),
        ("recent money is counted", per3.get(date(2026, 8, 26)), 1500.0),
        ("both columns were considered",
         sorted((st3.get("amount_candidates") or {})), [2, 3]),
    ]

    # ...and a tab that never moved is read exactly as before: the header's own
    # column wins the tie, so this change cannot quietly shift a healthy tab.
    per4, st4 = _tab(["Date,Patient Name,Amount Collected,Payment Method",
                      "8/26/2026,A,$700,CASH",
                      "8/26/2026,B,$300,ZELLE"])
    cases += [
        ("unchanged tab keeps its column", st4.get("amount_col"), 2),
        ("...and its total", per4.get(date(2026, 8, 26)), 1000.0),
    ]

    for label, got, want in cases:
        ok = got == want
        print("  %-34s want %-10s got %-10s %s"
              % (label, want, got, "OK" if ok else "<<< FAIL"))
        fails += 0 if ok else 1
    print("\n%d mismatched" % fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
