"""An ext_id that is not the extension it claims must be CALLED OUT, not counted.

Every billing seat names an ext_id and an extension number. The fetches use only
the ext_id and nothing compares them, so a mistyped ext_id reads as a real
extension that happens to be quiet -- and "quiet" is what a seat gets written off
for. Ana Salazar's roster ext_id (436846034) is one digit from Jorge Mier's
(436843034), and Danny said on 2026-10-05 that she is "supposed to be extension
271 on the RingEX side".

Three cases, because the failure is the interesting one:
  * agrees      -- directory id == roster id -> matches True, no warning
  * disagrees   -- directory id != roster id -> matches False, warning NAMES both,
                   and the count comes from the DIRECTORY's id (counting the
                   roster's would measure the wrong handset and prove nothing)
  * absent      -- the number is not in the directory at all -> matches None, and
                   it must not quietly report True

A day that could not be READ must report calls=None, never 0: this endpoint
exists to settle whether a line is silent, and a failed read that prints 0 is the
exact confusion it would be used to resolve.

Run with no arguments. Reads nothing from the network.
"""
import os
import sys

# GOOGLE_CLIENT_ID deliberately unset: _v6_allowed() opens the endpoint only when
# no Google client is configured, so setting one turns this into a 401 that reads
# as a broken endpoint. The gate has its own checks.
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
os.environ["TZ_OFFSET_HOURS"] = "-4"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

errors, passed = [], 0
import app as A  # noqa: E402


def ck(label, cond, got=None):
    global passed
    if cond:
        passed += 1
    else:
        errors.append("%s -- got %r" % (label, got))


DIRECTORY = {
    "137": {"id": "405657034", "name": "Vivian Martinez", "type": "User", "status": "Enabled"},
    "220": {"id": "998743035", "name": "Yareth Pavon", "type": "User", "status": "Enabled"},
    "125": {"id": "1027587035", "name": "Gabriela Maldonado", "type": "User", "status": "Enabled"},
    # The mismatch: 271 is a DIFFERENT id from the one on Ana's seat.
    "271": {"id": "999999271", "name": "Ana Salazar", "type": "User", "status": "Enabled"},
}
asked = []


def _fake_dir(*a, **k):
    return dict(DIRECTORY), {"pages": 1, "note": None, "http_error": None,
                             "rate_group": "medium", "total": len(DIRECTORY)}


def _fake_day(ext_id, day_iso, tz):
    asked.append((str(ext_id), day_iso))
    # The directory's id has real calls; the roster's mistyped id looks quiet --
    # which is the whole illusion this endpoint exists to break.
    if str(ext_id) == "999999271":
        return ([{"direction": "Outbound", "result": "Call connected"},
                 {"direction": "Outbound", "result": "No Answer"}], True, None)
    if str(ext_id) == "436846034":
        return ([], True, None)
    if str(ext_id) == "405657034":
        return ([], False, "RingEX is in a shared cooldown")      # could not read
    return ([{"direction": "Inbound", "result": "Accepted"}], True, None)


_real = (A._ringcx.fetch_extension_directory, A._v6_fetch_day_diag)
A._ringcx.fetch_extension_directory = _fake_dir
A._v6_fetch_day_diag = _fake_day
A.app.config["TESTING"] = True
try:
    c = A.app.test_client()
    r = c.get("/api/v6/ext-probe?days=2")
    body = r.get_json() if r.status_code == 200 else None
    r271 = c.get("/api/v6/ext-probe?ext=271&days=1")
    r404 = c.get("/api/v6/ext-probe?ext=9999&days=1")
    b404 = r404.get_json() if r404.status_code == 200 else None
finally:
    A._ringcx.fetch_extension_directory, A._v6_fetch_day_diag = _real

if body is None:
    print("  FAIL endpoint did not answer: HTTP %s %s" % (r.status_code, r.data[:300]))
    print("\n1 failed")
    sys.exit(1)

seats = {x["extension"]: x for x in body.get("checked", [])}
ck("every billing seat is checked", sorted(seats) == ["125", "137", "220", "271"], sorted(seats))

ana = seats.get("271", {})
ck("the mismatch is detected", ana.get("ext_id_matches_directory") is False, ana)
ck("the mismatch raises a warning", bool(ana.get("warning")), ana.get("warning"))
ck("the warning names the roster's id", "436846034" in (ana.get("warning") or ""),
   ana.get("warning"))
ck("the warning names the directory's id", "999999271" in (ana.get("warning") or ""),
   ana.get("warning"))
ck("the count comes from the DIRECTORY's id, not the roster's",
   ana.get("counted_ext_id") == "999999271", ana.get("counted_ext_id"))
ck("the directory's id shows its real calls",
   [d["calls"] for d in ana.get("calls_by_day", [])] == [2, 2],
   ana.get("calls_by_day"))
ck("connected is counted, not just calls",
   [d["connected"] for d in ana.get("calls_by_day", [])] == [1, 1],
   ana.get("calls_by_day"))
ck("the roster's id is ALSO shown, so the illusion is visible",
   [d["calls"] for d in ana.get("calls_by_day_roster_ext_id", [])] == [0, 0],
   ana.get("calls_by_day_roster_ext_id"))

ok_seat = seats.get("220", {})
ck("an agreeing seat matches", ok_seat.get("ext_id_matches_directory") is True, ok_seat)
ck("an agreeing seat raises no warning", not ok_seat.get("warning"), ok_seat.get("warning"))
ck("an agreeing seat is not given a second count",
   "calls_by_day_roster_ext_id" not in ok_seat, sorted(ok_seat))

# A day that could not be read is NOT a zero.
unread = seats.get("137", {}).get("calls_by_day", [])
ck("an unread day reports calls=None, not 0",
   all(d["calls"] is None for d in unread), unread)
ck("an unread day says read=False", all(d["read"] is False for d in unread), unread)
ck("an unread day carries the reason", all(d["why"] for d in unread), unread)

ck("?ext= narrows to the one asked for",
   [x["extension"] for x in (r271.get_json() or {}).get("checked", [])] == ["271"],
   r271.get_json())

absent = (b404 or {}).get("checked", [{}])[0]
ck("an extension missing from the directory is not reported as matching",
   absent.get("ext_id_matches_directory") is None, absent)
ck("and it is not claimed to have calls", absent.get("calls_by_day") == [], absent)

print("%d passed" % passed)
for e in errors:
    print("  FAIL", e)
print("\n%d failed" % len(errors))
sys.exit(1 if errors else 0)
