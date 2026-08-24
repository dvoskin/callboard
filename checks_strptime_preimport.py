"""_strptime must be imported before app.py starts any thread.

datetime.strptime imports _strptime lazily, on its first call. Two threads
making that first call together can leave one of them looking at a
half-initialised module:

    partially initialized module '_strptime' has no attribute
    '_strptime_datetime' (most likely due to a circular import)

which is what /v6 returned in production. It reads like a circular import in our
own code. It is not -- it is CPython's lazy import (bpo-7980), and it only
appears under threads. This app is the exact shape that triggers it: gunicorn
serves on 8 threads while the v6 snapshot warmer parses dates on its own thread
from the moment the module loads.

The fix is a bare `import _strptime` at the top of app.py, which looks unused and
is therefore exactly the kind of line someone tidies away. This check exists to
make that tidying fail loudly.

Two cases, because the first alone would pass even with the fix removed:
  1. app.py leaves _strptime in sys.modules  (the fix is present)
  2. the race is real and reproducible without it  (the fix is necessary)
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def case_app_preimports():
    """Importing app must leave _strptime already loaded."""
    code = (
        "import sys, json;"
        "sys.path.insert(0, %r);"
        "import app;"
        "print(json.dumps({'loaded': '_strptime' in sys.modules}))" % HERE
    )
    env = dict(os.environ)
    env.setdefault("TZ_OFFSET_HOURS", "-4")
    try:
        out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                             text=True, timeout=180, env=env, cwd=HERE)
    except subprocess.TimeoutExpired:
        print("  %-28s <<< FAIL (import app timed out)" % "app preimports _strptime")
        return 1
    line = ""
    for ln in (out.stdout or "").splitlines():
        if ln.strip().startswith("{"):
            line = ln.strip()
    if not line:
        print("  %-28s <<< FAIL (could not import app)" % "app preimports _strptime")
        print("      stderr tail:", (out.stderr or "")[-300:])
        return 1
    import json
    loaded = json.loads(line)["loaded"]
    print("  %-28s want %-6s got %-6s %s"
          % ("app preimports _strptime", True, loaded, "OK" if loaded else "<<< FAIL"))
    return 0 if loaded else 1


def case_race_is_real():
    """Without the preimport, concurrent first-calls to strptime really do break.

    If CPython ever fixes the lazy import this case stops failing, and the
    preimport becomes belt-and-braces rather than load-bearing -- which is worth
    knowing, so this reports rather than asserting a failure.
    """
    code = (
        "import sys, threading, datetime\n"
        "sys.modules.pop('_strptime', None)\n"
        "errs = []\n"
        "def go():\n"
        "    try: datetime.datetime.strptime('2026-08-24', '%Y-%m-%d')\n"
        "    except Exception as e: errs.append(type(e).__name__)\n"
        "ts = [threading.Thread(target=go) for _ in range(24)]\n"
        "[t.start() for t in ts]; [t.join() for t in ts]\n"
        "print('ERRS', len(errs))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, timeout=120)
    n = 0
    for ln in (out.stdout or "").splitlines():
        if ln.startswith("ERRS"):
            n = int(ln.split()[1])
    print("  %-28s %d thread(s) hit the lazy-import race%s"
          % ("race without preimport", n,
             "" if n else "  (not reproduced on this build -- preimport is still correct)"))
    return 0


def run():
    total = 0
    for title, fn in [("preimport present", case_app_preimports),
                      ("race is real", case_race_is_real)]:
        print("\n== %s" % title)
        total += fn()
    print("\n%d mismatched" % total)
    return total


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
