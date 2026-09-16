#!/usr/bin/env python3
"""fp#1226: one hung host must not abort the whole truth suite."""
import ast
import concurrent.futures
import subprocess
import types
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent / "truth_suite.py"

# Load only run()/probe_host; importing the module executes the live suite.
_tree = ast.parse(SRC.read_text())
_want = {"run", "probe_host"}
_kept = [n for n in _tree.body if isinstance(n, ast.FunctionDef) and n.name in _want]
assert {n.name for n in _kept} == _want, f"helpers missing: {_want - {n.name for n in _kept}}"
ts = types.ModuleType("ts")
ts.subprocess = subprocess
exec(compile(ast.Module(body=_kept, type_ignores=[]), str(SRC), "exec"), ts.__dict__)

HOSTS = {"alpha": ("10.0.0.1", "u"), "hung": ("10.0.0.2", "u"),
         "beta": ("10.0.0.3", "u"), "gamma": ("10.0.0.4", "u")}


class ProbeResilience(unittest.TestCase):
    def test_timeout_becomes_a_failed_probe_not_an_exception(self):
        cp = ts.run(["sleep", "5"], timeout=1)
        self.assertNotEqual(cp.returncode, 0, "a timed-out probe must not look successful")
        self.assertIn("timed out", cp.stderr)

    def test_unlaunchable_command_becomes_a_failed_probe(self):
        cp = ts.run(["/nonexistent/probe-binary"], timeout=2)
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("probe failed", cp.stderr)

    def test_a_successful_probe_is_unchanged(self):
        """The catch must not mask a real result."""
        cp = ts.run(["echo", "alpha"], timeout=5)
        self.assertEqual(cp.returncode, 0)
        self.assertEqual(cp.stdout.strip(), "alpha")

    def test_pool_map_yields_every_host_when_one_hangs(self):
        """The hung host is NOT last, so ordering cannot carry this test."""
        self.assertNotEqual(list(HOSTS)[-1], "hung", "fixture must not put the hang last")

        def probe(item):
            name, _ = item
            cp = ts.run(["sleep", "5"] if name == "hung" else ["echo", "ok"], timeout=1)
            return name, {"primary": cp.returncode == 0}

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            probes = dict(pool.map(probe, HOSTS.items()))

        self.assertEqual(set(probes), set(HOSTS), "a hung host cost other hosts their verdicts")
        self.assertFalse(probes["hung"]["primary"], "the hung host must be reported down")
        for name in ("alpha", "beta", "gamma"):
            self.assertTrue(probes[name]["primary"], f"{name} lost its verdict to an unrelated hang")

    def test_mutation_reraising_the_timeout_aborts_the_pool(self):
        """MUTANT: let TimeoutExpired escape -> the whole map dies (the old bug)."""
        def unguarded(cmd, timeout):
            return subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)

        def probe(item):
            name, _ = item
            cp = unguarded(["sleep", "5"] if name == "hung" else ["echo", "ok"], 1)
            return name, {"primary": cp.returncode == 0}

        with self.assertRaises(subprocess.TimeoutExpired,
                               msg="MUTANT SURVIVED: the guard in run() is not load-bearing"):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                dict(pool.map(probe, HOSTS.items()))

    def test_all_healthy_hosts_produce_no_false_down(self):
        def probe(item):
            name, _ = item
            return name, {"primary": ts.run(["echo", "ok"], timeout=5).returncode == 0}

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            probes = dict(pool.map(probe, HOSTS.items()))
        self.assertTrue(all(p["primary"] for p in probes.values()),
                        "the timeout handler reported a healthy host as down")


if __name__ == "__main__":
    unittest.main(verbosity=2)
