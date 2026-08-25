#!/bin/bash
# Runs every check and reports the TALLY LINE, not the last line printed.
#
# `| tail -1` lied here: when checks_ingest_scopes.py died on a ValueError the
# last line was a passing assertion, so the summary read OK for a suite that
# crashed. A suite that printed no tally did not pass -- it did not finish.
cd "$(dirname "$0")"
PY=./.venv/bin/python
fail=0
for f in checks_*.py; do
  out=$("$PY" "$f" 2>&1)
  # Two summary styles grew up in here: "N mismatched" and "N failed". Accept
  # both -- a runner that only knows one reports the other as "did not finish".
  tally=$(printf '%s\n' "$out" | grep -E '^[0-9]+ (mismatched|failed)$' | tail -1)
  if [ -z "$tally" ]; then
    printf '%-34s NO TALLY -- did not finish\n' "${f%.py}"
    printf '%s\n' "$out" | tail -4 | sed 's/^/      /'
    fail=1
  elif [ "${tally%% *}" != "0" ]; then
    printf '%-34s %s\n' "${f%.py}" "$tally"
    fail=1
  else
    printf '%-34s ok\n' "${f%.py}"
  fi
done
[ $fail -eq 0 ] && echo "ALL GREEN" || echo "SOMETHING FAILED"
exit $fail
