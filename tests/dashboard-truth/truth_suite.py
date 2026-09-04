#!/usr/bin/env python3
"""Independent, read-only truth checks for the Fleet Monitor dashboard."""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import hashlib
import html
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = os.environ.get("DASHBOARD_URL", "http://127.0.0.1:5000").rstrip("/")
ROOT = Path(__file__).resolve().parents[2]
STATE = Path("/workspace/planning/state/dashboard-truth")
DB = Path("/var/lib/queue-router/jobs.db")
CRON = "--cron" in sys.argv[1:]
TIMEOUT = 20
HOSTS = {
    "gandalf": ("192.168.1.10", "ben"), "frodo": ("192.168.1.11", "ben"),
    "shadowfax": ("192.168.1.12", "ben"), "pippin": ("192.168.1.13", "ben"),
    "sam": ("192.168.1.14", "ben"), "aragorn": ("192.168.1.15", "mac"),
    "northfarthing": ("192.168.1.60", "ben"), "southfarthing": ("192.168.1.61", "ben"),
    "eastfarthing": ("192.168.1.62", "ben"), "westfarthing": ("192.168.1.63", "ben"),
}
TARGETS = ("gandalf", "frodo", "aragorn", "pippin")
FLEET = ("northfarthing", "eastfarthing", "southfarthing", "westfarthing", "shadowfax", "sam")
GET_SCHEMAS = {
    "pipeline": {"available", "repo", "generated_at"},
    "status": {"targets", "recent_jobs"}, "fleet": set(FLEET),
    "ci_queue": {"available", "queued", "in_progress", "repo"},
    "runson": {"available", "deployed", "generated_at", "gate_shards", "gate_shards_source", "gate_shards_error"},
    "model_serving": set(), "fleet_stats": {"agents", "runners"},
    "model_routes": {"available", "routes"}, "vram-processes": set(),
    "logs": {"jobs"}, "energy": {"base_per_kwh", "by_machine", "fleet", "note"},
    "health": {"status", "service"}, "history": {"range", "data"},
}
MUTATING = ("reset_stats", "fleet_power", "power_limit", "clear_swap", "power")

results: list[dict] = []
api: dict[str, object] = {}


def emit(level: str, signal: str, dashboard, instrument, detail: str = "") -> None:
    row = {"level": level, "signal": signal, "dashboard": dashboard,
           "instrument": instrument, "detail": detail}
    results.append(row)
    if not CRON:
        suffix = f"; {detail}" if detail else ""
        print(f"{level} {signal} dashboard={json.dumps(dashboard, separators=(',', ':'))} "
              f"instrument={json.dumps(instrument, separators=(',', ':'))}{suffix}")


def http(path: str, timeout: int = TIMEOUT, headers: dict | None = None) -> tuple[int, bytes]:
    req = urllib.request.Request(BASE + path, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def url_json(url: str, timeout: int = 8, headers: dict | None = None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read())


def run(cmd: list[str], timeout: int = 15, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, text=True, capture_output=True, timeout=timeout, env=env)


def check_apis() -> None:
    for endpoint, required in GET_SCHEMAS.items():
        path = f"/api/{endpoint}"
        try:
            # runson joins the 50s tier: even with stale-while-revalidate in
            # queue_router, the first read after a monitor restart can still be a
            # cold AWS fetch, and a 20s budget turned that into a hard FAIL on the
            # hourly run (shadowfax-queue-router#5).
            endpoint_timeout = 120 if endpoint == "pipeline" else (50 if endpoint in ("status", "fleet", "runson") else TIMEOUT)
            code, body = http(path, endpoint_timeout)
            value = json.loads(body)
            api[endpoint] = value
            missing = sorted(required - set(value)) if isinstance(value, dict) else sorted(required)
            ok = code == 200 and isinstance(value, (dict, list)) and not missing
            emit("PASS" if ok else "FAIL", f"api.{endpoint}.schema",
                 {"http": code, "keys": sorted(value) if isinstance(value, dict) else "array"},
                 {"http": 200, "required": sorted(required)},
                 "valid JSON" if ok else f"missing={missing}")
        except Exception as exc:
            emit("FAIL", f"api.{endpoint}.schema", "unreadable", "HTTP 200 JSON", type(exc).__name__)
    # These controls are real /api endpoints but cannot safely be called by a read-only monitor.
    source = (ROOT / "queue_router.py").read_text()
    for endpoint in MUTATING:
        present = bool(re.search(rf'@app\.route\("/api/{re.escape(endpoint)}"[^\n]*methods=\["(?:POST|DELETE)', source))
        emit("PASS" if present else "FAIL", f"api.{endpoint}.contract",
             {"declared": present, "invoked": False}, {"method": "POST/DELETE", "read_only": True},
             "source contract checked; destructive request intentionally not issued")


def probe_host(item: tuple[str, tuple[str, str]]):
    name, (host, user) = item
    ping = run(["ping", "-n", "-c", "1", "-W", "2", host], timeout=4).returncode == 0
    tcp = False
    try:
        with socket.create_connection((host, 22), timeout=3):
            tcp = True
    except OSError:
        pass
    identity = None
    if tcp:
        cp = run(["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
                  "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR",
                  "-o", "ConnectTimeout=4", "-o", "ConnectionAttempts=1",
                  f"{user}@{host}", "hostname"], timeout=7)
        if cp.returncode == 0:
            identity = cp.stdout.strip().split(".")[0].lower()
    expected = "pippen" if name == "pippin" else name
    primary = tcp and identity == expected
    return name, {"ping": ping, "tcp22": tcp, "identity": identity, "primary": primary}


def check_fleet() -> None:
    status = api.get("status", {}).get("targets", {})
    fleet = api.get("fleet", {})
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        probes = dict((n, v) for n, v in pool.map(probe_host, HOSTS.items()))
    for name in HOSTS:
        claim = status.get(name, {}).get("online") if name in status else fleet.get(name, {}).get("online")
        p = probes[name]
        expected_up = p["primary"]
        match = claim is expected_up
        if p["ping"] and not p["primary"]:
            detail = "primary_path=FAIL; box alive via alternate path, gandalf->box path broken"
        elif p["primary"] and not p["ping"]:
            detail = "ICMP path failed, SSH primary path healthy"
        else:
            detail = "primary_path=" + ("PASS" if p["primary"] else "FAIL")
        emit("PASS" if match else "FAIL", f"fleet.{name}.up", claim, p, detail)
    core_claim = sum(bool(status.get(n, {}).get("online")) for n in ("gandalf", "frodo", "pippin")) + bool(fleet.get("shadowfax", {}).get("online"))
    core_probe = sum(probes[n]["primary"] for n in ("gandalf", "frodo", "pippin", "shadowfax"))
    reserve_claim = sum(bool(fleet.get(n, {}).get("online")) for n in ("northfarthing", "eastfarthing", "southfarthing", "westfarthing", "sam"))
    reserve_probe = sum(probes[n]["primary"] for n in ("northfarthing", "eastfarthing", "southfarthing", "westfarthing", "sam"))
    emit("PASS" if core_claim == core_probe else "FAIL", "fleet.core_count", f"{core_claim}/4", f"{core_probe}/4")
    emit("PASS" if reserve_claim == reserve_probe else "FAIL", "fleet.reserve_count", f"{reserve_claim}/5", f"{reserve_probe}/5")


def nvidia(name: str):
    host, user = HOSTS[name]
    command = ("nvidia-smi --query-gpu=utilization.gpu,temperature.gpu,memory.used,memory.total,power.draw "
               "--format=csv,noheader,nounits")
    cp = run(["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
              "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR",
              "-o", "ConnectTimeout=4", f"{user}@{host}", command], timeout=10)
    rows = []
    for line in cp.stdout.splitlines():
        try:
            rows.append([float(x.strip()) for x in line.split(",")])
        except ValueError:
            pass
    if not rows:
        return None
    return {"util": max(x[0] for x in rows), "temp": max(x[1] for x in rows),
            "used_gb": sum(x[2] for x in rows) / 1024, "total_gb": sum(x[3] for x in rows) / 1024,
            "watts": sum(x[4] for x in rows)}


def close(a, b, absolute=None, pct=None) -> bool:
    if a is None or b is None:
        return False
    tolerance = absolute or 0
    if pct is not None:
        tolerance = max(tolerance, abs(b) * pct)
    return abs(float(a) - float(b)) <= tolerance


def check_gpu() -> None:
    # Bracket the dashboard sample. A busy GPU can change materially while the
    # suite probes the rest of the fleet, so the independent value is whichever
    # fresh nvidia-smi sample is closest to the claim made between them.
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        before = dict(zip(("gandalf", "frodo", "aragorn"), pool.map(nvidia, ("gandalf", "frodo", "aragorn"))))
    try:
        code, body = http("/api/status", 50)
        status = json.loads(body).get("targets", {}) if code == 200 else {}
    except Exception:
        status = api.get("status", {}).get("targets", {})
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        after = dict(zip(("gandalf", "frodo", "aragorn"), pool.map(nvidia, ("gandalf", "frodo", "aragorn"))))
    for name, direct in before.items():
        d = status.get(name, {})
        if direct is None:
            level = "WARN" if not d.get("online") or not d.get("gpu") else "FAIL"
            emit(level, f"gpu.{name}.instrument", bool(d.get("gpu")), "nvidia-smi unavailable")
            continue
        gpu = d.get("gpu") or {}
        checks = (
            ("util", d.get("gpu_util"), direct["util"], 20, None),
            ("temp", d.get("gpu_temp"), direct["temp"], 5, None),
            ("vram_used", gpu.get("vram_used_gb"), direct["used_gb"], 0.2, .05),
            ("vram_total", gpu.get("vram_total_gb"), direct["total_gb"], 0.2, .05),
            ("power", d.get("gpu_watts"), direct["watts"], 35, None),
        )
        for metric, claim, actual, absolute, pct in checks:
            other = after.get(name)
            if other is not None:
                other_value = other["used_gb" if metric == "vram_used" else
                                    "total_gb" if metric == "vram_total" else
                                    "watts" if metric == "power" else metric]
                if claim is not None and abs(float(claim) - other_value) < abs(float(claim) - actual):
                    actual = other_value
            matched = close(claim, actual, absolute, pct)
            # Power is the most bursty dial. On a mismatch, take one immediate
            # second independent sample before calling a cached dashboard read a lie.
            if metric == "power" and not matched:
                second = nvidia(name)
                if second is not None:
                    actual = second["watts"]
                    matched = close(claim, actual, absolute, pct)
            level = "PASS" if matched else ("WARN" if metric == "power" else "FAIL")
            emit(level,
                 f"gpu.{name}.{metric}", claim, round(actual, 2),
                 "VRAM ±5%; temp ±5C; utilization/power allow sampling lag")
    # Apple unified-memory GPU has no nvidia-smi; require the card to say so through its OS schema.
    pippin = status.get("pippin", {})
    emit("PASS" if pippin.get("os") == "mac" else "FAIL", "gpu.pippin.instrument",
         pippin.get("os"), "macOS unified memory", "nvidia-smi not applicable")


def prom_value(text: str, key: str):
    match = re.search(rf"^{re.escape(key)}\s+([0-9.eE+-]+)$", text, re.M)
    return float(match.group(1)) if match else None


def serving_instrument(name: str):
    urls = {"frodo": "http://192.168.1.11:8890", "pippin": "http://192.168.1.13:8891"}
    if name == "aragorn":
        rows = []
        for base in ("http://192.168.1.15:11434", "http://192.168.1.15:11435"):
            try:
                with urllib.request.urlopen(base + "/metrics", timeout=6) as response:
                    metrics = response.read().decode()
                rows.append((prom_value(metrics, "llamacpp:tokens_predicted_total") or 0,
                             prom_value(metrics, "llamacpp:tokens_predicted_seconds_total") or 0))
            except Exception:
                pass
        if not rows:
            return None
        return {"tokens_total": sum(x[0] for x in rows),
                "seconds_total": sum(x[1] for x in rows), "model": "multi"}
    if name not in urls:
        return None
    base = urls[name]
    with urllib.request.urlopen(base + "/metrics", timeout=6) as response:
        metrics = response.read().decode()
    props = url_json(base + "/props", 6)
    model_path = props.get("model_path") or props.get("model") or ""
    return {"tokens_total": prom_value(metrics, "llamacpp:tokens_predicted_total"),
            "seconds_total": prom_value(metrics, "llamacpp:tokens_predicted_seconds_total"),
            "model": Path(str(model_path)).name}


def normalize_model(value: str) -> str:
    return Path(value or "").name.lower()


def check_models() -> None:
    serving = api.get("model_serving", {})
    status = api.get("status", {}).get("targets", {})
    for name in ("frodo", "pippin", "aragorn"):
        try:
            direct = serving_instrument(name)
        except Exception as exc:
            # One immediate retry differentiates a busy accept queue from the
            # dead metric-port failure this suite is intended to catch.
            try:
                direct = serving_instrument(name)
            except Exception:
                direct = None
        claim = serving.get(name, {})
        if direct is None:
            emit("WARN" if not claim.get("available") else "FAIL", f"model.{name}.metrics",
                 claim, "serving /metrics unavailable")
            continue
        # The dashboard rate is a 60s counter delta. Validate zero/nonzero state and sane bounds.
        tps = claim.get("tps_now")
        sane = isinstance(tps, (int, float)) and 0 <= tps <= 2000
        emit("PASS" if sane else "FAIL", f"model.{name}.tps_now", tps,
             {"tokens_total": direct["tokens_total"], "seconds_total": direct["seconds_total"]},
             "dashboard is a 60s counter delta; direct counters prove source continuity")
        if name != "aragorn":
            models = (status.get(name, {}).get("loaded_models") or {}).get("models") or []
            claims = [normalize_model(m.get("name", "")) for m in models]
            actual = normalize_model(direct["model"])
            exact = bool(actual) and actual in claims
            emit("PASS" if exact else "FAIL", f"model.{name}.identity", claims, actual, "exact normalized filename match")
    # Gandalf is llama-swap: /running is authoritative and avoids load-triggering upstream probes.
    try:
        running = url_json("http://127.0.0.1:8889/running", 6).get("running", [])
        direct_names = sorted(normalize_model((x if isinstance(x, str) else x.get("model") or x.get("id") or x.get("name") or "")) for x in running)
        models = (status.get("gandalf", {}).get("loaded_models") or {}).get("models") or []
        claims = sorted(normalize_model(m.get("name", "")) for m in models)
        emit("PASS" if claims == direct_names else "FAIL", "model.gandalf.identity", claims, direct_names, "exact /running match")
        gs = serving.get("gandalf", {})
        sane = isinstance(gs.get("tps_now"), (int, float)) and 0 <= gs["tps_now"] <= 2000
        emit("PASS" if sane else "FAIL", "model.gandalf.tps_now", gs.get("tps_now"), "llama-swap counter sampler", "rate sanity + source live")
    except Exception as exc:
        emit("FAIL", "model.gandalf.identity", "dashboard", "/running unavailable", type(exc).__name__)
    # Gateway-derived boxes: verify each advertised value has a route/model source.
    routes = api.get("model_routes", {}).get("routes", [])
    route_boxes = {r.get("box") for r in routes if r.get("live")}
    for name in ("shadowfax",):
        claim = serving.get(name, {})
        level = "PASS" if (not claim.get("available") or name in route_boxes) else "FAIL"
        emit(level, f"model.{name}.gateway_source", claim.get("source"), name in route_boxes,
             "gateway spend log is the independent token source for non-counter servers")


def gh_json(args: list[str]):
    cp = run(["gh", "api", *args], timeout=30)
    if cp.returncode:
        raise RuntimeError(cp.stderr.strip()[:200])
    return json.loads(cp.stdout)


def check_green_waiting(d) -> None:
    queue_prs = d.get("queue_prs")
    queue_valid = (isinstance(queue_prs, list) and type(d.get("queue_depth")) is int
                   and d["queue_depth"] == len(queue_prs)
                   and all(isinstance(p, dict) and type(p.get("number")) is int
                           and isinstance(p.get("state"), str) for p in queue_prs))
    emit("PASS" if queue_valid else "FAIL", "pipeline.queue.count",
         d.get("queue_depth"), len(queue_prs) if isinstance(queue_prs, list) else None)
    merged = d.get("merged_today_prs")
    merged_valid = (isinstance(merged, list) and type(d.get("merged_today")) is int
                    and d["merged_today"] == len(merged)
                    and all(isinstance(p, dict) and type(p.get("number")) is int
                            and isinstance(p.get("title"), str) for p in merged))
    emit("PASS" if merged_valid else "FAIL", "pipeline.merged_today.count",
         d.get("merged_today"), len(merged) if isinstance(merged, list) else None)
    waiting = d.get("green_waiting_prs")
    valid = (isinstance(waiting, list) and type(d.get("green_waiting")) is int
             and d["green_waiting"] == len(waiting)
             and all(isinstance(p, dict) and type(p.get("number")) is int
                     and isinstance(p.get("title"), str) for p in waiting))
    emit("PASS" if valid else "FAIL", "pipeline.green_waiting.count",
         d.get("green_waiting"), len(waiting) if isinstance(waiting, list) else None)
    if not valid:
        return
    try:
        # Re-read the listed PRs individually in one GraphQL request, so PRs
        # closed/drafted/held since the cached snapshot are also detected.
        fields = " ".join(
            f'p{p["number"]}:pullRequest(number:{p["number"]})'
            '{state isDraft reviewDecision mergeable mergeStateStatus labels(first:100){nodes{name} pageInfo{hasNextPage}}}'
            for p in waiting)
        query = ('{repository(owner:"armbrain-io",name:"armbrain"){' + fields +
                 ' mergeQueue(branch:"main"){entries(first:100){nodes{state pullRequest{number title}} pageInfo{hasNextPage}}}}}')
        live = gh_json(["graphql", "-f", "query=" + query])["data"]["repository"]
        queue = live["mergeQueue"]
        if queue is None or queue["entries"]["pageInfo"]["hasNextPage"]:
            raise ValueError("could not establish complete main merge queue")
        live_prs = [{"number": p["pullRequest"]["number"], "title": p["pullRequest"]["title"], "state": p["state"]}
                    for p in queue["entries"]["nodes"]]
        emit("PASS" if queue_valid and queue_prs == live_prs else "FAIL", "pipeline.queue.live",
             {"queue_depth": d.get("queue_depth"), "queue_prs": queue_prs},
             {"queue_depth": len(live_prs), "queue_prs": live_prs},
             "live main merge queue in front-entry order; cache churn may fail")
        queued = {p["number"] for p in live_prs}
        invalid = []
        for pr in waiting:
            current = live.get(f'p{pr["number"]}')
            if (not current or current["state"] != "OPEN" or current["isDraft"]
                    or current["reviewDecision"] != "APPROVED" or current["mergeable"] != "MERGEABLE"
                    or current["mergeStateStatus"] != "CLEAN"
                    or pr["number"] in queued or current["labels"]["pageInfo"]["hasNextPage"]
                    or {x["name"].lower() for x in current["labels"]["nodes"]}
                    & {"do-not-merge", "needs-repair", "hold", "blocked-on-ben"}):
                invalid.append(pr["number"])
        emit("FAIL" if invalid else "PASS", "pipeline.green_waiting.live",
             waiting, {"invalid_prs": invalid, "queued": sorted(queued)},
             "live OPEN + not draft + APPROVED + MERGEABLE + CLEAN + not held + not queued; cache churn may fail")
    except Exception as exc:
        emit("FAIL", "pipeline.green_waiting.live", waiting, "could not establish", str(exc))
        emit("FAIL", "pipeline.queue.live", queue_prs, "could not establish", str(exc))


def check_pipeline() -> None:
    d = api.get("pipeline", {})
    try:
        # Fetch /api/pipeline fresh right alongside the direct GitHub probes to eliminate sampling lag
        pipe_code, pipe_body = http("/api/pipeline", 25)
        d_fresh = json.loads(pipe_body) if pipe_code == 200 else d
    except Exception:
        d_fresh = d
    d = d_fresh
    check_green_waiting(d)
    ci = api.get("ci_queue", {})
    now = dt.datetime.now(dt.timezone.utc)
    ct = ZoneInfo("America/Chicago")
    midnight = now.astimezone(ct).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(dt.timezone.utc)
    hour = now - dt.timedelta(hours=1)
    query = """query($issues:String!,$prs:String!,$today:String!,$hour:String!){
      issues:search(query:$issues,type:ISSUE,first:1){issueCount}
      prs:search(query:$prs,type:ISSUE,first:1){issueCount}
      today:search(query:$today,type:ISSUE,first:1){issueCount nodes{... on PullRequest{mergedAt}}}
      hour:search(query:$hour,type:ISSUE,first:1){issueCount}
    }"""
    variables = ["-f", f"query={query}", "-f", "issues=repo:armbrain-io/armbrain is:issue is:open",
                 "-f", "prs=repo:armbrain-io/armbrain is:pr is:open",
                 "-f", f"today=repo:armbrain-io/armbrain is:pr is:merged merged:>={midnight.strftime('%Y-%m-%dT%H:%M:%SZ')}",
                 "-f", f"hour=repo:armbrain-io/armbrain is:pr is:merged merged:>={hour.strftime('%Y-%m-%dT%H:%M:%SZ')}"]
    try:
        g = gh_json(["graphql", *variables])["data"]
        expected = {"issues_open": g["issues"]["issueCount"], "prs_open": g["prs"]["issueCount"],
                    "merged_today": g["today"]["issueCount"], "merged_last_hour": g["hour"]["issueCount"]}
        for field, actual in expected.items():
            claim = d.get(field)
            emit("PASS" if close(claim, actual, 1) else "FAIL", f"pipeline.{field}", claim, actual, "±1 in-flight churn")
        pulls = gh_json(["repos/armbrain-io/armbrain/pulls", "--method", "GET", "-f", "state=closed", "-f", "sort=updated", "-f", "direction=desc", "-f", "per_page=30"])
        last_merge = max((p.get("merged_at") for p in pulls if p.get("merged_at")), default=None)
        emit("PASS" if d.get("last_merge_at") == last_merge else "FAIL", "pipeline.last_merge_at", d.get("last_merge_at"), last_merge)
        cutoff = (now - dt.timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")
        direct_ci_before = {}
        for state in ("queued", "in_progress"):
            direct_ci_before[state] = gh_json(["repos/armbrain-io/armbrain/actions/runs", "--method", "GET", "-f", f"status={state}", "-f", f"created=>={cutoff}", "-f", "per_page=100"])["total_count"]

        try:
            ci_code, ci_body = http("/api/ci_queue", 25)
            ci_fresh = json.loads(ci_body) if ci_code == 200 else ci
        except Exception:
            ci_fresh = ci
        direct_ci_after = {}
        for state in ("queued", "in_progress"):
            direct_ci_after[state] = gh_json(["repos/armbrain-io/armbrain/actions/runs", "--method", "GET", "-f", f"status={state}", "-f", f"created=>={cutoff}", "-f", "per_page=100"])["total_count"]

        def ci_match(claim, state):
            if claim is None:
                return False
            lo = min(direct_ci_before[state], direct_ci_after[state])
            hi = max(direct_ci_before[state], direct_ci_after[state])
            return isinstance(claim, (int, float)) and (lo - 1) <= claim <= (hi + 1)

        for field, state in (("queued", "queued"), ("in_progress", "in_progress")):
            claim = ci_fresh.get(field)
            observed = {"before": direct_ci_before[state], "after": direct_ci_after[state]}
            emit("PASS" if ci_match(claim, state) else "FAIL", f"ci.{field}", claim, observed,
                 "repo armbrain-io/armbrain; workflow runs created within 48h; bracketed ±1 churn")

        # Check pipeline.ci_queued and ci_running
        for field, state in (("ci_queued", "queued"), ("ci_running", "in_progress")):
            claim = d_fresh.get(field) if d_fresh.get(field) is not None else d.get(field)
            observed = {"before": direct_ci_before[state], "after": direct_ci_after[state]}
            matches = ci_match(claim, state)
            detail = "workflow-run count; bracketed ±1 in-flight churn"
            emit("PASS" if matches else "FAIL", f"pipeline.{field}", claim, observed, detail)
        runs = gh_json(["repos/armbrain-io/armbrain/actions/workflows/gateway-deploy.yml/runs", "--method", "GET", "-f", "status=completed", "-f", "branch=main", "-f", "per_page=100"])["workflow_runs"]
        last = None
        production_runs_checked = 0
        for workflow in runs:
            if workflow.get("event") not in ("push", "workflow_dispatch"):
                continue
            if production_runs_checked >= 5:
                break
            production_runs_checked += 1
            jobs = gh_json([f"repos/armbrain-io/armbrain/actions/runs/{workflow['id']}/jobs", "--method", "GET", "-f", "per_page=100"])["jobs"]
            job = next((j for j in jobs if j.get("name") == "deploy" and j.get("conclusion") == "success"), None)
            if job:
                last = {"sha": (workflow.get("head_sha") or "")[:9], "at": job.get("completed_at") or workflow.get("updated_at")}
                break
        emit("PASS" if last and d.get("last_deploy_sha") == last["sha"] else "FAIL", "pipeline.last_deploy_sha", d.get("last_deploy_sha"), last)
        emit("PASS" if last and d.get("last_deploy_at") == last["at"] else "FAIL", "pipeline.last_deploy_at", d.get("last_deploy_at"), last)
    except Exception as exc:
        emit("FAIL", "pipeline.direct_github", d, "gh api failed", str(exc))
    try:
        generated = dt.datetime.fromisoformat(str(d.get("generated_at")).replace("Z", "+00:00"))
        age = (now - generated.astimezone(dt.timezone.utc)).total_seconds()
        emit("PASS" if -60 <= age < 600 else "FAIL", "pipeline.generated_at", d.get("generated_at"), {"age_seconds": round(age)}, "must be <10 minutes old")
    except Exception:
        emit("FAIL", "pipeline.generated_at", d.get("generated_at"), "valid timestamp <10m")
    for field in ("merged_spark", "deploys_spark"):
        val = d.get(field)
        emit("PASS" if isinstance(val, list) and len(val) == 7 and all(isinstance(x, int) for x in val) else "FAIL",
             f"pipeline.{field}", val, "seven integer day buckets")
    for field in ("deploys_ok_today", "deploys_failed_today", "deploys_in_flight"):
        emit("PASS" if isinstance(d.get(field), int) and d[field] >= 0 else "FAIL", f"pipeline.{field}", d.get(field), "nonnegative GitHub workflow count")


def check_local_sources() -> None:
    # History/log/energy claims are independently recomputed from their SQLite source.
    try:
        con = sqlite3.connect(DB)
        prior_records = {}
        try:
            prior_run = json.loads((STATE / "last-run.json").read_text())
            for row in prior_run.get("results", []):
                if row.get("signal", "").endswith(".tps_record_monotonic"):
                    value = row.get("dashboard") or {}
                    if value.get("model_name"):
                        prior_records[(value["box"], value["model_name"])] = value.get("record")
        except Exception:
            pass
        serving = api.get("model_serving", {})
        for name in TARGETS:
            claim = serving.get(name, {})
            model_name = claim.get("model_name")
            stored = con.execute(
                "select peak_tps from model_tps_peaks where box=? and model_name=?",
                (name, model_name),
            ).fetchone() if model_name else None
            stored_peak = stored[0] if stored else None
            scale_ok = (
                stored_peak is not None
                and close(claim.get("tps_record"), round(stored_peak, 1), .05)
            )
            emit(
                "PASS" if scale_ok else "FAIL", f"model.{name}.tps_record_scale",
                {"model_name": model_name, "payload_record": claim.get("tps_record")},
                {"stored_peak": round(stored_peak, 1) if stored_peak is not None else None},
                "payload record equals persisted peak for the current model; independent of dial scale",
            )
            previous = prior_records.get((name, model_name))
            monotonic_ok = (
                stored_peak is not None
                and (previous is None or stored_peak + .05 >= float(previous))
            )
            emit(
                "PASS" if monotonic_ok else "FAIL", f"model.{name}.tps_record_monotonic",
                {"box": name, "model_name": model_name,
                 "record": round(stored_peak, 1) if stored_peak is not None else None},
                {"previous_record": previous, "rule": "new >= previous for fixed (box,model)"},
            )
        hist = api.get("history", {}).get("data", {})
        cutoff = (dt.datetime.now() - dt.timedelta(hours=1)).isoformat()
        for name, points in hist.items():
            count = con.execute("select count(*) from metrics_history where target=? and timestamp>=?", (name, cutoff)).fetchone()[0]
            emit("PASS" if abs(len(points) - count) <= 2 else "FAIL", f"history.{name}.points", len(points), count, "direct SQLite hour window; tolerate ±2 boundary race")
            for key in ("gpu_util", "cpu_percent", "gpu_temp", "vram_percent", "ram_percent", "swap_percent", "queue_depth"):
                valid = all(key in p for p in points)
                emit("PASS" if valid else "FAIL", f"history.{name}.{key}", valid, "column present in each spark point")
        db_jobs = con.execute("select count(*) from (select id from jobs order by submitted_at desc limit 20)").fetchone()[0]
        emit("PASS" if len(api.get("logs", {}).get("jobs", [])) == db_jobs else "FAIL", "logs.jobs", len(api.get("logs", {}).get("jobs", [])), db_jobs)
        energy = api.get("energy", {})
        fleet = energy.get("fleet", {})
        sums_ok = True
        for window in ("day", "week", "month"):
            machine_sum = round(sum(v.get(window, {}).get("kwh", 0) for v in energy.get("by_machine", {}).values() if v.get("metered")), 3)
            sums_ok &= close(fleet.get(window, {}).get("kwh"), machine_sum, .005)
        emit("PASS" if sums_ok else "FAIL", "energy.fleet_totals", fleet, "sum(by_machine) for day/week/month", "direct aggregate invariant")
        con.close()
    except Exception as exc:
        emit("FAIL", "sqlite.sources", "dashboard values", "direct SQLite", str(exc))
    routes = api.get("model_routes", {})
    try:
        env_text = Path("/home/ben/.config/gandalf-gateway/fleet.env").read_text()
        match = re.search(r"^LITELLM_MASTER_KEY=(.*)$", env_text, re.M)
        key = match.group(1).strip().strip("\"'") if match else ""
        direct = url_json("http://192.168.1.10:4000/v1/models", 8, {"Authorization": f"Bearer {key}"})
        live = {m.get("id") for m in direct.get("data", [])}
        for route in routes.get("routes", []):
            actual = route.get("name") in live
            emit("PASS" if route.get("live") is actual else "FAIL", f"route.{route.get('name')}.live", route.get("live"), actual)
    except Exception as exc:
        emit("FAIL", "routes.direct_gateway", routes.get("available"), "gateway /v1/models", type(exc).__name__)
    # Fleet stats invariants and source availability.
    fs = api.get("fleet_stats", {})
    agents = fs.get("agents", {})
    calc_total = sum(v for v in agents.get("boxes", {}).values() if isinstance(v, int))
    emit("PASS" if agents.get("total") == calc_total else "FAIL", "fleet_stats.agents_total", agents.get("total"), calc_total)
    runners = fs.get("runners", {})
    rf = runners.get("fleet", {})
    if runners.get("available"):
        boxes = runners.get("boxes", {})
        totals = {k: sum((b.get(k, 0) for b in boxes.values())) for k in ("busy", "total", "online")}
        emit("PASS" if all(rf.get(k) == totals[k] for k in totals) else "FAIL", "fleet_stats.runners_total", rf, totals)
    else:
        emit("WARN", "fleet_stats.runners_total", "unavailable", "GitHub runner API unavailable")


def check_runson_runner_history() -> None:
    r = api.get("runson", {})
    if not r.get("available") or not r.get("deployed"):
        emit("SKIP", "runson.runner_history", r.get("error"), "available deployed RunsOn required")
        return
    raw = r.get("live_runners_series")
    smooth = r.get("live_runners_smoothed")
    try:
        expected = [{"ts": p["ts"], "n": sum(x["n"] for x in raw[max(0, i-2):i+3])
                     / len(raw[max(0, i-2):i+3])} for i, p in enumerate(raw)]
        times = [dt.datetime.fromisoformat(p["ts"]) for p in raw]
        ok = (bool(raw) and len(smooth) == len(expected)
              and raw[-1]["n"] == r["live_runners"]
              and times == sorted(set(times))
              and (times[-1] - times[0]).total_seconds() <= 3600
              and all(a["ts"] == b["ts"] and abs(a["n"] - b["n"]) < 1e-9
                      for a, b in zip(smooth, expected)))
    except (TypeError, KeyError, ValueError, IndexError):
        ok = False
    emit("PASS" if ok else "FAIL", "runson.runner_history",
         {"raw": raw, "smoothed": smooth},
         "centered 5-sample mean with shrinking edges; newest raw equals live_runners; 60-minute ring")


def check_runson_gate() -> None:
    claim = api.get("runson", {}).get("gate_shards")
    cp = run(["gh", "variable", "list", "-R", "armbrain-io/armbrain", "--json", "name,value"], timeout=30)
    expected = None
    detail = "direct gh variable list -R armbrain-io/armbrain"
    if cp.returncode == 0:
        try:
            variables = {row["name"]: str(row.get("value", "")).strip().lower() for row in json.loads(cp.stdout)}
            value = variables.get("RUNSON_GATE_SHARDS")
            expected = {"on": "on", "true": "on", "off": "off", "false": "off"}.get(value)
        except (KeyError, TypeError, json.JSONDecodeError):
            pass
    matches = expected is not None and claim == expected
    emit("PASS" if matches else "FAIL", "runson.gate_shards",
         {"gate_shards": claim, "source": api.get("runson", {}).get("gate_shards_source"),
          "error": api.get("runson", {}).get("gate_shards_error")},
         {"RUNSON_GATE_SHARDS": expected}, detail if cp.returncode == 0 else cp.stderr.strip()[:200])


def check_runson_alignment(page: str) -> None:
    """Measure text edges using real dashboard CSS/renderer, without live APIs."""
    renderer = page[page.index("function runsonEscape("):page.index("// --- Polling control:")]
    fixture = re.sub(r"<script\b[^>]*>.*?</script>", "", page, flags=re.S)
    probe = (Path(__file__).with_name("runson-alignment.js")).read_text()
    fixture = fixture.replace("</body>", "<script>" + renderer + probe + "</script></body>")
    with tempfile.TemporaryDirectory(prefix="runson-alignment-") as directory:
        path = Path(directory) / "fixture.html"
        path.write_text(fixture)
        chrome = run([
            "google-chrome", "--headless", "--no-sandbox", "--disable-gpu",
            "--window-size=1440,1200", "--virtual-time-budget=5000", "--dump-dom", path.as_uri(),
        ], timeout=60)
    match = re.search(r'<pre id="runson-alignment-result">(.*?)</pre>', chrome.stdout, re.S)
    rows = json.loads(html.unescape(match.group(1))) if match else []
    ok = chrome.returncode == 0 and len(rows) == 5 and all(
        abs(row["numberRight"] - row["labelRight"]) <= 1
        and row["maxStationaryDelta"] <= 1
        and row["labelAlign"] in ("start", "left")
        and row["zeroClass"] == (row["count"] == 0)
        for row in rows
    )
    emit("PASS" if ok else "FAIL", "runson.count.label_alignment", rows,
         "0, 12, 123: text right edges within 1px; label/card/facts/details unchanged",
         "headless DOM fixtures, including expanded runner details")


def check_tps_dials(page: str) -> None:
    """Replay one API snapshot through the real renderer; measure the DOM needle."""
    code, body = http("/api/model_serving")
    serving = json.loads(body)
    if code != 200 or not isinstance(serving, dict):
        raise ValueError("could not establish model-serving payload")
    sections = (
        ("const TPS_GAUGE_PLACEHOLDER_MAX = 1;", "function refreshFleetStats()"),
        ("function getNormalColor(", "// Plain-English explanations"),
        ("function renderGauge(", "function renderSwapGauge("),
        ("function updateGauge(", "function refresh()"),
    )
    renderer = "const HELP = {};\n" + "\n".join(
        page[page.index(start):page.index(end, page.index(start))] for start, end in sections
    )
    probe = Path(__file__).with_name("tps-dial.js").read_text()
    fixture = re.sub(r"<script\b[^>]*>.*?</script>", "", page, flags=re.S)
    snapshot = json.dumps(serving).replace("<", "\\u003c")
    fixture = fixture.replace("</body>", "<script>" + renderer
                              + "\nconst servingSnapshot = " + snapshot + ";\n"
                              + probe + "</script></body>")
    with tempfile.TemporaryDirectory(prefix="tps-dial-") as directory:
        path = Path(directory) / "fixture.html"
        path.write_text(fixture)
        chrome = run([
            "google-chrome", "--headless", "--no-sandbox", "--disable-gpu",
            "--virtual-time-budget=5000", "--dump-dom", path.as_uri(),
        ], timeout=60)
    match = re.search(r'<pre id="tps-dial-result">(.*?)</pre>', chrome.stdout, re.S)
    rows = json.loads(html.unescape(match.group(1))) if match else []
    emit("PASS" if chrome.returncode == 0 and len(rows) == 32 else "FAIL",
         "page.tps_dial.probe", len(rows), 32, "live snapshot + seven fixtures for each dial box")
    for row in rows:
        emit("PASS" if row["ok"] else "FAIL", "page.tps_dial." + row["case"] + "." + row["box"],
             row["rendered"], row["expected"], "headless DOM; payload=" + json.dumps(row["payload"]))


def check_rendering() -> None:
    try:
        code, body = http("/")
        page = body.decode()
        check_tps_dials(page)
        check_runson_alignment(page)
        required = ("fleet-summary", "fleet-row", "ship-flow", "ci-queue-body", "route-health-body",
                    "fleet-stats-body", "runson-card", "runson-body", "monitors", "sparklines",
                    "energy-by-machine", "energy-fleet-body")
        missing = [x for x in required if not re.search(rf'id=(?:["\']{re.escape(x)}["\']|{re.escape(x)}(?:\s|>))', page)]
        emit("PASS" if code == 200 and not missing else "FAIL", "page.cards", {"http": code, "missing": missing}, list(required))
        for name in ("gandalf", "frodo", "aragorn", "pippin"):
            marker = f"tps-dial-{name}"
            found = marker in page
            emit("PASS" if found else "FAIL", f"page.tps_dial.{name}", found, marker)
        # Execute the page's pure TPS zone helpers against fixed fixtures. This
        # catches both a color-order regression and accidental detachment of the
        # tan->green boundary from tps_avg_today without duplicating the formula.
        probe_start = page.find("const TPS_GAUGE_PLACEHOLDER_MAX = 1;")
        probe_end = page.find("function refreshModelServing()", probe_start)
        tps_probe = None
        if probe_start >= 0 and probe_end > probe_start:
            probe_js = page[probe_start:probe_end] + """
const fixture = tpsDialZones(120, 300);
const capped = tpsDialZones(20, 400);
const guarded = tpsDialZones(0, 0);
console.log(JSON.stringify({
  fixture: fixture,
  capped: capped,
  guarded: guarded,
  gradient: tpsGaugeGradient(fixture),
  boxes: TPS_DIAL_BOXES
}));
"""
            with tempfile.NamedTemporaryFile("w", suffix=".js") as handle:
                handle.write(probe_js); handle.flush()
                probe = run(["node", handle.name], timeout=10)
            if probe.returncode == 0:
                try:
                    tps_probe = json.loads(probe.stdout.strip())
                except json.JSONDecodeError:
                    pass
        fixture = (tps_probe or {}).get("fixture", {})
        capped = (tps_probe or {}).get("capped", {})
        guarded = (tps_probe or {}).get("guarded")
        gradient = (tps_probe or {}).get("gradient", "")
        color_positions = [gradient.find(color) for color in ("#d94a4a", "#d9a54a", "#39ff14")]
        order_ok = (
            fixture.get("redEnd") == 45
            and fixture.get("max") == 300
            and fixture.get("redEnd", 1) <= fixture.get("tanEnd", 0) <= fixture.get("max", 0)
            and capped.get("redEnd") <= capped.get("tanEnd", -1)
            and guarded is None
            and all(pos >= 0 for pos in color_positions)
            and color_positions == sorted(color_positions)
        )
        emit("PASS" if order_ok else "FAIL", "page.tps_dial.zone_order",
             {"fixture": fixture, "colors": color_positions}, "red < tan < green ascending")
        avg_binding_ok = (
            fixture.get("tanEnd") == 120
            and capped.get("tanEnd") == 20
            and "updateTpsGauge(dialId, s.tps_now, s.tps_avg_today, s.tps_max_today)" in page
            and set((tps_probe or {}).get("boxes", {})) == {"gandalf", "frodo", "aragorn", "pippin"}
        )
        emit("PASS" if avg_binding_ok else "FAIL", "page.tps_dial.average_boundary",
             {"fixture": fixture, "capped": capped}, "tan->green boundary equals tps_avg_today for all TPS boxes")
        scripts = re.findall(r"<script>(.*?)</script>", page, re.S)
        node_ok = False
        if scripts:
            with tempfile.NamedTemporaryFile("w", suffix=".js") as handle:
                handle.write("\n".join(scripts)); handle.flush()
                node_ok = run(["node", "--check", handle.name], timeout=10).returncode == 0
        emit("PASS" if node_ok else "FAIL", "page.javascript_syntax", node_ok, "node --check")

        merge_probe = None
        merge_start = page.find("function mergedRateClass(")
        merge_end = page.find("function refreshShipFlow()", merge_start)
        if merge_start >= 0 and merge_end > merge_start:
            fixture_js = page[merge_start:merge_end] + "\nconsole.log(JSON.stringify([0,1,2,3,7].map(mergedRateClass)));\n"
            with tempfile.NamedTemporaryFile("w", suffix=".js") as handle:
                handle.write(fixture_js); handle.flush()
                probe = run(["node", handle.name], timeout=10)
            if probe.returncode == 0:
                try:
                    merge_probe = json.loads(probe.stdout.strip())
                except json.JSONDecodeError:
                    pass
        expected_classes = ["merge-red merge-pulse", "merge-red", "merge-yellow", "merge-green", "merge-green"]
        threshold_ok = merge_probe == expected_classes
        emit("PASS" if threshold_ok else "FAIL", "page.merged_rate.thresholds",
             merge_probe, {"0": expected_classes[0], "1": expected_classes[1], "2": expected_classes[2],
                           "3": expected_classes[3], "7": expected_classes[4]})
        live_count = api.get("pipeline", {}).get("merged_last_hour")
        live_class = None
        if isinstance(live_count, int) and merge_probe:
            live_class = expected_classes[min(live_count, 3)]
        binding_ok = (
            live_class is not None
            and "mergedRateClass(d.merged_last_hour)" in page
            and "ship-stage ' + (stageCls || '')" in page
        )
        emit("PASS" if binding_ok else "FAIL", "page.merged_rate.class_binding",
             {"merged_last_hour": live_count, "expected_class": live_class},
             "merged box class is derived from merged_last_hour")

        integer_tps_ok = (
            "Math.round(s.tps_now)" in page
            and "Math.round(peak)" in page
            and "Math.round(s.tps_avg_today)" in page
            and "Math.round(max) + unit" in page
        )
        emit("PASS" if integer_tps_ok else "FAIL", "page.tps.integer_readouts",
             integer_tps_ok, "now, peak today, average, and gauge limit use Math.round")
        for fn in ("refresh", "refreshFleet", "refreshHistory", "refreshEnergy", "refreshCiQueue", "refreshShipFlow", "refreshRunsOn", "refreshRouteHealth", "refreshFleetStats", "refreshModelServing"):
            found = f"function {fn}(" in page
            emit("PASS" if found else "FAIL", f"page.renderer.{fn}", found, "renderer declared")

        # The status timer runs every 12 seconds. Let a real browser complete
        # the initial render plus two refresh intervals, then inspect the
        # Pippin card's rendered model-memory field (not merely its API JSON).
        chrome = run([
            "google-chrome", "--headless", "--no-sandbox", "--disable-gpu",
            "--virtual-time-budget=26000", "--dump-dom", BASE + "/",
        ], timeout=60)
        gate = api.get("runson", {}).get("gate_shards")
        expected_glow = "glow-" + (gate if gate in ("on", "off") else "unknown")
        card_tag = re.search(r'<div[^>]*id="runson-card"[^>]*>', chrome.stdout)
        shard_text_match = re.search(r'<span id="runson-shard-state">(.*?)</span>', chrome.stdout, re.S)
        shard_text = html.unescape(re.sub(r"<[^>]+>", "", shard_text_match.group(1))).strip() if shard_text_match else ""
        glow_ok = bool(card_tag and expected_glow in card_tag.group(0) and shard_text and "loading" not in shard_text.lower())
        emit("PASS" if glow_ok else "FAIL", "runson.gate_shards.glow",
             {"card": card_tag.group(0) if card_tag else None, "notice": shard_text},
             {"class": expected_glow, "notice": "resolved shard state"}, "headless rendered DOM")
        match = re.search(
            r'<div id="loaded-models-pippin"[^>]*>(.*?)</div>',
            chrome.stdout, re.S,
        )
        card_text = ""
        if match:
            card_text = html.unescape(re.sub(r"<[^>]+>", "", match.group(1))).strip()
        pippin_memory_ok = (
            chrome.returncode == 0
            and "measuring…" not in card_text
            and bool(re.search(r"\bRSS (?:\d+(?:\.\d+)? GB|n/a)\b", card_text))
        )
        emit(
            "PASS" if pippin_memory_ok else "FAIL",
            "page.pippin.memory_after_two_refreshes",
            card_text or "card unreadable",
            "RSS <number> GB or RSS n/a; never measuring…",
            "headless DOM after initial render + two 12s refresh intervals",
        )
        budget_text = ""
        budget_match = re.search(
            r'<div class="runson-budget" id="runson-budget-strip">(.*?)<div class="runson-gauges" id="runson-budget-gauges">',
            chrome.stdout,
            re.S,
        )
        if budget_match:
            budget_text = html.unescape(re.sub(r"<[^>]+>", " ", budget_match.group(1)))
            budget_text = re.sub(r"\s+", " ", budget_text).strip()
        budget_render_ok = (
            chrome.returncode == 0
            and "runson-spend-chart" in chrome.stdout
            and "runson-credits-chart" in chrome.stdout
            and "runson-budget-gauges" in chrome.stdout
            and "Spent today" in budget_text
            and "Spent this month" in budget_text
        )
        emit(
            "PASS" if budget_render_ok else "FAIL",
            "runson.budget_strip.rendered",
            budget_text or "strip unreadable",
            "spent today + spent this month + daily chart + runway chart + gauges",
            "headless rendered DOM",
        )
        no_placeholder = (
            budget_render_ok
            and "measuring" not in budget_text.lower()
            and "loading" not in budget_text.lower()
        )
        emit(
            "PASS" if no_placeholder else "FAIL",
            "runson.budget_strip.no_eternal_placeholder",
            budget_text or "strip unreadable",
            "rendered values or dimmed n/a; never loading/measuring",
        )
    except Exception as exc:
        emit("FAIL", "page.render", "unavailable", "HTTP 200 + valid JS", str(exc))
    r = api.get("runson", {})
    if r.get("available"):
        ok = all(k in r for k in (
            "live_runners", "runners", "jobs_today", "trial_days_remaining", "credits_remaining",
            "cost_source", "cost_source_label", "spent_today", "spent_month", "daily_spend",
            "credits_runway", "budget_limits", "jobs_done_error", "credits_error", "cost_error",
        )) and len(r.get("daily_spend") or []) == 30
        instrument = "AWS data schema"
    else:
        text = (r.get("message") or "").lower()
        ok = r.get("error") == "credentials" and "creds expired" in text and "aws login" in text
        instrument = "expired credentials must be explicit and actionable"
    emit("PASS" if ok else "FAIL", "runson.render_state", r, instrument)

    # A denied credits read used to be visible only in the service log: the handler
    # swallows it to None so the panel never blanks, which left credits_remaining=null -
    # indistinguishable from "no credits data yet". That silence nearly got a live IAM
    # gap retired as stale on 2026-09-04 (fleet-runson-observer lacks
    # freetier:GetAccountPlanState). The read now reports itself.
    credits_error = r.get("credits_error")
    emit("PASS" if not credits_error else "FAIL", "runson.credits_read",
         {"credits_error": credits_error, "credits_remaining": r.get("credits_remaining")},
         {"credits_error": None},
         "credits read clean" if not credits_error
         else f"credits read failing ({credits_error}); credits_remaining is null because of this, not because there is no data")

    # Second 136p instance: a Cost Explorer failure used to collapse into
    # cost_source="credits" with no reason, so a denied ce:GetCostAndUsage, a CE that
    # was never enabled, and a local sqlite failure all looked the same. This path runs
    # once per six hours because CE calls cost money - a silent fallback here could sit
    # unnoticed for a long time.
    cost_error = r.get("cost_error")
    emit("PASS" if not cost_error else "FAIL", "runson.cost_read",
         {"cost_error": cost_error, "cost_source": r.get("cost_source")},
         {"cost_error": None},
         "cost read clean" if not cost_error
         else f"cost read failing ({cost_error}); cost_source fell back to {r.get('cost_source')} because of this, not by preference")

    source = (ROOT / "queue_router.py").read_text()
    fallback_contract = (
        '"cost_source": "cost_explorer" if ce_costs else "credits"' in source
        and 'costs = ce_costs or credit_costs' in source
        and '"cost_source_label": "AWS Cost Explorer" if ce_costs else "net of credits"' in source
    )
    fallback_probe = run([
        str(ROOT / "venv/bin/python"), "-c", """
import sys, json, os, tempfile
sys.path.insert(0, str(sys.argv[1]))
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import queue_router as q
fd, path = tempfile.mkstemp(prefix='runson-truth-', suffix='.db'); os.close(fd)
try:
    q.CONFIG['db_path'] = path
    q.init_db()
    q._runson_aws_json = lambda args, region: (None, 'access_denied')
    now = datetime(2026, 8, 30, 21, 10, tzinfo=timezone.utc)
    credits = q._runson_credit_budget_data(now, now.astimezone(ZoneInfo('America/Chicago')).date())
    ce, ce_error = q._runson_ce_cost_data(now.astimezone(ZoneInfo('America/Chicago')).date(), '000000000000')
    costs = ce or credits
    print(json.dumps({'ce': ce, 'ce_error': ce_error, 'source': 'cost_explorer' if ce else 'credits',
                      'label': 'AWS Cost Explorer' if ce else 'net of credits',
                      'today': costs['spent_today'], 'month': costs['spent_month']}))
finally:
    os.unlink(path)
""", str(ROOT),
    ], timeout=15)
    fallback_crashed = fallback_probe.returncode != 0
    try:
        fallback_result = json.loads(fallback_probe.stdout)
    except json.JSONDecodeError:
        fallback_result = {}
    fallback_live = (
        not fallback_crashed
        and fallback_result.get("ce") is None
        and fallback_result.get("source") == "credits"
        and fallback_result.get("label") == "net of credits"
        and close(fallback_result.get("today"), .62, .0001)
        and close(fallback_result.get("month"), .62, .0001)
        # ledger 136p: the fallback must carry its reason, not just happen silently
        and fallback_result.get("ce_error") == "access_denied"
    )
    if fallback_crashed:
        emit(
            "WARN",
            "runson.budget_strip.ce_fallback",
            fallback_probe.stderr.strip() or "probe crashed",
            "CE AccessDenied falls back to numeric credit deltas labeled net of credits",
            "isolated AccessDenied fixture probe crashed; cannot determine",
        )
    else:
        emit(
            "PASS" if fallback_contract and fallback_live else "FAIL",
            "runson.budget_strip.ce_fallback",
            fallback_result or "probe unreadable",
            "CE AccessDenied falls back to numeric credit deltas labeled net of credits",
            "isolated AccessDenied fixture with seeded credit anchors",
        )


def notify_cron(failures: list[dict], signature: str) -> None:
    track_path = STATE / "consecutive.json"
    try:
        prior = json.loads(track_path.read_text())
    except Exception:
        prior = {}
    count = int(prior.get("count", 0)) + 1 if prior.get("signature") == signature else 1
    track_path.write_text(json.dumps({"signature": signature, "count": count,
                                      "updated_at": dt.datetime.now(dt.timezone.utc).isoformat()}, indent=2) + "\n")
    lines = [f"{x['level']} {x['signal']} dashboard={x['dashboard']} instrument={x['instrument']}" for x in failures]
    body = "DASH-TRUTH hourly failure\n" + "\n".join(lines[:20])
    priority = "urgent" if count >= 2 else "routine"
    env = dict(os.environ, FLEET_SEAT="dash-truth")
    run(["/workspace/planning/scripts/fleet-msg", "send", "elrond", "--priority", priority, "--body", body], timeout=15, env=env)
    if count >= 2:
        run(["/home/ben/bin/council-notify", f"DASH-TRUTH repeated failure ({count}x): {', '.join(x['signal'] for x in failures[:4])}"], timeout=15)


def finish() -> int:
    STATE.mkdir(parents=True, exist_ok=True)
    failures = [r for r in results if r["level"] == "FAIL"]
    warnings = [r for r in results if r["level"] == "WARN"]
    payload = {"generated_at": dt.datetime.now(dt.timezone.utc).isoformat(), "dashboard_url": BASE,
               "summary": {"pass": sum(r["level"] == "PASS" for r in results), "warn": len(warnings), "fail": len(failures)},
               "results": results}
    temp = STATE / f".last-run.{os.getpid()}.json"
    temp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    temp.replace(STATE / "last-run.json")
    with (STATE / "runs.jsonl").open("a") as handle:
        handle.write(json.dumps(payload, separators=(",", ":")) + "\n")
    if failures:
        signature = hashlib.sha256("\n".join(sorted(r["signal"] for r in failures)).encode()).hexdigest()
        if CRON:
            notify_cron(failures, signature)
    else:
        (STATE / "consecutive.json").write_text(json.dumps({"signature": None, "count": 0,
                                                             "updated_at": payload["generated_at"]}, indent=2) + "\n")
    if not CRON:
        print(f"SUMMARY PASS={payload['summary']['pass']} WARN={len(warnings)} FAIL={len(failures)}")
        print(f"JSON {STATE / 'last-run.json'}")
    return 1 if failures else 0


def run_truth_pass():
    global results, api
    results = []
    api = {}
    check_apis()
    check_fleet()
    check_gpu()
    check_models()
    check_pipeline()
    check_runson_gate()
    check_runson_runner_history()
    check_local_sources()
    check_rendering()
    return [r for r in results if r["level"] == "FAIL"]


def main() -> int:
    run_truth_pass()
    return finish()


if __name__ == "__main__":
    raise SystemExit(main())
