# Dashboard truth alert disposition

Cron failures are keyed by the canonical JSON of signal, dashboard value and
instrument value. A new payload pages immediately at urgent. Identical failures
page at observations 1, 6, 12, ... during their first 24 hours, then at most once
per 24 hours since the last successful delivery. An explicit PASS rearms the check; WARN or missing results retain its incident.
A changed payload never inherits another payload's backoff or ownership.
Interactive runs record results but do not change cron notification state.

For `runson.credits_read`, the exact `expected_contract` wrapper with `independent_aws_measurement: false` and only `expected.credits_error` hashes as the legacy `{credits_error: ...}` instrument value. Labeling the same expectation therefore preserves its incident and ownership. Changed dashboard values or expectations, additional evidence fields, and other signals remain distinct.

`alerts.json` in `/workspace/planning/state/dashboard-truth` stores current
incidents. The lock covers read, send and atomic replacement, including recovery.
Failed delivery remains due on the next run. The old signal-only
`consecutive.json` is intentionally ignored: it cannot identify exact payloads.

To acknowledge a failure, copy its `Fingerprint:` from the inbox message into
`/workspace/planning/state/dashboard-truth/dispositions.json`:

```json
{
  "<fingerprint>": {
    "owner": "Cirdan",
    "issue": "https://github.com/armbrain-io/fleet-planning/issues/611",
    "note": "Diagnosis, prior ack and next action"
  }
}
```

Both a nonempty owner and tracking issue automatically make reminders routine
and stop council notifications. Each reminder includes the owner, issue and ack
note. An owner alone does not downgrade priority. Local dispositions override
the checked-in defaults; an empty object for a fingerprint revokes its default
ownership. The checked-in credits denial disposition records the ownership and
diagnosis provided in fp#611, scoped to that exact payload. This is structured
ack input; arbitrary inbox prose is not interpreted as ownership.

Validation (no live sensors or notifications):

```sh
python3 -m unittest discover -s tests/dashboard-truth -p test_alert_policy.py -v
python3 tests/dashboard-truth/mutate-alert-policy.py
```

Due failures are batched into at most one inbox message per priority and one
council notification per run, retaining every due signal in the message body.
The credits-failure-fixture.json payload was captured from the live last-run.json
on 2026-09-07; its exact fingerprint is tested against the default disposition.
