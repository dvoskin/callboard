"""Background loops must run in the process that answers requests.

Threads do not survive fork: the child keeps only the thread that called it.
When the app module is imported BEFORE gunicorn forks its worker, every loop
started at import keeps running in the master, while the worker serves every
request with none of them -- and the worker's own copy of their state stays
empty for the life of the process.

That is what the live board showed for hours. /api/v6/collections reported
thread_started_at set, so the start line had run, and threading.enumerate()
listing only MainThread and gunicorn's pool: no collections, no v6-warm, no
refresh. The sheet was being read the whole time, in a process that never
answers anything.

The boolean guards are the trap. `_collections_started`, `_bg_started` and
_v6_warm_state["running"] are all inherited across the fork, so in the very
process where nothing is running they every one read "already started".

This forks for real. A simulation would have to assume what fork does to
threads, which is the thing being tested.

Run with no arguments.
"""
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("GOOGLE_CLIENT_ID", "test-client-id")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")

import app as appmod  # noqa: E402

WANT = {"refresh", "v6-warm", "collections"}


def run():
    # The parent imported the module, so the loops are running here.
    parent_live = {t.name for t in threading.enumerate()}
    fails = 0
    for label, got, want in [
        ("parent runs the loops", WANT & parent_live, WANT),
    ]:
        ok = got == want
        print("  %-38s want %-26s got %-26s %s"
              % (label, want, got, "OK" if ok else "<<< FAIL"))
        fails += 0 if ok else 1

    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:                                  # ── child: the "worker"
        os.close(r)
        out = {}
        try:
            # Nothing real must run; only the wiring is under test.
            appmod._background_loop = lambda: time.sleep(60)
            appmod._v6_warm_loop = lambda: time.sleep(60)
            appmod._collections_loop = lambda: time.sleep(60)

            out["after_fork"] = sorted(WANT & {t.name for t in threading.enumerate()})
            # Every "already started" flag is inherited and lying.
            out["flags_say_started"] = bool(
                appmod.__dict__.get("_collections_started")
                and appmod.__dict__.get("_bg_started")
                and appmod._v6_warm_state.get("running"))
            # As if the master had already run the helper: the child inherits
            # ITS pid. A guard that only asked "have we started?" would read
            # this as yes and start nothing, in the one process that has
            # nothing running.
            appmod._thread_pid = os.getppid()
            appmod._ensure_worker_threads()
            time.sleep(0.3)
            out["after_ensure"] = sorted(WANT & {t.name for t in threading.enumerate()})
            out["pid_differs"] = os.getpid() != pid
        except Exception as e:  # noqa: BLE001
            out["error"] = "%s: %s" % (type(e).__name__, e)
        os.write(w, json.dumps(out).encode())
        os.close(w)
        os._exit(0)

    os.close(w)                                   # ── parent
    chunks = []
    while True:
        b = os.read(r, 65536)
        if not b:
            break
        chunks.append(b)
    os.close(r)
    os.waitpid(pid, 0)
    child = json.loads(b"".join(chunks) or b"{}")

    for label, got, want in [
        ("fork kills them", child.get("after_fork"), []),
        ("...but the flags claim otherwise", child.get("flags_say_started"), True),
        ("ensure brings them back", sorted(child.get("after_ensure") or []), sorted(WANT)),
        ("no error in the child", child.get("error"), None),
    ]:
        ok = got == want
        print("  %-38s want %-26s got %-26s %s"
              % (label, want, got, "OK" if ok else "<<< FAIL"))
        fails += 0 if ok else 1

    # And it must be idempotent: a second call in the same process starts
    # nothing new, or every request would spawn another set of loops.
    before = len(threading.enumerate())
    appmod._ensure_worker_threads()
    appmod._ensure_worker_threads()
    after = len(threading.enumerate())
    ok = before == after
    print("  %-38s want %-26s got %-26s %s"
          % ("repeat calls start nothing", before, after, "OK" if ok else "<<< FAIL"))
    fails += 0 if ok else 1

    print("\n%d mismatched" % fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
