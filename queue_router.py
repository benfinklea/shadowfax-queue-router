#!/usr/bin/env python3
"""
Queue Router Service for Shadowfax
Receives ComfyUI workflows and routes them to the best available GPU.
"""

import json
import os
import re
import sqlite3
import subprocess
import uuid
import requests
import logging
import threading
import time
import paramiko
from concurrent.futures import ThreadPoolExecutor, as_completed, wait
from datetime import datetime, timedelta
from pathlib import Path
from flask import Flask, request, jsonify, Response
from flask_cors import CORS

# Pushover notification settings
PUSHOVER_CONFIG = {
    "enabled": True,
    "user_key": "u3ibg3as81617ht1787m1pwepa6o9p",
    "api_token": "a8ykrj6mowicemgbuin7yom95dqntc",
    "sound": "magic"
}

# Pedernales Electric Cooperative time-of-use rates (effective 2026-03-01).
# PEC changes these ~twice a year - update the numbers below when they do.
# Total $/kWh = base_per_kwh + the period charge for that season/hour.
# Schedule applies every day (no weekday/weekend distinction). Hours are [start, end)
# in 24h local time; any hour not listed in "peak"/"mid" falls through to off-peak.
ELECTRIC_RATES = {
    "base_per_kwh": 0.042476,
    "currency": "$",
    "seasons": {
        # month number -> season key
        1: "winter", 2: "winter", 12: "winter",
        3: "shoulder", 4: "shoulder", 5: "shoulder", 10: "shoulder", 11: "shoulder",
        6: "summer", 7: "summer", 8: "summer", 9: "summer",
    },
    "schedule": {
        "summer": {
            "off_peak": 0.043481,
            "mid": {"charge": 0.093169, "hours": [(14, 16), (20, 21)]},
            "peak": {"charge": 0.161843, "hours": [(16, 20)]},
        },
        "shoulder": {
            "off_peak": 0.043481,
            "mid": {"charge": 0.086442, "hours": [(17, 21)]},
        },
        "winter": {
            "off_peak": 0.043481,
            "mid": {"charge": 0.086442, "hours": [(5, 9), (17, 21)]},
        },
    },
}

# Set up logging with clear formatting
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("QueueRouter")

app = Flask(__name__)
CORS(app)
# Preserve dict insertion order in JSON responses (so targets render gandalf, frodo, pippin)
app.json.sort_keys = False

# ─── TEMPORARY post-outage IP map — 2026-07-30 ───────────────────────────────
# THE ONE PLACE fleet IPs are written down. Change them here, nowhere else.
#
# WHY THIS BLOCK EXISTS: on 2026-07-30 northfarthing's DHCP server died (~2.5h
# outage). The router took over DHCP+DNS, re-leased the whole subnet, and the
# `.fleet` search domain went away with the old server. gandalf landed on a new
# address and every card that addressed it by the old one went dark.
#
# INTENDED HOME: gandalf = 192.168.1.10, per the router's reservation table.
# The .10 in this file was therefore CORRECT BY DESIGN, not rot.
# WHY IT ISN'T TRUE RIGHT NOW: a GL.iNet **GL-KVM** KVM-over-IP dongle
# (MAC 94:83:c4:cb:09:76, nginx/1.26.2 + dropbear_2025.89, web UI title
# "GLKVM") currently squats on .10, so gandalf took a dynamic lease on .6
# (enp5s0, MAC fc:9d:05:01:06:56).
#
# TO REVERT once .10 is reclaimed: set "gandalf" below back to "192.168.1.10".
# That single line is the entire diff. Do NOT re-scatter IPs through this file.
# Restore path is written up in
# /workspace/planning/overnight-20260730/NETWORK-REPAIR-PLAN.md
#
# EVERY address below was verified by probe (`ssh <host> hostname`, or a service
# GET) on 2026-07-30 ~02:20 CT. None is assumed.
FLEET_IPS = {
    "gandalf":       "192.168.1.10",  # BACK ON ITS INTENDED HOME as of 2026-07-30
                                      # ~09:05 UTC: Ben reserved .10 for MAC
                                      # fc:9d:05:01:06:56 and the lease renewed.
                                      # Verified: `ip -4 addr show enp5s0` = .10,
                                      # ssh .10 -> gandalf, and .6 is now dead
                                      # ("No route to host"). This was the
                                      # one-line revert the block was built for.
    "frodo":         "192.168.1.11",  # verified ssh->frodo
    "shadowfax":     "192.168.1.12",  # verified ssh->shadowfax
    "pippen":        "192.168.1.13",  # verified ssh->pippen.local
    "sam":           "192.168.1.14",  # verified ssh->sam  (mDNS still says .135: stale)
    "aragorn":       "192.168.1.15",  # verified ssh->aragorn  (reserved, did not move)
    "southfarthing": "192.168.1.61",  # verified ssh->southfarthing
    "eastfarthing":  "192.168.1.62",  # verified ssh->eastfarthing
    "westfarthing":  "192.168.1.63",  # verified ssh->westfarthing
    # UPDATED 2026-08-11: reverted to verified LAN IP. The tailscale-IP era
    # above (100.99.59.83) was itself the bug, not the fix: paramiko cannot
    # complete SSH auth over Tailscale here (AuthenticationException after a
    # 10s timeout - confirmed by direct repro), while a plain LAN connect
    # succeeds in ~0.1s. That mismatch was silently rendering northfarthing
    # OFFLINE on the dashboard for however long it's been live, while the box
    # was actually up (uptime, hostname, and normal SSH all confirmed healthy
    # via direct probe). 192.168.1.60 was verified moments before this edit:
    # `ssh ben@192.168.1.60 hostname` -> "northfarthing" (correct box, not the
    # old shadowfax mDNS collision this comment used to warn about), and the
    # same paramiko client this file uses connected in 0.12s and got the same
    # answer. If DHCP moves this box again, this will start reading OFFLINE
    # again (wrong-box guard fails closed, not open) rather than silently
    # showing a stale/wrong box - re-verify with the same probe before editing.
    "northfarthing": "192.168.1.60",  # verified ssh->northfarthing 2026-08-11
}
# Boxes reached over tailscale rather than LAN (identity-safe, LAN IP unknown).
# ─── end TEMPORARY post-outage IP map ────────────────────────────────────────

# Configuration
CONFIG = {
    "targets": {
        "gandalf": {
            "url": f"http://{FLEET_IPS['gandalf']}:8188",
            "ssh_host": FLEET_IPS["gandalf"],
            "ssh_user": "ben",
            "os": "linux",
            "model_status_urls": [
                ("running", "http://127.0.0.1:8889/running"),
                ("models", "http://127.0.0.1:8891/v1/models"),
            ],
            "vram_gb": 96,
            "gpu_power_limit": 450,
            "gpu_power_max": 600,
            "disk_path": "/workspace"
        },
        "frodo": {
            "url": f"http://{FLEET_IPS['frodo']}:8188",
            "ssh_host": FLEET_IPS["frodo"],
            "ssh_user": "ben",
            "os": "linux",
            "model_status_urls": [
                ("models", f"http://{FLEET_IPS['frodo']}:8890/v1/models"),
            ],
            "vram_gb": 32,
            "gpu_power_limit": 575,
            "gpu_power_max": 600,
            "disk_path": "/"
        },
        # aragorn (promoted to a full card 2026-07-29 at Ben's request; order is
        # gandalf, frodo, aragorn, pippin and dict order IS render order).
        # Two NVIDIA GPUs (PCI 01:00.0 = 2f04, 03:00.0 = 2c02) but the nvidia
        # driver is NOT installed yet, so nvidia-smi is absent and the card
        # renders its CPU/RAM/disk vitals until the driver lands.
        "aragorn": {
            "ssh_host": FLEET_IPS["aragorn"],
            "ssh_user": "mac",
            "os": "linux",
            "gpu_power_max": 675,
            "disk_path": "/",
            # Three ollama instances, each pinned to specific card(s) by
            # CUDA_VISIBLE_DEVICES in its systemd unit. CUDA_DEVICE_ORDER is
            # PCI_BUS_ID, so device 0 = RTX 5070 (01:00.0), 1 = RTX 5080 (03:00.0).
            # ollama.service :11434 -> devices "1"   (5080)  = route reason-oss
            # ollama-5070    :11435 -> devices "0"   (5070)  = route fast-mini
            # ollama-27b     :11436 -> devices "0,1" (both, OLLAMA_SCHED_SPREAD=1)
            "ollama_instances": [
                {"port": 11434, "where": "5080"},
                {"port": 11435, "where": "5070"},
                {"port": 11436, "where": "both GPUs"},
            ],
        },
        "pippin": {
            "ssh_host": FLEET_IPS["pippen"],
            "ssh_user": "ben",
            "os": "mac",
            "model_status_urls": [
                ("models", f"http://{FLEET_IPS['pippen']}:8891/v1/models"),
            ],
            "vram_gb": 64,
            "disk_path": "/"
        }
    },
    "db_path": "/var/lib/queue-router/jobs.db"
}

# Fleet host row (2026-07-12): the six boxes NOT covered by the GPU target cards.
# Compact CPU/temp/RAM tiles + reboot buttons at the top of the dashboard.
# "local": metrics come from this box itself (no SSH). "mac" is for Wake-on-LAN
# (all farthings have WoL enabled at NIC + BIOS level; sam/shadowfax N/A).
FLEET_NODES = {
    # ADDRESS BY HOSTNAME, NOT IP (2026-07-28). These boxes have no DHCP
    # reservations, so their IPs move: .147/.137/.136/.139 -> .73/.74/.76 ->
    # .62/.61/.63 in two weeks. Every time they moved, the dashboard reported them
    # OFFLINE and someone went looking for a sleep/power bug on a machine that was
    # wide awake at a new address. That happened at least three times.
    # mDNS (.local) follows the box wherever DHCP puts it, so this whole class of
    # false "offline" report goes away without needing router access.
    # NOTE: only the farthings are switched to names. gandalf.local resolves
    # IPv6-only and northfarthing.local can return a secondary address, so the GPU
    # targets above stay on explicit IPv4.
    # aragorn (2026-07-29): static DHCP reservation, so the no-reservation IP
    # caveat above does NOT apply - explicit IPv4 is safe here. User is "mac"
    # (ben@gandalf's key is installed there for that user). NO wol_mac recorded
    # yet, so no Wake button until someone captures the NIC MAC.
    # aragorn moved OUT of this row 2026-07-29 - it is a full target card now
    # (see CONFIG["targets"]). Listing it in both places would render it twice.
    # 2026-07-30, post-outage: temporarily BACK to verified raw IPv4 from FLEET_IPS.
    # The names-not-IPs rule above is still the right long-term answer, but mDNS is
    # not trustworthy right now - northfarthing.local resolves to shadowfax's
    # address, which silently duplicated one box's metrics onto two tiles. The
    # farthing .local names DID each resolve correctly when probed; they are pinned
    # only because the whole map is pinned for one night. Revert with the plan.
    "northfarthing": {"ssh_host": FLEET_IPS["northfarthing"], "ssh_user": "ben", "wol_mac": "84:47:09:65:43:c3"},
    "eastfarthing":  {"ssh_host": FLEET_IPS["eastfarthing"], "ssh_user": "ben", "wol_mac": "84:47:09:62:ef:60"},
    "southfarthing": {"ssh_host": FLEET_IPS["southfarthing"], "ssh_user": "ben", "wol_mac": "84:47:09:65:42:5b"},
    "westfarthing":  {"ssh_host": FLEET_IPS["westfarthing"], "ssh_user": "ben", "wol_mac": "84:47:09:65:42:85"},
    # Was {"local": True} when this service ran ON shadowfax. It moved to gandalf
    # 2026-07-21, so shadowfax is now just another remote box reached over SSH.
    "shadowfax":     {"ssh_host": FLEET_IPS["shadowfax"], "ssh_user": "ben"},
    "sam":           {"ssh_host": FLEET_IPS["sam"], "ssh_user": "ben"},
}

# --- Glance-view additions (2026-07-19): CI queue depth + local model-route
# health. Goal: "the Shire is flapping" should read as ONE clear signal
# instead of an OK/FAIL/OK phone-push flap. Both secrets below are read live
# over SSH from gandalf every poll (cached) - NEVER hardcoded here - reusing
# the exact same ben@gandalf SSH channel already used above for reboot/power
# actions. That keeps this file free of any GitHub token or gateway key.
GITHUB_CI_REPO = "armbrain-io/armbrain"
GH_TOKEN_ENV_PATH = "/opt/overflow-controller/gh-token.env"   # same file the Shire autoscaler mints/refreshes every 15 min
GATEWAY_KEY_ENV_PATH = "~/.config/gandalf-gateway/fleet.env"  # canonical fleet gateway key (per infra rules)
# Was gandalf.local:4000. Pinned to the verified IP with the rest of the map on
# 2026-07-30 - gandalf.local resolves IPv6-first here, and this service runs ON
# gandalf, so there is no reason to take a DNS dependency to reach itself.
GATEWAY_MODELS_URL = f"http://{FLEET_IPS['gandalf']}:4000/v1/models"
# The 🔒 local-only routes from the fleet gateway table - the ones that should
# always be up. (opus/sonnet/codex/gemini/etc. are external and expected to
# come and go with vendor availability, so they're left off this glance tile.)
# The chips on the dashboard. Rebuilt 2026-07-30 to match reality after the night's
# reconfiguration. Deliberately EXCLUDES alias-only routes (code-glm and big now
# both point at models listed here) so one model does not show up as two chips.
# Dropped: `coder` (devstral) - retired by council vote, 3.5 tok/s.
LOCAL_GATEWAY_ROUTES = [
    "flagship",    # gandalf 35B-A3B + n-gram, RESIDENT, never unloads
    "dense",       # gandalf 27B + MTP and n-gram, RESIDENT, never unloads
    "fast",        # frodo 35B-A3B - best prefill on the fleet, 4 slots
    "reason",      # gandalf gemma4-26b - on demand, loads alongside the pair
    "code",        # pippen
    "reason-oss",  # aragorn 5080
    "fast-mini",   # aragorn 5070
    "reason-27b",  # aragorn, 27B split across BOTH cards
    "cheap",       # shadowfax
]

# One-shot metrics: cpu% (0.6s /proc/stat delta), ram MB, hottest sensor (milli-C)
FLEET_METRICS_CMD = (
    "read -r t1 i1 < <(awk '/^cpu /{print $2+$3+$4+$5+$6+$7+$8, $5+$6}' /proc/stat); "
    "sleep 0.6; "
    "read -r t2 i2 < <(awk '/^cpu /{print $2+$3+$4+$5+$6+$7+$8, $5+$6}' /proc/stat); "
    "dt=$((t2-t1)); di=$((i2-i1)); cpu=$(( dt>0 ? (100*(dt-di))/dt : 0 )); "
    "read -r rt ru < <(free -m | awk '/^Mem:/{print $2, $3}'); "
    # temp: prefer the real CPU sensor (k10temp AMD / coretemp Intel), then the
    # generic ACPI/SoC zone (Pis), only then max-of-everything (NVMe etc. lie hot)
    "tp=; for h in /sys/class/hwmon/hwmon*; do case \"$(cat $h/name 2>/dev/null)\" in k10temp|coretemp) tp=$(cat $h/temp1_input 2>/dev/null); break;; esac; done; "
    "[ -z \"$tp\" ] && tp=$(cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null); "
    "[ -z \"$tp\" ] && tp=$(cat /sys/class/hwmon/hwmon*/temp1_input 2>/dev/null | sort -rn | head -1); "
    # host: WHICH BOX ACTUALLY ANSWERED. Added 2026-07-30 so a tile can never
    # again render a different machine's vitals under its own name - see
    # _fleet_host_matches below for why this exists.
    "echo \"cpu=$cpu ram_used=$ru ram_total=$rt temp=${tp:-0} host=$(hostname)\""
)

# --- Wrong-box guard (2026-07-30) -------------------------------------------
# On 2026-07-30 `northfarthing.local` began resolving to 192.168.1.12, which is
# SHADOWFAX. The dashboard dutifully SSHed there for both tiles and rendered one
# box's CPU/temp/RAM under two different names, so northfarthing - which was in
# fact dead, and was the machine whose DHCP failure had just taken the LAN down
# for 2.5 hours - displayed as healthy. It was only caught by a human noticing
# that two tiles showed byte-identical numbers.
#
# The lesson: a stale IP fails loudly, but a name another box can answer to lies
# quietly. So every metrics reply now has to prove it came from the box we asked
# for. If it does not, the tile shows MISMATCH and NO numbers - never a
# neighbour's. We deliberately do NOT show the stats and add a warning badge:
# numbers on screen get believed.
#
# Matching is on the short hostname, case-insensitively: boxes report things like
# "pippen.local" or a FQDN, and that is not a mismatch. `hostname_aka` is there
# for a box whose real hostname legitimately differs from its tile name.
# Cards whose tile name legitimately differs from the machine's real hostname.
# "pippin" the card vs "pippen" the box is a long-standing spelling split in this
# file, not a wrong-box condition - without this the guard would cry wolf on it.
TARGET_HOSTNAME_AKA = {
    "pippin": ("pippen",),
}


def _fleet_host_matches(expected, reported, aka=()):
    """True if `reported` hostname is the box we asked for. Empty reported = unknown -> treat as OK."""
    if not reported:
        return True          # older/odd hosts that print no host= field: don't cry wolf
    short = reported.strip().lower().split(".")[0]
    accepted = {expected.strip().lower()} | {a.strip().lower() for a in aka}
    return short in accepted

def get_fleet_node_metrics(name, cfg):
    """CPU/temp/RAM for one fleet node. Returns dict; online=False on any failure."""
    out = None
    try:
        if cfg.get("local"):
            import subprocess
            out = subprocess.run(["bash", "-c", FLEET_METRICS_CMD], capture_output=True,
                                 text=True, timeout=15).stdout
        else:
            # Retry once on a DEAD POOLED CONNECTION (fixed 2026-07-30).
            # get_ssh_client hands back a cached client whenever
            # transport.is_active() is true, but a peer that dropped the
            # connection its own side leaves is_active() true until we actually
            # write to it. The write then fails with "Socket exception:
            # Connection reset by peer (104)", we reported the box OFFLINE, and
            # the next poll reconnected and reported it online again - so a live
            # box flapped green/red on a fixed cadence for no reason. Seen every
            # ~45s on shadowfax, which is a Pi and reaps idle sshd sessions
            # briskly. Pre-existing bug, unrelated to the IP reshuffle; caught
            # while verifying the cards after that fix.
            # So: on any exec failure, evict the cached client and try once more
            # with a genuinely fresh connection before calling the box down.
            user = cfg.get("ssh_user", "ben")
            last_err = None
            for attempt in (1, 2):
                client = get_ssh_client(cfg["ssh_host"], user)
                if client is None:
                    break          # circuit breaker is open - respect it
                try:
                    # no extra bash -c wrapper: the CMD contains single quotes,
                    # and sshd already hands the command line to the login shell
                    _, stdout, _ = client.exec_command(FLEET_METRICS_CMD, timeout=15)
                    out = stdout.read().decode()
                    last_err = None
                    break
                except Exception as e:
                    last_err = e
                    ssh_clients.pop(f"{user}@{cfg['ssh_host']}", None)
                    try:
                        client.close()
                    except Exception:
                        pass
                    if attempt == 2:
                        logger.debug(f"fleet metrics {name}: failed twice: {e}")
            if last_err is not None or out is None:
                return {"name": name, "online": False, "can_wake": bool(cfg.get("wol_mac"))}
    except Exception as e:
        logger.debug(f"fleet metrics {name}: {e}")
        return {"name": name, "online": False, "can_wake": bool(cfg.get("wol_mac"))}
    m = dict(kv.split("=") for kv in (out or "").split() if "=" in kv)
    if "cpu" not in m:
        return {"name": name, "online": False, "can_wake": bool(cfg.get("wol_mac"))}
    # WRONG-BOX GUARD: prove the reply came from the box this tile names. A
    # local (no-SSH) node is trivially itself, so it is exempt.
    if not cfg.get("local"):
        reported = m.get("host", "")
        if not _fleet_host_matches(name, reported, cfg.get("hostname_aka", ())):
            logger.warning(
                f"fleet metrics {name}: WRONG BOX - asked {cfg['ssh_host']} for "
                f"'{name}', got '{reported}'. Refusing to render its numbers. "
                f"Check DNS/mDNS/DHCP for {name}."
            )
            return {
                "name": name, "online": False, "mismatch": True,
                "reported_host": reported.strip().split(".")[0],
                "ssh_host": cfg["ssh_host"],
                "can_wake": bool(cfg.get("wol_mac")),
            }
    return {
        "name": name, "online": True,
        "cpu": int(m.get("cpu", 0)),
        "ram_used_gb": round(int(m.get("ram_used", 0)) / 1024, 1),
        "ram_total_gb": round(int(m.get("ram_total", 0)) / 1024, 1),
        "ram_pct": int(100 * int(m.get("ram_used", 0)) / max(1, int(m.get("ram_total", 1)))),
        "temp_c": round(int(m.get("temp", 0)) / 1000),
        "can_wake": bool(cfg.get("wol_mac")),
    }

# ComfyUI watchdog state
comfyui_watchdog = {}  # {target: {"consecutive_failures": int, "last_restart": datetime, "was_down": bool}}

# --- Display hysteresis for the /api/status dashboard board (2026-07-18) ---
# Problem: gandalf/frodo run heavy CI jobs and can answer the ComfyUI /queue
# and /system_stats HTTP checks slowly. A single slow response was flipping
# them to OFFLINE (red) on the dashboard even though they were healthy and
# just busy, alarming Ben with a false "fleet is down" read.
# Fix: a target must fail TWO CONSECUTIVE probes (i.e. be unreachable across
# two ~12s poll cycles, ~24s) before the DASHBOARD shows it as OFFLINE. Any
# single successful probe immediately clears it back to ONLINE. This only
# smooths the /api/status display - it does NOT touch the raw probe used for
# metrics collection/DB (collect_metrics), so historical data stays accurate.
OFFLINE_FAIL_THRESHOLD = 2
_target_display_health = {name: {"consecutive_failures": 0, "displayed_online": True} for name in CONFIG["targets"]}
_target_display_health_lock = threading.Lock()

def apply_display_hysteresis(name, raw_online):
    """Smooth one target's raw online/offline reading for display purposes.

    Requires OFFLINE_FAIL_THRESHOLD consecutive raw failures before the
    dashboard shows OFFLINE; any raw success clears it back to ONLINE right away.
    """
    with _target_display_health_lock:
        h = _target_display_health.setdefault(
            name, {"consecutive_failures": 0, "displayed_online": True}
        )
        if raw_online:
            h["consecutive_failures"] = 0
            h["displayed_online"] = True
        else:
            h["consecutive_failures"] += 1
            if h["consecutive_failures"] >= OFFLINE_FAIL_THRESHOLD:
                h["displayed_online"] = False
            # else: still just show the last known-good state - probably busy,
            # not actually down. logger line below makes this visible in journalctl.
            logger.info(
                f"{name}: probe failed ({h['consecutive_failures']}/{OFFLINE_FAIL_THRESHOLD} "
                f"before dashboard shows OFFLINE) - displayed_online={h['displayed_online']}"
            )
        return h["displayed_online"]

def send_notification(title, message):
    """Send push notification via Pushover."""
    if not PUSHOVER_CONFIG["enabled"]:
        return

    try:
        response = requests.post(
            "https://api.pushover.net/1/messages.json",
            data={
                "token": PUSHOVER_CONFIG["api_token"],
                "user": PUSHOVER_CONFIG["user_key"],
                "title": title,
                "message": message,
                "sound": PUSHOVER_CONFIG["sound"]
            },
            timeout=10
        )
        if response.ok:
            logger.info(f"  📱 Notification sent: {title}")
        else:
            logger.warning(f"  Notification failed: {response.text}")
    except Exception as e:
        logger.warning(f"  Notification error: {e}")

def sync_comfyui_jobs():
    """Background thread to sync job history from ComfyUI instances."""
    # Wait a bit for startup
    time.sleep(10)

    while True:
        try:
            conn = sqlite3.connect(CONFIG["db_path"])
            jobs_added = 0

            for target_name, target_config in CONFIG["targets"].items():
                try:
                    target_url = target_config.get("url")
                    if not target_url:
                        continue

                    # Fetch recent history from ComfyUI
                    response = requests.get(f"{target_url}/history?max_items=50", timeout=10)
                    if not response.ok:
                        logger.debug(f"  Failed to fetch history from {target_name}: {response.status_code}")
                        continue

                    history = response.json()

                    for prompt_id, job_data in history.items():
                        # Check if this job already exists in our database
                        cursor = conn.execute("SELECT id FROM jobs WHERE id = ?", (prompt_id[:8],))
                        if cursor.fetchone():
                            continue  # Already tracked

                        # Extract job info from ComfyUI history
                        prompt_info = job_data.get("prompt", [])
                        status_info = job_data.get("status", {})
                        outputs = job_data.get("outputs", {})

                        # Get timestamps
                        status_msgs = status_info.get("status_str", "")
                        completed = status_info.get("completed", False)

                        # Try to get execution time from status
                        exec_info = status_info.get("messages", [])
                        start_time = None
                        end_time = None
                        for msg in exec_info:
                            if len(msg) >= 2:
                                if msg[0] == "execution_start":
                                    start_time = msg[1].get("timestamp") if isinstance(msg[1], dict) else None
                                elif msg[0] == "execution_success":
                                    end_time = msg[1].get("timestamp") if isinstance(msg[1], dict) else None

                        # Use current time as submitted_at if we don't have better info
                        # ComfyUI timestamps are in milliseconds, convert to seconds
                        submitted_at = datetime.now().isoformat()
                        if start_time:
                            submitted_at = datetime.fromtimestamp(start_time / 1000).isoformat()

                        completed_at = None
                        if completed and end_time:
                            completed_at = datetime.fromtimestamp(end_time / 1000).isoformat()
                        elif completed:
                            completed_at = datetime.now().isoformat()

                        # Analyze workflow for model types
                        model_types = []
                        is_video = False
                        estimated_vram = 12  # Default

                        if len(prompt_info) >= 3 and isinstance(prompt_info[2], dict):
                            workflow = prompt_info[2]
                            for node_id, node in workflow.items():
                                class_type = node.get("class_type", "").lower()
                                inputs = node.get("inputs", {})

                                # Detect model types
                                if "flux" in class_type or any("flux" in str(v).lower() for v in inputs.values() if isinstance(v, str)):
                                    if "flux/sd3" not in model_types:
                                        model_types.append("flux/sd3")
                                        estimated_vram = max(estimated_vram, 24)
                                elif "animatediff" in class_type or "wan" in class_type or "video" in class_type:
                                    if "video" not in model_types:
                                        model_types.append("video")
                                        is_video = True
                                        estimated_vram = max(estimated_vram, 48)
                                elif "sdxl" in class_type or any("sdxl" in str(v).lower() for v in inputs.values() if isinstance(v, str)):
                                    if "sdxl" not in model_types:
                                        model_types.append("sdxl")
                                        estimated_vram = max(estimated_vram, 12)

                        if not model_types:
                            model_types = ["standard"]

                        # Build routing hints
                        routing_hints = json.dumps({
                            "model_types": model_types,
                            "is_video": is_video,
                            "estimated_vram": estimated_vram,
                            "synced_from_comfyui": True
                        })

                        # Insert job
                        job_status = "completed" if completed else "running"
                        try:
                            conn.execute(
                                """INSERT OR IGNORE INTO jobs
                                   (id, target, status, submitted_at, started_at, completed_at, routing_hints)
                                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                                (prompt_id[:8], target_name, job_status, submitted_at, submitted_at, completed_at, routing_hints)
                            )
                            jobs_added += 1
                        except Exception as insert_err:
                            logger.error(f"  Failed to insert job {prompt_id[:8]}: {insert_err}")

                    conn.commit()

                except Exception as e:
                    logger.warning(f"Error syncing jobs from {target_name}: {e}")

            conn.close()

        except Exception as e:
            logger.error(f"Job sync error: {e}")

        time.sleep(30)  # Sync every 30 seconds

def init_db():
    """Initialize SQLite database for job tracking and metrics history."""
    db_dir = Path(CONFIG["db_path"]).parent
    db_dir.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(CONFIG["db_path"])
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            target TEXT NOT NULL,
            status TEXT DEFAULT 'queued',
            submitted_at TEXT,
            started_at TEXT,
            completed_at TEXT,
            workflow_json TEXT,
            routing_hints TEXT,
            result TEXT,
            error TEXT
        )
    """)
    # Historical metrics table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS metrics_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            target TEXT NOT NULL,
            gpu_util REAL,
            gpu_temp REAL,
            gpu_watts REAL,
            cpu_percent REAL,
            vram_percent REAL,
            ram_percent REAL,
            swap_percent REAL,
            disk_read_mbps REAL,
            disk_write_mbps REAL,
            net_rx_mbps REAL,
            net_tx_mbps REAL,
            queue_depth INTEGER
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_metrics_timestamp ON metrics_history(timestamp)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_metrics_target ON metrics_history(target)")
    # Model-serving stats (2026-07-29): per-request records from gandalf's
    # llama-swap (its /api/metrics buffer is in-memory and lost on restart -
    # persisting here is what makes hour/day/week windows survive) . . .
    conn.execute("""
        CREATE TABLE IF NOT EXISTS model_requests (
            box TEXT NOT NULL,
            req_id INTEGER NOT NULL,
            ts TEXT NOT NULL,
            model TEXT,
            output_tokens INTEGER,
            gen_tps REAL,
            PRIMARY KEY (box, req_id, ts)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_model_requests_box_ts ON model_requests(box, ts)")
    # . . . and Prometheus counter samples from frodo's bare llama-server
    # (tokens_predicted_total / tokens_predicted_seconds_total), sampled every
    # MODEL_SERVING_INTERVAL so rates and serving-time-only averages can be
    # derived across restarts.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS model_counter_samples (
            box TEXT NOT NULL,
            ts TEXT NOT NULL,
            tokens_total REAL,
            gen_seconds_total REAL,
            PRIMARY KEY (box, ts)
        )
    """)
    conn.commit()
    conn.close()


def collect_metrics():
    """Background thread to collect and store metrics every minute."""
    while True:
        try:
            conn = sqlite3.connect(CONFIG["db_path"])
            timestamp = datetime.now().isoformat()

            for target_name, target_config in CONFIG["targets"].items():
                try:
                    status = get_target_status(target_name, target_config)
                    queue_depth = status.get("queue_running", 0) + status.get("queue_pending", 0)

                    conn.execute("""
                        INSERT INTO metrics_history
                        (timestamp, target, gpu_util, gpu_temp, gpu_watts, cpu_percent,
                         vram_percent, ram_percent, swap_percent, disk_read_mbps,
                         disk_write_mbps, net_rx_mbps, net_tx_mbps, queue_depth)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        timestamp,
                        target_name,
                        status.get("gpu_util"),
                        status.get("gpu_temp"),
                        status.get("gpu_watts"),
                        status.get("cpu_percent"),
                        status.get("gpu", {}).get("vram_percent") if status.get("gpu") else None,
                        status.get("ram", {}).get("percent") if status.get("ram") else None,
                        status.get("swap", {}).get("percent") if status.get("swap") else None,
                        status.get("disk_io", {}).get("read_mbps") if status.get("disk_io") else None,
                        status.get("disk_io", {}).get("write_mbps") if status.get("disk_io") else None,
                        status.get("net_io", {}).get("rx_mbps") if status.get("net_io") else None,
                        status.get("net_io", {}).get("tx_mbps") if status.get("net_io") else None,
                        queue_depth
                    ))
                except Exception as e:
                    logger.warning(f"Failed to collect metrics for {target_name}: {e}")

            conn.commit()

            # Cleanup old metrics (keep 30 days)
            cutoff = (datetime.now() - timedelta(days=30)).isoformat()
            conn.execute("DELETE FROM metrics_history WHERE timestamp < ?", (cutoff,))
            conn.commit()
            conn.close()

        except Exception as e:
            logger.error(f"Metrics collection error: {e}")

        time.sleep(METRICS_INTERVAL)

# ComfyUI watchdog settings
WATCHDOG_INTERVAL = 30           # Check every 30 seconds
WATCHDOG_FAILURES_BEFORE_RESTART = 3  # 3 consecutive failures (~90s) before restart
WATCHDOG_RESTART_COOLDOWN = 300  # 5 minutes between restart attempts per target

def restart_comfyui(target_name, target_config):
    """Restart ComfyUI service on a target via SSH."""
    ssh_host = target_config.get("ssh_host")
    ssh_user = target_config.get("ssh_user", "ben")
    if not ssh_host:
        return False
    try:
        client = get_ssh_client(ssh_host, ssh_user)
        if client is None:
            logger.warning(f"  Watchdog: Cannot restart ComfyUI on {target_name}: SSH circuit breaker open")
            return False
        stdin, stdout, stderr = client.exec_command("sudo systemctl restart comfyui")
        exit_status = stdout.channel.recv_exit_status()
        if exit_status == 0:
            logger.info(f"  Watchdog: Restarted comfyui.service on {target_name}")
            return True
        else:
            error = stderr.read().decode().strip()
            logger.error(f"  Watchdog: Failed to restart ComfyUI on {target_name}: {error}")
            return False
    except Exception as e:
        logger.error(f"  Watchdog: SSH error restarting ComfyUI on {target_name}: {e}")
        return False

def comfyui_watchdog_loop():
    """Background thread that monitors ComfyUI and auto-restarts if down."""
    time.sleep(30)  # Wait for startup
    logger.info("Watchdog: ComfyUI auto-restart monitor started")

    while True:
        try:
            for target_name, target_config in CONFIG["targets"].items():
                url = target_config.get("url")
                if not url:
                    continue

                # Initialize state
                if target_name not in comfyui_watchdog:
                    comfyui_watchdog[target_name] = {
                        "consecutive_failures": 0,
                        "last_restart": datetime.min,
                        "was_down": False,
                        "notifications_sent": 0,
                        "last_notification": datetime.min
                    }
                state = comfyui_watchdog[target_name]

                # Check if ComfyUI responds
                try:
                    response = requests.get(f"{url}/queue", timeout=5)
                    is_up = response.ok
                except Exception:
                    is_up = False

                if is_up:
                    if state["was_down"]:
                        logger.info(f"  Watchdog: {target_name} ComfyUI is back online")
                    state["consecutive_failures"] = 0
                    state["was_down"] = False
                    state["notifications_sent"] = 0
                else:
                    state["consecutive_failures"] += 1
                    state["was_down"] = True

                    if state["consecutive_failures"] == 1:
                        logger.warning(f"  Watchdog: {target_name} ComfyUI not responding (1st failure)")
                    elif state["consecutive_failures"] >= WATCHDOG_FAILURES_BEFORE_RESTART:
                        # Check cooldown
                        since_last = (datetime.now() - state["last_restart"]).total_seconds()
                        if since_last < WATCHDOG_RESTART_COOLDOWN:
                            remaining = int(WATCHDOG_RESTART_COOLDOWN - since_last)
                            logger.warning(f"  Watchdog: {target_name} still down but cooldown active ({remaining}s remaining)")
                        else:
                            logger.warning(f"  Watchdog: {target_name} ComfyUI down for {state['consecutive_failures']} checks, restarting...")

                            # Notification #1: first restart attempt
                            if state["notifications_sent"] == 0:
                                send_notification(
                                    f"🔄 Restarting {target_name.capitalize()} ComfyUI",
                                    f"ComfyUI on {target_name} unresponsive for ~{state['consecutive_failures'] * WATCHDOG_INTERVAL}s. Auto-restarting."
                                )
                                state["notifications_sent"] = 1
                                state["last_notification"] = datetime.now()

                            # Notification #2: still down after 1 hour, one final reminder
                            elif state["notifications_sent"] == 1:
                                since_notif = (datetime.now() - state["last_notification"]).total_seconds()
                                if since_notif >= 3600:
                                    send_notification(
                                        f"⚠️ {target_name.capitalize()} ComfyUI Still Down",
                                        f"ComfyUI on {target_name} has been down for over an hour despite restart attempts."
                                    )
                                    state["notifications_sent"] = 2

                            success = restart_comfyui(target_name, target_config)
                            state["last_restart"] = datetime.now()
                            state["consecutive_failures"] = 0

        except Exception as e:
            logger.error(f"Watchdog error: {e}")

        time.sleep(WATCHDOG_INTERVAL)


# SSH connection cache
ssh_clients = {}

# SSH circuit breaker - prevents connection flood when a host is unreachable
ssh_circuit = {}  # {key: {"failures": int, "backoff_until": datetime, "backoff_secs": int}}
SSH_CIRCUIT_MAX_FAILURES = 3
SSH_CIRCUIT_INITIAL_BACKOFF = 30   # seconds
SSH_CIRCUIT_MAX_BACKOFF = 300      # 5 minutes

# Disk usage cache (updates every 5 minutes)
disk_cache = {}  # {host: {"data": {...}, "updated_at": datetime}}
DISK_CACHE_TTL = 300  # 5 minutes

# /api/fleet cache - it sshes into all 6 fleet-row hosts per call; a short
# TTL keeps fast/parallel browser polls from multiplying ssh sessions.
fleet_cache = {"data": None, "ts": 0.0}
FLEET_CACHE_TTL = 15  # seconds
fleet_cache_lock = threading.Lock()
STATUS_PROBE_TIMEOUT = 15  # Keep one unreachable SSH host from blocking /api/status
SSH_COMMAND_TIMEOUT = 5

# Historical metrics collection interval
METRICS_INTERVAL = 60  # Collect every 60 seconds

def get_ssh_client(host, user):
    """Get or create cached SSH client for a host, with circuit breaker."""
    key = f"{user}@{host}"

    # Circuit breaker: if too many recent failures, skip attempting connection
    if key in ssh_circuit:
        cb = ssh_circuit[key]
        if cb["failures"] >= SSH_CIRCUIT_MAX_FAILURES:
            if datetime.now() < cb["backoff_until"]:
                return None  # Circuit open - don't attempt connection
            else:
                logger.info(f"SSH circuit breaker: retrying {key} after {cb['backoff_secs']}s backoff")

    # Try cached connection first
    if key in ssh_clients:
        client = ssh_clients[key]
        if client.get_transport() and client.get_transport().is_active():
            # Working connection - reset circuit breaker
            if key in ssh_circuit:
                del ssh_circuit[key]
            return client

    # Create new connection
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            host, username=user, timeout=5, banner_timeout=5, auth_timeout=5
        )
        ssh_clients[key] = client

        # Success - reset circuit breaker
        if key in ssh_circuit:
            logger.info(f"SSH circuit breaker: {key} recovered")
            del ssh_circuit[key]

        return client
    except Exception as e:
        # Track failure
        if key not in ssh_circuit:
            ssh_circuit[key] = {"failures": 0, "backoff_until": datetime.now(), "backoff_secs": SSH_CIRCUIT_INITIAL_BACKOFF}

        cb = ssh_circuit[key]
        cb["failures"] += 1

        if cb["failures"] >= SSH_CIRCUIT_MAX_FAILURES:
            cb["backoff_until"] = datetime.now() + timedelta(seconds=cb["backoff_secs"])
            logger.warning(f"SSH circuit breaker OPEN for {key}: {cb['failures']} consecutive failures, backing off {cb['backoff_secs']}s")
            cb["backoff_secs"] = min(cb["backoff_secs"] * 2, SSH_CIRCUIT_MAX_BACKOFF)

        # Clean up bad cached client
        if key in ssh_clients:
            try:
                ssh_clients[key].close()
            except:
                pass
            del ssh_clients[key]

        raise

def get_ssh_metrics(host, user, expected_host=None, hostname_aka=()):
    """Get CPU, GPU, swap, disk I/O, and network I/O metrics via SSH.

    `expected_host` is the name of the CARD these metrics will fill. When given,
    the box is asked who it is and the reply is checked before any number is
    returned - same wrong-box guard as the fleet row (see _fleet_host_matches).
    On a mismatch the result carries `mismatch`/`reported_host` and NO metrics,
    so a card can never render a different machine's dials. gandalf is the reason
    this matters here: it currently sits on a dynamic lease, so it is the target
    most likely to move again.
    """
    result = {
        "cpu_percent": None,
        "cpu_temp": None,
        "gpu_name": None,
        "gpu_count": None,
        "gpu_watts": None,
        "gpu_power_limit": None,
        "gpu_temp": None,
        "gpu_util": None,
        "vram_total_gb": None,
        "vram_used_gb": None,
        "vram_percent": None,
        "ram_total_gb": None,
        "ram_used_gb": None,
        "ram_percent": None,
        "swap_percent": None,
        "swap_used_gb": None,
        "swap_total_gb": None,
        "disk_read_mbps": None,
        "disk_write_mbps": None,
        "net_rx_mbps": None,
        "net_tx_mbps": None
    }
    try:
        client = get_ssh_client(host, user)
        if client is None:
            return result  # Circuit breaker open, return empty metrics

        # WRONG-BOX GUARD: before trusting a single dial, make the box say who it
        # is. Cheap (one `hostname`) and it runs first, so a mismatch costs us the
        # card's numbers rather than filling them from the wrong machine.
        if expected_host:
            _, who_out, _ = client.exec_command("hostname", timeout=SSH_COMMAND_TIMEOUT)
            reported = who_out.read().decode().strip()
            if not _fleet_host_matches(expected_host, reported, hostname_aka):
                logger.warning(
                    f"{expected_host} metrics: WRONG BOX - asked {host} for "
                    f"'{expected_host}', got '{reported}'. Refusing to render its "
                    f"dials. Check DNS/mDNS/DHCP for {expected_host}."
                )
                result["mismatch"] = True
                result["reported_host"] = reported.split(".")[0]
                return result

        # Include GPU identity and VRAM here so cards do not depend on ComfyUI's
        # /system_stats endpoint to render their gauges.
        stdin, stdout, stderr = client.exec_command(
            "nvidia-smi --query-gpu=name,power.draw,power.limit,temperature.gpu,"
            "utilization.gpu,memory.total,memory.used --format=csv,noheader,nounits",
            timeout=SSH_COMMAND_TIMEOUT,
        )
        gpu_output = stdout.read().decode().strip()
        if gpu_output:
            # MULTI-GPU (2026-07-29): aragorn has two cards (RTX 5070 + 5080), so
            # nvidia-smi returns one line PER GPU. This used to split the whole
            # blob on commas and crash on "2\nNVIDIA GeForce RTX 5080", which
            # took the entire SSH metrics call down with it (no CPU, RAM, temp,
            # or I/O for that box). Now every line is parsed and the box is
            # reported as one logical accelerator: VRAM and watts SUM, temp is
            # the HOTTEST card, utilization is the average.
            gpus = []
            for line in gpu_output.splitlines():
                parts = [part.strip() for part in line.split(",")]
                if len(parts) < 7:
                    continue
                try:
                    gpus.append({
                        "name": parts[0],
                        "watts": float(parts[1]),
                        "limit": float(parts[2]),
                        "temp": float(parts[3]),
                        "util": float(parts[4]),
                        "vram_total_mb": float(parts[5]),
                        "vram_used_mb": float(parts[6]),
                    })
                except ValueError:
                    continue
            if gpus:
                names = [g["name"] for g in gpus]
                if len(gpus) == 1:
                    result["gpu_name"] = names[0]
                elif len(set(names)) == 1:
                    result["gpu_name"] = f"{len(gpus)} x {names[0]}"
                else:
                    result["gpu_name"] = " + ".join(names)
                result["gpu_count"] = len(gpus)
                result["gpu_watts"] = round(sum(g["watts"] for g in gpus), 1)
                result["gpu_power_limit"] = round(sum(g["limit"] for g in gpus))
                result["gpu_temp"] = max(g["temp"] for g in gpus)
                result["gpu_util"] = round(sum(g["util"] for g in gpus) / len(gpus), 1)
                vram_total_mb = sum(g["vram_total_mb"] for g in gpus)
                vram_used_mb = sum(g["vram_used_mb"] for g in gpus)
                result["vram_total_gb"] = round(vram_total_mb / 1024, 1)
                result["vram_used_gb"] = round(vram_used_mb / 1024, 1)
                result["vram_percent"] = (
                    round(vram_used_mb / vram_total_mb * 100, 1)
                    if vram_total_mb > 0 else 0
                )

        # Get CPU usage using top (parse idle and subtract from 100)
        stdin, stdout, stderr = client.exec_command(
            "top -bn1 | grep '%Cpu' | sed 's/,/ /g' | awk '{for(i=1;i<=NF;i++) if($i==\"id\") print 100-$(i-1)}'",
            timeout=SSH_COMMAND_TIMEOUT,
        )
        cpu_output = stdout.read().decode().strip()
        if cpu_output:
            result["cpu_percent"] = round(float(cpu_output), 1)

        # CPU package temperature (2026-07-29): needed by boxes with no readable
        # GPU (aragorn before its nvidia driver lands) so their card still has a
        # TEMP dial. Same sensor preference order as FLEET_METRICS_CMD.
        stdin, stdout, stderr = client.exec_command(
            "tp=; for h in /sys/class/hwmon/hwmon*; do case \"$(cat $h/name 2>/dev/null)\" in "
            "k10temp|coretemp) tp=$(cat $h/temp1_input 2>/dev/null); break;; esac; done; "
            "[ -z \"$tp\" ] && tp=$(cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null); "
            "echo ${tp:-0}",
            timeout=SSH_COMMAND_TIMEOUT,
        )
        temp_output = stdout.read().decode().strip()
        if temp_output and temp_output.isdigit() and int(temp_output) > 0:
            result["cpu_temp"] = round(int(temp_output) / 1000)

        # System RAM (2026-07-29): this used to come from ComfyUI's
        # /system_stats endpoint; with ComfyUI disabled fleet-wide that source
        # silently vanished and RAM dropped off the big-machine cards. Read it
        # here over SSH like everything else ("used" from free already
        # excludes buffers/cache, matching the fleet tiles).
        stdin, stdout, stderr = client.exec_command(
            "free -b | grep Mem | awk '{print $2, $3}'",
            timeout=SSH_COMMAND_TIMEOUT,
        )
        ram_output = stdout.read().decode().strip()
        if ram_output:
            parts = ram_output.split()
            if len(parts) >= 2:
                total = int(parts[0])
                used = int(parts[1])
                result["ram_total_gb"] = round(total / (1024**3), 1)
                result["ram_used_gb"] = round(used / (1024**3), 1)
                result["ram_percent"] = round(used / total * 100, 1) if total > 0 else 0

        # Get swap usage
        stdin, stdout, stderr = client.exec_command(
            "free -b | grep Swap | awk '{print $2, $3}'",
            timeout=SSH_COMMAND_TIMEOUT,
        )
        swap_output = stdout.read().decode().strip()
        if swap_output:
            parts = swap_output.split()
            if len(parts) >= 2:
                total = int(parts[0])
                used = int(parts[1])
                result["swap_total_gb"] = round(total / (1024**3), 1)
                result["swap_used_gb"] = round(used / (1024**3), 1)
                result["swap_percent"] = round(used / total * 100, 1) if total > 0 else 0

        # Get disk I/O (using iostat if available, fallback to /proc/diskstats)
        stdin, stdout, stderr = client.exec_command(
            "iostat -d -k 1 2 2>/dev/null | tail -n +7 | head -1 | awk '{print $3, $4}' || "
            "cat /proc/diskstats | awk '/nvme0n1 |sda /{print $6*512/1024, $10*512/1024}' | head -1",
            timeout=SSH_COMMAND_TIMEOUT,
        )
        disk_io_output = stdout.read().decode().strip()
        if disk_io_output:
            parts = disk_io_output.split()
            if len(parts) >= 2:
                result["disk_read_mbps"] = round(float(parts[0]) / 1024, 1)
                result["disk_write_mbps"] = round(float(parts[1]) / 1024, 1)

        # Get network I/O (bytes per second on primary interface)
        stdin, stdout, stderr = client.exec_command(
            "cat /proc/net/dev | grep -E 'eth0|eno|enp' | head -1 | awk '{print $2, $10}'",
            timeout=SSH_COMMAND_TIMEOUT,
        )
        net_output1 = stdout.read().decode().strip()
        if net_output1:
            time.sleep(0.5)
            stdin, stdout, stderr = client.exec_command(
                "cat /proc/net/dev | grep -E 'eth0|eno|enp' | head -1 | awk '{print $2, $10}'",
                timeout=SSH_COMMAND_TIMEOUT,
            )
            net_output2 = stdout.read().decode().strip()
            if net_output2:
                parts1 = net_output1.split()
                parts2 = net_output2.split()
                if len(parts1) >= 2 and len(parts2) >= 2:
                    rx_diff = int(parts2[0]) - int(parts1[0])
                    tx_diff = int(parts2[1]) - int(parts1[1])
                    result["net_rx_mbps"] = round(rx_diff * 2 / (1024 * 1024), 1)  # *2 because 0.5s sample
                    result["net_tx_mbps"] = round(tx_diff * 2 / (1024 * 1024), 1)

    except Exception as e:
        logger.warning(f"SSH metrics error for {host}: {e}")
        # Close bad connection so it reconnects next time
        if f"{user}@{host}" in ssh_clients:
            try:
                ssh_clients[f"{user}@{host}"].close()
            except:
                pass
            del ssh_clients[f"{user}@{host}"]

    return result


loaded_models_cache = {}
loaded_models_cache_lock = threading.Lock()
LOADED_MODELS_CACHE_TTL = 15

MODEL_DISPLAY_NAMES = {
    "devstral-small-2-24b": "Devstral-Small-2 24B",
    "gemma4-26b": "Gemma4 26B",
    "glm-4.5-air": "GLM-4.5-Air 106B",
    "qwen3-235b-a22b": "Qwen3-235B-A22B",
    "qwen3-coder-next": "Qwen3-Coder-Next 80B",
    "qwen3.6-35b-a3b": "Qwen3.6-35B-A3B",
}


def _model_display_name(model_id):
    """Turn live server IDs into stable, readable dashboard labels."""
    if model_id.startswith("qwen-daily-"):
        return "Qwen3.6-35B-A3B Q8_0"
    return MODEL_DISPLAY_NAMES.get(model_id, model_id)


def _served_model_ids(target_config):
    """Read models that are actually running/served, never the gateway route list."""
    model_ids = []
    available = False
    for source_type, url in target_config.get("model_status_urls", []):
        try:
            response = requests.get(url, timeout=4)
            response.raise_for_status()
            payload = response.json()
            available = True
            records = payload.get("running", []) if source_type == "running" else (
                payload.get("data") or payload.get("models") or []
            )
            for record in records:
                model_id = record if isinstance(record, str) else (
                    record.get("id") or record.get("model") or record.get("name")
                )
                if model_id and model_id not in model_ids:
                    model_ids.append(model_id)
        except Exception as e:
            logger.debug(f"model status unavailable at {url}: {e}")
    return available, model_ids


def _model_processes(target_name, target_config):
    """Return live llama-server processes and their nvidia-smi VRAM footprints."""
    command = (
        "nvidia-smi --query-compute-apps=pid,process_name,used_memory "
        "--format=csv,noheader,nounits"
    )
    rows = []
    if target_name == "gandalf":
        import subprocess
        output = subprocess.run(
            command.split(), capture_output=True, text=True, timeout=5, check=False
        ).stdout
        read_cmdline = lambda pid: Path(f"/proc/{pid}/cmdline").read_bytes().replace(
            b"\x00", b" "
        ).decode(errors="ignore")
    else:
        client = get_ssh_client(
            target_config["ssh_host"], target_config.get("ssh_user", "ben")
        )
        if client is None:
            return rows
        _, stdout, _ = client.exec_command(command, timeout=5)
        output = stdout.read().decode(errors="ignore")

        def read_cmdline(pid):
            _, cmdout, _ = client.exec_command(
                f"tr '\\0' ' ' < /proc/{pid}/cmdline 2>/dev/null", timeout=4
            )
            return cmdout.read().decode(errors="ignore")

    for line in output.splitlines():
        parts = [part.strip() for part in line.split(",", 2)]
        if len(parts) != 3 or not parts[0].isdigit() or "llama-server" not in parts[1]:
            continue
        try:
            rows.append({
                "pid": int(parts[0]),
                "vram_mb": int(parts[2]),
                "cmdline": read_cmdline(int(parts[0])),
            })
        except (OSError, ValueError):
            continue
    return rows


def get_ollama_loaded_models(target_config):
    """What each instance on a box has RESIDENT, and on which card.

    Handles BOTH engines, because aragorn is mid-migration from ollama to
    llama.cpp (2026-07-30) and a per-port engine can change under us:
      - ollama: /api/ps is the honest signal. Its /v1/models lists everything
        PULLED, which would show models that are not loaded at all.
      - llama.cpp: /api/ps 404s. Its /v1/models IS the resident set, because a
        llama-server process holds exactly one model.
    Trying ollama first and falling back means a port can flip engines without
    the dashboard going blank - which is exactly what it just did.
    """
    host = target_config.get("ssh_host")
    models = []
    reachable = False
    for inst in target_config.get("ollama_instances", []):
        port = inst["port"]
        got = False
        # --- ollama path
        try:
            r = requests.get(f"http://{host}:{port}/api/ps", timeout=4)
            if r.ok:
                for m in r.json().get("models", []):
                    name = (m.get("model") or m.get("name") or "").replace(":latest", "")
                    if not name:
                        continue
                    reachable = True
                    got = True
                    models.append({
                        "name": name,
                        "vram_gb": round(m["size_vram"] / (1024 ** 3), 1) if m.get("size_vram") else None,
                        "where": inst.get("where"),
                    })
        except Exception as e:
            logger.debug(f"ollama :{port} /api/ps unavailable: {e}")
        if got:
            continue
        # --- llama.cpp path (a llama-server holds exactly one model, so its
        #     model list IS what is resident)
        try:
            r = requests.get(f"http://{host}:{port}/v1/models", timeout=4)
            if r.ok:
                for m in r.json().get("data", []):
                    mid = m.get("id")
                    if not mid:
                        continue
                    reachable = True
                    meta = m.get("meta") or {}
                    size = meta.get("size")
                    models.append({
                        "name": mid,
                        "vram_gb": round(size / (1024 ** 3), 1) if size else None,
                        "where": inst.get("where"),
                    })
        except Exception as e:
            logger.debug(f"llama.cpp :{port} /v1/models unavailable: {e}")
    return {"available": reachable, "models": models}


def get_loaded_models(target_name, target_config):
    """Live served-model names paired with their real per-process VRAM use."""
    now = time.time()
    with loaded_models_cache_lock:
        cached = loaded_models_cache.get(target_name)
        if cached and now - cached["ts"] < LOADED_MODELS_CACHE_TTL:
            return cached["data"]

    result = {"available": False, "models": []}
    try:
        result["available"], model_ids = _served_model_ids(target_config)
        processes = _model_processes(target_name, target_config) if result["available"] else []
        unused = list(processes)
        for model_id in model_ids:
            normalized_id = re.sub(r"[^a-z0-9]", "", model_id.lower())
            match = next(
                (
                    proc for proc in unused
                    if model_id.lower() in proc["cmdline"].lower()
                    or normalized_id in re.sub(r"[^a-z0-9]", "", proc["cmdline"].lower())
                ),
                None,
            )
            if match is None and len(model_ids) == 1 and len(unused) == 1:
                match = unused[0]
            if match is not None:
                unused.remove(match)
            result["models"].append({
                "name": _model_display_name(model_id),
                "vram_gb": round(match["vram_mb"] / 1024, 1) if match else None,
            })
    except Exception as e:
        logger.debug(f"loaded-model check failed for {target_name}: {e}")

    with loaded_models_cache_lock:
        loaded_models_cache[target_name] = {"data": result, "ts": time.time()}
    return result


def get_disk_usage(host, user, path="/"):
    """Get disk usage via SSH with caching (updates every 5 minutes)."""
    cache_key = f"{user}@{host}:{path}"
    now = datetime.now()

    # Check cache
    if cache_key in disk_cache:
        cached = disk_cache[cache_key]
        age = (now - cached["updated_at"]).total_seconds()
        if age < DISK_CACHE_TTL:
            return cached["data"]

    result = {"total_gb": None, "used_gb": None, "percent": None}
    try:
        client = get_ssh_client(host, user)
        if client is None:
            return result  # Circuit breaker open, return empty

        # Get disk usage for specified path
        stdin, stdout, stderr = client.exec_command(
            f"df -B1 {path} | tail -1 | awk '{{print $2, $3, $5}}'",
            timeout=SSH_COMMAND_TIMEOUT,
        )
        disk_output = stdout.read().decode().strip()
        if disk_output:
            parts = disk_output.split()
            if len(parts) >= 3:
                total_bytes = int(parts[0])
                used_bytes = int(parts[1])
                result["total_gb"] = round(total_bytes / (1024**3), 1)
                result["used_gb"] = round(used_bytes / (1024**3), 1)
                result["percent"] = round(used_bytes / total_bytes * 100, 1) if total_bytes > 0 else 0

        # Update cache
        disk_cache[cache_key] = {"data": result, "updated_at": now}

    except Exception as e:
        logger.warning(f"Disk usage error for {host}: {e}")

    return result

def set_gpu_power_limit(host, user, watts):
    """Set GPU power limit via SSH."""
    try:
        client = get_ssh_client(host, user)
        if client is None:
            logger.warning(f"Cannot set power limit on {host}: SSH circuit breaker open")
            return False
        cmd = f"sudo nvidia-smi -pl {int(watts)}"
        stdin, stdout, stderr = client.exec_command(cmd)
        exit_status = stdout.channel.recv_exit_status()
        if exit_status == 0:
            logger.info(f"Set {host} GPU power limit to {watts}W")
            return True
        else:
            error = stderr.read().decode().strip()
            logger.error(f"Failed to set power limit on {host}: {error}")
            return False
    except Exception as e:
        logger.error(f"SSH error setting power limit on {host}: {e}")
        return False

def clear_swap(host, user):
    """Clear swap by turning it off and back on via SSH."""
    try:
        client = get_ssh_client(host, user)
        if client is None:
            logger.warning(f"Cannot clear swap on {host}: SSH circuit breaker open")
            return False
        cmd = "sudo swapoff -a && sudo swapon -a"
        stdin, stdout, stderr = client.exec_command(cmd)
        exit_status = stdout.channel.recv_exit_status()
        if exit_status == 0:
            logger.info(f"Cleared swap on {host}")
            return True
        else:
            error = stderr.read().decode().strip()
            logger.error(f"Failed to clear swap on {host}: {error}")
            return False
    except Exception as e:
        logger.error(f"SSH error clearing swap on {host}: {e}")
        return False

# --- CI queue depth + model-route health (2026-07-19) -----------------------
# Secrets are never stored/hardcoded here. Both are read live, over the same
# ben@gandalf SSH channel already used above for reboot/power actions, from
# their existing canonical files - then cached briefly so a burst of page
# loads/tab polls doesn't multiply SSH sessions, GitHub API calls, or gateway
# hits.
_secret_cache = {}  # {"path:VAR": {"value": str, "ts": float}}
_secret_cache_lock = threading.Lock()

def _read_remote_env_value(var_name, remote_path, ttl):
    """Read one KEY=VALUE line out of a file on gandalf via SSH, cached for `ttl`s.

    Returns None (never raises) on any failure - callers must treat that as
    "signal unavailable right now", not "value is empty/off".
    """
    cache_key = f"{remote_path}:{var_name}"
    now = time.time()
    with _secret_cache_lock:
        cached = _secret_cache.get(cache_key)
        if cached and (now - cached["ts"]) < ttl:
            return cached["value"]
    value = None
    try:
        client = get_ssh_client(CONFIG["targets"]["gandalf"]["ssh_host"], CONFIG["targets"]["gandalf"]["ssh_user"])
        if client is not None:
            _, stdout, _ = client.exec_command(f"grep '^{var_name}=' {remote_path} | head -1", timeout=10)
            line = stdout.read().decode().strip()
            if "=" in line:
                value = line.split("=", 1)[1].strip()
    except Exception as e:
        logger.debug(f"Could not read {var_name} from gandalf:{remote_path}: {e}")
    if value:
        with _secret_cache_lock:
            _secret_cache[cache_key] = {"value": value, "ts": now}
    return value

def get_gh_ci_token():
    """Shire autoscaler's own GH App installation token (refreshed there every 15 min)."""
    return _read_remote_env_value("GH_TOKEN", GH_TOKEN_ENV_PATH, ttl=600)

def get_gateway_key():
    """Fleet gateway master key (static; re-checked occasionally in case it rotates)."""
    return _read_remote_env_value("LITELLM_MASTER_KEY", GATEWAY_KEY_ENV_PATH, ttl=1800)

ci_queue_cache = {"data": None, "ts": 0.0}
ci_queue_cache_lock = threading.Lock()
CI_QUEUE_CACHE_TTL = 45  # seconds - don't hammer the GitHub API

def get_ci_queue_status():
    """Current GitHub Actions queue depth and job-level activity for armbrain."""
    now = time.time()
    with ci_queue_cache_lock:
        if ci_queue_cache["data"] is not None and (now - ci_queue_cache["ts"]) < CI_QUEUE_CACHE_TTL:
            return ci_queue_cache["data"]

    result = {"available": False, "queued": 0, "in_progress": 0, "repo": GITHUB_CI_REPO}
    token = get_gh_ci_token()
    if token:
        try:
            headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}
            base = f"https://api.github.com/repos/{GITHUB_CI_REPO}/actions/runs"
            # GitHub can strand queued runs when a branch disappears. Keep those
            # visible as orphans, but exclude them from the live queue-depth signal.
            from datetime import timezone
            stale_cut = datetime.now(timezone.utc) - timedelta(hours=48)
            cutoff = stale_cut.strftime("%Y-%m-%dT%H:%M:%SZ")
            q = requests.get(
                base,
                params={"status": "queued", "per_page": 100, "created": f">={cutoff}"},
                headers=headers,
                timeout=8,
            )
            ip = requests.get(
                base,
                params={"status": "in_progress", "per_page": 100, "created": f">={cutoff}"},
                headers=headers,
                timeout=8,
            )
            if q.ok and ip.ok:
                result["available"] = True
                result["queued"] = q.json().get("total_count", 0)
                result["in_progress"] = ip.json().get("total_count", 0)

                # A workflow run may contain many concurrent jobs. Count the jobs
                # actually running so the dashboard reflects fleet workload.
                try:
                    active_jobs = 0
                    active_runners = set()
                    for run in (ip.json().get("workflow_runs") or [])[:12]:
                        jobs = requests.get(
                            f"{base}/{run['id']}/jobs",
                            params={"per_page": 100},
                            headers=headers,
                            timeout=8,
                        )
                        if not jobs.ok:
                            continue
                        for job in jobs.json().get("jobs", []):
                            if job.get("status") == "in_progress":
                                active_jobs += 1
                                if job.get("runner_name"):
                                    active_runners.add(job["runner_name"])

                    orphaned = 0
                    all_queued = requests.get(
                        base,
                        params={"status": "queued", "per_page": 100},
                        headers=headers,
                        timeout=8,
                    )
                    if all_queued.ok:
                        for run in all_queued.json().get("workflow_runs", []):
                            try:
                                created = datetime.strptime(
                                    run.get("created_at") or "", "%Y-%m-%dT%H:%M:%SZ"
                                ).replace(tzinfo=timezone.utc)
                            except ValueError:
                                continue
                            if created < stale_cut:
                                orphaned += 1

                    result["active_jobs"] = active_jobs
                    result["active_runners"] = sorted(active_runners)
                    result["active_runner_count"] = len(active_runners)
                    result["orphaned_queued"] = orphaned
                except Exception as e:
                    # An unreadable jobs API must not silently look like zero work.
                    logger.warning(f"CI job-level count failed: {e}")
                    result["active_jobs"] = None
                    result["active_runner_count"] = None
            else:
                logger.warning(f"CI queue check: GitHub returned {q.status_code}/{ip.status_code}")
        except Exception as e:
            logger.warning(f"CI queue check failed: {e}")

    with ci_queue_cache_lock:
        ci_queue_cache["data"] = result
        ci_queue_cache["ts"] = time.time()
    return result

route_health_cache = {"data": None, "ts": 0.0}
route_health_cache_lock = threading.Lock()
ROUTE_HEALTH_CACHE_TTL = 30  # seconds

# --- Route topology (2026-07-29, Ben: "move the coding pathways down to the
# machine they are running on"). Each route badge renders on ITS box's card, so
# the dashboard has to know which box serves which route. Parsed from the LIVE
# gateway config rather than hardcoded, so a route moving boxes can't silently
# leave the dashboard lying. Falls back to the known map if the file is gone.
GATEWAY_CONFIG_PATH = "/workspace/gandalf-gateway/fleet-litellm-config.yaml"
ROUTE_TOPOLOGY_TTL = 300  # seconds - config changes are rare
# host in api_base -> the box name this dashboard uses ("pippen" is the
# hostname, "pippin" is the card name).
_ROUTE_HOST_TO_BOX = {
    "gandalf": "gandalf", "frodo": "frodo", "pippen": "pippin",
    "pippin": "pippin", "shadowfax": "shadowfax", "sam": "sam",
    "aragorn": "aragorn", "127.0.0.1": "gandalf", "localhost": "gandalf",
    # Derived from FLEET_IPS so the temporary post-outage map (2026-07-30) can't
    # drift from the target definitions above. NOTE 192.168.1.10 is deliberately
    # NOT mapped to gandalf any more: a GL-KVM dongle holds that address tonight,
    # so claiming it is gandalf would attribute another device's routes to gandalf.
    FLEET_IPS["aragorn"]: "aragorn",
    FLEET_IPS["gandalf"]: "gandalf",
    FLEET_IPS["frodo"]: "frodo",
    FLEET_IPS["pippen"]: "pippin",
    FLEET_IPS["shadowfax"]: "shadowfax",
}
_ROUTE_TOPOLOGY_FALLBACK = {
    "flagship": {"box": "gandalf", "model": "qwen3.6-35b-a3b-q8-256k", "port": 8889},
    "dense": {"box": "gandalf", "model": "qwen3.6-27b", "port": 8889},
    "reason-27b": {"box": "aragorn", "model": "qwen3.6-27b-q5km", "port": 11436},
    "fast": {"box": "frodo", "model": "qwen3.6-35b-a3b", "port": 8890},
    "code": {"box": "pippin", "model": "qwen3-coder-next", "port": 8891},
    "code-glm": {"box": "gandalf", "model": "glm-4.5-air", "port": 8889},
    "reason": {"box": "gandalf", "model": "gemma4-26b", "port": 8889},
    "coder": {"box": "gandalf", "model": "devstral-small-2-24b", "port": 8889},
    "big": {"box": "gandalf", "model": "qwen3-235b-a22b", "port": 8889},
    "cheap": {"box": "shadowfax", "model": "glm-edge", "port": 8081},
    "reason-oss": {"box": "aragorn", "model": "gpt-oss:20b", "port": 11434},
    "fast-mini": {"box": "aragorn", "model": "qwen3:8b", "port": 11435},
}
route_topology_cache = {"data": None, "ts": 0.0}
route_topology_lock = threading.Lock()


def get_route_topology():
    """route name -> {box, model, port}, read from the live gateway config."""
    now = time.time()
    with route_topology_lock:
        if route_topology_cache["data"] is not None and (now - route_topology_cache["ts"]) < ROUTE_TOPOLOGY_TTL:
            return route_topology_cache["data"]

    topo = {}
    try:
        text = open(GATEWAY_CONFIG_PATH).read()
        for block in re.split(r"\n\s*-\s+model_name:\s*", text)[1:]:
            name = block.split("\n")[0].split("#")[0].strip().strip("\"'")
            if name not in LOCAL_GATEWAY_ROUTES:
                continue
            base = re.search(r"api_base:\s*[\"']?(\S+?)[\"']?\s*(?:$|,|\})", block, re.M)
            model = re.search(r"model:\s*[\"']?([^\"'\s,}]+)", block)
            if not (base and model):
                continue
            host_port = re.sub(r"^https?://", "", base.group(1)).split("/")[0]
            host, _, port = host_port.partition(":")
            topo[name] = {
                "box": _ROUTE_HOST_TO_BOX.get(host.replace(".local", ""), host),
                "model": model.group(1).split("/")[-1],
                "port": int(port) if port.isdigit() else None,
            }
    except Exception as e:
        logger.warning(f"gateway config unreadable ({e}); using fallback route map")

    for name, info in _ROUTE_TOPOLOGY_FALLBACK.items():
        topo.setdefault(name, dict(info))

    with route_topology_lock:
        route_topology_cache["data"] = topo
        route_topology_cache["ts"] = time.time()
    return topo


def get_loaded_route_models():
    """Model ids currently RESIDENT in memory, per box.

    gandalf runs llama-swap, so only one of its four models is loaded at a
    time - /running is the authority. frodo/pippin/shadowfax run a single
    llama-server each, so anything their /v1/models lists IS loaded.
    """
    loaded = {}
    try:
        r = requests.get("http://127.0.0.1:8889/running", timeout=4)
        if r.ok:
            running = r.json().get("running", [])
            ids = set()
            for entry in running:
                if isinstance(entry, str):
                    ids.add(entry)
                elif isinstance(entry, dict):
                    ids.add(entry.get("model") or entry.get("id") or entry.get("name"))
            loaded["gandalf"] = {i for i in ids if i}
    except Exception as e:
        logger.debug(f"gandalf /running unavailable: {e}")

    for port in (11434, 11435):
        try:
            r = requests.get(f"http://{FLEET_IPS['aragorn']}:{port}/api/ps", timeout=4)
            if r.ok:
                ids = {m.get("model") or m.get("name") for m in r.json().get("models", [])}
                loaded.setdefault("aragorn", set()).update(i for i in ids if i)
        except Exception as e:
            logger.debug(f"aragorn ollama :{port} /api/ps unavailable: {e}")

    for box, url in (("frodo", f"http://{FLEET_IPS['frodo']}:8890/v1/models"),
                     ("pippin", f"http://{FLEET_IPS['pippen']}:8891/v1/models"),
                     ("shadowfax", f"http://{FLEET_IPS['shadowfax']}:8081/v1/models")):
        try:
            r = requests.get(url, timeout=4)
            if r.ok:
                loaded[box] = {m.get("id") for m in r.json().get("data", []) if m.get("id")}
        except Exception as e:
            logger.debug(f"{box} model list unavailable: {e}")
    return loaded

def get_model_route_health():
    """Which 🔒 local fleet-gateway routes are live right now vs missing -
    the "is a model route silently gone" signal."""
    now = time.time()
    with route_health_cache_lock:
        if route_health_cache["data"] is not None and (now - route_health_cache["ts"]) < ROUTE_HEALTH_CACHE_TTL:
            return route_health_cache["data"]

    routes_live = {name: False for name in LOCAL_GATEWAY_ROUTES}
    available = False
    key = get_gateway_key()
    if key:
        try:
            resp = requests.get(GATEWAY_MODELS_URL, headers={"Authorization": f"Bearer {key}"}, timeout=6)
            if resp.ok:
                live_ids = {m.get("id") for m in resp.json().get("data", [])}
                for name in LOCAL_GATEWAY_ROUTES:
                    routes_live[name] = name in live_ids
                available = True
            else:
                logger.warning(f"Model route check: gateway returned {resp.status_code}")
        except Exception as e:
            logger.warning(f"Model route check failed: {e}")

    topo = get_route_topology()
    loaded_by_box = get_loaded_route_models()
    routes = []
    for name in LOCAL_GATEWAY_ROUTES:
        info = topo.get(name, {})
        box = info.get("box")
        model = info.get("model")
        # Three states, not two: live+resident = green, live but nothing loaded
        # in memory = grey (llama-swap unloads after idle - that is normal, not
        # an alarm), absent from the gateway = red.
        resident = bool(model and box and model in loaded_by_box.get(box, set()))
        routes.append({
            "name": name,
            "live": routes_live[name],
            "loaded": resident,
            "box": box,
            "model": model,
        })
    result = {"available": available, "routes": routes}
    with route_health_cache_lock:
        route_health_cache["data"] = result
        route_health_cache["ts"] = time.time()
    return result

# --- Fleet stats bar (2026-07-29): CLI agent sessions + CI runner busy/total ---
# (a) CLI agent sessions: count of claude/codex/agy MAIN processes per box.
#     A pid whose parent also matches is collapsed into it (codex's node wrapper
#     spawns the vendor binary - counting both would double every session).
#     gandalf is measured locally (this service runs there); the others go over
#     the same cached SSH channel + circuit breaker as the rest of the file.
#     No session limits are configured anywhere, so these are plain counts.
# (b) CI runners: busy/total per box from the GitHub org runner list, fetched
#     server-side via the gh CLI (authed for ben on gandalf) and cached 60s so
#     the dashboard can't hammer the GitHub API.
AGENT_STAT_BOXES = {
    "gandalf": {"local": True},
    "aragorn": {"ssh_host": FLEET_IPS["aragorn"], "ssh_user": "mac"},
    "frodo":   {"ssh_host": FLEET_IPS["frodo"], "ssh_user": "ben"},
    "pippen":  {"ssh_host": FLEET_IPS["pippen"], "ssh_user": "ben"},
}
# Portable across linux + mac: pgrep the main CLIs by full cmdline, then drop
# any pid whose parent is also in the match set. The regex text itself never
# self-matches (in our own cmdline "claude" is preceded by "(", not "/" or ^).
AGENT_COUNT_CMD = (
    "pids=$(pgrep -f '(^|/)(claude|codex|agy)( |$)' | tr '\\n' ' '); n=0; "
    "for p in $pids; do pp=$(ps -o ppid= -p $p 2>/dev/null | tr -d ' '); "
    "case \" $pids \" in *\" $pp \"*) ;; *) n=$((n+1));; esac; done; "
    "echo agents=$n"
)
GITHUB_RUNNER_ORG = "armbrain-io"

fleet_stats_cache = {"data": None, "ts": 0.0}
fleet_stats_cache_lock = threading.Lock()
FLEET_STATS_CACHE_TTL = 60  # seconds - matches the dashboard poll cadence

def _count_cli_agents(name, cfg):
    """claude/codex/agy main-process count for one box. None = unreadable
    right now (box down / SSH circuit open) - callers must NOT show it as 0."""
    out = None
    try:
        if cfg.get("local"):
            import subprocess
            out = subprocess.run(["bash", "-c", AGENT_COUNT_CMD], capture_output=True,
                                 text=True, timeout=15).stdout
        else:
            client = get_ssh_client(cfg["ssh_host"], cfg.get("ssh_user", "ben"))
            if client is None:
                return None
            _, stdout, _ = client.exec_command(AGENT_COUNT_CMD, timeout=15)
            out = stdout.read().decode()
        for kv in (out or "").split():
            if kv.startswith("agents="):
                return int(kv.split("=", 1)[1])
    except Exception as e:
        logger.debug(f"agent count {name}: {e}")
    return None

def _get_runner_stats():
    """CI runner busy/total per box from GitHub (gh api, run on gandalf)."""
    result = {"available": False, "boxes": {}, "fleet": {"busy": 0, "total": 0, "online": 0}}
    try:
        import subprocess, pwd
        env = dict(os.environ)
        # systemd system services don't set HOME; gh refuses to find its auth without it
        env.setdefault("HOME", pwd.getpwuid(os.getuid()).pw_dir)
        out = subprocess.run(
            ["gh", "api", f"orgs/{GITHUB_RUNNER_ORG}/actions/runners", "--paginate",
             "--jq", '.runners[] | [.name, .status, (.busy|tostring)] | @tsv'],
            capture_output=True, text=True, timeout=25, env=env,
        )
        if out.returncode != 0:
            logger.warning(f"runner stats: gh api failed: {out.stderr.strip()[:200]}")
            return result
        boxes = {}
        for line in out.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            name, status, busy = parts
            box = re.sub(r"-\d+$", "", name) or name  # aragorn-12 -> aragorn
            b = boxes.setdefault(box, {"busy": 0, "total": 0, "online": 0})
            b["total"] += 1
            if status == "online":
                b["online"] += 1
                if busy == "true":
                    b["busy"] += 1
        result["available"] = True
        result["boxes"] = {k: boxes[k] for k in sorted(boxes)}
        result["fleet"] = {
            "busy": sum(b["busy"] for b in boxes.values()),
            "total": sum(b["total"] for b in boxes.values()),
            "online": sum(b["online"] for b in boxes.values()),
        }
    except Exception as e:
        logger.warning(f"runner stats failed: {e}")
    return result

def get_fleet_stats():
    """Cached agents-per-box + runner busy/total. One cache for both halves so
    a dashboard refresh costs at most one SSH sweep + one gh call per minute."""
    now = time.time()
    with fleet_stats_cache_lock:
        if fleet_stats_cache["data"] is not None and (now - fleet_stats_cache["ts"]) < FLEET_STATS_CACHE_TTL:
            return fleet_stats_cache["data"]
    agents = {}
    with ThreadPoolExecutor(max_workers=len(AGENT_STAT_BOXES)) as ex:
        futs = {ex.submit(_count_cli_agents, n, c): n for n, c in AGENT_STAT_BOXES.items()}
        for fut in as_completed(futs):
            agents[futs[fut]] = fut.result()
    counts = {n: agents.get(n) for n in AGENT_STAT_BOXES}  # keep display order
    result = {
        "agents": {
            "boxes": counts,
            "total": sum(v for v in counts.values() if v is not None),
        },
        "runners": _get_runner_stats(),
    }
    with fleet_stats_cache_lock:
        fleet_stats_cache["data"] = result
        fleet_stats_cache["ts"] = time.time()
    return result

# --- Model serving stats (2026-07-29): tokens/sec dials + requests served ---
# Two very different sources, one shape out:
#   gandalf: llama-swap (port 8889) keeps a per-request JSON buffer at
#     /api/metrics (id, model, output_tokens, tokens_per_second, timestamp).
#     Its own /metrics is system-level only, and the per-model llama-server
#     /upstream/<model>/metrics MUST NOT be probed - hitting an upstream path
#     triggers a model LOAD/swap (44-72s, evicts whatever is resident).
#     The request buffer is in-memory, so we persist rows into sqlite.
#   frodo: bare llama-server --metrics (port 8890) exposes Prometheus counters
#     llamacpp:tokens_predicted_total + llamacpp:tokens_predicted_seconds_total.
#     (Its *_tokens_seconds gauges are LIFETIME averages, not instantaneous -
#     verified: 1.227e6 tok / 6436s = the exact 190.7 the gauge showed.)
#     We sample the counters every 60s; deltas give rates, and the
#     seconds_total counter only advances WHILE GENERATING, so
#     sum(d_tokens)/sum(d_seconds) is exactly the serving-time-only average.
# pippen: skipped - no llama-server there (route `code` moved; nothing serves).
MODEL_SERVING_SOURCES = {
    "gandalf": {"kind": "llamaswap", "url": "http://127.0.0.1:8889/api/metrics"},
    "frodo":   {"kind": "llamacpp",  "url": f"http://{FLEET_IPS['frodo']}:8890/metrics"},
    "pippin":  {"kind": "llamacpp",  "url": f"http://{FLEET_IPS['pippen']}:8891/metrics"},
}
MODEL_SERVING_INTERVAL = 60      # sampling cadence, matches METRICS_INTERVAL
MODEL_SERVING_RETENTION_DAYS = 8  # a hair over the 7d display window
MODEL_SERVING_HTTP_TIMEOUT = 6

def _parse_llamaswap_ts(ts):
    """llama-swap timestamps are UTC RFC3339 with nanoseconds - normalize to
    the local naive ISO format the rest of this file stores."""
    try:
        ts = re.sub(r"\.(\d{6})\d*", r".\1", ts).replace("Z", "+00:00")
        return datetime.fromisoformat(ts).astimezone().replace(tzinfo=None).isoformat()
    except Exception:
        return None

# --- Gateway-derived token stats (2026-07-29). aragorn serves via ollama, which
# has NO token-counter endpoint, so its tokens/sec cannot come from the box.
# LiteLLM already logs completion_tokens + start/end time for every request it
# routes, which yields real measured tokens/sec for ANY route. Read over
# `docker exec` (peer auth inside the container - no password in this file).
GATEWAY_TOKENS_TTL = 45  # seconds
gateway_tokens_cache = {"data": None, "ts": 0.0}
gateway_tokens_lock = threading.Lock()

# NOTE (2026-07-31): the day boundary below is now a {DAY_START} placeholder
# rather than a hardcoded date_trunc, so /api/reset_stats?host=... can move it.
# It was hardcoded, which is why clearing aragorn's numbers appeared to do
# nothing - this path never consulted the reset marker at all.
#
# SEPARATE, AND WORSE: tps here is MAX(completion_tokens / request_wall_seconds).
# For a short request that ratio is not a generation rate - a 200-token reply
# that returns in 0.23s reads as 870 t/s. That is the origin of aragorn's
# "877 peak", and it is a bad metric, not a fast machine. Fixing the metric is
# a separate change; this one only makes the reset work.
_GATEWAY_TOKENS_SQL = """
WITH d AS (
  SELECT model,
         completion_tokens AS tok,
         GREATEST(EXTRACT(epoch FROM ("endTime"-"startTime")), 0.001) AS secs,
         "startTime" AS ts
  FROM "LiteLLM_SpendLogs"
  WHERE completion_tokens > 0
    AND "startTime" >= now() - interval '7 days'
)
SELECT model,
       COALESCE(SUM(tok)  FILTER (WHERE ts >= {DAY_START}), 0),
       COALESCE(SUM(secs) FILTER (WHERE ts >= {DAY_START}), 0),
       COALESCE(MAX(tok/secs) FILTER (WHERE ts >= {DAY_START}), 0),
       COALESCE(MAX(tok/secs) FILTER (WHERE ts >= now() - interval '150 seconds'), 0),
       COUNT(*) FILTER (WHERE ts >= now() - interval '1 hour'),
       COUNT(*) FILTER (WHERE ts >= now() - interval '1 day'),
       COUNT(*)
FROM d GROUP BY model;
"""


def get_gateway_token_stats():
    """model id -> measured token throughput, straight from the gateway's log."""
    now_t = time.time()
    with gateway_tokens_lock:
        if gateway_tokens_cache["data"] is not None and (now_t - gateway_tokens_cache["ts"]) < GATEWAY_TOKENS_TTL:
            return gateway_tokens_cache["data"]

    stats = {}
    try:
        # Resolve the day boundary, honouring a fleet-wide ("*") reset. Per-host
        # markers are applied after the fetch, below, because this query groups
        # by MODEL and the model->host mapping lives in Python.
        _fleet_start = stats_window_start()
        _sql = _GATEWAY_TOKENS_SQL.replace(
            "{DAY_START}", "TIMESTAMP '" + _fleet_start.replace("'", "") + "'")
        out = subprocess.run(
            ["docker", "exec", "litellm-db", "psql", "-U", "litellm", "-d", "litellm",
             "-t", "-A", "-F", "|", "-c", _sql],
            capture_output=True, text=True, timeout=15,
        )
        for line in out.stdout.strip().splitlines():
            parts = line.split("|")
            if len(parts) < 8:
                continue
            model = parts[0].split("/")[-1].strip()
            tok, secs, peak, nowtps, h, d, w = (float(x) for x in parts[1:8])
            stats[model] = {
                "tokens_today": tok,
                "seconds_today": secs,
                "tps_max_today": round(peak, 1),
                "tps_now": round(nowtps, 1),
                "requests": {"hour": int(h), "day": int(d), "week": int(w)},
            }
    except Exception as e:
        logger.debug(f"gateway token stats unavailable: {e}")

    with gateway_tokens_lock:
        gateway_tokens_cache["data"] = stats
        gateway_tokens_cache["ts"] = time.time()
    return stats


def collect_model_serving():
    """Background sampler: persist llama-swap request records + llama-server
    counter samples into sqlite so windows survive restarts (both ours and
    the model servers')."""
    while True:
        try:
            conn = sqlite3.connect(CONFIG["db_path"])
            # gandalf: upsert the whole request buffer (INSERT OR IGNORE makes
            # re-reading the same records free; llama-swap restarts reset id to
            # 0 but the (box, req_id, ts) key keeps old rows distinct)
            try:
                resp = requests.get(MODEL_SERVING_SOURCES["gandalf"]["url"],
                                    timeout=MODEL_SERVING_HTTP_TIMEOUT)
                if resp.ok:
                    for rec in resp.json():
                        ts = _parse_llamaswap_ts(rec.get("timestamp", ""))
                        tok = rec.get("tokens") or {}
                        if ts is None or not tok:
                            continue
                        conn.execute(
                            "INSERT OR IGNORE INTO model_requests "
                            "(box, req_id, ts, model, output_tokens, gen_tps) VALUES (?,?,?,?,?,?)",
                            ("gandalf", rec.get("id", -1), ts, rec.get("model"),
                             tok.get("output_tokens"), tok.get("tokens_per_second")))
            except Exception as e:
                logger.debug(f"model serving sample gandalf: {e}")
            # every plain llama-server box: one counter sample per cycle
            for _box, _src in MODEL_SERVING_SOURCES.items():
                if _src.get("kind") != "llamacpp":
                    continue
                try:
                    resp = requests.get(_src["url"], timeout=MODEL_SERVING_HTTP_TIMEOUT)
                    if resp.ok:
                        vals = {}
                        for line in resp.text.splitlines():
                            for key in ("llamacpp:tokens_predicted_total ",
                                        "llamacpp:tokens_predicted_seconds_total "):
                                if line.startswith(key):
                                    vals[key.strip()] = float(line.split()[-1])
                        if "llamacpp:tokens_predicted_total" in vals:
                            conn.execute(
                                "INSERT OR IGNORE INTO model_counter_samples "
                                "(box, ts, tokens_total, gen_seconds_total) VALUES (?,?,?,?)",
                                (_box, datetime.now().isoformat(),
                                 vals.get("llamacpp:tokens_predicted_total"),
                                 vals.get("llamacpp:tokens_predicted_seconds_total")))
                except Exception as e:
                    logger.debug(f"model serving sample {_box}: {e}")
            cutoff = (datetime.now() - timedelta(days=MODEL_SERVING_RETENTION_DAYS)).isoformat()
            conn.execute("DELETE FROM model_requests WHERE ts < ?", (cutoff,))
            conn.execute("DELETE FROM model_counter_samples WHERE ts < ?", (cutoff,))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning(f"Model serving collection error: {e}")
        time.sleep(MODEL_SERVING_INTERVAL)

model_serving_cache = {"data": None, "ts": 0.0}
model_serving_cache_lock = threading.Lock()
MODEL_SERVING_CACHE_TTL = 30  # seconds

# --- Stats reset marker (2026-07-30, Ben: "reset all the averages on the fleet
# monitor"). After a config change - new models, speculation switched on - the
# day's peaks and averages describe a machine that no longer exists, so they
# mislead rather than inform. This does NOT delete history (the historical charts
# still want it); it just moves the start of the "today" window forward.
# Reset:  POST /api/reset_stats     Clear:  DELETE /api/reset_stats
STATS_RESET_FILE = "/var/lib/queue-router/stats_reset_at"


def stats_window_start(host=None):
    """Start of the 'today' window: midnight, or a later reset if one is set.

    PER-HOST since 2026-07-31 (Ben: "clear aragorn's peak and averages, they're
    askew"). A fleet-wide reset would also throw away frodo's and gandalf's
    legitimate numbers, so the marker file now holds a JSON map of
    {host: iso-timestamp} plus an optional "*" entry meaning all hosts.
    A bare timestamp (the pre-2026-07-31 format) is still read and treated as
    "*", so an existing marker keeps working untouched.
    """
    midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    try:
        with open(STATS_RESET_FILE) as fh:
            raw = fh.read().strip()
    except OSError:
        return midnight
    if not raw:
        return midnight
    try:
        marks = json.loads(raw)
        if not isinstance(marks, dict):
            marks = {"*": str(marks)}
    except (ValueError, TypeError):
        marks = {"*": raw}          # legacy bare-timestamp file
    candidates = [marks.get("*"), marks.get(host) if host else None]
    best = midnight
    for c in candidates:
        # A marker from a previous day must not pin the window in the past.
        if isinstance(c, str) and c > best:
            best = c
    return best


def get_model_serving_stats():
    """Per-box: tps_now, serving-time-only tps average today, requests served
    in the last hour/day/week. Read from sqlite, cached 30s."""
    now_t = time.time()
    with model_serving_cache_lock:
        if model_serving_cache["data"] is not None and (now_t - model_serving_cache["ts"]) < MODEL_SERVING_CACHE_TTL:
            return model_serving_cache["data"]

    now = datetime.now()
    # Per-host reset windows; resolved per box below.
    midnight = stats_window_start()   # fleet-wide floor (honours a "*" reset)
    hour_ago = (now - timedelta(hours=1)).isoformat()
    day_ago = (now - timedelta(days=1)).isoformat()
    week_ago = (now - timedelta(days=7)).isoformat()
    result = {}
    try:
        conn = sqlite3.connect(CONFIG["db_path"])

        # gandalf - request-level records
        rows = conn.execute(
            "SELECT ts, output_tokens, gen_tps FROM model_requests "
            "WHERE box='gandalf' AND ts >= ? ORDER BY ts", (week_ago,)).fetchall()
        recent_cut = (now - timedelta(seconds=150)).isoformat()

        def _agg(rs):
            """Token-weighted generation speed over a set of requests: total
            output tokens / total generation seconds (out/tps per request)."""
            toks = secs = 0.0
            for _, out, tps in rs:
                if out and tps and tps > 0:
                    toks += out
                    secs += out / tps
            return (round(toks / secs, 1) if secs > 0 else 0.0), secs
        tps_now, _ = _agg([r for r in rows if r[0] >= recent_cut])
        _g_start = stats_window_start('gandalf')   # per-host reset window
        tps_today, secs_today = _agg([r for r in rows if r[0] >= _g_start])
        # Peak generation speed seen today (single fastest request) - the bottom
        # band of the card's tokens/sec strip.
        _today_tps = [r[2] for r in rows if r[0] >= _g_start and r[2]]
        result["gandalf"] = {
            "available": True,
            "tps_now": tps_now,
            "tps_avg_today": tps_today,
            "tps_max_today": round(max(_today_tps), 1) if _today_tps else 0.0,
            "serving_minutes_today": round(secs_today / 60),
            "requests": {
                "hour": sum(1 for r in rows if r[0] >= hour_ago),
                "day": sum(1 for r in rows if r[0] >= day_ago),
                "week": len(rows),
            },
            "approx_requests": False,
        }

        # every counter-sampled box (frodo, pippin) -> reset-aware deltas.
        # Generalised 2026-07-29: pippin serves the `code` route, so Ben wants
        # its tokens/sec on the card like the other model boxes.
        def _bursts(ds):
            """Requests can't be counted exactly from counters (llama-server
            exposes no requests_total) - count busy-after-idle transitions in
            the 60s samples as a lower-bound estimate."""
            n = 0
            prev_busy = False
            for _, d_tok, _ in ds:
                busy = d_tok > 0
                if busy and not prev_busy:
                    n += 1
                prev_busy = busy
            return n

        for box, src in MODEL_SERVING_SOURCES.items():
            if src.get("kind") != "llamacpp":
                continue
            samples = conn.execute(
                "SELECT ts, tokens_total, gen_seconds_total FROM model_counter_samples "
                "WHERE box=? AND ts >= ? ORDER BY ts", (box, week_ago)).fetchall()
            deltas = []  # (ts, d_tokens, d_secs)
            for i in range(1, len(samples)):
                ts, tok, sec = samples[i]
                ptok, psec = samples[i - 1][1], samples[i - 1][2]
                d_tok, d_sec = tok - ptok, (sec - psec) if sec is not None and psec is not None else 0.0
                if d_tok < 0 or d_sec < 0:  # counter reset (llama-server restart)
                    d_tok, d_sec = tok, sec or 0.0
                deltas.append((ts, d_tok, d_sec))
            tps_now = 0.0
            if deltas and deltas[-1][0] >= (now - timedelta(seconds=3 * MODEL_SERVING_INTERVAL)).isoformat():
                _, d_tok, d_sec = deltas[-1]
                tps_now = round(d_tok / d_sec, 1) if d_tok > 0 and d_sec > 0 else 0.0
            box_start = stats_window_start(box)   # per-host reset window
            today = [d for d in deltas if d[0] >= box_start]
            tok_today = sum(d[1] for d in today if d[1] > 0)
            sec_today = sum(d[2] for d in today if d[1] > 0)
            # Peak 60s-window generation speed today (bottom band of the t/s strip).
            _win_tps = [d[1] / d[2] for d in today if d[1] > 0 and d[2] > 0]
            result[box] = {
                "available": len(samples) >= 2,
                "tps_now": tps_now,
                "tps_avg_today": round(tok_today / sec_today, 1) if sec_today > 0 else 0.0,
                "tps_max_today": round(max(_win_tps), 1) if _win_tps else 0.0,
                "serving_minutes_today": round(sec_today / 60),
                "requests": {
                    "hour": _bursts([d for d in deltas if d[0] >= hour_ago]),
                    "day": _bursts([d for d in deltas if d[0] >= day_ago]),
                    "week": _bursts(deltas),
                },
                "approx_requests": True,
            }
        conn.close()
    except Exception as e:
        logger.warning(f"model serving stats failed: {e}")
        for box in MODEL_SERVING_SOURCES:
            result.setdefault(box, {"available": False})

    # Boxes with no native token counters (ollama) get their numbers from the
    # gateway's request log instead - measured, not estimated.
    try:
        gw = get_gateway_token_stats()
        topo = get_route_topology()
        by_box = {}
        for route, info in topo.items():
            if info.get("box") in MODEL_SERVING_SOURCES:
                continue  # native counters win
            by_box.setdefault(info["box"], set()).add(info.get("model"))
        for box, models in by_box.items():
            # Per-host reset (2026-07-31). This gateway-derived path groups by
            # MODEL, so a per-host marker can only be applied here, where the
            # model->box mapping exists. If this box was reset after the fleet
            # window began, its today-figures are zeroed rather than shown
            # stale: the underlying spend-log query cannot be re-cut per box
            # without a second round-trip, and showing the OLD number after an
            # explicit reset is the worse failure.
            _box_start = stats_window_start(box)
            if _box_start > stats_window_start():
                result[box] = {
                    "available": True, "source": "gateway", "reset": _box_start,
                    "tps_now": 0.0, "tps_avg_today": 0.0, "tps_max_today": 0.0,
                    "serving_minutes_today": 0,
                    "requests": {"hour": 0, "day": 0, "week": 0},
                }
                continue
            rows = [gw[m] for m in models if m in gw]
            if not rows:
                result.setdefault(box, {"available": False, "source": "gateway"})
                continue
            tok = sum(r["tokens_today"] for r in rows)
            secs = sum(r["seconds_today"] for r in rows)
            result[box] = {
                "available": True,
                "source": "gateway",
                "tps_now": max(r["tps_now"] for r in rows),
                "tps_avg_today": round(tok / secs, 1) if secs > 0 else 0.0,
                "tps_max_today": max(r["tps_max_today"] for r in rows),
                "serving_minutes_today": round(secs / 60),
                "requests": {
                    "hour": sum(r["requests"]["hour"] for r in rows),
                    "day": sum(r["requests"]["day"] for r in rows),
                    "week": sum(r["requests"]["week"] for r in rows),
                },
                "approx_requests": False,
            }
    except Exception as e:
        logger.debug(f"gateway-derived serving stats failed: {e}")

    with model_serving_cache_lock:
        model_serving_cache["data"] = result
        model_serving_cache["ts"] = time.time()
    return result

def get_mac_metrics(host, user):
    """Get CPU%, GPU utilization, RAM, swap, and disk for a macOS target via SSH.

    macOS has no nvidia-smi/ComfyUI here, so we read native tools:
    top (CPU), ioreg (GPU util), vm_stat + hw.memsize (RAM), sysctl (swap), df (disk).
    """
    result = {
        "cpu_percent": None,
        "gpu_util": None,
        "swap_percent": None,
        "swap_used_gb": None,
        "swap_total_gb": None,
        "ram": None,
        "disk": None,
    }
    try:
        client = get_ssh_client(host, user)
        if client is None:
            return result  # Circuit breaker open

        # CPU utilization = 100 - idle
        _, stdout, _ = client.exec_command(
            "top -l 1 -n 0 | awk '/CPU usage/{for(i=1;i<=NF;i++) if($i==\"idle\"){gsub(/%/,\"\",$(i-1)); printf \"%.1f\", 100-$(i-1)}}'"
        )
        out = stdout.read().decode().strip()
        if out:
            result["cpu_percent"] = round(float(out), 1)

        # GPU utilization via IOAccelerator "Device Utilization %"
        _, stdout, _ = client.exec_command(
            "ioreg -r -d1 -c IOAccelerator | grep -o '\"Device Utilization %\"=[0-9]*' | head -1 | grep -o '[0-9]*$'"
        )
        out = stdout.read().decode().strip()
        if out:
            result["gpu_util"] = float(out)

        # Swap usage: "total = X.00M  used = Y.00M  free = Z.00M"
        _, stdout, _ = client.exec_command("sysctl -n vm.swapusage")
        out = stdout.read().decode().strip()
        m = re.search(r"total = ([\d.]+)M.*used = ([\d.]+)M", out)
        if m:
            total_mb = float(m.group(1))
            used_mb = float(m.group(2))
            result["swap_total_gb"] = round(total_mb / 1024, 1)
            result["swap_used_gb"] = round(used_mb / 1024, 1)
            result["swap_percent"] = round(used_mb / total_mb * 100, 1) if total_mb > 0 else 0

        # RAM: total from hw.memsize, used = (active + wired + compressed) pages
        _, stdout, _ = client.exec_command("sysctl -n hw.memsize; vm_stat")
        lines = stdout.read().decode().strip().splitlines()
        if lines:
            try:
                mem_total = int(lines[0].strip())
            except ValueError:
                mem_total = 0
            page_size = 4096
            pages = {}
            for line in lines[1:]:
                ps = re.search(r"page size of (\d+)", line)
                if ps:
                    page_size = int(ps.group(1))
                km = re.match(r"(.+?):\s+(\d+)\.", line)
                if km:
                    pages[km.group(1).strip()] = int(km.group(2))
            used_pages = (pages.get("Pages active", 0)
                          + pages.get("Pages wired down", 0)
                          + pages.get("Pages occupied by compressor", 0))
            used_bytes = used_pages * page_size
            if mem_total > 0:
                result["ram"] = {
                    "total_gb": round(mem_total / (1024**3), 1),
                    "used_gb": round(used_bytes / (1024**3), 1),
                    "percent": round(used_bytes / mem_total * 100, 1),
                }

        # Disk usage for / (df -k = 1024-byte blocks; macOS df lacks -B1)
        _, stdout, _ = client.exec_command("df -k / | tail -1")
        parts = stdout.read().decode().strip().split()
        if len(parts) >= 3:
            total_kb = int(parts[1])
            used_kb = int(parts[2])
            result["disk"] = {
                "total_gb": round(total_kb / (1024**2), 1),
                "used_gb": round(used_kb / (1024**2), 1),
                "percent": round(used_kb / total_kb * 100, 1) if total_kb > 0 else 0,
            }

    except Exception as e:
        logger.warning(f"Mac metrics error for {host}: {e}")
        key = f"{user}@{host}"
        if key in ssh_clients:
            try:
                ssh_clients[key].close()
            except:
                pass
            del ssh_clients[key]

    return result


def offline_target_status(target_config):
    """Full dashboard shape for a target whose live probes failed."""
    return {
        "online": False,
        "os": target_config.get("os", "linux"),
        "queue_running": 0,
        "queue_pending": 0,
        "gpu": None,
        "ram": None,
        "cpu_percent": None,
        "cpu_temp": None,
        "gpu_watts": None,
        "gpu_temp": None,
        "gpu_util": None,
        "gpu_power_limit": target_config.get("gpu_power_limit", 300),
        "gpu_power_max": target_config.get("gpu_power_max", 400),
        "disk": None,
        "swap": None,
        "disk_io": None,
        "net_io": None,
        "loaded_models": None,
    }


def get_target_status(target_name, target_config, fast=False):
    """Check a target's availability and gather live metrics.

    Linux targets are probed via ComfyUI HTTP + SSH (nvidia-smi). Mac targets
    have no ComfyUI here, so they report online via SSH and use native metrics.
    """
    result = offline_target_status(target_config)

    # macOS targets: no ComfyUI/nvidia-smi, gather native metrics over SSH
    if target_config.get("os") == "mac":
        if "ssh_host" in target_config:
            try:
                m = get_mac_metrics(
                    target_config["ssh_host"],
                    target_config.get("ssh_user", "ben")
                )
                # Online if we got any live reading back over SSH
                if m.get("cpu_percent") is not None or m.get("ram") is not None:
                    result["online"] = True
                result["cpu_percent"] = m.get("cpu_percent")
                result["gpu_util"] = m.get("gpu_util")
                result["ram"] = m.get("ram")
                result["disk"] = m.get("disk")
                if m.get("swap_total_gb") is not None:
                    result["swap"] = {
                        "total_gb": m["swap_total_gb"],
                        "used_gb": m["swap_used_gb"],
                        "percent": m["swap_percent"]
                    }
            except Exception as e:
                logger.warning(f"{target_name} Mac metrics unreachable: {e}")
        # Macs return early, so their loaded-model read happens here (pippin
        # serves the `code` route - Ben, 2026-07-29: "if it can run a local AI,
        # I want it on that screen").
        if target_config.get("model_status_urls"):
            try:
                result["loaded_models"] = get_loaded_models(target_name, target_config)
            except Exception as e:
                logger.debug(f"{target_name} loaded-model read failed: {e}")
        return result

    # Try ComfyUI HTTP endpoints
    try:
        # Get queue status
        # Timeout raised 5s -> 10s (2026-07-18): gandalf/frodo run heavy CI jobs
        # and can be slow to answer while busy-but-healthy; 5s was flickering
        # them to false OFFLINE. See apply_display_hysteresis() for the second
        # half of this fix (consecutive-failure hysteresis on the display).
        response = requests.get(f"{target_config['url']}/queue", timeout=10)
        if response.ok:
            data = response.json()
            result["online"] = True
            result["queue_running"] = len(data.get("queue_running", []))
            result["queue_pending"] = len(data.get("queue_pending", []))

        # Get system stats (GPU, RAM)
        stats_response = requests.get(f"{target_config['url']}/system_stats", timeout=10)
        if stats_response.ok:
            stats = stats_response.json()

            # System RAM
            system = stats.get("system", {})
            ram_total = system.get("ram_total", 0)
            ram_free = system.get("ram_free", 0)
            if ram_total > 0:
                result["ram"] = {
                    "total_gb": round(ram_total / (1024**3), 1),
                    "used_gb": round((ram_total - ram_free) / (1024**3), 1),
                    "percent": round((ram_total - ram_free) / ram_total * 100, 1)
                }

            # Extract CUDA version from pytorch_version (e.g., "2.9.1+cu128" -> "12.8")
            pytorch_ver = system.get("pytorch_version", "")
            cuda_match = re.search(r"\+cu(\d+)", pytorch_ver)
            cuda_version = None
            if cuda_match:
                cuda_num = cuda_match.group(1)
                cuda_version = f"{cuda_num[:-1]}.{cuda_num[-1]}" if len(cuda_num) >= 2 else cuda_num

            # GPU stats
            devices = stats.get("devices", [])
            if devices:
                gpu = devices[0]  # Primary GPU
                vram_total = gpu.get("vram_total", 0)
                vram_free = gpu.get("vram_free", 0)
                result["gpu"] = {
                    "name": re.sub(r"(cuda:\d+ | : .*$)", "", gpu.get("name", "Unknown")),  # Clean up name
                    "cuda_version": cuda_version,
                    "vram_total_gb": round(vram_total / (1024**3), 1),
                    "vram_used_gb": round((vram_total - vram_free) / (1024**3), 1),
                    "vram_percent": round((vram_total - vram_free) / vram_total * 100, 1) if vram_total > 0 else 0
                }
    except Exception as e:
        logger.warning(f"{target_name} ComfyUI unreachable: {e}")

    # Get SSH metrics (CPU, GPU power, temp, util, swap, I/O) - independent of ComfyUI status
    if "ssh_host" in target_config:
        try:
            # expected_host: verify the box is the one this card names before
            # trusting its dials (2026-07-30 wrong-box guard). NOTE the card is
            # called "pippin" but that machine's hostname is "pippen" - a real
            # spelling difference, not a mismatch, hence TARGET_HOSTNAME_AKA.
            ssh_metrics = get_ssh_metrics(
                target_config["ssh_host"],
                target_config.get("ssh_user", "ben"),
                expected_host=target_name,
                hostname_aka=TARGET_HOSTNAME_AKA.get(target_name, ()),
            )
            if ssh_metrics.get("mismatch"):
                # Wrong machine answered. Report the card as NOT online and carry
                # the mismatch through, rather than showing another box's numbers.
                result["online"] = False
                result["mismatch"] = True
                result["reported_host"] = ssh_metrics.get("reported_host")
                return result
            result["cpu_percent"] = ssh_metrics.get("cpu_percent")
            result["cpu_temp"] = ssh_metrics.get("cpu_temp")
            result["gpu_count"] = ssh_metrics.get("gpu_count")
            result["gpu_watts"] = ssh_metrics.get("gpu_watts")
            result["gpu_temp"] = ssh_metrics.get("gpu_temp")
            result["gpu_util"] = ssh_metrics.get("gpu_util")
            if ssh_metrics.get("gpu_power_limit"):
                result["gpu_power_limit"] = ssh_metrics["gpu_power_limit"]

            # /api/status uses this data even when ComfyUI is intentionally down.
            # Previously its fast path returned before SSH, leaving Frodo's dial
            # values null despite the machine and nvidia-smi being healthy.
            if any(ssh_metrics.get(key) is not None for key in (
                "cpu_percent", "gpu_watts", "gpu_temp", "gpu_util"
            )):
                result["online"] = True

            if result["gpu"] is None and ssh_metrics.get("vram_total_gb") is not None:
                result["gpu"] = {
                    "name": ssh_metrics.get("gpu_name") or "Unknown",
                    "cuda_version": None,
                    "vram_total_gb": ssh_metrics["vram_total_gb"],
                    "vram_used_gb": ssh_metrics["vram_used_gb"],
                    "vram_percent": ssh_metrics["vram_percent"],
                }

            # System RAM: fall back to the SSH reading when ComfyUI's
            # /system_stats didn't provide it (ComfyUI is disabled fleet-wide
            # since 2026-07-25, so in practice this IS the source now) - same
            # fallback pattern as the GPU/VRAM block above.
            if result["ram"] is None and ssh_metrics.get("ram_total_gb") is not None:
                result["ram"] = {
                    "total_gb": ssh_metrics["ram_total_gb"],
                    "used_gb": ssh_metrics["ram_used_gb"],
                    "percent": ssh_metrics["ram_percent"],
                }

            # Swap usage
            if ssh_metrics.get("swap_total_gb") is not None:
                result["swap"] = {
                    "total_gb": ssh_metrics["swap_total_gb"],
                    "used_gb": ssh_metrics["swap_used_gb"],
                    "percent": ssh_metrics["swap_percent"]
                }

            # Disk I/O
            if ssh_metrics.get("disk_read_mbps") is not None:
                result["disk_io"] = {
                    "read_mbps": ssh_metrics["disk_read_mbps"],
                    "write_mbps": ssh_metrics["disk_write_mbps"]
                }

            # Network I/O
            if ssh_metrics.get("net_rx_mbps") is not None:
                result["net_io"] = {
                    "rx_mbps": ssh_metrics["net_rx_mbps"],
                    "tx_mbps": ssh_metrics["net_tx_mbps"]
                }

            # Get disk usage (cached, updates every 5 min)
            disk_usage = get_disk_usage(
                target_config["ssh_host"],
                target_config.get("ssh_user", "ben"),
                target_config.get("disk_path", "/")
            )
            if disk_usage.get("total_gb"):
                result["disk"] = disk_usage

            if target_config.get("model_status_urls"):
                result["loaded_models"] = get_loaded_models(target_name, target_config)
            elif target_config.get("ollama_instances"):
                result["loaded_models"] = get_ollama_loaded_models(target_config)
        except Exception as e:
            logger.warning(f"{target_name} SSH unreachable: {e}")

    return result


# ── Shipping pipeline snapshot (ported forward from bak-20260725; Gemini fleet monitor consumes /api/pipeline) ──
PIPELINE_CACHE_TTL = 0  # seconds
pipeline_cache = {"data": None, "ts": 0.0}
pipeline_cache_lock = threading.Lock()

def get_pipeline_status():
    """Shipping-pipeline snapshot for armbrain: issues -> PRs -> CI -> merged
    today -> deployed today. Read-only GitHub queries, cached 5 min."""
    now = time.time()
    with pipeline_cache_lock:
        if pipeline_cache["data"] is not None and (now - pipeline_cache["ts"]) < PIPELINE_CACHE_TTL:
            return pipeline_cache["data"]

    result = {"available": False, "repo": GITHUB_CI_REPO}
    token = get_gh_ci_token()
    if token:
        try:
            from datetime import datetime, timezone
            headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}
            # "Today" is CENTRAL TIME (Ben's day), not UTC - counters were resetting at 7pm CT.
            from zoneinfo import ZoneInfo
            from datetime import timedelta
            CT = ZoneInfo("America/Chicago")
            now_ct = datetime.now(CT)
            ct_midnight_utc = now_ct.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
            today = ct_midnight_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
            def search_count(q):
                r = requests.get("https://api.github.com/search/issues",
                                 params={"q": q, "per_page": 1}, headers=headers, timeout=8)
                return r.json().get("total_count", 0) if r.ok else None
            result["issues_open"] = search_count(f"repo:{GITHUB_CI_REPO} type:issue state:open")
            result["prs_open"] = search_count(f"repo:{GITHUB_CI_REPO} type:pr state:open")
            result["merged_today"] = search_count(f"repo:{GITHUB_CI_REPO} type:pr merged:>={today}")
            spark = []
            for i in range(6, 0, -1):
                d0 = (now_ct - timedelta(days=i)).replace(hour=0, minute=0, second=0, microsecond=0)
                d1 = d0 + timedelta(days=1)
                q = (f"repo:{GITHUB_CI_REPO} type:pr merged:{d0.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}"
                     f"..{d1.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}")
                spark.append(search_count(q) or 0)
            spark.append(result.get("merged_today") or 0)
            result["merged_spark"] = spark
            ci = get_ci_queue_status()
            result["ci_queued"] = ci.get("queued", 0) if ci.get("available") else None
            result["ci_running"] = ci.get("in_progress", 0) if ci.get("available") else None
            # deploys today (Gateway Deploy workflow - paginated & filtered in Central Time)
            week_start = (now_ct - timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0)
            dep_ok = dep_fail = dep_live = 0
            live_started_min = None
            dep_days = [0] * 7
            deploy_url = f"https://api.github.com/repos/{GITHUB_CI_REPO}/actions/workflows/gateway-deploy.yml/runs"
            for page in (1, 2, 3):
                r = requests.get(deploy_url, params={"created": f">={week_start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}", "per_page": 100, "page": page}, headers=headers, timeout=8)
                if not r.ok:
                    break
                runs = r.json().get("workflow_runs", [])
                if not runs:
                    break
                for run in runs:
                    run_ct = None
                    try:
                        run_ct = datetime.strptime(run.get("created_at"), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone(CT)
                    except Exception:
                        pass
                    if run_ct is None:
                        continue
                    days_ago = (now_ct.date() - run_ct.date()).days
                    idx = 6 - days_ago
                    is_today = (run_ct.date() == now_ct.date())
                    # Only count ACTUAL production deploy triggers (push to main or workflow_dispatch).
                    # Exclude merge_group, pull_request, and scheduled test-runs from deploy counters (Ben 2026-08-07).
                    if run.get("event") not in ("push", "workflow_dispatch"):
                        continue
                    if run.get("status") == "completed" and run.get("conclusion") == "success":
                        if 0 <= idx <= 6:
                            dep_days[idx] += 1
                        if is_today:
                            dep_ok += 1
                    elif run.get("status") != "completed":
                        if is_today:
                            dep_live += 1
                            try:
                                t = datetime.strptime(run.get("run_started_at") or run.get("created_at"), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                                m = int((datetime.now(timezone.utc) - t).total_seconds() // 60)
                                live_started_min = m if live_started_min is None else min(live_started_min, m)
                            except Exception:
                                pass
                    elif run.get("conclusion") not in ("cancelled", "skipped"):
                        if is_today:
                            dep_fail += 1
            result["deploys_spark"] = dep_days
            result.update({"deploys_ok_today": dep_ok, "deploys_failed_today": dep_fail,
                           "deploys_in_flight": dep_live, "deploy_started_min": live_started_min})
            # Last REAL deployment: newest completed run whose `deploy` JOB
            # succeeded (a green run can be validation-only with deploy
            # skipped, so run conclusion alone is not enough).
            try:
                found = False
                for page in (1, 2):
                    if found:
                        break
                    r = requests.get(f"https://api.github.com/repos/{GITHUB_CI_REPO}/actions/workflows/gateway-deploy.yml/runs",
                                     params={"status": "completed", "branch": "main",
                                             "per_page": 100, "page": page},
                                     headers=headers, timeout=8)
                    if not r.ok:
                        break
                    runs = r.json().get("workflow_runs", [])
                    if not runs:
                        break
                    for run in runs:
                        # scheduled guard ticks never run the deploy job — skip
                        # them without burning a jobs API call (same event
                        # filter the deploy-guard itself uses).
                        if run.get("event") not in ("push", "workflow_dispatch"):
                            continue
                        jr = requests.get(run["jobs_url"], params={"per_page": 100},
                                          headers=headers, timeout=8)
                        if not jr.ok:
                            continue
                        dep_job = next((j for j in jr.json().get("jobs", [])
                                        if j.get("name") == "deploy" and j.get("conclusion") == "success"), None)
                        if dep_job:
                            result["last_deploy_at"] = dep_job.get("completed_at") or run.get("updated_at")
                            result["last_deploy_sha"] = (run.get("head_sha") or "")[:9]
                            found = True
                            break
            except Exception as e:
                logger.warning(f"last-deploy lookup failed: {e}")
            result["available"] = True
        except Exception as e:
            logger.warning(f"pipeline status failed: {e}")

    with pipeline_cache_lock:
        pipeline_cache["data"] = result
        pipeline_cache["ts"] = time.time()
    return result


@app.route("/api/pipeline", methods=["GET"])
def api_pipeline():
    return jsonify(get_pipeline_status())


@app.route("/api/status", methods=["GET"])
def get_status():
    """Get status of all targets and recent jobs."""
    results = {
        name: offline_target_status(config)
        for name, config in CONFIG["targets"].items()
    }
    executor = ThreadPoolExecutor(max_workers=max(1, len(CONFIG["targets"])))
    try:
        futures = {
            executor.submit(get_target_status, name, config, True): (name, config)
            for name, config in CONFIG["targets"].items()
        }
        done, pending = wait(futures, timeout=STATUS_PROBE_TIMEOUT)
        for future in done:
            name, config = futures[future]
            try:
                status = future.result()
                if not isinstance(status, dict):
                    raise TypeError("target status was not an object")
            except Exception as e:
                logger.warning(f"{name} status unavailable: {e}")
                status = offline_target_status(config)
            results[name] = {**offline_target_status(config), **status}
        for future in pending:
            name, _ = futures[future]
            logger.warning(
                f"{name} status probe exceeded {STATUS_PROBE_TIMEOUT}s; using offline stub"
            )
            future.cancel()
    finally:
        # Do not wait for a slow host after the endpoint's response deadline.
        executor.shutdown(wait=False, cancel_futures=True)

    # Preserve CONFIG order (gandalf, frodo, pippin) regardless of completion order
    targets_status = {
        name: {
            **results[name],
            # Display-only smoothing: don't let one slow/busy probe flip
            # the dashboard to a false OFFLINE alarm (see apply_display_hysteresis).
            # A completely empty fail-soft stub is definitively offline.
            "online": (
                False
                if not results[name]["online"] and not any(
                    results[name].get(key) is not None
                    for key in ("gpu", "ram", "cpu_percent", "gpu_watts", "gpu_temp")
                )
                else apply_display_hysteresis(name, results[name]["online"])
            ),
            "vram_gb": config.get("vram_gb"),
            "url": config.get("url"),
        }
        for name, config in CONFIG["targets"].items()
    }

    today_start = stats_window_start()   # honours a manual reset
    jobs_today = {}
    max_util_today = {}
    peaks_today = {}
    recent_jobs = []
    conn = None
    try:
        conn = sqlite3.connect(CONFIG["db_path"])

        cursor = conn.execute(
            "SELECT target, COUNT(*) FROM jobs WHERE submitted_at >= ? GROUP BY target",
            (today_start,)
        )
        jobs_today = {row[0]: row[1] for row in cursor.fetchall()}

        cursor = conn.execute(
            "SELECT target, MAX(gpu_util) FROM metrics_history WHERE timestamp >= ? GROUP BY target",
            (today_start,)
        )
        max_util_today = {row[0]: row[1] for row in cursor.fetchall()}

        # Daily high-water mark for EVERY metric, not just GPU use (Ben,
        # 2026-07-30: he wants the amplifier-style peak-hold marker on all the
        # dials and bars). Every column is already recorded per poll.
        cursor = conn.execute(
            "SELECT target, MAX(gpu_temp), MAX(gpu_watts), MAX(cpu_percent), "
            "MAX(vram_percent), MAX(ram_percent), MAX(swap_percent) "
            "FROM metrics_history WHERE timestamp >= ? GROUP BY target",
            (today_start,)
        )
        for row in cursor.fetchall():
            peaks_today[row[0]] = {
                "gpu_temp": row[1], "gpu_watts": row[2], "cpu_percent": row[3],
                "vram_percent": row[4], "ram_percent": row[5], "swap_percent": row[6],
            }

        cursor = conn.execute(
            "SELECT id, target, status, submitted_at FROM jobs ORDER BY submitted_at DESC LIMIT 10"
        )
        recent_jobs = [
            {"id": row[0], "target": row[1], "status": row[2], "submitted_at": row[3]}
            for row in cursor.fetchall()
        ]
    except Exception as e:
        logger.warning(f"status history unavailable: {e}")
    finally:
        if conn is not None:
            conn.close()

    for name in targets_status:
        targets_status[name]["jobs_today"] = jobs_today.get(name, 0)
        v = max_util_today.get(name)
        targets_status[name]["max_util_today"] = round(v) if v is not None else None
        targets_status[name]["peaks_today"] = peaks_today.get(name, {})

    return jsonify({
        "targets": targets_status,
        "recent_jobs": recent_jobs
    })

@app.route("/api/fleet", methods=["GET"])
def get_fleet():
    """Compact CPU/temp/RAM for the six fleet-row hosts.

    Sshes into every node to gather this, so it's cached for FLEET_CACHE_TTL
    seconds - back-to-back/parallel polls (multiple tabs, fast refresh) hit
    the cache instead of opening a fresh ssh session per node per request.
    """
    now = time.time()
    with fleet_cache_lock:
        if fleet_cache["data"] is not None and (now - fleet_cache["ts"]) < FLEET_CACHE_TTL:
            return jsonify(fleet_cache["data"])

    results = {}
    with ThreadPoolExecutor(max_workers=len(FLEET_NODES)) as ex:
        futs = {ex.submit(get_fleet_node_metrics, n, c): n for n, c in FLEET_NODES.items()}
        for f in as_completed(futs):
            r = f.result()
            results[r["name"]] = r
    # preserve display order
    ordered = {n: results[n] for n in FLEET_NODES if n in results}

    with fleet_cache_lock:
        fleet_cache["data"] = ordered
        fleet_cache["ts"] = time.time()

    return jsonify(ordered)

@app.route("/api/ci_queue", methods=["GET"])
def api_ci_queue():
    """Queued/running GitHub Actions counts for armbrain-io/armbrain (cached)."""
    return jsonify(get_ci_queue_status())

@app.route("/api/reset_stats", methods=["POST", "DELETE"])
def api_reset_stats():
    """Reset today's peaks and averages without destroying history."""
    try:
        if request.method == "DELETE":
            try:
                os.remove(STATS_RESET_FILE)
            except FileNotFoundError:
                pass
            return jsonify({"reset_at": None, "note": "back to midnight"})
        now_iso = datetime.now().isoformat()
        # Optional ?host=aragorn (or JSON {"host": ...}) resets ONE box; omitting
        # it resets the fleet, which is the historical behaviour. Per-host was
        # added 2026-07-31 so clearing one machine's skewed numbers no longer
        # discards every other machine's legitimate ones.
        host = request.args.get("host") or (request.get_json(silent=True) or {}).get("host")
        os.makedirs(os.path.dirname(STATS_RESET_FILE), exist_ok=True)
        marks = {}
        try:
            with open(STATS_RESET_FILE) as fh:
                raw = fh.read().strip()
            if raw:
                parsed = json.loads(raw)
                marks = parsed if isinstance(parsed, dict) else {"*": str(parsed)}
        except (OSError, ValueError, TypeError):
            marks = {}
        marks[host or "*"] = now_iso
        with open(STATS_RESET_FILE, "w") as fh:
            fh.write(json.dumps(marks))
        # Drop the cached stats so the reset shows up immediately rather than
        # after the next cache expiry.
        with model_serving_cache_lock:
            model_serving_cache["data"] = None
            model_serving_cache["ts"] = 0.0
        return jsonify({"reset_at": now_iso, "host": host or "*", "markers": marks})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/armbrain-logo.svg", methods=["GET"])
def armbrain_logo():
    """The real Armbrain mark, recoloured in the brand's own indigo/coral.
    Lives next to this file on purpose - the original is in an armbrain
    worktree that can be deleted at any time."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "armbrain-logo.svg")
    try:
        with open(path) as fh:
            return Response(fh.read(), mimetype="image/svg+xml",
                            headers={"Cache-Control": "public, max-age=86400"})
    except OSError:
        return Response("", status=404)


@app.route("/api/model_serving", methods=["GET"])
def api_model_serving():
    """Tokens/sec dials (now + today-while-serving) and requests served
    (hour/day/week) for the model boxes."""
    return jsonify(get_model_serving_stats())

@app.route("/api/fleet_stats", methods=["GET"])
def api_fleet_stats():
    """Compact stats bar: CLI agent sessions per box + CI runner busy/total."""
    return jsonify(get_fleet_stats())

@app.route("/api/model_routes", methods=["GET"])
def api_model_routes():
    """Live/missing status for each 🔒 local fleet-gateway route (cached)."""
    return jsonify(get_model_route_health())

@app.route("/api/fleet_power", methods=["POST"])
def fleet_power():
    """Reboot a fleet-row host, or WoL-wake an offline farthing."""
    try:
        data = request.json
        node = data.get("node")
        action = data.get("action")
        cfg = FLEET_NODES.get(node)
        if not cfg:
            return jsonify({"error": "Unknown node"}), 400
        if action not in ("reboot", "wake", "shutdown"):
            return jsonify({"error": "Action must be 'reboot', 'shutdown' or 'wake'"}), 400
        # shutdown only where we can power back on remotely (WoL boxes = the Shire)
        if action == "shutdown" and not cfg.get("wol_mac"):
            return jsonify({"error": "Shutdown disabled for this node (no WoL to bring it back)"}), 400

        if action == "wake":
            mac = cfg.get("wol_mac")
            if not mac:
                return jsonify({"error": "No WoL MAC for this node"}), 400
            import subprocess
            for _ in range(3):
                subprocess.run(["wakeonlan", "-i", "192.168.1.255", mac],
                               capture_output=True, timeout=10)
            logger.info(f"Sent WoL magic packets to {node} ({mac})")
            return jsonify({"success": True, "node": node, "action": "wake"})

        # reboot / shutdown
        if cfg.get("local"):
            import subprocess
            logger.info("Self-reboot requested from dashboard")
            send_notification("🔄 Gandalf reboot", "Dashboard-triggered self reboot")
            subprocess.Popen(["bash", "-c", "sleep 2; sudo -n systemctl reboot"])
            return jsonify({"success": True, "node": node, "action": "reboot"})
        client = get_ssh_client(cfg["ssh_host"], cfg.get("ssh_user", "ben"))
        if client is None:
            return jsonify({"error": f"SSH circuit breaker open for {node}"}), 503
        cmd = "sudo -n reboot" if action == "reboot" else "sudo -n poweroff"
        client.exec_command(cmd)
        logger.info(f"Sent {action} to {node} ({cfg['ssh_host']})")
        icon = "🔄" if action == "reboot" else "🔴"
        send_notification(f"{icon} {node.capitalize()} {action}", f"Dashboard-triggered {action} of {node}")
        key = f"{cfg.get('ssh_user', 'ben')}@{cfg['ssh_host']}"
        if key in ssh_clients:
            try:
                ssh_clients[key].close()
            except:
                pass
            del ssh_clients[key]
        return jsonify({"success": True, "node": node, "action": action})
    except Exception as e:
        logger.error(f"fleet_power error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/logs", methods=["GET"])
def get_logs():
    """Get recent job logs."""
    conn = sqlite3.connect(CONFIG["db_path"])
    cursor = conn.execute(
        "SELECT id, target, status, submitted_at, started_at, completed_at, routing_hints FROM jobs ORDER BY submitted_at DESC LIMIT 20"
    )
    jobs = []
    for row in cursor.fetchall():
        hints = json.loads(row[6]) if row[6] else {}

        # Calculate duration if completed
        duration = None
        if row[4] and row[5]:  # started_at and completed_at
            try:
                start = datetime.fromisoformat(row[4])
                end = datetime.fromisoformat(row[5])
                delta = end - start
                total_secs = int(delta.total_seconds())
                if total_secs >= 60:
                    duration = f"{total_secs // 60}m{total_secs % 60}s"
                else:
                    duration = f"{total_secs}s"
            except:
                pass

        jobs.append({
            "id": row[0],
            "target": row[1],
            "status": row[2],
            "submitted_at": row[3],
            "started_at": row[4],
            "completed_at": row[5],
            "duration": duration,
            "is_video": hints.get("is_video", False),
            "estimated_vram": hints.get("estimated_vram", 0),
            "model_types": hints.get("model_types", [])
        })
    conn.close()
    return jsonify({"jobs": jobs})

@app.route("/api/power_limit", methods=["POST"])
def set_power_limit():
    """Set GPU power limit for a target."""
    try:
        data = request.json
        target = data.get("target")
        watts = data.get("watts")

        if not target or target not in CONFIG["targets"]:
            return jsonify({"error": "Invalid target"}), 400

        target_config = CONFIG["targets"][target]
        max_watts = target_config.get("gpu_power_max", 400)

        if not watts or not isinstance(watts, (int, float)):
            return jsonify({"error": "Invalid watts value"}), 400

        watts = int(watts)
        if watts < 100 or watts > max_watts:
            return jsonify({"error": f"Watts must be between 100 and {max_watts}"}), 400

        success = set_gpu_power_limit(
            target_config["ssh_host"],
            target_config.get("ssh_user", "ben"),
            watts
        )

        if success:
            # Update config with new limit
            CONFIG["targets"][target]["gpu_power_limit"] = watts
            return jsonify({"success": True, "target": target, "watts": watts})
        else:
            return jsonify({"error": "Failed to set power limit"}), 500

    except Exception as e:
        logger.error(f"Error setting power limit: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/clear_swap", methods=["POST"])
def api_clear_swap():
    """Clear swap for a target."""
    try:
        data = request.json
        target = data.get("target")
        if target not in CONFIG["targets"]:
            return jsonify({"error": "Unknown target"}), 400

        target_config = CONFIG["targets"][target]
        success = clear_swap(
            target_config["ssh_host"],
            target_config.get("ssh_user", "ben")
        )

        if success:
            return jsonify({"success": True, "target": target})
        else:
            return jsonify({"error": "Failed to clear swap"}), 500
    except Exception as e:
        logger.error(f"Error clearing swap: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/power", methods=["POST"])
def api_power():
    """Reboot or shut down a target machine."""
    try:
        data = request.json
        target = data.get("target")
        action = data.get("action")  # "reboot" or "shutdown"

        if target not in CONFIG["targets"]:
            return jsonify({"error": "Unknown target"}), 400
        if action not in ("reboot", "shutdown"):
            return jsonify({"error": "Action must be 'reboot' or 'shutdown'"}), 400

        target_config = CONFIG["targets"][target]
        ssh_host = target_config.get("ssh_host")
        ssh_user = target_config.get("ssh_user", "ben")

        if not ssh_host:
            return jsonify({"error": "No SSH host configured for target"}), 400

        cmd = "sudo reboot" if action == "reboot" else "sudo poweroff"

        try:
            client = get_ssh_client(ssh_host, ssh_user)
            if client is None:
                return jsonify({"error": f"SSH circuit breaker open for {target}"}), 503

            stdin, stdout, stderr = client.exec_command(cmd)
            # Don't wait for exit status - the machine is going down
            logger.info(f"Sent {action} command to {target} ({ssh_host})")
            send_notification(
                f"{'🔄' if action == 'reboot' else '🔴'} {target.capitalize()} {action}",
                f"Sent {action} command to {target}"
            )

            # Remove cached SSH client since host is going down
            key = f"{ssh_user}@{ssh_host}"
            if key in ssh_clients:
                try:
                    ssh_clients[key].close()
                except:
                    pass
                del ssh_clients[key]

            return jsonify({"success": True, "target": target, "action": action})

        except Exception as e:
            logger.error(f"SSH error during {action} on {target}: {e}")
            return jsonify({"error": str(e)}), 500

    except Exception as e:
        logger.error(f"Error in power action: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/")
def index():
    """Dashboard home page."""
    return '''<!DOCTYPE html><html><head><title>GANDALF // FLEET MONITOR</title>
<link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@400;700;900&family=Rajdhani:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{
--neon-cyan:#00fff2;--neon-magenta:#ff00ff;--neon-blue:#00a8ff;--neon-green:#39ff14;
--neon-yellow:#ffff00;--neon-orange:#ff6600;--neon-red:#ff0044;
--bg-dark:#0a0a0f;--bg-card:#0d1117;--bg-panel:#161b22;
--glow-cyan:0 0 10px #00fff2,0 0 20px #00fff2,0 0 40px #00fff288;
--glow-green:0 0 10px #39ff14,0 0 20px #39ff14,0 0 40px #39ff1488;
--glow-red:0 0 10px #ff0044,0 0 20px #ff0044,0 0 40px #ff004488;
--glow-yellow:0 0 10px #ffff00,0 0 20px #ffff00,0 0 40px #ffff0088;
}
*{box-sizing:border-box}
body{font-family:'Rajdhani',sans-serif;background:var(--bg-dark);color:#e0e0e0;padding:20px;margin:0;
background-image:radial-gradient(ellipse at top,#0d1a2d 0%,transparent 50%),
linear-gradient(180deg,transparent 0%,rgba(0,255,242,0.03) 100%);min-height:100vh;font-size:18px}
@media(min-width:768px){body{padding:20px 40px}}
@media(max-width:767px){
h1{font-size:1.4em;letter-spacing:2px}
.gauge-row{gap:10px}
.gauge{transform:scale(0.9)}
.gpu-card{padding:15px}
.io-stats{flex-wrap:wrap;gap:10px}
.time-range{flex-wrap:wrap}
.sparkline-container{grid-template-columns:1fr 1fr}
}
body::before{content:'';position:fixed;top:0;left:0;right:0;bottom:0;
background:repeating-linear-gradient(0deg,transparent,transparent 2px,rgba(0,0,0,0.1) 2px,rgba(0,0,0,0.1) 4px);
pointer-events:none;z-index:9999;opacity:0.3}
h1{font-family:'Orbitron',monospace;color:var(--neon-cyan);font-size:2.2em;font-weight:900;letter-spacing:4px;
text-shadow:var(--glow-cyan);border-bottom:2px solid var(--neon-cyan);padding-bottom:15px;margin-bottom:30px;
text-transform:uppercase;text-align:center}
h1::before{content:'◈ ';color:var(--neon-magenta)}
h1::after{content:' ◈';color:var(--neon-magenta)}
h3{margin-top:0;color:var(--neon-cyan);font-family:'Orbitron',monospace;font-size:1.1em;letter-spacing:2px;
text-transform:uppercase;text-shadow:0 0 10px var(--neon-cyan)}
.card{background:var(--bg-card);padding:20px;border-radius:4px;margin:20px 0;
border:1px solid #1a2332;box-shadow:0 0 20px rgba(0,255,242,0.1),inset 0 0 60px rgba(0,0,0,0.3)}
.card::before{content:'';position:absolute;top:0;left:0;right:0;height:1px;
background:linear-gradient(90deg,transparent,var(--neon-cyan),transparent)}
.online{color:var(--neon-green);font-weight:bold;text-shadow:var(--glow-green)}
.offline{color:var(--neon-red);font-weight:bold;text-shadow:var(--glow-red);animation:pulse 1s infinite}
/* MISMATCH (2026-07-30): amber, and it pulses like OFFLINE because it also needs
   attention - but a distinct colour, because "I reached the wrong machine" is a
   different fault from "this machine is down" and wants a different fix (DNS/DHCP,
   not a power button). */
.mismatch{color:var(--neon-amber,#ffb020);font-weight:bold;animation:pulse 1s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.7}}
@keyframes glow-pulse{0%,100%{filter:brightness(1)}50%{filter:brightness(1.3)}}
.job{padding:10px 15px;border-left:3px solid var(--neon-cyan);margin:8px 0;background:var(--bg-panel);
border-radius:0 4px 4px 0;font-family:'Rajdhani',sans-serif;transition:all 0.3s}
.job:hover{background:#1a2332;border-left-color:var(--neon-magenta);box-shadow:0 0 15px rgba(0,255,242,0.2)}
.job.video{border-color:var(--neon-magenta)}
.job.completed{opacity:0.7}
pre{background:var(--bg-panel);padding:15px;border-radius:4px;overflow-x:auto;border:1px solid #1a2332;
font-family:'Rajdhani',monospace;color:var(--neon-cyan)}
table{width:100%;border-collapse:collapse}
td,th{padding:12px;text-align:left;border-bottom:1px solid #1a2332}
th{color:var(--neon-cyan);font-family:'Orbitron',monospace;font-size:0.8em;letter-spacing:1px}
.gpu-card{background:linear-gradient(135deg,var(--bg-panel) 0%,var(--bg-card) 100%);
padding:20px;border-radius:8px;margin:10px 0;border:1px solid #1a2332;position:relative;overflow:hidden}
.gpu-card::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;
background:linear-gradient(90deg,var(--neon-cyan),var(--neon-magenta),var(--neon-cyan))}
.gpu-card::after{content:'';position:absolute;top:0;right:0;width:100px;height:100px;
background:radial-gradient(circle,rgba(0,255,242,0.1) 0%,transparent 70%);pointer-events:none}
.gpu-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:15px}
.gpu-name{font-family:'Orbitron',monospace;font-size:1.3em;font-weight:700;text-transform:uppercase;
letter-spacing:2px;color:var(--neon-cyan);text-shadow:0 0 10px var(--neon-cyan)}
.progress-bar{background:#1a1a2e;border-radius:2px;height:24px;overflow:hidden;margin:5px 0;
border:1px solid #2a2a4e;position:relative}
.progress-bar::before{content:'';position:absolute;top:0;left:0;right:0;bottom:0;
background:repeating-linear-gradient(90deg,transparent,transparent 10px,rgba(255,255,255,0.03) 10px,rgba(255,255,255,0.03) 20px)}
.progress-fill{height:100%;transition:width 2s cubic-bezier(0.25,0.1,0.25,1);position:relative}
.progress-fill::after{content:'';position:absolute;top:0;right:0;width:30px;height:100%;
background:linear-gradient(90deg,transparent,rgba(255,255,255,0.4));opacity:0.3}
.progress-fill.progress-red{animation:bar-breathe 4s ease-in-out infinite}
.progress-fill.progress-red::after{animation:shimmer 2s ease-in-out infinite}
@keyframes bar-breathe{0%,100%{filter:brightness(1)}50%{filter:brightness(1.15)}}
@keyframes shimmer{0%,100%{opacity:0.3}50%{opacity:0.8}}
.progress-vram,.progress-ram,.progress-disk{background:linear-gradient(90deg,#00ff8844,var(--neon-green));box-shadow:0 0 20px #39ff1466}
.progress-swap{background:linear-gradient(90deg,#ff004444,var(--neon-red));box-shadow:0 0 20px #ff004466}
.progress-yellow{background:linear-gradient(90deg,#ffaa0044,var(--neon-yellow));box-shadow:0 0 20px #ffff0066}
.progress-red{background:linear-gradient(90deg,#ff004444,var(--neon-red));box-shadow:0 0 20px #ff004466}
.io-stats{display:flex;gap:20px;margin-top:15px;font-size:1.05em;padding:12px;background:var(--bg-dark);border-radius:4px;border:1px solid #1a2332}
.io-stat{display:flex;align-items:center;gap:8px}
.io-stat .value{color:var(--neon-cyan);font-weight:bold;font-family:'Orbitron',monospace;text-shadow:0 0 10px var(--neon-cyan)}
.io-stat.warning .value{color:var(--neon-yellow);text-shadow:0 0 10px var(--neon-yellow)}
.io-stat.danger .value{color:var(--neon-red);text-shadow:0 0 10px var(--neon-red)}
.time-range{display:flex;gap:8px;margin-bottom:15px}
.time-range button{background:var(--bg-panel);color:#888;border:1px solid #2a2a4e;padding:10px 18px;
border-radius:4px;cursor:pointer;font-family:'Orbitron',monospace;font-size:0.9em;letter-spacing:1px;
text-transform:uppercase;transition:all 0.3s}
.time-range button:hover{border-color:var(--neon-cyan);color:var(--neon-cyan);box-shadow:0 0 15px rgba(0,255,242,0.3)}
.time-range button.active{background:transparent;border-color:var(--neon-cyan);color:var(--neon-cyan);
box-shadow:var(--glow-cyan)}
.sparkline-container{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}
.sparkline-box{background:var(--bg-panel);padding:12px;border-radius:4px;border:1px solid #1a2332}
.sparkline-label{font-size:0.9em;color:#888;margin-bottom:8px;font-family:'Orbitron',monospace;letter-spacing:1px}
.sparkline{height:45px;display:flex;align-items:end;gap:2px;width:100%;overflow:hidden}
.sparkline-bar{background:var(--neon-cyan);flex:1 1 0;min-width:0;border-radius:1px 1px 0 0;
transition:height 0.5s cubic-bezier(0.4,0,0.2,1);box-shadow:0 0 5px var(--neon-cyan)}
.sparkline-bar.high{background:var(--neon-yellow);box-shadow:0 0 5px var(--neon-yellow)}
.sparkline-bar.critical{background:var(--neon-red);box-shadow:0 0 5px var(--neon-red);animation:spark-glow 1.2s ease-in-out infinite}
.history-machine{padding:15px 18px;border-radius:6px;margin-bottom:16px;border:1px solid #1a2332;overflow:hidden}
.history-machine.alt-0{background:rgba(0,255,242,0.04)}
.history-machine.alt-1{background:rgba(255,0,255,0.05)}
.max-util{color:var(--neon-green);text-shadow:0 0 8px var(--neon-green);font-family:'Orbitron',monospace}
.cost{color:var(--neon-green);font-family:'Orbitron',monospace;text-shadow:0 0 6px var(--neon-green)}
#energy td,#energy-fleet td{font-size:0.95em}
@keyframes spark-glow{0%,100%{opacity:0.85}50%{opacity:1}}
.gauge-arc{}
.progress-label{display:flex;justify-content:space-between;font-size:1em;color:#888;
font-family:'Rajdhani',sans-serif;letter-spacing:1px}
.loaded-models{margin-top:7px;padding:7px 9px;border-left:2px solid var(--neon-cyan);
background:rgba(0,255,242,0.04);font-size:0.85em;color:#889}
.loaded-model-title{font-family:'Orbitron',monospace;font-size:0.72em;letter-spacing:1px;color:#667}
.loaded-model{display:flex;justify-content:space-between;gap:12px;margin-top:3px}
.loaded-model-name{color:var(--neon-cyan);font-weight:bold}
.loaded-model-vram{color:var(--neon-green);white-space:nowrap}
.stat-row{margin:10px 0}
/* --- COMPACT CARD LAYOUT (2026-07-29, Ben: same info, smaller space) --- */
.gpu-card.compact{padding:12px 14px;margin:0}
.gpu-card.compact .gpu-header{margin-bottom:6px}
.gpu-card.compact .gpu-name{font-size:1.05em;letter-spacing:1px}
.hdr-right{display:flex;align-items:center;gap:8px;font-size:0.8em}
.peak-inline{font-family:'Orbitron',monospace;font-size:0.72em;color:var(--neon-green);
text-shadow:0 0 6px var(--neon-green);white-space:nowrap;opacity:0.85}
.dial-strip{display:flex;gap:2px;justify-content:space-between;align-items:flex-start;margin:8px 0 6px}
.io-line{display:flex;gap:12px;white-space:nowrap;align-items:center}
.card-foot .io-stat{gap:5px}
/* Each dial owns an equal column so neighbouring labels can never collide. */
.dial-strip .gauge{flex:1 1 0;min-width:0;overflow:hidden}
.dial-strip .gauge-dial{margin:0 auto}
.dial-strip .gauge-label{font-size:0.55em;letter-spacing:1px;margin-top:3px;
display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.dial-strip .gauge-value{font-size:0.78em;margin-top:0;display:block;
white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
/* --- Tokens/sec strip (2026-07-29, Ben): the card's top accent line became a
   two-band live indicator. TOP band = tokens/sec right now, BOTTOM band =
   today's peak tokens/sec. Both on the same scale so they compare. --- */
.gpu-card.compact.has-tps::before{display:none}
.tps-strip{position:absolute;top:0;left:0;right:0;height:4px;display:flex;
flex-direction:column;z-index:2}
.tps-band{height:2px;width:100%;background:#1d2640;position:relative;overflow:hidden}
.tps-band .tps-fill{position:absolute;left:0;top:0;bottom:0;width:0;
transition:width 1.2s cubic-bezier(0.4,0,0.2,1)}
.tps-band.now .tps-fill{background:linear-gradient(90deg,#00fff2,#39ff14);
box-shadow:0 0 8px #00fff2}
.tps-band.peak .tps-fill{background:linear-gradient(90deg,#7a3fff,#ff00ff);
box-shadow:0 0 8px #ff00ff;opacity:0.85}
.mini-row{display:grid;grid-template-columns:44px 1fr auto;align-items:center;gap:8px;margin:4px 0}
.mini-row .progress-bar{height:9px;margin:0;border-radius:3px}
.mini-label{font-family:'Orbitron',monospace;font-size:0.6em;letter-spacing:1px;color:#778}
.mini-val{font-family:'Orbitron',monospace;font-size:0.72em;color:#aab;white-space:nowrap}
.serving-line{margin-top:7px;padding:5px 8px;border-left:2px solid var(--neon-cyan);
background:rgba(0,255,242,0.04);font-size:0.78em;color:#889;line-height:1.5}
.serving-line b{color:var(--neon-cyan)}
.serving-line .tps{font-family:'Orbitron',monospace;color:var(--neon-cyan)}
.serving-line .dim{color:#667}
.card-foot{display:flex;justify-content:space-between;align-items:center;gap:8px;
margin-top:8px;font-size:0.72em;color:#667}
.card-foot .val{font-family:'Orbitron',monospace;color:var(--neon-cyan)}
.card-actions{display:flex;gap:6px}
.card-actions button{margin:0!important;padding:4px 9px;font-size:0.62em;letter-spacing:1px;white-space:nowrap}
.queue-badge{background:var(--neon-yellow);color:#000;padding:3px 10px;border-radius:2px;font-size:0.8em;
font-family:'Orbitron',monospace;font-weight:bold;box-shadow:0 0 10px var(--neon-yellow)}
.queue-badge.empty{background:#2a2a4e;color:#666;box-shadow:none}
.gauge-row{display:flex;gap:15px;margin:20px 0;justify-content:center;flex-wrap:wrap}
.gauge{text-align:center}
.gauge-dial{position:relative;margin:0 auto;overflow:hidden;background:#1a1a2e;border-radius:999px 999px 0 0}
.gauge-bg{position:absolute;width:100%;height:200%;border-radius:50%}
.gauge-mask{position:absolute;bottom:0;left:10%;width:80%;height:80%;background:var(--bg-card);border-radius:999px 999px 0 0}
.gauge-needle{position:absolute;bottom:0;left:50%;background:linear-gradient(to top,#fff 0%,#fff 60%,var(--neon-cyan) 100%);transform-origin:bottom center;transition:transform 1.5s cubic-bezier(0.4,0,0.2,1);border-radius:2px}
.gauge-peak{position:absolute;bottom:0;left:50%;transform-origin:bottom center;
border-radius:1px;transition:transform 1.5s cubic-bezier(0.4,0,0.2,1);pointer-events:none;z-index:4;
filter:drop-shadow(0 0 3px #ffb000)}
/* Peak-hold on the bars too: a bright vertical tick parked at today's max. */
.progress-bar{position:relative}
.bar-peak{position:absolute;top:-1px;bottom:-1px;width:2px;background:#fff8e1;
box-shadow:0 0 6px #ffb000,0 0 2px #fff;z-index:3;pointer-events:none;
transition:left 1.5s cubic-bezier(0.4,0,0.2,1)}
.gauge-center{position:absolute;bottom:-5px;left:50%;background:#0a0a0f;border:2px solid var(--neon-cyan);border-radius:50%;transition:border-color 0.8s}
.gauge-label{font-family:'Orbitron',monospace;font-size:0.85em;color:#888;margin-top:8px;
letter-spacing:2px;text-transform:uppercase}
.gauge-value{font-family:'Orbitron',monospace;font-size:1.3em;font-weight:bold;margin-top:4px;
color:var(--neon-cyan);text-shadow:0 0 10px var(--neon-cyan)}
.power-btn{background:transparent;color:var(--neon-yellow);border:1px solid var(--neon-yellow);
padding:10px 18px;border-radius:4px;cursor:pointer;font-family:'Orbitron',monospace;font-size:0.9em;
letter-spacing:1px;text-transform:uppercase;transition:all 0.3s;margin-top:10px}
.power-btn:hover{background:var(--neon-yellow);color:#000;box-shadow:var(--glow-yellow)}
.swap-btn{background:transparent;color:var(--neon-cyan);border:1px solid var(--neon-cyan);
padding:10px 18px;border-radius:4px;cursor:pointer;font-family:'Orbitron',monospace;font-size:0.9em;
letter-spacing:1px;text-transform:uppercase;transition:all 0.3s;margin-top:10px;margin-left:10px}
.swap-btn:hover{background:var(--neon-cyan);color:#000;box-shadow:var(--glow-cyan)}
.job-time{color:#666;font-size:0.9em}
.job-duration{color:var(--neon-green);font-weight:bold;font-family:'Orbitron',monospace;text-shadow:0 0 5px var(--neon-green)}
.job-filters{display:flex;gap:8px;margin-bottom:15px;flex-wrap:wrap}
.job-filter{background:var(--bg-panel);color:#888;border:1px solid #2a2a4e;padding:8px 14px;
border-radius:4px;cursor:pointer;font-family:'Orbitron',monospace;font-size:0.85em;transition:all 0.3s}
.job-filter:hover{border-color:var(--neon-cyan);color:var(--neon-cyan)}
.job-filter.active{border-color:var(--neon-cyan);color:var(--neon-cyan);box-shadow:0 0 10px rgba(0,255,242,0.3)}
.queue-info{margin:10px 0 15px 0;font-family:'Orbitron',monospace;font-size:0.95em}
.queue-count{color:var(--neon-yellow);text-shadow:0 0 8px var(--neon-yellow)}
.jobs-today{color:#888;font-size:0.85em;margin-top:4px}
.machine-power{display:flex;gap:6px;align-items:center}
.machine-power button{background:transparent;border:1px solid #2a2a4e;color:#888;padding:4px 10px;
border-radius:4px;cursor:pointer;font-family:'Orbitron',monospace;font-size:0.7em;
letter-spacing:1px;text-transform:uppercase;transition:all 0.3s}
.machine-power .reboot-btn:hover{border-color:var(--neon-yellow);color:var(--neon-yellow);box-shadow:0 0 10px rgba(255,255,0,0.3)}
.machine-power .shutdown-btn:hover{border-color:var(--neon-red);color:var(--neon-red);box-shadow:0 0 10px rgba(255,0,68,0.3)}
.fleet-row{display:grid;grid-template-columns:repeat(6,1fr);gap:8px}
@media(max-width:900px){.fleet-row{grid-template-columns:repeat(3,1fr)}}
@media(max-width:520px){.fleet-row{grid-template-columns:repeat(2,1fr)}}
.fleet-tile{background:var(--bg-panel);border:1px solid #1a2332;border-radius:6px;padding:8px 10px;
position:relative;overflow:hidden;font-size:0.78em;line-height:1.5}
.fleet-tile::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;
background:linear-gradient(90deg,var(--neon-green),transparent)}
.fleet-tile.off::before{background:linear-gradient(90deg,var(--neon-red),transparent)}
.fleet-tile.off{opacity:0.65}
/* MISMATCH (2026-07-30): the address answered as a different machine. Amber, not
   red - this is "do not trust this tile", which is a different problem from
   "this box is down", and it must not look like ordinary offline. Full opacity
   so it draws the eye instead of fading out like a box that is merely asleep. */
.fleet-tile.mismatch::before{background:linear-gradient(90deg,var(--neon-amber,#ffb020),transparent)}
.fleet-tile.mismatch{opacity:1;border-color:var(--neon-amber,#ffb020)}
.ft-name{font-family:'Orbitron',monospace;font-size:0.85em;letter-spacing:1px;color:var(--neon-cyan);
text-transform:uppercase;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-bottom:2px}
.ft-stat{display:flex;justify-content:space-between;color:#9ab}
.ft-stat b{font-family:'Orbitron',monospace;font-weight:400;color:var(--neon-green)}
.ft-stat b.warn{color:var(--neon-yellow)}
.ft-stat b.hot{color:var(--neon-red)}
.ft-btn{width:100%;margin-top:5px;background:transparent;color:#667;border:1px solid #2a2a4e;
padding:3px 0;border-radius:3px;cursor:pointer;font-family:'Orbitron',monospace;font-size:0.75em;
letter-spacing:1px;text-transform:uppercase;transition:all 0.3s}
.ft-btn:hover{border-color:var(--neon-yellow);color:var(--neon-yellow);box-shadow:0 0 8px rgba(255,255,0,0.3)}
.ft-btn.wake:hover{border-color:var(--neon-green);color:var(--neon-green);box-shadow:0 0 8px rgba(57,255,20,0.3)}
.ft-btns{display:flex;gap:4px;margin-top:5px}
.ft-btns .ft-btn{margin-top:0;flex:1;padding:3px 0;font-size:0.72em}
.ft-btn.down:hover{border-color:var(--neon-red);color:var(--neon-red);box-shadow:0 0 8px rgba(255,0,68,0.3)}
.ft-btn:disabled{opacity:0.25;cursor:default}
.ft-btn:disabled:hover{border-color:#2a2a4e;color:#667;box-shadow:none}
.glance-row{display:flex;gap:20px;flex-wrap:wrap;align-items:center}
.glance-stat{text-align:center;min-width:90px}
.glance-num{font-family:'Orbitron',monospace;font-size:1.8em;font-weight:700;color:var(--neon-cyan);text-shadow:0 0 10px var(--neon-cyan);line-height:1.1}
.glance-num.zero{color:var(--neon-green);text-shadow:var(--glow-green)}
.glance-num.warn{color:var(--neon-yellow);text-shadow:0 0 10px var(--neon-yellow)}
.glance-num.hot{color:var(--neon-red);text-shadow:var(--glow-red)}
.glance-label{font-size:0.75em;color:#889;letter-spacing:1px;text-transform:uppercase;margin-top:2px}
.glance-unavailable{color:#667;font-size:0.9em;font-style:italic}
.route-pills{display:flex;gap:8px;flex-wrap:wrap}
/* Route badges on the machine card that actually serves them (2026-07-29, Ben).
   GREEN = model resident in memory, GREY = route up but nothing loaded (normal
   for llama-swap after idle), RED = route missing from the gateway. */
.card-routes{display:flex;gap:4px;flex-wrap:wrap;margin:6px 0 2px}
.card-route{font-family:'Orbitron',monospace;font-size:0.6em;letter-spacing:1px;
padding:2px 7px;border-radius:999px;border:1px solid #2a3450;color:#778;white-space:nowrap}
.card-route.loaded{color:var(--neon-green);border-color:var(--neon-green);
box-shadow:0 0 7px rgba(57,255,20,0.3);background:rgba(57,255,20,0.07)}
.card-route.idle{color:#7d8798;border-color:#333f5c}
.card-route.missing{color:var(--neon-red);border-color:var(--neon-red);
box-shadow:0 0 7px rgba(255,0,68,0.3)}
.ft-routes{display:flex;gap:3px;flex-wrap:wrap;margin-top:5px}
/* One-row glance strip: CI queue + route health + agents, ~1/3 the height of
   the three cards it replaced (Ben, 2026-07-29). Numbers big, words small. */
.ship-flow{display:flex;align-items:stretch;gap:0;flex-wrap:wrap;margin:0 0 9px 0}
.ship-logo{width:30px;height:30px;display:block;margin:0 auto 3px auto;
filter:drop-shadow(0 0 6px rgba(99,102,241,0.75))}
.ship-label{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:0;font-family:'Orbitron',monospace;
font-size:0.72em;letter-spacing:2px;color:var(--neon-cyan);text-shadow:0 0 8px var(--neon-cyan);
padding-right:12px;white-space:nowrap}
.ship-stage{background:rgba(255,255,255,0.025);border:1px solid #1e2942;border-radius:7px;
padding:7px 14px;min-width:92px;text-align:center;display:flex;flex-direction:column;
align-items:center;justify-content:center;gap:2px}
.ship-num{font-family:'Orbitron',monospace;font-size:1.5em;font-weight:700;line-height:1;
color:var(--neon-cyan);text-shadow:0 0 10px var(--neon-cyan)}
.ship-num.ok{color:var(--neon-green);text-shadow:0 0 10px var(--neon-green)}
.ship-num.warn{color:var(--neon-yellow);text-shadow:0 0 10px var(--neon-yellow)}
.ship-num.hot{color:var(--neon-red);text-shadow:0 0 10px var(--neon-red)}
.ship-num.stamp{font-size:1.05em;white-space:nowrap}
.ship-cap{font-family:'Orbitron',monospace;font-size:0.56em;letter-spacing:1.5px;
text-transform:uppercase;color:#7a839c;white-space:nowrap}
.ship-sub{font-size:0.62em;color:#5d6478;white-space:nowrap}
.ship-arrow{display:flex;align-items:center;padding:0 9px;color:var(--neon-magenta);
font-size:0.9em;text-shadow:0 0 8px var(--neon-magenta)}
.ship-spark{display:flex;align-items:flex-end;gap:2px;height:11px;margin-top:3px}
.ship-spark i{width:5px;background:var(--neon-cyan);box-shadow:0 0 4px var(--neon-cyan);
border-radius:1px 1px 0 0;min-height:1px}
.gstrip{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:10px;align-items:center}
.gcell{display:flex;align-items:center;gap:9px;padding:2px 4px;border-left:2px solid #24304d;min-width:0}
.gcell .gicon{font-size:1.15em;line-height:1;flex:0 0 auto}
.gnum{font-family:'Orbitron',monospace;font-size:1.35em;font-weight:700;line-height:1;
color:var(--neon-cyan);text-shadow:0 0 9px var(--neon-cyan)}
.gnum.ok{color:var(--neon-green);text-shadow:0 0 9px var(--neon-green)}
.gnum.warn{color:var(--neon-yellow);text-shadow:0 0 9px var(--neon-yellow)}
.gnum.hot{color:var(--neon-red);text-shadow:0 0 9px var(--neon-red)}
.gcap{font-size:0.62em;letter-spacing:1px;text-transform:uppercase;color:#778;
font-family:'Orbitron',monospace;margin-top:2px;white-space:nowrap}
.gpair{display:flex;flex-direction:column;align-items:center;flex:0 0 auto}
.gdim{color:#667;font-size:0.8em}
.gbar{flex:1 1 auto;min-width:36px;height:7px;border-radius:3px;background:#182137;
border:1px solid #24304d;overflow:hidden;position:relative}
.gbar i{display:block;height:100%;background:linear-gradient(90deg,#00fff2,#39ff14);
box-shadow:0 0 7px #00fff2;transition:width 1.2s cubic-bezier(0.4,0,0.2,1)}
.gbar i.hot{background:linear-gradient(90deg,#ff9500,#ff0044);box-shadow:0 0 7px #ff0044}
.gdots{display:flex;gap:3px;flex-wrap:wrap;flex:1 1 auto}
.gdot{width:8px;height:8px;border-radius:50%;background:#2a3450;border:1px solid #37436a}
.gdot.loaded{background:var(--neon-green);border-color:var(--neon-green);box-shadow:0 0 6px var(--neon-green)}
.gdot.missing{background:var(--neon-red);border-color:var(--neon-red);box-shadow:0 0 6px var(--neon-red)}
.route-pill{font-family:'Orbitron',monospace;font-size:0.8em;letter-spacing:1px;padding:5px 12px;
border-radius:999px;border:1px solid #2a2a4e;background:var(--bg-panel);color:#889}
.route-pill.live{color:var(--neon-green);border-color:var(--neon-green);box-shadow:0 0 8px rgba(57,255,20,0.25)}
.route-pill.missing{color:var(--neon-red);border-color:var(--neon-red);box-shadow:0 0 8px rgba(255,0,68,0.25);animation:pulse 1.5s infinite}
</style></head>
<body><h1>GANDALF // FLEET MONITOR</h1>

<div id="fleet-summary" style="margin:4px 0 14px 0;font-size:0.95em;color:#889">Loading fleet summary...</div>

<div class=card id=glance-strip style="padding:9px 12px;margin:10px 0">
<div id="ship-flow" class="ship-flow"><span class="gdim">shipping pipeline…</span></div>
<div class="gstrip">
<div class="gcell" id="route-health-body"><span class="gdim">routes…</span></div>
<div class="gcell" id="fleet-stats-body"><span class="gdim">agents…</span></div>
</div>
</div>

<div class=card id=monitors><p>Loading...</p></div>
<div class=card id=fleet-hosts style="padding:12px 15px;margin:12px 0">
<div id=fleet-row class=fleet-row><p style="grid-column:1/-1;color:#667;margin:0">Scanning fleet hosts...</p></div>
</div>

<div class=card id=history>
<h3>📈 Historical Metrics</h3>
<div class="time-range">
<button onclick="setRange('hour')" id="btn-hour" class="active">Last Hour</button>
<button onclick="setRange('day')" id="btn-day">Last 24h</button>
<button onclick="setRange('week')" id="btn-week">Last Week</button>
<button onclick="setRange('month')" id="btn-month">Last Month</button>
</div>
<div id="sparklines"><p>Loading history...</p></div>
</div>

<div class=card id=energy>
<h3>⚡ Energy &amp; Cost — by Machine</h3>
<div id="energy-by-machine"><p>Loading...</p></div>
<p style="margin-top:12px;color:#666;font-size:0.8em">GPU power only (nvidia-smi) at PEC time-of-use rates. Excludes CPU/PSU overhead; Macs report no wattage.</p>
</div>

<div class=card><h3>🖥️ Fleet</h3>
<table>
<tr><th>Machine</th><th>Hardware</th><th>GPU</th><th>Role</th></tr>
<tr><td>🧙 <b>Gandalf</b></td><td>Ryzen 9 9950X · 256GB</td><td><b style="color:#d9a54a">RTX PRO 6000 · 96GB</b></td><td>Video + heavy generation</td></tr>
<tr><td>🧝 <b>Frodo</b></td><td>Core i9-9900K · 128GB</td><td><b style="color:#5a8a4a">RTX 5090 · 32GB</b></td><td>Flux / SDXL generation</td></tr>
<tr><td>🍎 <b>Pippin</b></td><td>Mac Studio M1 Max · 64GB</td><td><b style="color:#5a8a4a">32-core Apple GPU</b></td><td>Mac workloads</td></tr>
</table>
</div>

<div class=card id=energy-fleet>
<h3>⚡ Fleet Energy &amp; Cost — All Machines</h3>
<div id="energy-fleet-body"><p>Loading...</p></div>
</div>

<div class=card><h3>📡 API Endpoints</h3><pre>GET  /api/status  - Target status + live metrics
GET  /api/logs    - Job / usage history
GET  /api/history - Historical metrics (range=hour|day|week|month)
GET  /api/energy  - GPU energy + time-of-use cost (day/week/month)
GET  /api/fleet_stats - CLI agent sessions per box + CI runner busy/total
GET  /api/model_serving - tokens/sec dials + requests served (model boxes)
GET  /api/health  - Health check</pre>
</div>

<script>
const icons = {gandalf: "🧙", frodo: "🧝", pippin: "🍎", shadowfax: "🐴", aragorn: "👑"};
const fleetIcons = {northfarthing: "🌾", eastfarthing: "🌾", southfarthing: "🌾", westfarthing: "🌾", shadowfax: "🐴", sam: "🌱"};

// Core vs Reserve summary (2026-07-18): 5-of-6 reserve boxes down at any given
// time is EXPECTED (the Shire farthings + sam are spun up on demand, not
// always-on) but reads at a glance like "everything is down". This banner
// separates the always-on CORE (gandalf/frodo/pippin from /api/status, plus
// shadowfax itself from /api/fleet) from the RESERVE/Shire boxes so Ben can
// see the core is healthy without counting red tiles.
const CORE_TARGET_NAMES = ['gandalf', 'frodo', 'pippin'];       // from /api/status
const RESERVE_FLEET_NAMES = ['northfarthing', 'eastfarthing', 'southfarthing', 'westfarthing', 'sam']; // from /api/fleet
let lastTargetsOnline = {};
let lastFleetOnline = {};

function updateFleetSummary() {
    const el = document.getElementById('fleet-summary');
    if (!el) return;
    const coreFromTargets = CORE_TARGET_NAMES.filter(n => lastTargetsOnline[n]).length;
    const shadowfaxUp = lastFleetOnline['shadowfax'] ? 1 : 0;
    const coreUp = coreFromTargets + shadowfaxUp;
    const coreTotal = CORE_TARGET_NAMES.length + 1;
    const reserveUp = RESERVE_FLEET_NAMES.filter(n => lastFleetOnline[n]).length;
    const reserveTotal = RESERVE_FLEET_NAMES.length;
    const coreColor = (coreUp === coreTotal) ? 'var(--neon-green)' : 'var(--neon-red)';
    el.innerHTML =
        '<b>Core:</b> <span style="color:' + coreColor + ';font-weight:bold">' + coreUp + '/' + coreTotal + ' up</span>' +
        ' &nbsp;·&nbsp; <b>Reserve (Shire):</b> <span style="color:#8a8">' + reserveUp + '/' + reserveTotal + ' up</span>' +
        ' <span style="color:#556;font-size:0.85em">— reserve boxes are on-demand, not always-on</span>';
}

function tempClass(t) { return t >= 85 ? "hot" : (t >= 70 ? "warn" : ""); }
function pctClass(p) { return p >= 90 ? "hot" : (p >= 70 ? "warn" : ""); }

let fleetInitialized = false;

function fleetTileHtml(name, n) {
    const icon = fleetIcons[name] || '🖥️';
    let html = '<div class="fleet-tile' + (n.online ? '' : ' off') + (n.mismatch ? ' mismatch' : '') + '" id="fleet-tile-' + name + '">';
    html += '<div class="ft-name">' + icon + ' ' + name + '</div>';
    if (n.online) {
        html += '<div class="ft-stat"><span>CPU</span><b class="' + pctClass(n.cpu) + '" id="ft-cpu-' + name + '">' + n.cpu + '%</b></div>';
        html += '<div class="ft-stat"><span>TEMP</span><b class="' + tempClass(n.temp_c) + '" id="ft-temp-' + name + '">' + n.temp_c + '°C</b></div>';
        html += '<div class="ft-stat"><span>RAM</span><b class="' + pctClass(n.ram_pct) + '" id="ft-ram-' + name + '">' + n.ram_used_gb + '/' + n.ram_total_gb + 'G</b></div>';
    } else if (n.mismatch) {
        // The address for this tile answered as a DIFFERENT machine, so we have
        // no trustworthy numbers for THIS box. Say exactly that - deliberately no
        // stats, because numbers on screen get believed. See _fleet_host_matches.
        html += '<div class="ft-stat"><span style="color:var(--neon-amber,#ffb020)">MISMATCH</span><b></b></div>';
        html += '<div class="ft-stat"><span style="font-size:10px">got</span><b style="font-size:10px">' + (n.reported_host || '?') + '</b></div>';
        html += '<div class="ft-stat"><span style="font-size:10px">at</span><b style="font-size:10px">' + (n.ssh_host || '?') + '</b></div>';
    } else {
        html += '<div class="ft-stat"><span style="color:var(--neon-red)">OFFLINE</span><b></b></div>';
        html += '<div class="ft-stat"><span>&nbsp;</span><b></b></div>';
        html += '<div class="ft-stat"><span>&nbsp;</span><b></b></div>';
    }
    html += '<div class="ft-routes" id="ft-routes-' + name + '"></div>';
    if (n.can_wake) {
        // Shire boxes: full power control — shutdown / restart / wake
        html += '<div class="ft-btns">';
        html += '<button class="ft-btn down" ' + (n.online ? '' : 'disabled ') + 'onclick="fleetPower(\\'' + name + '\\', \\'shutdown\\')" title="Shut down">⏻ Off</button>';
        html += '<button class="ft-btn" ' + (n.online ? '' : 'disabled ') + 'onclick="fleetPower(\\'' + name + '\\', \\'reboot\\')" title="Restart">⟳ Boot</button>';
        html += '<button class="ft-btn wake" ' + (n.online ? 'disabled ' : '') + 'onclick="fleetPower(\\'' + name + '\\', \\'wake\\')" title="Wake up">⚡ Wake</button>';
        html += '</div>';
    } else if (n.online) {
        html += '<button class="ft-btn" onclick="fleetPower(\\'' + name + '\\', \\'reboot\\')">⟳ Reboot</button>';
    } else {
        html += '<button class="ft-btn" disabled>—</button>';
    }
    html += '</div>';
    return html;
}

function refreshFleet() {
    fetch('/api/fleet').then(r => r.json()).then(data => {
        for (const [name, n] of Object.entries(data)) { lastFleetOnline[name] = n.online; }
        updateFleetSummary();
        if (!fleetInitialized) {
            let html = '';
            for (const [name, n] of Object.entries(data)) {
                html += fleetTileHtml(name, n);
            }
            document.getElementById('fleet-row').innerHTML = html;
            for (const [name, n] of Object.entries(data)) {
                const tile = document.getElementById('fleet-tile-' + name);
                if (tile) tile.dataset.shape = n.online + ':' + n.can_wake + ':' + !!n.mismatch;
            }
            fleetInitialized = true;
            return;
        }
        // UPDATE PATH: only touch a tile when something about it changed
        // (online/offline flips the button set, so those still get rebuilt;
        // pure stat ticks update text in place).
        for (const [name, n] of Object.entries(data)) {
            const tile = document.getElementById('fleet-tile-' + name);
            // mismatch is part of the shape: flipping into or out of MISMATCH must
            // rebuild the tile, or a stale tile keeps showing the old body.
            const key = n.online + ':' + n.can_wake + ':' + !!n.mismatch;
            if (!tile || tile.dataset.shape !== key) {
                const html = fleetTileHtml(name, n);
                if (tile) {
                    tile.outerHTML = html;
                } else {
                    document.getElementById('fleet-row').insertAdjacentHTML('beforeend', html);
                }
                const newTile = document.getElementById('fleet-tile-' + name);
                if (newTile) newTile.dataset.shape = key;
                continue;
            }
            if (n.online) {
                const cpuEl = document.getElementById('ft-cpu-' + name);
                if (cpuEl && cpuEl.textContent !== n.cpu + '%') {
                    cpuEl.textContent = n.cpu + '%';
                    cpuEl.className = pctClass(n.cpu);
                }
                const tempEl = document.getElementById('ft-temp-' + name);
                if (tempEl && tempEl.textContent !== n.temp_c + '°C') {
                    tempEl.textContent = n.temp_c + '°C';
                    tempEl.className = tempClass(n.temp_c);
                }
                const ramEl = document.getElementById('ft-ram-' + name);
                const ramText = n.ram_used_gb + '/' + n.ram_total_gb + 'G';
                if (ramEl && ramEl.textContent !== ramText) {
                    ramEl.textContent = ramText;
                    ramEl.className = pctClass(n.ram_pct);
                }
            }
        }
    }).catch(() => {});
}

function fleetPower(node, action) {
    if (action === 'reboot' && !confirm('Reboot ' + node + (node === 'shadowfax' ? ' (this dashboard will go dark for ~2 min)' : '') + '?')) return;
    if (action === 'shutdown' && !confirm('SHUT DOWN ' + node + '? It will stay off until you hit Wake.')) return;
    fetch('/api/fleet_power', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({node: node, action: action})
    }).then(r => r.json()).then(d => {
        if (d.error) { alert('Failed: ' + d.error); return; }
        if (action === 'wake') alert('Magic packets sent to ' + node + ' — it should appear within ~90s.');
    }).catch(e => alert('Request failed: ' + e));
}

function ciQueueNumClass(n) { return n === 0 ? 'zero' : (n <= 3 ? 'warn' : 'hot'); }

function sparkHtml(vals) {
    if (!vals || !vals.length) return '';
    const peak = Math.max(1, ...vals);
    return '<div class="ship-spark">' + vals.map(v =>
        '<i style="height:' + Math.max(1, Math.round((v / peak) * 11)) + 'px"></i>').join('') + '</div>';
}

function shipStage(num, cap, cls, sub, spark, help) {
    return '<div class="ship-stage"' + (help ? ' title="' + help.replace(/"/g, '') + '"' : '') + '><div class="ship-num ' + (cls || '') + '">' + num + '</div>'
         + '<div class="ship-cap">' + cap + '</div>'
         + (sub ? '<div class="ship-sub">' + sub + '</div>' : '')
         + (spark ? sparkHtml(spark) : '') + '</div>';
}

// The development pipeline as a left-to-right flow: what is queued, what is in
// flight, what actually shipped. Ben asked for this shape specifically.
function refreshShipFlow() {
    fetch('/api/pipeline').then(r => r.json()).then(d => {
        const el = document.getElementById('ship-flow');
        if (!el) return;
        if (!d.available) {
            el.innerHTML = '<span class="gdim">shipping pipeline unavailable (GitHub read failed) — stale, not an outage</span>';
            return;
        }
        const ARROW = '<div class="ship-arrow">&#10148;</div>';
        const ciNum = d.ci_queued + '/' + d.ci_running;
        const ciCls = d.ci_queued > 5 ? 'hot' : (d.ci_queued > 0 ? 'warn' : 'ok');
        let deploySub = '';
        if (d.deploys_in_flight) deploySub = d.deploys_in_flight + ' in flight' + (d.deploy_started_min !== null && d.deploy_started_min !== undefined ? ' · ' + d.deploy_started_min + 'm' : '');
        else if (d.deploys_failed_today) deploySub = d.deploys_failed_today + ' failed';
        else if (d.deploys_ok_today) deploySub = 'all landed';
        let stamp = '--';
        if (d.last_deploy_at) {
            const t = new Date(d.last_deploy_at);
            stamp = t.toLocaleString('en-US', {month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', timeZone: 'America/Chicago'});
        }
        el.innerHTML =
            '<div class="ship-label"><img src="/armbrain-logo.svg" alt="Armbrain" class="ship-logo" title="Armbrain - the product this pipeline ships">SHIPPING</div>' +
            shipStage(d.issues_open, 'issues open', '', '', null, HELP.issues) + ARROW +
            shipStage(d.prs_open, 'prs open', '', '', null, HELP.prs) + ARROW +
            shipStage(ciNum, 'ci q/run', ciCls, '', null, HELP.ciqr) + ARROW +
            shipStage(d.merged_today, 'merged today', 'ok', '', d.merged_spark, HELP.merged) + ARROW +
            shipStage(d.deploys_ok_today, 'deployed today', d.deploys_failed_today ? 'hot' : '', deploySub, d.deploys_spark, HELP.deployed) + ARROW +
            '<div class="ship-stage" title="' + HELP.lastdep.replace(/"/g, '') + '"><div class="ship-num stamp">' + stamp + '</div>'
              + '<div class="ship-cap">last deploy (CT)</div>'
              + (d.last_deploy_sha ? '<div class="ship-sub">⎇ ' + d.last_deploy_sha + '</div>' : '') + '</div>';
    }).catch(() => {
        const el = document.getElementById('ship-flow');
        if (el) el.innerHTML = '<span class="gdim">shipping pipeline failed to load.</span>';
    });
}

function refreshCiQueue() {
    fetch('/api/ci_queue').then(r => r.json()).then(d => {
        const el = document.getElementById('ci-queue-body');
        if (!el) return;
        if (!d.available) {
            el.innerHTML = '<p class="glance-unavailable">CI signal unavailable right now (token fetch or GitHub API failed) — not necessarily a real outage, just a stale read.</p>';
            return;
        }
        const running = (d.active_jobs === null || d.active_jobs === undefined) ? d.in_progress : d.active_jobs;
        const runningLabel = (d.active_jobs === null || d.active_jobs === undefined) ? 'Running Runs' : 'Running Jobs';
        const runnerDetail = (d.active_runner_count === null || d.active_runner_count === undefined)
            ? ''
            : d.active_runner_count + ' active runner' + (d.active_runner_count === 1 ? '' : 's');
        const orphanDetail = d.orphaned_queued
            ? ' · ' + d.orphaned_queued + ' stale orphan' + (d.orphaned_queued === 1 ? '' : 's') + ' excluded'
            : '';
        const tip = d.repo + ' — the same queue-depth signal the Shire autoscaler watches'
                  + (runnerDetail ? ' · ' + runnerDetail : '') + orphanDetail;
        el.innerHTML =
            '<span class="gicon" title="CI queue">🚦</span>' +
            '<div class="gpair"><div class="gnum ' + ciQueueNumClass(d.queued) + '">' + d.queued + '</div><div class="gcap">queued</div></div>' +
            '<div class="gpair"><div class="gnum ok">' + running + '</div><div class="gcap">' + (runningLabel === 'Running Jobs' ? 'running' : 'runs') + '</div></div>' +
            '<div class="gbar" title="' + tip + '"><i class="' + (d.queued > 5 ? 'hot' : '') + '" style="width:' +
                Math.min(100, (d.queued + running) === 0 ? 0 : (running / Math.max(1, running + d.queued)) * 100) + '%"></i></div>';
    }).catch(() => {
        const el = document.getElementById('ci-queue-body');
        if (el) el.innerHTML = '<p class="glance-unavailable">CI queue check failed to load.</p>';
    });
}

function refreshRouteHealth() {
    fetch('/api/model_routes').then(r => r.json()).then(d => {
        const el = document.getElementById('route-health-body');
        if (!el) return;
        if (!d.available) {
            el.innerHTML = '<p class="glance-unavailable">Gateway unreachable right now (key fetch or gateway ping failed) — not necessarily a real outage, just a stale read.</p>';
            return;
        }
        const missing = d.routes.filter(r => !r.live);
        const loaded = d.routes.filter(r => r.loaded);

        // The pills themselves live on each machine's own card now (Ben,
        // 2026-07-29: "move the coding pathways down to the machine they are
        // running on"). This panel keeps only the fleet-wide summary so a
        // route vanishing on a DARK box is still visible somewhere.
        const byBox = {};
        d.routes.forEach(r => { (byBox[r.box] = byBox[r.box] || []).push(r); });
        Object.keys(byBox).forEach(box => {
            const slot = document.getElementById('routes-' + box) || document.getElementById('ft-routes-' + box);
            if (!slot) return;
            const pills = byBox[box].map(r => {
                const cls = !r.live ? 'missing' : (r.loaded ? 'loaded' : 'idle');
                const mark = !r.live ? '✕ ' : (r.loaded ? '● ' : '○ ');
                const state = !r.live ? 'route MISSING from the gateway'
                          : (r.loaded ? r.model + ' loaded in memory'
                                      : r.model + ' configured, not loaded right now');
                return '<span class="card-route ' + cls + '" title="' + r.name + ' on ' + r.box + ' — ' + state + ' (' + r.model + '). ' + HELP.routepill.replace(/"/g, '') + '">' + mark + r.name + '</span>';
            }).join('');
            if (slot.dataset.lastVal !== pills) { slot.dataset.lastVal = pills; slot.innerHTML = pills; }
        });

        const dots = d.routes.map(r => {
            const cls = !r.live ? 'missing' : (r.loaded ? 'loaded' : '');
            const state = !r.live ? 'MISSING from gateway' : (r.loaded ? 'loaded in memory' : 'configured, not loaded');
            return '<span class="gdot ' + cls + '" title="' + r.name + ' (' + r.box + ') — ' + state + '"></span>';
        }).join('');
        el.innerHTML =
            '<span class="gicon" title="' + HELP.routesum.replace(/"/g, '') + '">🛰️</span>' +
            '<div class="gpair" title="' + HELP.routesum.replace(/"/g, '') + '"><div class="gnum ' + (missing.length ? 'hot' : 'ok') + '">' + loaded.length + '/' + d.routes.length + '</div>' +
            '<div class="gcap">' + (missing.length ? missing.length + ' missing' : 'loaded') + '</div></div>' +
            '<div class="gdots" title="' + HELP.routesum.replace(/"/g, '') + '">' + dots + '</div>';
    }).catch(err => {
        console.error('route health render failed:', err);   // do not hide real bugs
        const el = document.getElementById('route-health-body');
        if (!el) return;
        if (!el.dataset.retried) {
            el.dataset.retried = '1';
            el.innerHTML = '<span class="gdim">routes…</span>';
            setTimeout(refreshRouteHealth, 3000);
            return;
        }
        el.innerHTML = '<span class="gdim" title="The dashboard could not reach its own route-health endpoint. Usually means the service restarted; it retries automatically.">route check unavailable — retrying</span>';
    });
}

// Model serving dials: t/s now + today's serving-time-only average, and
// requests served 1h/24h/7d. Containers live inside the gandalf/frodo target
// cards (built on the initial /api/status render), so this fills them lazily
// and just updates the gauges afterwards.
const TPS_GAUGE_MAX = 400;
function tpsColor() { return '#00fff2'; }  // throughput isn't an alarm metric - keep it cyan
function refreshModelServing() {
    fetch('/api/model_serving').then(r => r.json()).then(data => {
        for (const [name, s] of Object.entries(data)) {
            const el = document.getElementById('serving-' + name);
            if (!el) continue;
            if (!s.available) {
                if (!el.dataset.init) el.innerHTML = '<span class="dim">serving stats warming up…</span>';
                continue;
            }
            // Two-band strip at the top of the card: now (top) vs today's peak
            // (bottom), on one shared scale so the bars are comparable.
            const peak = s.tps_max_today || 0;
            const scale = Math.max(TPS_GAUGE_MAX, peak);
            const nowFill = document.getElementById('tps-fill-now-' + name);
            const peakFill = document.getElementById('tps-fill-peak-' + name);
            if (nowFill) nowFill.style.width = Math.min(100, (s.tps_now / scale) * 100) + '%';
            if (peakFill) peakFill.style.width = Math.min(100, (peak / scale) * 100) + '%';

            // COMPACT (2026-07-29): the two t/s dials collapsed into one text line.
            const servingHtml = '<span class="tps">' + Math.round(s.tps_now) + '</span> t/s now · '
                + '<span class="tps">' + Math.round(peak) + '</span> peak today · '
                + '<span class="tps">' + Math.round(s.tps_avg_today) + '</span> avg · '
                + '<span class="dim">req ' + s.requests.hour + '/1h · ' + s.requests.day + '/24h · ' + s.requests.week + '/7d'
                + (s.approx_requests ? '≈' : '')
                + ' · ' + s.serving_minutes_today + ' min served</span>';
            if (el.dataset.lastVal !== servingHtml) {
                el.dataset.lastVal = servingHtml;
                el.innerHTML = servingHtml;
            }
            el.dataset.init = '1';
        }
    }).catch(() => {});
}

function refreshFleetStats() {
    fetch('/api/fleet_stats').then(r => r.json()).then(d => {
        const el = document.getElementById('fleet-stats-body');
        if (!el) return;
        const a = d.agents || {boxes: {}, total: 0};
        // null = box unreadable right now (down / circuit open) - show ?, not 0
        const agentDetail = Object.entries(a.boxes).map(([n, v]) =>
            n + ' ' + (v === null || v === undefined ? '?' : v)).join(' · ');
        const r = d.runners || {};
        const f = (r.available ? (r.fleet || {busy: 0, total: 0, online: 0}) : null);
        const runnerDetail = r.available
            ? Object.entries(r.boxes).map(([n, b]) => n + ' ' + b.busy + '/' + b.total).join(' · ')
            : 'runner counts unavailable (gh / GitHub API failed) — stale read, not an outage';
        let html = '<span class="gicon" title="agents: ' + agentDetail + '">🤖</span>'
                 + '<div class="gpair" title="' + HELP.agents.replace(/"/g, '') + ' Per box: ' + agentDetail + '"><div class="gnum">' + a.total + '</div><div class="gcap">agents</div></div>';
        if (f) {
            const pct = f.total > 0 ? (f.busy / f.total) * 100 : 0;
            const hot = (f.online > 0 && f.busy >= f.online);
            html += '<div class="gpair" title="' + HELP.runners.replace(/"/g, '') + ' Per box: ' + runnerDetail + '"><div class="gnum ' + (hot ? 'hot' : (f.busy ? 'warn' : 'ok')) + '">'
                 + f.busy + '/' + f.total + '</div><div class="gcap">runners</div></div>'
                 + '<div class="gbar" title="' + runnerDetail + '"><i class="' + (hot ? 'hot' : '') + '" style="width:' + pct + '%"></i></div>';
        } else {
            html += '<span class="gdim" title="' + runnerDetail + '">runners ?</span>';
        }
        el.innerHTML = html;
    }).catch(() => {
        const el = document.getElementById('fleet-stats-body');
        if (el) el.innerHTML = '<p class="glance-unavailable">Fleet stats failed to load.</p>';
    });
}

function progressBar(percent, cls, id, peakPct) {
    let barClass = cls;
    if (percent > 90) barClass = 'progress-red';
    else if (percent > 70) barClass = 'progress-yellow';
    const peak = (peakPct === null || peakPct === undefined)
        ? ''
        : '<div class="bar-peak" id="' + id + '-peak" title="Peak today: ' + Math.round(peakPct) + '% — ' + HELP.peak.replace(/"/g, '') + '" style="left:' + Math.max(0, Math.min(100, peakPct)) + '%"></div>';
    return '<div class="progress-bar"><div id="' + id + '" class="progress-fill ' + barClass + '" data-percent="' + percent + '" style="width:' + percent + '%"></div>' + peak + '</div>';
}

function updateBarPeak(id, peakPct) {
    const el = document.getElementById(id + '-peak');
    if (!el) return;
    if (peakPct === null || peakPct === undefined) { el.style.display = 'none'; return; }
    const v = Math.max(0, Math.min(100, peakPct));
    if (el.dataset.lastVal === String(v)) return;
    el.dataset.lastVal = String(v);
    el.style.display = '';
    el.style.left = v + '%';
    el.title = 'Peak today: ' + Math.round(peakPct) + '%';
}

// COMPACT (2026-07-29): one line, not a titled block.
function loadedModelsHtml(lm) {
    if (!lm || !lm.available) {
        return '<span class="dim">model status unavailable</span>';
    }
    if (!lm.models || !lm.models.length) {
        return '<span class="dim">no model loaded</span>';
    }
    return lm.models.map(model =>
        '<b>● ' + model.name + '</b> <span class="tps">' +
        (model.vram_gb === null || model.vram_gb === undefined ? 'measuring…' : model.vram_gb.toFixed(1) + ' GB') +
        '</span>' + (model.where ? ' <span class="dim">on ' + model.where + '</span>' : '')
    ).join('<br>');
}

// Label + thin bar + value, all on ONE line (compact card layout).
function miniRow(label, percent, cls, barId, labelId, valueText, peakPct) {
    const help = HELP[label.toLowerCase()] || '';
    return '<div class="mini-row" title="' + help.replace(/"/g, '') + '"><span class="mini-label">' + label + '</span>' +
        progressBar(percent, cls, barId, peakPct) +
        '<span class="mini-val" id="' + labelId + '">' + valueText + '</span></div>';
}

// Update existing progress bars smoothly
function updateProgressBar(id, percent, baseClass) {
    const el = document.getElementById(id);
    if (el) {
        let barClass = baseClass;
        if (percent > 90) barClass = 'progress-red';
        else if (percent > 70) barClass = 'progress-yellow';
        const key = barClass + ':' + percent;
        if (el.dataset.lastVal === key) return;  // unchanged - skip the write
        el.dataset.lastVal = key;
        el.className = 'progress-fill ' + barClass;
        el.style.width = percent + '%';
    }
}

// Track if initial render is done
let initialized = false;
let lastData = null;

// Error tracking for auto-reload
let consecutiveErrors = 0;
let reloadCountdown = null;

// Smoothly update a gauge arc
function updateGaugeArc(id, percent, color) {
    const arc = document.querySelector('#' + id + ' .gauge-arc');
    const circle = document.querySelector('#' + id + ' circle');
    const valueEl = document.querySelector('#' + id + ' .gauge-value');
    if (arc) {
        const r = parseFloat(arc.getAttribute('r') || 45);
        const arcLength = Math.PI * r;
        const dashOffset = arcLength * (1 - percent / 100);
        arc.style.strokeDashoffset = dashOffset;
        arc.style.stroke = color;
    }
    if (circle) {
        circle.style.stroke = color;
    }
    if (valueEl) {
        valueEl.style.color = color;
        valueEl.style.textShadow = '0 0 10px ' + color;
    }
}

// Get color for normal gauges (low=good)
function getNormalColor(percent) {
    if (percent > 90) return '#ff0044';
    if (percent > 70) return '#ffff00';
    return '#39ff14';
}

// Get color for utilization (low=bad)
function getUtilColor(percent) {
    if (percent < 5) return '#ff0044';
    if (percent < 25) return '#ffff00';
    return '#39ff14';
}

// Get color for swap (0=good)
function getSwapColor(percent) {
    if (percent === 0) return '#39ff14';
    if (percent < 70) return '#ffff00';
    return '#ff0044';
}

// Plain-English explanations for every reading on the page (Ben, 2026-07-30).
// Static text, no live values - the tooltip explains WHAT the number is, the
// number itself says how much.
const HELP = {
    gpu:   'GPU USE - how hard the graphics card is working right now, 0 to 100%. A model answering a question pushes this up. The bright mark on the arc is the highest it reached today.',
    power: 'POWER DRAW - watts the graphics card is pulling right now. The small grey number at the bottom of the card is its ceiling. The mark on the arc is today high point.',
    temp:  'TEMPERATURE - how hot the chip is, in Celsius. Green is fine, yellow is warm, red means it is throttling itself to avoid damage. Around 85C is where to start worrying.',
    cpu:   'PROCESSOR USE - how busy the regular processor is, separate from the graphics card. High here with low GPU usually means the box is compiling or running tests, not serving a model.',
    swap:  'SWAP - memory that has spilled from RAM onto the disk. Zero is ideal. Anything high means the box ran out of real memory and is now much slower.',
    vram:  'GRAPHICS MEMORY - the graphics card own memory, which is what a model has to fit inside. A model bigger than this either will not run or spills onto the processor and crawls.',
    ram:   'SYSTEM MEMORY - ordinary RAM. Separate from graphics memory. Running out shows up as swap.',
    mem:   'MEMORY - on a Mac the processor and graphics share one pool, so this single number covers both.',
    disk:  'DISK - how full the drive is. No high-water mark here on purpose: disk usage only climbs, so today maximum is just the current number.',
    io:    'DISK and NETWORK traffic in megabytes per second, read/write and in/out. Useful for spotting a box that is busy moving data rather than thinking.',
    tps:   'TOKENS PER SECOND - the speed the model is writing. A token is roughly three quarters of a word. NOW is this second, PEAK TODAY is the fastest it managed today, AVG is the average across everything it served today.',
    reqs:  'REQUESTS - how many times something asked this box for an answer, in the last hour, last 24 hours and last 7 days.',
    served:'SERVING TIME - total minutes this box spent actually generating today. A big request count with few minutes means lots of short answers.',
    model: 'LOADED MODEL - which AI model is sitting in memory right now, and how much memory it is using. Empty means nothing is loaded and the next request will wait for it to load.',
    peak:  'The high-water mark for today. Like the peak needle on a stereo amplifier: it stays where the loudest moment was, even after the level drops back down. Resets at midnight.',
    routepill: 'A ROUTE is a nickname you ask for instead of naming a model, so the gateway can pick the machine. This badge sits on the machine that serves it. GREEN means the model is loaded in memory and will answer immediately. GREY means the route works but nothing is loaded, so the first request waits while it loads. RED means the route is missing from the gateway entirely - that one is a problem.',
    routesum:  'LOADED ROUTES - how many of your local model routes have their model actually sitting in memory right now, out of the total number of local routes. Grey dots are idle, not broken: models unload after sitting unused, and the next request loads them again. Only red is a real fault.',
    agents:    'CLI AGENTS - how many AI coding agents are running across the fleet right now.',
    runners:   'RUNNERS - GitHub Actions workers available to run your tests and builds, shown as busy out of total. When busy equals total, new work waits in line.',
    issues:    'ISSUES OPEN - open tickets on the armbrain repository.',
    prs:       'PULL REQUESTS OPEN - finished work waiting to be reviewed and merged.',
    ciqr:      'CI QUEUED / RUNNING - automated test runs waiting to start, and runs happening now. A growing queued number means you are short on runners.',
    merged:    'MERGED TODAY - pull requests that landed in the main branch today. The little bars are the last seven days, so you can see whether today is normal.',
    deployed:  'DEPLOYED TODAY - releases that went live today. The little bars are the last seven days.',
    lastdep:   'LAST DEPLOY - when the most recent release went out, in Central Time, and the short code identifying exactly which version it was.',
    maxutil:   'The busiest this graphics card got today, as a percentage.',
};

function peakMarkerHtml(peakValue, min, max, size, id) {
    // A thin tick parked at today's high-water mark. Only the outer ~28% of the
    // bar is painted, so it reads as a mark ON the arc, not a second needle.
    const s = size || 1;
    const h = Math.round(52 * s);   // past the arc's outer rim, per Ben
    const w = Math.max(3, Math.round(3.5 * s));
    const has = peakValue !== null && peakValue !== undefined;
    const pct = has ? Math.max(0, Math.min(100, ((peakValue - min) / (max - min)) * 100)) : 0;
    const angle = -90 + (pct * 1.8);
    return '<div class="gauge-peak" id="' + id + '" title="' + HELP.peak.replace(/"/g, '') + '"' +
        ' style="width:' + w + 'px;height:' + h + 'px;margin-left:' + (-w / 2) + 'px;' +
        'transform:rotate(' + angle + 'deg);opacity:' + (has ? 0.95 : 0) + ';' +
        'background:linear-gradient(to top,transparent 0%,transparent 55%,#ffd166 55%,#fff8e1 100%)"></div>';
}

function updatePeakMarker(id, peakValue, min, max) {
    const el = document.getElementById(id);
    if (!el) return;
    const has = peakValue !== null && peakValue !== undefined;
    const pct = has ? Math.max(0, Math.min(100, ((peakValue - min) / (max - min)) * 100)) : 0;
    const angle = -90 + (pct * 1.8);
    const key = has ? String(angle) : 'none';
    if (el.dataset.lastVal === key) return;
    el.dataset.lastVal = key;
    el.style.opacity = has ? 0.95 : 0;
    el.style.transform = 'rotate(' + angle + 'deg)';
}

function renderGauge(value, min, max, label, unit, showLimit, size, reverseColors, id, peakValue) {
    const s = size || 1;
    const w = Math.round(100 * s);
    const h = Math.round(50 * s);
    const needleH = Math.round(42 * s);
    const needleW = Math.round(3 * s);
    const centerSize = Math.round(14 * s);
    const gaugeId = id || 'gauge-' + label.replace(/\\s/g,'');

    // Calculate angle: -90 (left) to +90 (right)
    const clampedValue = value !== null ? Math.max(min, Math.min(max, value)) : min;
    const percent = value !== null ? ((clampedValue - min) / (max - min)) * 100 : 0;
    const angle = -90 + (percent * 1.8);

    // Determine color based on percent
    let color;
    if (reverseColors) {
        color = getUtilColor(percent);
    } else {
        color = getNormalColor(percent);
    }

    const displayValue = value !== null ? (showLimit ? Math.round(value) + '/' + max + unit : Math.round(value) + unit) : '--';

    // Conic gradient: green 0-70%, yellow 70-90%, red 90-100% (or reversed)
    let gradient;
    if (reverseColors) {
        gradient = 'conic-gradient(from 0.75turn, #d94a4a 0deg, #d94a4a 9deg, #d9a54a 9deg, #d9a54a 45deg, #39ff14 45deg, #39ff14 180deg, transparent 180deg)';
    } else {
        gradient = 'conic-gradient(from 0.75turn, #39ff14 0deg, #39ff14 126deg, #d9a54a 126deg, #d9a54a 162deg, #d94a4a 162deg, #d94a4a 180deg, transparent 180deg)';
    }

    const help = HELP[label.toLowerCase().replace(/[^a-z]/g, '')] || '';
    return '<div class="gauge" id="' + gaugeId + '" style="width:' + w + 'px" data-min="' + min + '" data-max="' + max + '" title="' + help.replace(/"/g, '') + '">' +
        '<div class="gauge-dial" style="width:' + w + 'px;height:' + h + 'px">' +
        '<div class="gauge-bg" style="background:' + gradient + '"></div>' +
        '<div class="gauge-mask"></div>' +
        '<div class="gauge-needle" style="width:' + needleW + 'px;height:' + needleH + 'px;margin-left:' + (-needleW/2) + 'px;transform:rotate(' + angle + 'deg);background:linear-gradient(to top,#fff 0%,#fff 60%,' + color + ' 100%)"></div>' +
        (peakValue !== undefined ? peakMarkerHtml(peakValue, min, max, s, gaugeId + '-peak') : '') +
        '<div class="gauge-center" style="width:' + centerSize + 'px;height:' + centerSize + 'px;margin-left:' + (-centerSize/2) + 'px;border-color:' + color + '"></div>' +
        '</div>' +
        '<div class="gauge-label">' + label + '</div>' +
        '<div class="gauge-value" style="color:' + color + ';text-shadow:0 0 10px ' + color + '">' + displayValue + '</div></div>';
}

function renderSwapGauge(value, max, size, id, peakValue) {
    // Swap uses different color logic
    let color;
    if (value === 0) { color = '#39ff14'; }
    else if (value < 70) { color = '#d9a54a'; }
    else { color = '#d94a4a'; }

    const s = size || 1;
    const w = Math.round(100 * s);
    const h = Math.round(50 * s);
    const needleH = Math.round(42 * s);
    const needleW = Math.round(3 * s);
    const centerSize = Math.round(14 * s);
    const gaugeId = id || 'gauge-swap';

    const percent = (value / max) * 100;
    const angle = -90 + (percent * 1.8);
    const displayValue = Math.round(value) + '%';

    // Swap gradient: green at 0, yellow 0-70%, red >70%
    const gradient = 'conic-gradient(from 0.75turn, #39ff14 0deg, #39ff14 5deg, #d9a54a 5deg, #d9a54a 126deg, #d94a4a 126deg, #d94a4a 180deg, transparent 180deg)';

    return '<div class="gauge" id="' + gaugeId + '" style="width:' + w + 'px" data-min="0" data-max="' + max + '" title="' + HELP.swap + '">' +
        '<div class="gauge-dial" style="width:' + w + 'px;height:' + h + 'px">' +
        '<div class="gauge-bg" style="background:' + gradient + '"></div>' +
        '<div class="gauge-mask"></div>' +
        '<div class="gauge-needle" style="width:' + needleW + 'px;height:' + needleH + 'px;margin-left:' + (-needleW/2) + 'px;transform:rotate(' + angle + 'deg);background:linear-gradient(to top,#fff 0%,#fff 60%,' + color + ' 100%)"></div>' +
        (peakValue !== undefined ? peakMarkerHtml(peakValue, 0, max, s, gaugeId + '-peak') : '') +
        '<div class="gauge-center" style="width:' + centerSize + 'px;height:' + centerSize + 'px;margin-left:' + (-centerSize/2) + 'px;border-color:' + color + '"></div>' +
        '</div>' +
        '<div class="gauge-label">SWAP</div>' +
        '<div class="gauge-value" style="color:' + color + ';text-shadow:0 0 10px ' + color + '">' + displayValue + '</div></div>';
}

// Event delegation for all action buttons
document.addEventListener('click', function(e) {
    const btn = e.target.closest('button');
    if (!btn) return;

    if (btn.classList.contains('power-btn')) {
        const target = btn.dataset.target;
        const current = btn.dataset.current;
        const max = parseInt(btn.dataset.max);
        const watts = prompt('Set GPU power limit for ' + target + ' (100-' + max + 'W):', current);
        if (watts === null) return;
        const w = parseInt(watts);
        if (isNaN(w) || w < 100 || w > max) {
            alert('Invalid value. Must be between 100 and ' + max);
            return;
        }
        fetch('/api/power_limit', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({target: target, watts: w})
        }).then(r => r.json()).then(data => {
            if (data.success) { refresh(); }
            else { alert('Failed: ' + (data.error || 'Unknown error')); }
        }).catch(err => alert('Error: ' + err));
    }

    if (btn.classList.contains('swap-btn')) {
        const target = btn.dataset.target;
        if (!confirm('Clear swap on ' + target + '? This forces swapped data back into RAM.')) return;
        btn.disabled = true;
        btn.textContent = 'Clearing...';
        fetch('/api/clear_swap', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({target: target})
        }).then(r => r.json()).then(data => {
            if (data.success) {
                btn.textContent = 'Cleared!';
                setTimeout(() => { btn.textContent = 'Clear Swap'; btn.disabled = false; }, 2000);
            } else {
                alert('Failed: ' + (data.error || 'Unknown error'));
                btn.textContent = 'Clear Swap'; btn.disabled = false;
            }
        }).catch(err => {
            alert('Error: ' + err);
            btn.textContent = 'Clear Swap'; btn.disabled = false;
        });
    }

    if (btn.classList.contains('reboot-btn')) {
        const target = btn.dataset.target;
        btn.disabled = true;
        btn.textContent = '...';
        fetch('/api/power', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({target: target, action: 'reboot'})
        }).then(r => r.json()).then(data => {
            if (data.success) {
                btn.textContent = 'Sent';
                setTimeout(() => { btn.innerHTML = '&#x21bb;'; btn.disabled = false; }, 3000);
            } else {
                alert('Reboot failed: ' + (data.error || 'Unknown error'));
                btn.innerHTML = '&#x21bb;'; btn.disabled = false;
            }
        }).catch(err => {
            alert('Error: ' + err);
            btn.innerHTML = '&#x21bb;'; btn.disabled = false;
        });
    }

    if (btn.classList.contains('shutdown-btn')) {
        const target = btn.dataset.target;
        if (!confirm('Are you sure you want to shut down ' + target + '?')) return;
        btn.disabled = true;
        btn.textContent = '...';
        fetch('/api/power', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({target: target, action: 'shutdown'})
        }).then(r => r.json()).then(data => {
            if (data.success) {
                btn.textContent = 'Sent';
            } else {
                alert('Shutdown failed: ' + (data.error || 'Unknown error'));
                btn.innerHTML = '&#x23FB;'; btn.disabled = false;
            }
        }).catch(err => {
            alert('Error: ' + err);
            btn.innerHTML = '&#x23FB;'; btn.disabled = false;
        });
    }
});

function formatJobTime(isoString) {
    if (!isoString) return "";
    const date = new Date(isoString);
    const now = new Date();
    const isToday = date.toDateString() === now.toDateString();
    const yesterday = new Date(now);
    yesterday.setDate(yesterday.getDate() - 1);
    const isYesterday = date.toDateString() === yesterday.toDateString();
    const time = date.toLocaleTimeString('en-US', {hour: '2-digit', minute: '2-digit', hour12: false});
    if (isToday) return "Today " + time;
    if (isYesterday) return "Yesterday " + time;
    return date.toLocaleDateString('en-US', {month: 'short', day: 'numeric'}) + " " + time;
}


function updateGauge(id, value, min, max, unit, showLimit, colorFn) {
    const gauge = document.getElementById(id);
    if (!gauge) return;

    const clampedValue = Math.max(min, Math.min(max, value));
    const percent = ((clampedValue - min) / (max - min)) * 100;
    const angle = -90 + (percent * 1.8);
    const color = colorFn(percent);
    const displayValue = showLimit ? Math.round(value) + '/' + max + unit : Math.round(value) + unit;

    const key = Math.round(angle * 10) + ':' + color + ':' + displayValue;
    if (gauge.dataset.lastVal === key) return;  // unchanged - skip the write
    gauge.dataset.lastVal = key;

    const needle = gauge.querySelector('.gauge-needle');
    const center = gauge.querySelector('.gauge-center');
    const valueEl = gauge.querySelector('.gauge-value');

    if (needle) {
        needle.style.transform = 'rotate(' + angle + 'deg)';
        needle.style.background = 'linear-gradient(to top, #fff 0%, #fff 60%, ' + color + ' 100%)';
    }
    if (center) {
        center.style.borderColor = color;
    }
    if (valueEl) {
        valueEl.textContent = displayValue;
        valueEl.style.color = color;
        valueEl.style.textShadow = '0 0 10px ' + color;
    }
}

function refresh() {
    fetch("/api/status").then(r => r.json()).then(data => {
        const targets = data.targets || {};
        for (const [name, info] of Object.entries(targets)) { lastTargetsOnline[name] = info.online; }
        updateFleetSummary();
        // If not initialized, build the full HTML
        if (!initialized) {
            let html = '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:20px">';

            for (const [name, info] of Object.entries(targets)) {
                const icon = icons[name] || "🖥️";
                // MISMATCH outranks ONLINE/OFFLINE: if the address answered as a
                // different machine we know nothing trustworthy about this box, and
                // saying so beats saying "offline" (which would read as "asleep").
                const status = info.mismatch
                    ? '<span class="mismatch" title="This address answered as ' + (info.reported_host || 'another machine') + ' - check DNS/DHCP">MISMATCH (' + (info.reported_host || '?') + ')</span>'
                    : (info.online ? '<span class="online">ONLINE</span>' : '<span class="offline">OFFLINE</span>');

                const isMac = info.os === 'mac';

                const isModelBox = ['gandalf', 'frodo', 'pippin', 'aragorn'].includes(name);
                html += '<div class="gpu-card compact' + (isModelBox ? ' has-tps' : '') + '" id="card-' + name + '">';
                // Two-band tokens/sec strip in place of the flat accent line.
                if (isModelBox) {
                    html += '<div class="tps-strip" id="tps-strip-' + name + '">'
                         +  '<div class="tps-band now" title="Tokens/sec being served right now"><div class="tps-fill" id="tps-fill-now-' + name + '"></div></div>'
                         +  '<div class="tps-band peak" title="Peak tokens/sec reached today"><div class="tps-fill" id="tps-fill-peak-' + name + '"></div></div>'
                         +  '</div>';
                }
                html += '<div class="gpu-header"><span class="gpu-name">' + icon + ' ' + name + '</span>';
                html += '<div class="hdr-right"><span id="status-' + name + '">' + status + '</span>';
                if (!isMac) {
                    html += '<div class="machine-power">';
                    html += '<button class="reboot-btn" data-target="' + name + '" title="Reboot">&#x21bb;</button>';
                    html += '<button class="shutdown-btn" data-target="' + name + '" title="Shut Down">&#x23FB;</button>';
                    html += '</div>';
                }
                html += '</div></div>';

                const maxUtil = (info.max_util_today === null || info.max_util_today === undefined) ? '--' : info.max_util_today;
                // The old "99% max utilization today" text line is gone - that
                // number is now the peak-hold tick on the GPU dial itself.

                html += '<div class="card-routes" id="routes-' + name + '"></div>';

                if (info.online && isMac) {
                    // COMPACT: one dial strip, then inline micro-bars.
                    const pk = info.peaks_today || {};
                    html += '<div class="dial-strip">';
                    html += renderGauge(info.gpu_util, 0, 100, "GPU", "%", false, 0.7, true, 'util-' + name, info.max_util_today);
                    html += renderGauge(info.cpu_percent, 0, 100, "CPU", "%", false, 0.7, false, 'cpu-' + name, pk.cpu_percent);
                    const swapPct = info.swap ? info.swap.percent : 0;
                    html += renderSwapGauge(swapPct, 100, 0.7, 'swap-' + name, pk.swap_percent);
                    html += '</div>';

                    if (info.ram) {
                        html += miniRow('MEM', info.ram.percent, 'progress-ram', 'ram-' + name,
                                        'ram-label-' + name, info.ram.used_gb + ' / ' + info.ram.total_gb + ' GB', pk.ram_percent);
                    }

                    if (info.disk) {
                        const diskUsed = info.disk.total_gb >= 1000 ? (info.disk.used_gb / 1024).toFixed(1) + ' TB' : info.disk.used_gb + ' GB';
                        const diskTotal = info.disk.total_gb >= 1000 ? (info.disk.total_gb / 1024).toFixed(1) + ' TB' : info.disk.total_gb + ' GB';
                        html += miniRow('DISK', info.disk.percent, 'progress-disk', 'disk-' + name,
                                        'disk-label-' + name, diskUsed + ' / ' + diskTotal);
                    }

                    if (isModelBox) {
                        html += '<div class="serving-line">';
                        html += '<div id="loaded-models-' + name + '" title="' + HELP.model.replace(/"/g, '') + '">' + loadedModelsHtml(info.loaded_models) + '</div>';
                        html += '<div id="serving-' + name + '" title="' + HELP.tps.replace(/"/g, '') + ' ' + HELP.reqs.replace(/"/g, '') + '"></div>';
                        html += '</div>';
                    }
                    html += '<div class="card-foot"><span>Apple M1 Max · 64GB Unified · 32-core GPU</span></div>';
                } else if (info.online && info.gpu) {
                    // COMPACT: all five vitals in ONE dial row.
                    const pk = info.peaks_today || {};
                    html += '<div class="dial-strip">';
                    html += renderGauge(info.gpu_util, 0, 100, "GPU", "%", false, 0.7, true, 'util-' + name, info.max_util_today);
                    html += renderGauge(info.gpu_watts, 0, info.gpu_power_max, "POWER", "W", false, 0.7, false, 'power-' + name, pk.gpu_watts);
                    html += renderGauge(info.gpu_temp, 24, 90, "TEMP", "°C", false, 0.7, false, 'temp-' + name, pk.gpu_temp);
                    html += renderGauge(info.cpu_percent, 0, 100, "CPU", "%", false, 0.7, false, 'cpu-' + name, pk.cpu_percent);
                    const swapPct = info.swap ? info.swap.percent : 0;
                    html += renderSwapGauge(swapPct, 100, 0.7, 'swap-' + name, pk.swap_percent);
                    html += '</div>';

                    html += miniRow('VRAM', info.gpu.vram_percent, 'progress-vram', 'vram-' + name,
                                    'vram-label-' + name, info.gpu.vram_used_gb + ' / ' + info.gpu.vram_total_gb + ' GB', pk.vram_percent);

                    if (info.ram) {
                        html += miniRow('RAM', info.ram.percent, 'progress-ram', 'ram-' + name,
                                        'ram-label-' + name, info.ram.used_gb + ' / ' + info.ram.total_gb + ' GB', pk.ram_percent);
                    }

                    if (info.disk) {
                        const diskUsed = info.disk.total_gb >= 1000 ? (info.disk.used_gb / 1024).toFixed(1) + ' TB' : info.disk.used_gb + ' GB';
                        const diskTotal = info.disk.total_gb >= 1000 ? (info.disk.total_gb / 1024).toFixed(1) + ' TB' : info.disk.total_gb + ' GB';
                        html += miniRow('DISK', info.disk.percent, 'progress-disk', 'disk-' + name,
                                        'disk-label-' + name, diskUsed + ' / ' + diskTotal);
                    }

                    // Loaded model + tokens/sec, one line each (was 2 gauges + a block)
                    if (isModelBox || info.loaded_models) {
                        html += '<div class="serving-line">';
                        html += '<div id="loaded-models-' + name + '" title="' + HELP.model.replace(/"/g, '') + '">' + loadedModelsHtml(info.loaded_models) + '</div>';
                        if (isModelBox) html += '<div id="serving-' + name + '" title="' + HELP.tps.replace(/"/g, '') + ' ' + HELP.reqs.replace(/"/g, '') + '"></div>';
                        html += '</div>';
                    }

                    html += '<div class="card-foot">';
                    let ioTxt = '';
                    if (info.disk_io) {
                        ioTxt += '<span class="io-stat" id="disk-io-' + name + '">📀 <span class="value val">' + info.disk_io.read_mbps + '/' + info.disk_io.write_mbps + '</span> MB/s</span>';
                    }
                    if (info.net_io) {
                        ioTxt += '<span class="io-stat" id="net-io-' + name + '">🌐 <span class="value val">' + info.net_io.rx_mbps + '/' + info.net_io.tx_mbps + '</span> MB/s</span>';
                    }
                    html += '<span class="io-line" title="' + HELP.io.replace(/"/g, '') + '">' + ioTxt + '</span>';
                    html += '<span class="card-actions">';
                    html += '<button class="power-btn" data-target="' + name + '" data-current="' + info.gpu_power_limit + '" data-max="' + info.gpu_power_max + '" title="Set Power Limit">⚡ PWR</button>';
                    html += '<button class="swap-btn" data-target="' + name + '" title="Clear Swap">🧹 SWAP</button>';
                    html += '</span></div>';
                    html += '<div style="margin-top:5px;font-size:0.7em;color:#556">' + info.gpu.name + (info.gpu.cuda_version ? ' (CUDA ' + info.gpu.cuda_version + ')' : '') + ' · ' + info.gpu_power_max + 'W' + (info.gpu_count > 1 ? ' · ' + info.gpu_count + ' GPUs pooled' : '') + '</div>';
                } else if (info.online) {
                    // Online box with no readable GPU (e.g. aragorn before its
                    // nvidia driver is installed). Same compact shape, CPU vitals.
                    const pk = info.peaks_today || {};
                    html += '<div class="dial-strip">';
                    html += renderGauge(info.cpu_percent, 0, 100, "CPU", "%", false, 0.7, false, 'cpu-' + name, pk.cpu_percent);
                    html += renderGauge(info.cpu_temp, 24, 90, "TEMP", "°C", false, 0.7, false, 'temp-' + name, pk.gpu_temp);
                    const swapPct = info.swap ? info.swap.percent : 0;
                    html += renderSwapGauge(swapPct, 100, 0.7, 'swap-' + name, pk.swap_percent);
                    html += '</div>';

                    if (info.ram) {
                        html += miniRow('RAM', info.ram.percent, 'progress-ram', 'ram-' + name,
                                        'ram-label-' + name, info.ram.used_gb + ' / ' + info.ram.total_gb + ' GB', pk.ram_percent);
                    }
                    if (info.disk) {
                        const diskUsed = info.disk.total_gb >= 1000 ? (info.disk.used_gb / 1024).toFixed(1) + ' TB' : info.disk.used_gb + ' GB';
                        const diskTotal = info.disk.total_gb >= 1000 ? (info.disk.total_gb / 1024).toFixed(1) + ' TB' : info.disk.total_gb + ' GB';
                        html += miniRow('DISK', info.disk.percent, 'progress-disk', 'disk-' + name,
                                        'disk-label-' + name, diskUsed + ' / ' + diskTotal);
                    }
                    html += '<div class="serving-line"><span class="dim">no GPU driver installed - GPU dials appear once nvidia-smi is available</span></div>';
                    html += '<div class="card-foot">';
                    let ioTxt2 = '';
                    if (info.disk_io) {
                        ioTxt2 += '<span class="io-stat" id="disk-io-' + name + '">📀 <span class="value val">' + info.disk_io.read_mbps + '/' + info.disk_io.write_mbps + '</span> MB/s</span>';
                    }
                    if (info.net_io) {
                        ioTxt2 += '<span class="io-stat" id="net-io-' + name + '">🌐 <span class="value val">' + info.net_io.rx_mbps + '/' + info.net_io.tx_mbps + '</span> MB/s</span>';
                    }
                    html += '<span class="io-line" title="' + HELP.io.replace(/"/g, '') + '">' + ioTxt2 + '</span>';
                    html += '<span class="card-actions">';
                    html += '<button class="swap-btn" data-target="' + name + '" title="Clear Swap">🧹 SWAP</button>';
                    html += '</span></div>';
                }

                // ComfyUI link removed 2026-07-28 (Ben): ComfyUI is disabled fleet-wide,
                // so the link only ever led to a dead port.
                html += '</div>';
            }
            html += '</div>';
            document.getElementById("monitors").innerHTML = html;
            initialized = true;

        } else {
            // UPDATE PATH: Just update values in place
            for (const [name, info] of Object.entries(targets)) {
                // Update max utilization today
                updatePeakMarker('util-' + name + '-peak', info.max_util_today, 0, 100);

                if (info.online && info.os === 'mac') {
                    const lmEl = document.getElementById('loaded-models-' + name);
                    if (lmEl) {
                        const lmHtml = loadedModelsHtml(info.loaded_models);
                        if (lmEl.dataset.lastVal !== lmHtml) { lmEl.dataset.lastVal = lmHtml; lmEl.innerHTML = lmHtml; }
                    }
                    updateGauge('util-' + name, info.gpu_util, 0, 100, '%', false, getUtilColor);
                    updateGauge('cpu-' + name, info.cpu_percent, 0, 100, '%', false, getNormalColor);
                    const swapPct = info.swap ? info.swap.percent : 0;
                    updateGauge('swap-' + name, swapPct, 0, 100, '%', false, getSwapColor);
                    const pk = info.peaks_today || {};
                    updatePeakMarker('cpu-' + name + '-peak', pk.cpu_percent, 0, 100);
                    updatePeakMarker('swap-' + name + '-peak', pk.swap_percent, 0, 100);
                    updateBarPeak('ram-' + name, pk.ram_percent);
                    updateBarPeak('disk-' + name, null);

                    if (info.ram) {
                        updateProgressBar('ram-' + name, info.ram.percent, 'progress-ram');
                        const ramLabel = document.getElementById('ram-label-' + name);
                        if (ramLabel) ramLabel.textContent = info.ram.used_gb + ' / ' + info.ram.total_gb + ' GB';
                    }
                    if (info.disk) {
                        updateProgressBar('disk-' + name, info.disk.percent, 'progress-disk');
                        const diskLabel = document.getElementById('disk-label-' + name);
                        if (diskLabel) {
                            const diskUsed = info.disk.total_gb >= 1000 ? (info.disk.used_gb / 1024).toFixed(1) + ' TB' : info.disk.used_gb + ' GB';
                            const diskTotal = info.disk.total_gb >= 1000 ? (info.disk.total_gb / 1024).toFixed(1) + ' TB' : info.disk.total_gb + ' GB';
                            diskLabel.textContent = diskUsed + ' / ' + diskTotal;
                        }
                    }
                } else if (info.online && info.gpu) {
                    // Update gauges
                    updateGauge('util-' + name, info.gpu_util, 0, 100, '%', false, getUtilColor);
                    updateGauge('power-' + name, info.gpu_watts, 0, info.gpu_power_max, 'W', false, getNormalColor);
                    updateGauge('temp-' + name, info.gpu_temp, 24, 90, '°C', false, getNormalColor);
                    updateGauge('cpu-' + name, info.cpu_percent, 0, 100, '%', false, getNormalColor);
                    const swapPct = info.swap ? info.swap.percent : 0;
                    updateGauge('swap-' + name, swapPct, 0, 100, '%', false, getSwapColor);
                    // Peak-hold marks: today's high-water mark on every dial and bar.
                    const pk = info.peaks_today || {};
                    updatePeakMarker('power-' + name + '-peak', pk.gpu_watts, 0, info.gpu_power_max);
                    updatePeakMarker('temp-' + name + '-peak', pk.gpu_temp, 24, 90);
                    updatePeakMarker('cpu-' + name + '-peak', pk.cpu_percent, 0, 100);
                    updatePeakMarker('swap-' + name + '-peak', pk.swap_percent, 0, 100);
                    updateBarPeak('vram-' + name, pk.vram_percent);
                    updateBarPeak('ram-' + name, pk.ram_percent);

                    // Update progress bars
                    updateProgressBar('vram-' + name, info.gpu.vram_percent, 'progress-vram');
                    const vramLabel = document.getElementById('vram-label-' + name);
                    if (vramLabel) vramLabel.textContent = info.gpu.vram_used_gb + ' / ' + info.gpu.vram_total_gb + ' GB';
                    const loadedModelsEl = document.getElementById('loaded-models-' + name);
                    if (loadedModelsEl) {
                        const loadedHtml = loadedModelsHtml(info.loaded_models);
                        if (loadedModelsEl.dataset.lastVal !== loadedHtml) {
                            loadedModelsEl.dataset.lastVal = loadedHtml;
                            loadedModelsEl.innerHTML = loadedHtml;
                        }
                    }

                    if (info.ram) {
                        updateProgressBar('ram-' + name, info.ram.percent, 'progress-ram');
                        const ramLabel = document.getElementById('ram-label-' + name);
                        if (ramLabel) ramLabel.textContent = info.ram.used_gb + ' / ' + info.ram.total_gb + ' GB';
                    }

                    if (info.disk) {
                        updateProgressBar('disk-' + name, info.disk.percent, 'progress-disk');
                        const diskLabel = document.getElementById('disk-label-' + name);
                        if (diskLabel) {
                            const diskUsed = info.disk.total_gb >= 1000 ? (info.disk.used_gb / 1024).toFixed(1) + ' TB' : info.disk.used_gb + ' GB';
                            const diskTotal = info.disk.total_gb >= 1000 ? (info.disk.total_gb / 1024).toFixed(1) + ' TB' : info.disk.total_gb + ' GB';
                            diskLabel.textContent = diskUsed + ' / ' + diskTotal;
                        }
                    }

                    // Update I/O stats
                    if (info.disk_io) {
                        const diskIoEl = document.getElementById('disk-io-' + name);
                        if (diskIoEl) {
                            diskIoEl.querySelector('.value').textContent = info.disk_io.read_mbps + '/' + info.disk_io.write_mbps;
                        }
                    }
                    if (info.net_io) {
                        const netIoEl = document.getElementById('net-io-' + name);
                        if (netIoEl) {
                            netIoEl.querySelector('.value').textContent = info.net_io.rx_mbps + '/' + info.net_io.tx_mbps;
                        }
                    }
                }
            }
        }
        // Success - reset error tracking
        consecutiveErrors = 0;
        if (reloadCountdown) {
            clearInterval(reloadCountdown);
            reloadCountdown = null;
        }
    }).catch(err => {
        console.error('Error fetching status:', err);
        consecutiveErrors++;

        // Show error with auto-reload countdown
        let secondsLeft = 5;
        const updateMessage = () => {
            document.getElementById("monitors").innerHTML =
                "<p style='color:#f44'>Error loading status: " + err.message + "</p>" +
                "<p style='color:#888'>Retrying in <span id='countdown'>" + secondsLeft + "</span> seconds...</p>";
        };
        updateMessage();

        // Clear any existing countdown
        if (reloadCountdown) clearInterval(reloadCountdown);

        // Start countdown
        reloadCountdown = setInterval(() => {
            secondsLeft--;
            const el = document.getElementById('countdown');
            if (el) el.textContent = secondsLeft;
            if (secondsLeft <= 0) {
                clearInterval(reloadCountdown);
                reloadCountdown = null;
                // After 3 consecutive errors, do a full page reload
                if (consecutiveErrors >= 3) {
                    location.reload();
                }
            }
        }, 1000);
    });

}

let currentRange = 'hour';

function setRange(range) {
    currentRange = range;
    document.querySelectorAll('.time-range button').forEach(b => b.classList.remove('active'));
    document.getElementById('btn-' + range).classList.add('active');
    refreshHistory();
}

function renderSparkline(data, key, max, label) {
    if (!data || data.length === 0) return '';

    const values = data.map(d => d[key]).filter(v => v !== null);
    if (values.length === 0) return '';

    const actualMax = max || Math.max(...values, 1);
    const current = values[values.length - 1];
    const avg = values.reduce((a, b) => a + b, 0) / values.length;

    let bars = '';
    const barCount = Math.min(60, data.length);
    const step = Math.max(1, Math.floor(data.length / barCount));

    for (let i = 0; i < data.length; i += step) {
        const v = data[i][key];
        if (v === null) continue;
        const height = Math.max(2, (v / actualMax) * 40);
        const cls = v > actualMax * 0.9 ? 'critical' : v > actualMax * 0.7 ? 'high' : '';  // 90% red, 70% yellow
        bars += '<div class="sparkline-bar ' + cls + '" style="height:' + height + 'px" title="' + v + '"></div>';
    }

    return '<div class="sparkline-box"><div class="sparkline-label">' + label + ' (now: ' + (current !== undefined ? current.toFixed(1) : '--') + ', avg: ' + avg.toFixed(1) + ')</div><div class="sparkline">' + bars + '</div></div>';
}

function refreshHistory() {
    fetch('/api/history?range=' + currentRange).then(r => r.json()).then(data => {
        let html = '';

        let idx = 0;
        for (const [target, points] of Object.entries(data.data || {})) {
            const icon = icons[target] || '🖥️';
            html += '<div class="history-machine alt-' + (idx % 2) + '"><h4 style="margin:0 0 12px 0;color:#ccc">' + icon + ' ' + target.charAt(0).toUpperCase() + target.slice(1) + '</h4>';
            html += '<div class="sparkline-container">';
            html += renderSparkline(points, 'gpu_util', 100, '🎮 GPU Util %');
            html += renderSparkline(points, 'cpu_percent', 100, '💻 CPU %');
            html += renderSparkline(points, 'gpu_temp', 90, '🌡️ Temp °C');
            html += renderSparkline(points, 'vram_percent', 100, '🎮 VRAM %');
            html += renderSparkline(points, 'ram_percent', 100, '🧠 RAM %');
            html += renderSparkline(points, 'swap_percent', 100, '⚠️ Swap %');
            html += renderSparkline(points, 'queue_depth', null, '📋 Queue');
            html += '</div></div>';
            idx++;
        }

        if (!html) {
            html = '<p style="color:#888">No historical data yet. Metrics are collected every minute.</p>';
        }

        document.getElementById('sparklines').innerHTML = html;
    }).catch(err => {
        document.getElementById('sparklines').innerHTML = '<p style="color:#d94a4a">Error loading history: ' + err + '</p>';
    });
}

function fmtCell(ec) {
    if (!ec) return '<span style="color:#555">--</span>';
    return '<b style="color:var(--neon-cyan)">' + ec.kwh.toFixed(2) + '</b> kWh<br><b class="cost">$' + ec.cost.toFixed(2) + '</b>';
}

function refreshEnergy() {
    fetch('/api/energy').then(r => r.json()).then(data => {
        const order = ['gandalf', 'frodo', 'pippin'];
        const byMachine = data.by_machine || {};
        const names = order.filter(n => n in byMachine)
            .concat(Object.keys(byMachine).filter(n => !order.includes(n)));

        // Per-machine table
        let rows = '<table><tr><th>Machine</th><th>Today</th><th>This Week</th><th>This Month</th></tr>';
        names.forEach(name => {
            const m = byMachine[name];
            const icon = icons[name] || '🖥️';
            const label = name.charAt(0).toUpperCase() + name.slice(1);
            if (!m || m.metered === false) {
                rows += '<tr><td>' + icon + ' <b>' + label + '</b></td>'
                     + '<td colspan="3" style="color:#666">no power sensor (Mac - needs sudo powermetrics)</td></tr>';
            } else {
                rows += '<tr><td>' + icon + ' <b>' + label + '</b></td><td>' + fmtCell(m.day)
                     + '</td><td>' + fmtCell(m.week) + '</td><td>' + fmtCell(m.month) + '</td></tr>';
            }
        });
        rows += '</table>';
        document.getElementById('energy-by-machine').innerHTML = rows;

        // Fleet aggregate — fail-soft: a partial/failed fetch (e.g. flaky phone
        // link over Tailscale) must not throw on a missing fleet/window, it just
        // shows 0 for that cell instead of nuking the whole panel.
        const f = data.fleet || {};
        let fleet = '<table><tr><th>Period</th><th>GPU Energy</th><th>Cost</th></tr>';
        [['Today', 'day'], ['This Week', 'week'], ['This Month', 'month']].forEach(([lbl, k]) => {
            const c = f[k] || {kwh: 0, cost: 0};
            fleet += '<tr><td><b>' + lbl + '</b></td><td><b style="color:var(--neon-cyan)">'
                  + (c.kwh || 0).toFixed(2) + '</b> kWh</td><td><b class="cost">$' + (c.cost || 0).toFixed(2) + '</b></td></tr>';
        });
        fleet += '</table>';
        fleet += '<p style="margin-top:10px;color:#666;font-size:0.8em">Base ' + (data.base_per_kwh || 0).toFixed(6)
              + ' $/kWh + PEC time-of-use. ' + (data.note || '') + '</p>';
        document.getElementById('energy-fleet-body').innerHTML = fleet;
    }).catch(err => {
        document.getElementById('energy-by-machine').innerHTML = '<p style="color:#d94a4a">Error loading energy: ' + err + '</p>';
    });
}

// --- Polling control: pause everything when the tab isn't visible, resume ---
// --- with an immediate refresh when it becomes visible again.              ---
let statusTimer = null, fleetTimer = null, historyTimer = null, energyTimer = null, ciQueueTimer = null, routeHealthTimer = null, fleetStatsTimer = null, modelServingTimer = null, shipFlowTimer = null;

function startPolling() {
    if (!statusTimer) statusTimer = setInterval(refresh, 12000);     // queue/GPU state: 12s
    if (!fleetTimer) fleetTimer = setInterval(refreshFleet, 45000);  // ssh fleet stats: 45s
    if (!historyTimer) historyTimer = setInterval(refreshHistory, 60000);
    if (!energyTimer) energyTimer = setInterval(refreshEnergy, 60000);
    if (!ciQueueTimer) ciQueueTimer = setInterval(refreshCiQueue, 45000);        // matches backend cache TTL
    if (!shipFlowTimer) shipFlowTimer = setInterval(refreshShipFlow, 120000);    // /api/pipeline is cached 5 min
    if (!routeHealthTimer) routeHealthTimer = setInterval(refreshRouteHealth, 30000);
    if (!fleetStatsTimer) fleetStatsTimer = setInterval(refreshFleetStats, 60000); // matches backend cache TTL
    if (!modelServingTimer) modelServingTimer = setInterval(refreshModelServing, 60000); // matches sampler cadence
}

function stopPolling() {
    clearInterval(statusTimer); statusTimer = null;
    clearInterval(fleetTimer); fleetTimer = null;
    clearInterval(historyTimer); historyTimer = null;
    clearInterval(energyTimer); energyTimer = null;
    clearInterval(ciQueueTimer); ciQueueTimer = null;
    clearInterval(shipFlowTimer); shipFlowTimer = null;
    clearInterval(routeHealthTimer); routeHealthTimer = null;
    clearInterval(fleetStatsTimer); fleetStatsTimer = null;
    clearInterval(modelServingTimer); modelServingTimer = null;
}

document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
        stopPolling();
    } else {
        // Immediate catch-up refresh, then resume the interval cadence.
        refresh();
        refreshFleet();
        refreshHistory();
        refreshEnergy();
        refreshCiQueue();
        refreshShipFlow();
        refreshRouteHealth();
        refreshFleetStats();
        refreshModelServing();
        startPolling();
    }
});

refresh();
refreshHistory();
refreshEnergy();
refreshFleet();
refreshCiQueue();
refreshShipFlow();
refreshRouteHealth();
refreshFleetStats();
// first serving refresh waits for the target cards (built by refresh()) to exist
setTimeout(refreshModelServing, 5000);
if (!document.hidden) startPolling();
</script></body></html>'''

def rate_for(dt):
    """Total $/kWh (base + time-of-use period charge) for a given datetime."""
    season = ELECTRIC_RATES["seasons"].get(dt.month, "shoulder")
    sched = ELECTRIC_RATES["schedule"][season]
    base = ELECTRIC_RATES["base_per_kwh"]
    hour = dt.hour
    for tier in ("peak", "mid"):
        info = sched.get(tier)
        if info:
            for start, end in info["hours"]:
                if start <= hour < end:
                    return base + info["charge"]
    return base + sched["off_peak"]


def energy_cost_since(conn, target, since):
    """Integrate GPU energy (kWh) and $ cost for a target since a datetime.

    Trapezoidal integration over the sampled gpu_watts series. Gaps larger than
    GAP_CAP seconds (host offline / restart) are clamped so downtime is not
    counted as steady draw. Cost uses the TOU rate at each segment's start time.
    """
    GAP_CAP = 300  # seconds
    cursor = conn.execute(
        "SELECT timestamp, gpu_watts FROM metrics_history "
        "WHERE target = ? AND timestamp >= ? AND gpu_watts IS NOT NULL "
        "ORDER BY timestamp ASC",
        (target, since.isoformat())
    )
    rows = cursor.fetchall()
    kwh = 0.0
    cost = 0.0
    prev_t = None
    prev_w = None
    for ts, w in rows:
        try:
            t = datetime.fromisoformat(ts)
        except (ValueError, TypeError):
            continue
        if prev_t is not None:
            dt_s = min((t - prev_t).total_seconds(), GAP_CAP)
            if dt_s > 0:
                seg_kwh = ((prev_w + w) / 2.0) * (dt_s / 3600.0) / 1000.0
                kwh += seg_kwh
                cost += seg_kwh * rate_for(prev_t)
        prev_t = t
        prev_w = w
    return {"kwh": round(kwh, 3), "cost": round(cost, 2)}


@app.route("/api/energy", methods=["GET"])
def get_energy():
    """GPU energy (kWh) and time-of-use cost per machine and fleet-wide,
    for the current day, week (since Monday), and month (since the 1st)."""
    now = datetime.now()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = day_start - timedelta(days=day_start.weekday())  # Monday 00:00
    month_start = day_start.replace(day=1)
    windows = {"day": day_start, "week": week_start, "month": month_start}

    by_machine = {
        name: (
            {"metered": False}
            if cfg.get("os") == "mac"
            else {
                "metered": True,
                **{w: {"kwh": 0.0, "cost": 0.0} for w in windows},
            }
        )
        for name, cfg in CONFIG["targets"].items()
    }
    fleet = {w: {"kwh": 0.0, "cost": 0.0} for w in windows}
    conn = None
    try:
        conn = sqlite3.connect(CONFIG["db_path"])
        for name, cfg in CONFIG["targets"].items():
            # Macs report no wattage (powermetrics needs sudo), so they aren't metered
            if cfg.get("os") == "mac":
                continue
            for wname, wstart in windows.items():
                ec = energy_cost_since(conn, name, wstart)
                by_machine[name][wname] = ec
                fleet[wname]["kwh"] += ec["kwh"]
                fleet[wname]["cost"] += ec["cost"]
    except Exception as e:
        logger.warning(f"energy history unavailable: {e}")
    finally:
        if conn is not None:
            conn.close()

    for w in fleet:
        fleet[w]["kwh"] = round(fleet[w]["kwh"], 3)
        fleet[w]["cost"] = round(fleet[w]["cost"], 2)

    return jsonify({
        "base_per_kwh": ELECTRIC_RATES["base_per_kwh"],
        "note": "GPU power only (nvidia-smi); excludes CPU/PSU overhead. Macs report no wattage.",
        "by_machine": by_machine,
        "fleet": fleet,
    })


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "queue-router"})


@app.route("/api/history", methods=["GET"])
def get_history():
    """Get historical metrics for charts.

    Query params:
    - range: 'hour' (60 min), 'day' (24 hours), 'week' (7 days), 'month' (30 days)
    - target: optional filter by target name
    """
    range_param = request.args.get("range", "hour")
    target_filter = request.args.get("target")

    # Determine time range and aggregation
    now = datetime.now()
    if range_param == "hour":
        cutoff = now - timedelta(hours=1)
        # Return minute-by-minute data
        group_minutes = 1
    elif range_param == "day":
        cutoff = now - timedelta(days=1)
        # Group by hour (60 minutes)
        group_minutes = 60
    elif range_param == "week":
        cutoff = now - timedelta(weeks=1)
        # Group by 6 hours
        group_minutes = 360
    elif range_param == "month":
        cutoff = now - timedelta(days=30)
        # Group by day
        group_minutes = 1440
    else:
        return jsonify({
            "range": range_param,
            "data": {},
            "error": "Invalid range. Use: hour, day, week, month",
        }), 400

    # Build query
    query = """
        SELECT timestamp, target, gpu_util, gpu_temp, gpu_watts, cpu_percent,
               vram_percent, ram_percent, swap_percent, disk_read_mbps,
               disk_write_mbps, net_rx_mbps, net_tx_mbps, queue_depth
        FROM metrics_history
        WHERE timestamp >= ?
    """
    params = [cutoff.isoformat()]

    if target_filter:
        query += " AND target = ?"
        params.append(target_filter)

    query += " ORDER BY timestamp ASC"

    rows = []
    conn = None
    try:
        conn = sqlite3.connect(CONFIG["db_path"])
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(query, params)
        rows = cursor.fetchall()
    except Exception as e:
        logger.warning(f"metrics history unavailable: {e}")
    finally:
        if conn is not None:
            conn.close()

    # Group and aggregate data
    result = {
        name: []
        for name in CONFIG["targets"]
        if not target_filter or name == target_filter
    }
    for row in rows:
        target = row["target"]
        if target not in result:
            continue

        result[target].append({
            "timestamp": row["timestamp"],
            "gpu_util": row["gpu_util"],
            "gpu_temp": row["gpu_temp"],
            "gpu_watts": row["gpu_watts"],
            "cpu_percent": row["cpu_percent"],
            "vram_percent": row["vram_percent"],
            "ram_percent": row["ram_percent"],
            "swap_percent": row["swap_percent"],
            "disk_read_mbps": row["disk_read_mbps"],
            "disk_write_mbps": row["disk_write_mbps"],
            "net_rx_mbps": row["net_rx_mbps"],
            "net_tx_mbps": row["net_tx_mbps"],
            "queue_depth": row["queue_depth"]
        })

    # Aggregate if needed (for day/week/month views)
    if group_minutes > 1:
        for target in result:
            aggregated = []
            bucket = []
            bucket_start = None

            for point in result[target]:
                try:
                    ts = datetime.fromisoformat(point["timestamp"])
                except (TypeError, ValueError):
                    continue
                if bucket_start is None:
                    bucket_start = ts
                    bucket = [point]
                elif (ts - bucket_start).total_seconds() < group_minutes * 60:
                    bucket.append(point)
                else:
                    # Aggregate bucket
                    if bucket:
                        aggregated.append(aggregate_bucket(bucket))
                    bucket_start = ts
                    bucket = [point]

            # Don't forget last bucket
            if bucket:
                aggregated.append(aggregate_bucket(bucket))

            result[target] = aggregated

    return jsonify({
        "range": range_param,
        "data": result
    })


def aggregate_bucket(bucket):
    """Average a bucket of metrics."""
    if not bucket:
        return {}

    def avg(key):
        values = [p[key] for p in bucket if p[key] is not None]
        return round(sum(values) / len(values), 1) if values else None

    def max_val(key):
        values = [p[key] for p in bucket if p[key] is not None]
        return max(values) if values else None

    return {
        "timestamp": bucket[len(bucket) // 2]["timestamp"],  # Middle timestamp
        "gpu_util": avg("gpu_util"),
        "gpu_temp": max_val("gpu_temp"),  # Max temp is more useful
        "gpu_watts": avg("gpu_watts"),
        "cpu_percent": avg("cpu_percent"),
        "vram_percent": avg("vram_percent"),
        "ram_percent": avg("ram_percent"),
        "swap_percent": max_val("swap_percent"),  # Max swap is more concerning
        "disk_read_mbps": avg("disk_read_mbps"),
        "disk_write_mbps": avg("disk_write_mbps"),
        "net_rx_mbps": avg("net_rx_mbps"),
        "net_tx_mbps": avg("net_tx_mbps"),
        "queue_depth": max_val("queue_depth")
    }

if __name__ == "__main__":
    init_db()

    # Start background metrics collector
    metrics_thread = threading.Thread(target=collect_metrics, daemon=True)
    metrics_thread.start()

    # Start background job sync from ComfyUI
    sync_thread = threading.Thread(target=sync_comfyui_jobs, daemon=True)
    sync_thread.start()

    # Model-serving sampler (tokens/sec + requests served, persisted to sqlite)
    serving_thread = threading.Thread(target=collect_model_serving, daemon=True)
    serving_thread.start()

    # ComfyUI watchdog (auto-restart if down) — OFF BY DEFAULT.
    #
    # Ben, 2026-07-25: "I'm not even running comfy" / "take it off the auto
    # boot up". ComfyUI holds GPU memory he wants free. The systemd unit was
    # disabled and comfyui was dropped from the service-watchdog lists, but
    # THIS thread kept resurrecting it anyway — it SSHes to each target and
    # runs `sudo systemctl restart comfyui` (see restart_comfyui), which
    # starts a *disabled* unit just fine. Caught 2026-07-27: comfyui had been
    # restarting on gandalf hourly, and the watchdog was also failing against
    # frodo (unit renamed comfyui.service.disabled-2026-07-25) every cycle.
    #
    # A kill switch was documented as existing but was never actually in the
    # code, so there was no way to turn this off short of editing the file.
    # Set COMFYUI_WATCHDOG_ENABLED=1 to opt back in when ComfyUI is
    # deliberately put into service again.
    if os.environ.get("COMFYUI_WATCHDOG_ENABLED", "").strip() in ("1", "true", "yes", "on"):
        watchdog_thread = threading.Thread(target=comfyui_watchdog_loop, daemon=True)
        watchdog_thread.start()
        logger.info("ComfyUI watchdog: ENABLED (COMFYUI_WATCHDOG_ENABLED set)")
    else:
        logger.info(
            "ComfyUI watchdog: DISABLED (default). ComfyUI will NOT be auto-restarted. "
            "Set COMFYUI_WATCHDOG_ENABLED=1 to re-enable."
        )

    logger.info("")
    logger.info("=" * 60)
    logger.info("  GANDALF FLEET MONITOR")
    logger.info("=" * 60)
    logger.info(f"  Listening: http://0.0.0.0:5000")
    # Stale since the 2026-07-21 move to gandalf; corrected 2026-07-30.
    logger.info(f"  Dashboard: http://{FLEET_IPS['gandalf']}:5000")
    logger.info(f"  Targets:   {', '.join(CONFIG['targets'].keys())}")
    logger.info(f"  Notifications: Pushover {'enabled' if PUSHOVER_CONFIG['enabled'] else 'disabled'}")
    logger.info("=" * 60)
    logger.info("")
    app.run(host="0.0.0.0", port=5000)
