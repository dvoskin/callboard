"""Streaming a tab must not change a single number.

read_tab used to pull a whole tab into a string, build a list of every row,
then slice that list twice -- one tab resident three times over, four of them
back to back. On the live 512MB instance the refresher never once finished and
the board sat on "loading: true" with no error and no tabs, which is the shape
of a worker being killed: nothing raises, nothing logs, the cache stays empty.

It streams now. This asserts the parse is unchanged on the cases that break
naive line splitting -- CRLF endings, which Google serves, and a quoted field
containing a newline, which survives only if csv.reader is fed the lines in
order rather than handed pre-split records.

Run with no arguments. Reads nothing from the network.
"""
import csv
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import collections_client as cc  # noqa: E402

HDR = "Patient,Date,Amount Collected,Note"
ROWS = [
    'Someone,08/24/2026,"$6,350",ok',
    'Another,,"$1,200",same day as above',
    'Third,08/25/2026,$800,"a note\nsplit over two lines"',
    'Fourth,,"$2,000",inherits 08/25',
    'Fifth,99/99/9999,$500,bad date -- must not be forward-filled from',
]


def _materialised(text):
    rows = list(csv.reader(io.StringIO(text)))
    return rows


def _streamed(text):
    # Feed _fetch_lines' own splitting rules without touching the network.
    def gen():
        tail = ""
        for chunk in [text[i:i + 7] for i in range(0, len(text), 7)]:   # nasty chunking
            tail += chunk
            lines = tail.split("\n")
            tail = lines.pop()
            for ln in lines:
                yield cc._term(ln)
        if tail:
            yield cc._term(tail)
    return list(csv.reader(gen()))


def run():
    fails = 0
    for label, sep in [("LF", "\n"), ("CRLF", "\r\n")]:
        text = sep.join([HDR] + ROWS) + sep
        want = _materialised(text)
        got = _streamed(text)
        # The materialised parse of CRLF text keeps no stray CR either, so the
        # two must agree cell for cell.
        cases = [
            ("%s: same row count" % label, len(got), len(want)),
            ("%s: identical cells" % label, got, want),
            # csv.reader strips the CRLF terminator itself once the newline
            # is restored -- asserted as a property of the OUTPUT, not as proof
            # that any code of ours does the stripping. It does not, and should
            # not: a CR inside a quoted note is data.
            ("%s: no stray CR" % label,
             any("\r" in c for r in got for c in r), False),
            ("%s: embedded newline kept" % label,
             any("\n" in c for r in got for c in r), True),
        ]
        for lbl, g, w in cases:
            ok = g == w
            print("  %-28s %s" % (lbl, "OK" if ok else "<<< FAIL  got %r" % (g,)))
            fails += 0 if ok else 1

    # A CR *inside* a quoted field is content and must survive. This is the
    # case that makes stripping CRs the wrong fix rather than a harmless one.
    inner = 'Patient,Date,Amount Collected,Note\r\nX,08/25/2026,$100,"two\r\nlines"\r\n'
    got_inner = _streamed(inner)
    want_inner = _materialised(inner)
    for lbl, g, w in [
        ("quoted CRLF survives", got_inner, want_inner),
        ("...and is still one row", len(got_inner), 2),
    ]:
        ok = g == w
        print("  %-28s %s" % (lbl, "OK" if ok else "<<< FAIL  got %r" % (g,)))
        fails += 0 if ok else 1

    # And the sniff window must be big enough for _find_columns to verify a
    # column really holds money -- it needs five hits inside the sample.
    print("  %-28s %s" % ("sniff window >= 400",
                          "OK" if cc.SNIFF_ROWS >= 400 else "<<< FAIL"))
    fails += 0 if cc.SNIFF_ROWS >= 400 else 1

    print("\n%d mismatched" % fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
