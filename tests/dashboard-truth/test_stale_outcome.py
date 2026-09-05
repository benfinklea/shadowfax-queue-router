#!/usr/bin/env python3
"""Proves a collector that could not READ its subject does not report a fault.

Context: 2026-09-05 17:14Z, gandalf at load 95-103 with its own lanes taking
21.3 of 32 cores (fleet-planning#340). The hourly run emitted EIGHT FAILs at
once - api.runson.schema "unreadable", model.pippin.metrics "unavailable",
pipeline fields stale, runson.gate_shards None, render_state "expired
credentials" - while the subjects were healthy: /api/runson answered 200 in 7 ms
and pippen:8891/metrics answered 200 in 3.2 s when probed directly. The checker
was timing out, not the fleet.

Eight fabricated faults is worse than no signal, because someone acts on them.

The fix is a STALE outcome that does not page, plus a streak counter so a
subject that stays unreadable still escalates - as a COLLECTOR fault, under its
own signal name. This file tests both halves, because a STALE that never
escalates would just be a quieter way of failing open.

Run: python3 tests/dashboard-truth/test_stale_outcome.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import urllib.error
from pathlib import Path

HERE = Path(__file__).resolve().parent
FAILURES: list[str] = []


def check(ok: bool, label: str) -> None:
    if ok:
        print(f"PASS: {label}")
    else:
        print(f"FAIL: {label}", file=sys.stderr)
        FAILURES.append(label)


def load_suite(state_dir: Path):
    """Import truth_suite with STATE pointed at a scratch directory.

    DASH_TRUTH_STATE is honoured by the module if it reads it; if not, STATE is
    overridden after import. Either way nothing here writes to the real
    /workspace/planning/state/dashboard-truth.
    """
    os.environ["DASH_TRUTH_STATE"] = str(state_dir)
    spec = importlib.util.spec_from_file_location("truth_suite", HERE / "truth_suite.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.STATE = state_dir
    state_dir.mkdir(parents=True, exist_ok=True)
    return module


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "dashboard-truth"
        ts = load_suite(state)

        # ── the classifier ──────────────────────────────────────────────────
        transport = [
            TimeoutError("timed out"),
            socket.timeout("timed out"),
            socket.gaierror("name resolution"),
            ConnectionResetError("reset by peer"),
            ConnectionRefusedError("refused"),
            urllib.error.URLError("unreachable"),
            subprocess.TimeoutExpired(cmd=["x"], timeout=6),
        ]
        check(all(ts.is_transport_failure(e) for e in transport),
              "every no-answer-arrived failure classifies as transport")

        # The subject ANSWERED in each of these - answered wrongly, or the
        # collector's own parsing is broken. Both are real faults and must stay
        # FAIL. HTTPError is the trap: urllib makes it a SUBCLASS of URLError,
        # so a naive isinstance check on URLError would swallow every 500 the
        # dashboard ever returns.
        answered = [
            json.JSONDecodeError("bad", "{}", 0),
            KeyError("missing"),
            ValueError("nonsense"),
            urllib.error.HTTPError("http://x", 500, "Server Error", {}, None),
            urllib.error.HTTPError("http://x", 403, "Forbidden", {}, None),
        ]
        check(not any(ts.is_transport_failure(e) for e in answered),
              "a subject that answered wrongly is NOT reclassified as transport")
        check(not ts.is_transport_failure(urllib.error.HTTPError("http://x", 500, "e", {}, None)),
              "HTTPError does not slip through as URLError despite being its subclass")

        # ── emit_unreadable routes to the right level ───────────────────────
        ts.results.clear()
        ts.emit_unreadable("api.runson.schema", TimeoutError(), "unreadable", "HTTP 200 JSON")
        ts.emit_unreadable("api.status.schema", ValueError(), "unreadable", "HTTP 200 JSON")
        levels = {r["signal"]: r["level"] for r in ts.results}
        check(levels.get("api.runson.schema") == "STALE",
              "a timed-out read emits STALE, not FAIL")
        check(levels.get("api.status.schema") == "FAIL",
              "a malformed answer still emits FAIL")

        # ── a stale run does not page ───────────────────────────────────────
        # This is the whole point: eight timeouts must not become eight faults.
        ts.results.clear()
        for endpoint in ("runson", "status", "fleet", "pipeline", "queue", "models", "routes", "sqlite"):
            ts.emit_unreadable(f"api.{endpoint}.schema", TimeoutError(), "unreadable", "HTTP 200 JSON")
        stale = [r for r in ts.results if r["level"] == "STALE"]
        streaks = ts.stale_streaks(stale)
        stuck = [r for r in stale if streaks.get(r["signal"], 0) >= ts.CONSECUTIVE_STALE_ESCALATION]
        check(len(stale) == 8, f"all eight reads recorded as STALE (got {len(stale)})")
        check(stuck == [], "on the first stale run nothing is escalated - the 17:14Z page would not have fired")

        # ── but a subject that STAYS unreadable does page ───────────────────
        for _ in range(ts.CONSECUTIVE_STALE_ESCALATION - 1):
            streaks = ts.stale_streaks(stale)
        stuck = [r for r in stale if streaks.get(r["signal"], 0) >= ts.CONSECUTIVE_STALE_ESCALATION]
        check(len(stuck) == 8,
              f"after {ts.CONSECUTIVE_STALE_ESCALATION} consecutive stale runs it escalates (got {len(stuck)})")

        # ── the streak must be CONSECUTIVE, not cumulative ──────────────────
        # Two unrelated blips a day apart must never add up to an escalation.
        ts.stale_streaks([])                       # a clean run clears everything
        streaks = ts.stale_streaks(stale)
        check(all(v == 1 for v in streaks.values()),
              "a clean run resets the streak; blips do not accumulate into a page")

        # ── the escalation names the COLLECTOR, not the subject ─────────────
        for _ in range(ts.CONSECUTIVE_STALE_ESCALATION - 1):
            streaks = ts.stale_streaks(stale)
        escalated = [{**r, "signal": f"collector.unreadable.{r['signal']}"}
                     for r in stale if streaks.get(r["signal"], 0) >= ts.CONSECUTIVE_STALE_ESCALATION]
        check(escalated and all(s["signal"].startswith("collector.unreadable.") for s in escalated),
              "the escalation is named as a collector fault so nobody chases the subject")

    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed", file=sys.stderr)
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
