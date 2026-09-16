#!/usr/bin/env python3
"""fp#1209: prove gpu util comparison discriminates drift from variance."""
import random, statistics, unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent / "truth_suite.py"

# Load ONLY the helpers under test. Importing the module executes the live suite.
import ast, types
tree = ast.parse(SRC.read_text())
wanted = {"util_matches", "nvidia_util_window", "GPU_UTIL_SAMPLES", "UTIL_INDISCRIMINATE_BAND"}
kept = [n for n in tree.body
        if (isinstance(n, ast.FunctionDef) and n.name in wanted)
        or (isinstance(n, ast.Assign) and any(
            getattr(tgt, "id", None) in wanted for tgt in n.targets))]
assert {getattr(n, "name", None) or n.targets[0].id for n in kept} == wanted, \
    f"helpers missing from source: {wanted - {getattr(n,'name',None) or n.targets[0].id for n in kept}}"
ts = types.ModuleType("ts")
ts.statistics = statistics
exec(compile(ast.Module(body=kept, type_ignores=[]), str(SRC), "exec"), ts.__dict__)

# Real gandalf samples, 2026-09-16 (Elrond's 25 at 9:18 AM CDT + mine at 9:56 AM CDT).
NOISY = [99,36,36,91,86,99,25,90,90,83,56,0,0,0,0,16,51,90,99,81,81,99,99,99,99]
CALM  = [86,86,86,86,86,86,86,86,86,83,83,83,83,83,83,83,83,83,83,83,50,50,86,86]


def window(vals):
    return statistics.mean(vals), statistics.pstdev(vals), len(vals)


class GpuUtilWindow(unittest.TestCase):
    def test_honest_reads_of_a_noisy_gauge_stay_silent(self):
        """50 rounds: two honest samples of the same gauge must not FAIL."""
        rng = random.Random(1209)
        fired = 0
        for _ in range(50):
            dash = rng.choice(NOISY)          # dashboard: an honest point read
            inst = [rng.choice(NOISY) for _ in range(12)]
            level, detail = ts.util_matches(dash, window(inst), 20)
            if level == "FAIL":
                fired += 1
        self.assertLessEqual(
            fired, 12,
            f"honest reads of a gauge with sd~37 fired {fired}/50 times; "
            "the check still cannot tell variance from drift")

    def test_genuinely_drifted_dashboard_fires(self):
        """A dashboard pinned far from a calm instrument MUST be caught."""
        level, detail = ts.util_matches(0.0, window(CALM), 20)
        self.assertEqual(level, "FAIL", f"pinned-at-zero dashboard was not caught: {detail}")
        level2, _ = ts.util_matches(100.0, window([0]*12), 20)
        self.assertEqual(level2, "FAIL", "dashboard at 100 vs idle instrument was not caught")

    def test_agreeing_reads_pass(self):
        level, detail = ts.util_matches(84.0, window(CALM), 20)
        self.assertEqual(level, "PASS", f"an agreeing claim was rejected: {detail}")

    def test_unreadable_window_is_not_a_pass(self):
        level, detail = ts.util_matches(50.0, None, 20)
        self.assertEqual(level, "WARN", "a missing window must not report agreement")
        self.assertIn("no sample", detail)

    def test_mutation_tolerance_must_be_noise_aware(self):
        """MUTANT: drop the noise term. The quiet case must then break."""
        def fixed_only(claim, win, floor):
            return abs(float(claim) - win[0]) <= floor
        noisy_win = window(NOISY)
        self.assertNotEqual(
            ts.util_matches(99, noisy_win, 20)[0], "FAIL",
            "noise-aware comparison must not call a sample from its own distribution a drift")
        self.assertFalse(
            fixed_only(99, noisy_win, 20),
            "MUTANT SURVIVED: a fixed 20-point tolerance accepted the same "
            "value, so the noise term is not load-bearing")

    def test_mutation_widening_forever_must_break_detection(self):
        """MUTANT: an unbounded tolerance must fail to catch real drift."""
        def runaway(claim, win, floor):
            return abs(float(claim) - win[0]) <= max(floor, 10 * win[1] + 100)
        self.assertTrue(
            runaway(0.0, window(CALM), 20),
            "control: runaway tolerance does swallow the drift case")
        self.assertEqual(
            ts.util_matches(0.0, window(CALM), 20)[0], "FAIL",
            "MUTANT SURVIVED: the real comparison also swallowed genuine drift")


    def test_indiscriminate_band_warns_instead_of_passing(self):
        """A band wider than half the range must NOT report PASS."""
        noisy = window(NOISY)
        level, detail = ts.util_matches(57.0, noisy, 20)
        self.assertEqual(
            level, "WARN",
            f"a comparison that cannot fail reported {level}, asserting a "
            f"verification it did not perform: {detail}")
        self.assertIn("no verdict possible", detail)

    def test_mutation_removing_the_band_guard_restores_the_false_pass(self):
        """MUTANT: drop the indiscriminate-band check -> the false PASS returns."""
        def without_guard(claim, win, floor):
            mean, sd, n = win
            tol = max(floor, 2 * sd * (1 + 1 / n) ** 0.5)
            return "PASS" if abs(float(claim) - mean) <= tol else "FAIL"
        self.assertEqual(
            without_guard(57.0, window(NOISY), 20), "PASS",
            "control: without the guard this case does report PASS")
        self.assertEqual(
            ts.util_matches(57.0, window(NOISY), 20)[0], "WARN",
            "MUTANT SURVIVED: the guard is not load-bearing")

    def test_calm_gauge_still_gets_a_real_verdict(self):
        """The band guard must not disable the check on a usable gauge."""
        level, detail = ts.util_matches(84.0, window(CALM), 20)
        self.assertEqual(level, "PASS", f"calm gauge lost its verdict: {detail}")
        self.assertNotIn("no verdict", detail)


if __name__ == "__main__":
    unittest.main(verbosity=2)
