#!/usr/bin/env python3
"""Tests for the Rangers panel's health derivation (_ranger_state).

Run:  ./venv/bin/python test_rangers.py

Why this file exists. The Rangers row on the dashboard answers one question:
"are my automated patrols actually running?" Its whole value is in the states
that mean NO - dead and unknown - and those are exactly the states that never
occur while you are developing, so they are the ones that rot silently. Two real
bugs shipped in the first version of this panel and both were state-derivation
bugs, not UI bugs:

  1. Alert patterns were matched against the tail of the whole log, so a FATAL
     from hours earlier kept a ranger amber forever even after it recovered. A
     permanently-amber ranger is exactly as useless as a permanently-green one.
  2. Status files whose verdict sits on line 1 (palantir's "status: RED") were
     missed entirely, because a tail-only read cut the header off. That reported
     a RED fleet sweep as green.

Both are covered below. Add a case here before changing _ranger_state.
"""
import importlib.util
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def load_module():
    spec = importlib.util.spec_from_file_location("qr", os.path.join(HERE, "queue_router.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["qr"] = mod
    spec.loader.exec_module(mod)
    return mod


def make_file(content, age_seconds=0, suffix=".log", now=None):
    now = now or time.time()
    fh = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    fh.write(content)
    fh.close()
    if age_seconds:
        os.utime(fh.name, (now - age_seconds, now - age_seconds))
    return fh.name


def main():
    mod = load_module()
    now = time.time()
    created = []

    def spec(**kw):
        base = {"key": "t", "display": "T", "schedule": "every 5 min"}
        base.update(kw)
        return base

    def tmp(*a, **kw):
        path = make_file(*a, now=now, **kw)
        created.append(path)
        return path

    stale = tmp(b"ts OK fine\n", age_seconds=7200)
    fatal_last = tmp(b"ts OK good\nts FATAL broken\n")
    fatal_history = tmp(b"ts FATAL old failure\nts OK recovered\n")
    verdict_first_line = tmp(b"status: RED\n" + b"filler\n" * 3000, suffix=".md")

    cases = [
        # A ranger that has stopped running is the state this panel exists for.
        ("stale heartbeat is dead, not ok",
         spec(max_age_min=15, heartbeat=stale), "dead"),
        ("heartbeat that never appeared is dead",
         spec(max_age_min=15, heartbeat="/nonexistent/never-written"), "dead"),
        # Honesty rule: no evidence must never render as green.
        ("no heartbeat configured is unknown, never ok",
         spec(heartbeat=None), "unknown"),
        ("latest run reporting FATAL is alert",
         spec(max_age_min=15, heartbeat=fatal_last, alert_grep=["FATAL"]), "alert"),
        # Regression: a recovered ranger must clear, or the pill sticks amber forever.
        ("FATAL only in history, latest run OK, is ok",
         spec(max_age_min=15, heartbeat=fatal_history, alert_grep=["FATAL"]), "ok"),
        # Regression: whole-file scope must see a verdict above the tail window.
        ("verdict on line 1 of a long status file is alert",
         spec(max_age_min=600, heartbeat=verdict_first_line,
              alert_grep=["status: RED"], alert_scope="whole"), "alert"),
    ]

    failures = 0
    for name, sp, expected in cases:
        got = mod._ranger_state(sp, now)["state"]
        ok = got == expected
        if not ok:
            failures += 1
        print(f"{'PASS' if ok else 'FAIL'}  {name:52} got={got} want={expected}")

    # Every registered ranger must be well-formed, or the panel silently drops it.
    for sp in mod.RANGER_SPECS:
        for required in ("key", "display", "watches"):
            if not sp.get(required):
                print(f"FAIL  RANGER_SPECS entry {sp.get('key','?')} missing {required}")
                failures += 1
        if sp.get("heartbeat") and not sp.get("max_age_min"):
            # Without a staleness limit a stopped ranger can never go red.
            print(f"FAIL  ranger {sp['key']} has a heartbeat but no max_age_min")
            failures += 1

    for path in created:
        try:
            os.unlink(path)
        except OSError:
            pass

    print("\nALL PASS" if not failures else f"\n{failures} FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
