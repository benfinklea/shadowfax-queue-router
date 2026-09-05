"""The RunsOn budget fuse actually stops spending (council 136bh, Ben 2026-09-05).

Ben's ruling, verbatim: "only use spot units on aws to keep costs down. I am authorizing
$25/day and $500/month until we get this backlog fully completed and we are down to less
than 100 open issues."

WHY THIS FILE EXISTS. Until 2026-09-05 there was a RUNSON_DAILY_BUDGET_USD = 15.0 that
appeared exactly twice in queue_router.py - its definition and the API payload - and the
dashboard drew a bar captioned "$15 fuse" beside it. Nothing ever compared spend against
it. On 2026-09-04 the account spent $38.78, 2.6x that "fuse", and nothing fired, because
there was nothing to fire.

So every case here asks the same question in a different way: IF THE ENFORCEMENT WERE
DELETED, WOULD THIS TEST GO RED? Each carries a KILLS: comment naming the one subject line
that does it, per council 136as. A budget guard whose tests pass with the guard removed is
the exact failure it is meant to prevent.
"""
import unittest
from unittest.mock import patch

import queue_router as qr


class BudgetVerdictTests(unittest.TestCase):
    """The pure decision. No network, so the thresholds themselves are pinned."""

    def test_the_2026_09_04_day_would_have_tripped_it(self):
        """The actual incident as a fixture: $38.78 against a $25 day.

        KILLS: if today >= RUNSON_DAILY_BUDGET_USD:
        """
        state, reason = qr.runson_budget_verdict(38.7795, 42.68)
        self.assertEqual(state, "tripped", f"the 9/4 spend did not trip the fuse: {reason}")
        self.assertIn("daily", reason)

    def test_daily_threshold_is_inclusive_at_the_limit(self):
        """Exactly at the limit is spent, not remaining. A `>` here buys one more day
        of overspend at the exact boundary and reads as correct.

        KILLS: if today >= RUNSON_DAILY_BUDGET_USD:
        """
        self.assertEqual(qr.runson_budget_verdict(25.0, 0.0)[0], "tripped")
        self.assertEqual(qr.runson_budget_verdict(24.99, 0.0)[0], "ok")

    def test_monthly_threshold_trips_independently_of_the_day(self):
        """A quiet day inside a blown month must still stop.

        KILLS: if month >= RUNSON_MONTHLY_BUDGET_USD:
        """
        state, reason = qr.runson_budget_verdict(1.00, 500.0)
        self.assertEqual(state, "tripped", f"a blown month did not trip on a quiet day: {reason}")
        self.assertIn("monthly", reason)
        self.assertEqual(qr.runson_budget_verdict(1.00, 499.99)[0], "ok")

    def test_the_limits_are_bens_authorized_numbers(self):
        """Pinned so a future edit to 15/350 is a visible change, not a silent one.

        KILLS: RUNSON_DAILY_BUDGET_USD = 25.0
        """
        self.assertEqual(qr.RUNSON_DAILY_BUDGET_USD, 25.0)
        self.assertEqual(qr.RUNSON_MONTHLY_BUDGET_USD, 500.0)

    def test_an_unreadable_cost_read_fails_closed(self):
        """Unreadable is not zero, and it is not permission to keep spending.

        This is the whole doctrine of the night in one assertion: continuing to burn
        against a number nobody could read is the mistake that costs money rather than
        time. Same rule as SPEND_ON_UNREADABLE_POOL=false in the lane keeper.

        KILLS: return "unreadable", f"cost read failed ({cost_error}); refusing to spend against an unknown"
        """
        for today, month, err in [
            (None, None, "access_denied"),
            (None, 10.0, None),
            (10.0, None, None),
            (12.0, 20.0, "throttled"),
            ("n/a", 20.0, None),
        ]:
            state, reason = qr.runson_budget_verdict(today, month, err)
            self.assertEqual(
                state, "unreadable",
                f"cost read ({today!r},{month!r},{err!r}) was treated as {state}, not unreadable: {reason}",
            )

    def test_a_healthy_day_is_ok(self):
        """The control. Without it every case above could pass by always tripping,
        which would take CI offline permanently and look like caution."""
        state, reason = qr.runson_budget_verdict(3.90, 42.0)
        self.assertEqual(state, "ok", f"a normal day tripped the fuse: {reason}")


class EnforcementTests(unittest.TestCase):
    """The write. One direction only."""

    def test_a_tripped_verdict_turns_the_gate_off(self):
        """KILLS: json={"name": "RUNSON_GATE_SHARDS", "value": "false"},"""
        calls = []

        class Resp:
            status_code = 204

        def fake_patch(url, **kw):
            calls.append((url, kw.get("json")))
            return Resp()

        with patch.object(qr, "get_gh_ci_token", return_value="t"), \
             patch.object(qr.requests, "patch", side_effect=fake_patch):
            action, detail = qr.runson_enforce_budget("tripped", "over", "on")
        self.assertEqual(action, "tripped", detail)
        self.assertEqual(len(calls), 1, "the gate was not written")
        self.assertIn("RUNSON_GATE_SHARDS", calls[0][0])
        self.assertEqual(calls[0][1]["value"], "false")

    def test_it_can_never_turn_the_gate_on(self):
        """A fuse that re-arms itself is a flapping loop. Turning capacity back on is a
        readiness judgement (136bh ties it to armbrain#7070 deploying), never a spend one.

        KILLS: if verdict_state == "ok":
        """
        with patch.object(qr, "get_gh_ci_token", return_value="t"), \
             patch.object(qr.requests, "patch") as p:
            action, _ = qr.runson_enforce_budget("ok", "within budget", "off")
        self.assertEqual(action, "none")
        p.assert_not_called()

    def test_an_unreadable_verdict_also_trips(self):
        """Fail-closed has to survive the whole path, not just the verdict function.

        KILLS: if verdict_state == "ok":
        """
        with patch.object(qr, "get_gh_ci_token", return_value="t"), \
             patch.object(qr.requests, "patch", return_value=type("R", (), {"status_code": 204})()) as p:
            action, _ = qr.runson_enforce_budget("unreadable", "cost unreadable", "on")
        self.assertEqual(action, "tripped")
        p.assert_called_once()

    def test_it_does_not_rewrite_a_gate_that_is_already_off(self):
        """Idempotent. Otherwise every 30s poll writes the variable again, which burns
        API quota and fills the audit trail with noise that hides the real trip.

        KILLS: if current_gate == "off":
        """
        with patch.object(qr, "get_gh_ci_token", return_value="t"), \
             patch.object(qr.requests, "patch") as p:
            action, _ = qr.runson_enforce_budget("tripped", "over", "off")
        self.assertEqual(action, "already-off")
        p.assert_not_called()

    def test_it_refuses_to_write_blind_when_the_gate_is_unknown(self):
        """An unreadable GATE is different from an unreadable COST. We know we should
        stop, but we cannot see what we would be overwriting, and the read failure is
        already surfaced as gate_shards_error.

        KILLS: if current_gate != "on":
        """
        with patch.object(qr, "get_gh_ci_token", return_value="t"), \
             patch.object(qr.requests, "patch") as p:
            action, _ = qr.runson_enforce_budget("tripped", "over", "unknown")
        self.assertEqual(action, "skipped")
        p.assert_not_called()

    def test_a_failed_write_says_the_gate_was_not_tripped(self):
        """The dangerous lie is reporting success on a write that did not land - someone
        reads "tripped" and stops watching while the money keeps going.

        KILLS: return "failed", f"gate write returned HTTP {response.status_code}; gate NOT tripped"
        """
        with patch.object(qr, "get_gh_ci_token", return_value="t"), \
             patch.object(qr.requests, "patch", return_value=type("R", (), {"status_code": 403})()):
            action, detail = qr.runson_enforce_budget("tripped", "over", "on")
        self.assertEqual(action, "failed")
        self.assertIn("NOT tripped", detail)

    def test_a_raising_write_never_escapes(self):
        """A monitoring endpoint that 500s because a budget write failed has turned a
        cost guard into an outage.

        KILLS: except Exception as exc:
        """
        with patch.object(qr, "get_gh_ci_token", return_value="t"), \
             patch.object(qr.requests, "patch", side_effect=RuntimeError("boom")):
            action, detail = qr.runson_enforce_budget("tripped", "over", "on")
        self.assertEqual(action, "failed")
        self.assertIn("NOT tripped", detail)

    def test_no_token_means_no_silent_success(self):
        """KILLS: return "failed", "GitHub token unavailable; gate NOT tripped\""""
        with patch.object(qr, "get_gh_ci_token", return_value=None):
            action, detail = qr.runson_enforce_budget("tripped", "over", "on")
        self.assertEqual(action, "failed")
        self.assertIn("NOT tripped", detail)


if __name__ == "__main__":
    unittest.main()
