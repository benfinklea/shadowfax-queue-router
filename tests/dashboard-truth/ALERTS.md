# Dashboard truth alert disposition

Cron failures are keyed by the canonical JSON of signal, dashboard value and
instrument value. A new payload pages immediately at urgent. Identical failures
page at observations 1, 6, 12, ... during their first 24 hours, then at most once
per 24 hours since the last successful delivery. Recovery rearms the check.
A changed payload never inherits another payload's backoff or ownership.
Interactive runs record results but do not change cron notification state.

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
