"""A day-total row whose name cell is a number is a subtotal, not a payment (2026-10-29).

Naomi's tab ended 10/29 with a row reading "| 46 | ... | 10,551$" -- the day's row
count in the name column and the day's sum in Amount Collected. The board counted it
as a payment and showed $60,216 for a $49,665 day. A name cell with no letter in it
now marks the row as a subtotal, the same as a blank one.

Reads nothing from the network. Run: python3 checks_collections_count_row.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import collections_client as cc  # noqa: E402
from datetime import date

passed, errors = 0, []
def ck(label, cond, got=None):
    global passed
    if cond: passed += 1
    else: errors.append("%s (got %r)" % (label, got))

def tab(lines):
    real = cc._fetch_lines
    cc._fetch_lines = lambda url, timeout=120: iter([l + "\n" for l in lines])
    try:
        return cc.read_tab("sheet", "gid")
    finally:
        cc._fetch_lines = real

HEAD = "Date,Patient Name,Patient DOB,Surgery Date,Location/Center,Dr. Name,Amount Collected,,Payment Method"
lines = [HEAD,
         "10/29/2026,Laureana Garcia,(05/06/1989),11/6,Atlanta,Z,\"5,300$\",,BOA",
         "10/29/2026,Ana Medina,,,,,500$,,CARD",
         "10/29/2026,Brittany Mills,(10/04/1990),1/29,Atlanta,,\"3,000$\",,BOA",
         ",46,,,,,\"8,800$\",,",                       # the day's count + total
         ",,,,,,\"8,800$\",,",                         # a blank-name subtotal (already skipped)
         "10/29/2026,José Ñúñez,,,,,100$,,CARD",       # accented name still counts
         ",María,,,,,50$,,CARD"]                       # a name with the date carried counts
per, st = tab(lines)
day = date(2026, 10, 29)
ck("numeric name row is not a payment", per.get(day) == 8950.0, per.get(day))
ck("both total rows reported as subtotals", st.get("subtotal_rows") == 2, st.get("subtotal_rows"))
ck("subtotal amount", st.get("subtotal_amount") == 17600.0, st.get("subtotal_amount"))
ck("_has_letter digits", cc._has_letter("46") is False)
ck("_has_letter blank", cc._has_letter("") is False and cc._has_letter(None) is False)
ck("_has_letter accented", cc._has_letter("Ñ") is True)
ck("_has_letter mixed", cc._has_letter("Pt 2") is True)
print("%d passed, %d failed" % (passed, len(errors)))
for e in errors: print("  FAIL", e)
sys.exit(1 if errors else 0)
