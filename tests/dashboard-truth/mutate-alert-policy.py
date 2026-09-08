#!/usr/bin/env python3
"""Run isolated, named mutants; each must fail the corresponding behavioral assertion."""
from pathlib import Path
import shutil
import subprocess
import tempfile

source = Path(__file__).resolve().parent
mutants = [
    ("repage every observation", "due = last is None or", "due = True or",
     "test_same_failure_twice_does_not_repage_urgent", "identical second failure must not page"),
    ("ignore changed payload", '("signal", "dashboard", "instrument")', '("signal",)',
     "test_changed_payload_pages_immediately", "new payload on same check must immediately page"),
    ("keep owned failures urgent", '"routine" if owned else "urgent"', '"urgent"',
     "test_owned_reminder_carries_ack_and_issue", "'urgent' != 'routine'"),
]
for name, before, after, test, assertion in mutants:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for filename in ("alert_policy.py", "truth_suite.py", "test_alert_policy.py", "dispositions.json", "credits-failure-fixture.json"):
            shutil.copy(source / filename, root / filename)
        policy = root / "alert_policy.py"
        text = policy.read_text()
        assert before in text, name
        policy.write_text(text.replace(before, after, 1))
        result = subprocess.run(["python3", "-m", "unittest", f"test_alert_policy.AlertTests.{test}"],
                                cwd=root, capture_output=True, text=True)
        assert result.returncode != 0 and assertion in result.stderr, (name, result.stderr)
        print(f"KILLED {name}: {test}: {assertion}")
print(f"PASS: {len(mutants)} mutants killed by behavioral assertions")
