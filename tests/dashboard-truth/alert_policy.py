"""Payload-scoped dashboard alert reminders; no sensor or transport dependencies."""
import hashlib
import json
import math

DAY = 86400


def fingerprint(failure):
    payload = {key: failure[key] for key in ("signal", "dashboard", "instrument")}
    instrument = failure["instrument"]
    # Credits-read reporting now labels its expected value honestly. Preserve the
    # existing incident key for that presentation change, without discarding any
    # changed expectation, dashboard claim, or additional measurement evidence.
    if (payload["signal"] == "runson.credits_read"
            and isinstance(instrument, dict)
            and set(instrument) == {"kind", "expected", "independent_aws_measurement"}
            and instrument["kind"] == "expected_contract"
            and instrument["independent_aws_measurement"] is False
            and isinstance(instrument["expected"], dict)
            and set(instrument["expected"]) == {"credits_error"}):
        payload["instrument"] = instrument["expected"]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def plan(failures, prior, dispositions, now, passed_signals=()):
    """Return current incidents and due pages. Only successful delivery advances last_page."""
    evaluated = set(passed_signals) | {failure["signal"] for failure in failures}
    current = {key: value for key, value in prior.items()
               if isinstance(value, dict) and value.get("signal") not in evaluated}
    pages = []
    seen = set()
    for failure in failures:
        key = fingerprint(failure)
        if key in seen:
            continue
        seen.add(key)
        old = prior.get(key, {})
        if not isinstance(old, dict) or not (
            isinstance(old.get("count"), int) and old["count"] > 0
            and isinstance(old.get("first_seen"), (int, float)) and math.isfinite(old["first_seen"])
            and (old.get("last_page") is None or (
                isinstance(old["last_page"], (int, float)) and math.isfinite(old["last_page"])))
        ):
            old = {}
        count = old.get("count", 0) + 1
        first = old.get("first_seen", now)
        incident = {"signal": failure["signal"], "count": count, "first_seen": first, "last_page": old.get("last_page")}
        current[key] = incident
        disposition = dispositions.get(key, {})
        if not isinstance(disposition, dict):
            disposition = {}
        owned = all(isinstance(disposition.get(field), str) and disposition[field].strip()
                    for field in ("owner", "issue"))
        last = incident["last_page"]
        due = last is None or (now - first < DAY and count % 6 == 0) or (now - last >= DAY)
        if due:
            pages.append((key, failure, "routine" if owned else "urgent", disposition))
    return current, pages
