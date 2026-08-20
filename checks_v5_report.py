"""v5_report must reproduce the hand-verified 2026-08-20 figures.

The two CSV exports for that day were reconciled by hand and every number below
was checked against the source: 22:32:03 of talk, 149/77/36 conversations past
1/3/10 minutes, and 20 of 46 connected RingEX calls present in RingCX.

This feeds those same CSVs through an adapter into the row shapes the live API
returns, so the reconciliation LOGIC is verified without credentials. It does not
prove the live API returns the same rows -- that is what /v5's parity view measures.
"""
import csv, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from v5_report import build_report

EX_CSV = "/Users/danielvoskin/Downloads/RingCentral_Sales_Team_Report_Calls_Calls_08_20_2026_1_40_12_PM.csv"
CX_CSV = "/Users/danielvoskin/Desktop/Daily Interaction Report (Sales) (3).csv"
TZ = -240  # US/Eastern in August
_P = re.compile(r'^(.*?)\s*\((\d{3})\)\s*(\d{3})-(\d{4})\s*$')


def _party(s):
    m = _P.match((s or "").strip())
    if m:
        return m.group(1).strip(), "+1" + m.group(2) + m.group(3) + m.group(4)
    d = re.sub(r"\D", "", s or "")
    return ("", "+1" + d[-10:]) if len(d) >= 10 else ((s or "").strip(), "")


def _n(v):
    try:
        return float(str(v).strip() or 0)
    except ValueError:
        return 0.0


def load_ex():
    out = []
    for r in csv.DictReader(open(EX_CSV, newline="", encoding="utf-8-sig")):
        if not (r.get("Call Start Time") or "").strip():
            continue
        fn, fp = _party(r["From Name"]); tn, tp = _party(r["To Name"])
        d = r["Call Direction"].strip()
        out.append({"agent_name": fn if d in ("Outbound", "Internal") else tn,
                    "duration": _n(r["Call Length"]), "direction": d,
                    "result": r["Result"].strip(), "start_time": r["Call Start Time"].strip(),
                    "from_number": fp, "to_number": tp, "source": "ringex"})
    return out


def load_cx():
    out = []
    for r in csv.DictReader(open(CX_CSV, newline="", encoding="utf-8-sig")):
        if r["Date"].strip().lower() == "average":
            continue
        uc = r["Channel Type"].strip() == "UC Call"
        ch = r["Channel"].strip()
        t = r["Interaction Start Time"].split(" ")[0]
        out.append({
            "agent_name": r["Agent Full Name"].strip(),
            "talk_time": _n(r["Talk Time (min)"]),          # CSV holds SECONDS
            "duration": _n(r["Sum of Interaction Duration"]),
            "direction": r["Call Type"].strip(), "result": r["Call Result"].strip(),
            "start_time": "%s %s" % (r["Date"].strip(), t),
            "ani": r["Lead Phone"].strip().strip("'"),      # originating party
            "dnis": r["Caller ID"].strip().strip("'"),      # destination
            "campaign_name": "" if uc else ch,
            "queue_name": "UC" if uc else "",
            "call_type": "UC Call" if uc else "Voice",
            "agent_disposition": r["Agent Disposition"].strip(), "source": "ringcx"})
    return out


# talk is 81145, not the 81123 the CSV-only script produced. The 22s difference is
# exactly three inbound calls (Magen Fermin 6s + 15s, Salome Tsertsvadze 1s) where the
# CSV carries a separate Handle Time that excludes ring, while the live RingEX "Simple"
# call-log view returns only `duration`, which includes it. v5 uses duration because
# that is all the API gives; the report footnote says so. 0.03% of the day.
EXPECT = {"talk": 81145, "over_1": 149, "over_3": 77, "over_10": 36, "attempts": 1056,
          "matched": 20, "ex_only": 26, "uc_only": 22, "ex_missed_absent": 19,
          "ringcx_uc": 42, "ringcx_campaign": 970, "ringex_calls": 65}


def run():
    rep = build_report(load_ex(), load_cx(), tz_offset_minutes=TZ)
    got = dict(rep["totals"]); got.update(rep["recon"]); got.update(rep["meta"])
    fails = 0
    for k, want in EXPECT.items():
        have = got.get(k)
        ok = have == want
        print("%-18s want %-8s got %-8s %s" % (k, want, have, "OK" if ok else "<<< FAIL"))
        fails += 0 if ok else 1
    print("\nranked %d | unranked %d | floor %s"
          % (len(rep["ranked"]), len(rep["unranked"]), rep["floor"]))
    print("below on all three:", [a["name"] for a in rep["ranked"] if a["band"] == "low"])
    if rep["warnings"]:
        print("\nwarnings surfaced (expected -- the export has 'Unknown' results):")
        for w in rep["warnings"]:
            print("  ", w["kind"], w.get("values") or w.get("count") or "")
    print("\n%d mismatched" % fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
