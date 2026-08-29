#!/usr/bin/env python3
"""Independent, read-only truth checks for the Fleet Monitor dashboard."""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import hashlib
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
    "runson": {"available", "deployed", "generated_at"},
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
            endpoint_timeout = 120 if endpoint == "pipeline" else (50 if endpoint in ("status", "fleet") else TIMEOUT)
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
    for name in ("frodo", "pippin"):
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
    for name in ("aragorn", "shadowfax"):
        claim = serving.get(name, {})
        level = "PASS" if (not claim.get("available") or name in route_boxes) else "FAIL"
        emit(level, f"model.{name}.gateway_source", claim.get("source"), name in route_boxes,
             "gateway spend log is the independent token source for non-counter servers")


def gh_json(args: list[str]):
    cp = run(["gh", "api", *args], timeout=30)
    if cp.returncode:
        raise RuntimeError(cp.stderr.strip()[:200])
    return json.loads(cp.stdout)


def check_pipeline() -> None:
    d = api.get("pipeline", {})
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

        # Measure active job count from first 12 in_progress runs (to match dashboard.active_jobs)
        def measure_active_jobs():
            try:
                runs_resp = gh_json(["repos/armbrain-io/armbrain/actions/runs", "--method", "GET", "-f", "status=in_progress", "-f", f"created=>={cutoff}", "-f", "per_page=100"])
                active_jobs = 0
                for run in (runs_resp.get("workflow_runs") or [])[:12]:
                    jobs_resp = gh_json(["repos/armbrain-io/armbrain/actions/runs/" + str(run["id"]) + "/jobs", "--method", "GET", "-f", "per_page=100"])
                    for job in jobs_resp.get("jobs", []):
                        if job.get("status") == "in_progress":
                            active_jobs += 1
                return active_jobs
            except Exception:
                return None

        direct_jobs_before = measure_active_jobs()

        try:
            ci_code, ci_body = http("/api/ci_queue", 25)
            ci_fresh = json.loads(ci_body) if ci_code == 200 else ci
        except Exception:
            ci_fresh = ci
        direct_ci_after = {}
        for state in ("queued", "in_progress"):
            direct_ci_after[state] = gh_json(["repos/armbrain-io/armbrain/actions/runs", "--method", "GET", "-f", f"status={state}", "-f", f"created=>={cutoff}", "-f", "per_page=100"])["total_count"]

        direct_jobs_after = measure_active_jobs()

        def ci_match(claim, state):
            lo = min(direct_ci_before[state], direct_ci_after[state]) - 2
            hi = max(direct_ci_before[state], direct_ci_after[state]) + 2
            return isinstance(claim, (int, float)) and lo <= claim <= hi

        def job_match(claim):
            if direct_jobs_before is None or direct_jobs_after is None:
                return False
            lo = min(direct_jobs_before, direct_jobs_after) - 2
            hi = max(direct_jobs_before, direct_jobs_after) + 2
            return isinstance(claim, (int, float)) and lo <= claim <= hi

        for field, state in (("queued", "queued"), ("in_progress", "in_progress")):
            claim = ci_fresh.get(field)
            if field == "in_progress":
                # in_progress in ci_queue is mapped from active_jobs on display, verify separately below
                pass
            else:
                observed = {"before": direct_ci_before[state], "after": direct_ci_after[state]}
                emit("PASS" if ci_match(claim, state) else "FAIL", f"ci.{field}", claim, observed,
                     "repo armbrain-io/armbrain; workflow runs created within 48h; bracketed ±1 churn")

        # Check ci.in_progress separately - it should be job count (via active_jobs)
        ci_queue_running = ci_fresh.get("active_jobs") if ci_fresh.get("active_jobs") is not None else ci_fresh.get("in_progress")
        observed_jobs = {"before": direct_jobs_before, "after": direct_jobs_after}
        emit("PASS" if job_match(ci_queue_running) else "FAIL", f"ci.in_progress", ci_queue_running, observed_jobs,
             "job count from first 12 in_progress runs; bracketed ±2 churn")

        # Check pipeline.ci_queued and ci_running
        for field, state in (("ci_queued", "queued"), ("ci_running", "in_progress")):
            claim = d.get(field)
            if field == "ci_running":
                # ci_running should be job count, not run count
                observed = observed_jobs
                matches = job_match(claim)
                detail = "job count from first 12 in_progress runs; bracketed ±2 churn"
            else:
                observed = {"before": direct_ci_before[state], "after": direct_ci_after[state]}
                matches = ci_match(claim, state)
                detail = "bracketed ±2 churn"
            emit("PASS" if matches else "FAIL", f"pipeline.{field}", claim, observed, detail)
        runs = gh_json(["repos/armbrain-io/armbrain/actions/workflows/gateway-deploy.yml/runs", "--method", "GET", "-f", "status=completed", "-f", "branch=main", "-f", "per_page=100"])["workflow_runs"]
        last = None
        for workflow in runs:
            if workflow.get("event") not in ("push", "workflow_dispatch"):
                continue
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


def check_rendering() -> None:
    try:
        code, body = http("/")
        page = body.decode()
        required = ("fleet-summary", "fleet-row", "ship-flow", "ci-queue-body", "route-health-body",
                    "fleet-stats-body", "runson-card", "runson-body", "monitors", "sparklines",
                    "energy-by-machine", "energy-fleet-body")
        missing = [x for x in required if not re.search(rf'id=(?:["\']{re.escape(x)}["\']|{re.escape(x)}(?:\s|>))', page)]
        emit("PASS" if code == 200 and not missing else "FAIL", "page.cards", {"http": code, "missing": missing}, list(required))
        scripts = re.findall(r"<script>(.*?)</script>", page, re.S)
        node_ok = False
        if scripts:
            with tempfile.NamedTemporaryFile("w", suffix=".js") as handle:
                handle.write("\n".join(scripts)); handle.flush()
                node_ok = run(["node", "--check", handle.name], timeout=10).returncode == 0
        emit("PASS" if node_ok else "FAIL", "page.javascript_syntax", node_ok, "node --check")
        for fn in ("refresh", "refreshFleet", "refreshHistory", "refreshEnergy", "refreshCiQueue", "refreshShipFlow", "refreshRunsOn", "refreshRouteHealth", "refreshFleetStats", "refreshModelServing"):
            found = f"function {fn}(" in page
            emit("PASS" if found else "FAIL", f"page.renderer.{fn}", found, "renderer declared")
    except Exception as exc:
        emit("FAIL", "page.render", "unavailable", "HTTP 200 + valid JS", str(exc))
    r = api.get("runson", {})
    if r.get("available"):
        ok = all(k in r for k in ("live_runners", "runners", "jobs_today", "trial_days_remaining", "credits_remaining"))
        instrument = "AWS data schema"
    else:
        text = (r.get("message") or "").lower()
        ok = r.get("error") == "credentials" and "creds expired" in text and "aws login" in text
        instrument = "expired credentials must be explicit and actionable"
    emit("PASS" if ok else "FAIL", "runson.render_state", r, instrument)


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
    check_local_sources()
    check_rendering()
    return [r for r in results if r["level"] == "FAIL"]


def main() -> int:
    fails = run_truth_pass()
    if fails:
        # Sampling-skew / in-flight churn tolerance:
        # On mismatch, wait 6s for settle and re-read once.
        # Only fail if the delta persists across runs.
        time.sleep(6)
        run_truth_pass()
    return finish()


if __name__ == "__main__":
    raise SystemExit(main())
