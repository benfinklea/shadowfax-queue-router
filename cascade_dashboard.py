"""Strict reader for cascade-scraper's atomic dashboard snapshot."""

import json
import os
from typing import Any, Dict


MAX_REPORT_BYTES = 1_000_000


def unavailable(reason: str) -> Dict[str, Any]:
    return {
        "available": False,
        "stale": True,
        "error": reason,
        "tasks": {},
        "tiers": [],
        "links": [],
    }


def load_cascade_report(path: str) -> Dict[str, Any]:
    try:
        if os.path.getsize(path) > MAX_REPORT_BYTES:
            return unavailable("cascade report exceeds size limit")
        with open(path, encoding="utf-8") as handle:
            report = json.load(handle)
    except FileNotFoundError:
        return unavailable("cascade report has not been generated")
    except (OSError, json.JSONDecodeError):
        return unavailable("cascade report is unreadable")
    if not isinstance(report, dict):
        return unavailable("cascade report root is invalid")
    if not isinstance(report.get("tasks"), dict):
        return unavailable("cascade task summary is invalid")
    if not isinstance(report.get("tiers"), list) or not isinstance(report.get("links"), list):
        return unavailable("cascade waterfall is invalid")
    if not isinstance(report.get("stale"), bool):
        return unavailable("cascade freshness is invalid")
    report["available"] = True
    return report
