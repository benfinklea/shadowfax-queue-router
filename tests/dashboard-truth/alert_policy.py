"""Payload-scoped dashboard alert reminders; no sensor or transport dependencies."""
import hashlib
import json
import math

DAY = 86400


def fingerprint(failure):
    payload = {key: failure[key] for key in ("signal", "dashboard", "instrument")}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def plan(failures, prior, dispositions, now):
    """Return current incidents and due pages. Only successful delivery advances last_page."""
    current, pages = {}, []
    for failure in failures:
        key = fingerprint(failure)
        if key in current:
            continue
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
        incident = {"count": count, "first_seen": first, "last_page": old.get("last_page")}
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
