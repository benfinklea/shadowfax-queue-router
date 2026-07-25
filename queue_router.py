#!/usr/bin/env python3
"""
Queue Router Service for Shadowfax
Receives ComfyUI workflows and routes them to the best available GPU.
"""

import json
import re
import sqlite3
import uuid
import requests
import logging
import threading
import time
import paramiko
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from flask import Flask, request, jsonify
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

# Configuration
CONFIG = {
    "targets": {
        "gandalf": {
            "url": "http://192.168.1.122:8188",
            "ssh_host": "192.168.1.122",
            "ssh_user": "ben",
            "os": "linux",
            "net": "eth",
            "vram_gb": 96,
            "gpu_power_limit": 450,
            "gpu_power_max": 600,
            "disk_path": "/workspace"
        },
        "frodo": {
            "url": "http://192.168.1.105:8188",
            "ssh_host": "192.168.1.105",
            "ssh_user": "ben",
            "os": "linux",
            "net": "eth",
            "vram_gb": 32,
            "gpu_power_limit": 575,
            "gpu_power_max": 600,
            "disk_path": "/"
        },
        # --- The Shire (CI runner boxes) — added 2026-07-25 -------------
        # These were NEVER on the dashboard, so when three of them dropped
        # simultaneously the panel showed nothing at all. A machine that is
        # missing looked identical to a machine that is fine, which is the
        # worst possible failure mode for a monitor: Ben concluded the whole
        # fleet was dead because the healthy boxes had also vanished (their
        # liveness was tied to ComfyUI, which he had asked us to stop).
        #
        # No "url" key: these run no ComfyUI. Liveness comes from SSH via
        # _ssh_liveness(), which is now the honest signal for every target.
        # Addressed by .local (mDNS), matching the convention already used by
        # the overflow controller and runner-health-monitor. Tailscale is NOT
        # usable here: Tailscale SSH demands interactive browser approval and
        # these probes run unattended.
        "northfarthing": {
            "ssh_host": "northfarthing.local", "ssh_user": "ben",
            "os": "linux", "net": "eth", "disk_path": "/", "role": "ci-runner"
        },
        "southfarthing": {
            "ssh_host": "southfarthing.local", "ssh_user": "ben",
            "os": "linux", "net": "eth", "disk_path": "/", "role": "ci-runner"
        },
        "eastfarthing": {
            "ssh_host": "eastfarthing.local", "ssh_user": "ben",
            "os": "linux", "net": "eth", "disk_path": "/", "role": "ci-runner"
        },
        "westfarthing": {
            "ssh_host": "westfarthing.local", "ssh_user": "ben",
            "os": "linux", "net": "eth", "disk_path": "/", "role": "ci-runner"
        },
        "pippin": {
            "ssh_host": "pippen",
            "ssh_user": "ben",
            "os": "mac",
            "net": "eth",
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
    "northfarthing": {"ssh_host": "192.168.1.147", "ssh_user": "ben", "wol_mac": "84:47:09:65:43:c0", "net": "eth"},
    "eastfarthing":  {"ssh_host": "192.168.1.145", "ssh_user": "ben", "wol_mac": "84:47:09:62:ef:69", "net": "eth"},
    "southfarthing": {"ssh_host": "192.168.1.146", "ssh_user": "ben", "wol_mac": "84:47:09:65:42:58", "net": "eth"},
    "westfarthing":  {"ssh_host": "192.168.1.138", "ssh_user": "ben", "wol_mac": "84:47:09:65:42:88", "net": "eth"},
    # 2026-07-21: this dashboard now runs on GANDALF (migrated off shadowfax).
    # shadowfax must be SSH-managed like every other node — leaving it
    # "local: True" here would make its stats tile show gandalf's numbers and,
    # far worse, its REBOOT button reboot gandalf. Tailscale IP because
    # shadowfax's LAN mDNS is unreliable.
    "shadowfax":     {"ssh_host": "100.70.76.51", "ssh_user": "ben", "net": "wifi"},
    "sam":           {"ssh_host": "192.168.1.135", "ssh_user": "ben", "net": "wifi"},
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
GATEWAY_MODELS_URL = "http://gandalf.local:4000/v1/models"
# The 🔒 local-only routes from the fleet gateway table - the ones that should
# always be up. (opus/sonnet/codex/gemini/etc. are external and expected to
# come and go with vendor availability, so they're left off this glance tile.)
LOCAL_GATEWAY_ROUTES = ["fast", "code", "code-glm", "reason", "coder", "big", "cheap"]

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
    "echo \"cpu=$cpu ram_used=$ru ram_total=$rt temp=${tp:-0}\""
)

def get_fleet_node_metrics(name, cfg):
    """CPU/temp/RAM for one fleet node. Returns dict; online=False on any failure."""
    out = None
    try:
        if cfg.get("local"):
            import subprocess
            out = subprocess.run(["bash", "-c", FLEET_METRICS_CMD], capture_output=True,
                                 text=True, timeout=15).stdout
        else:
            client = get_ssh_client(cfg["ssh_host"], cfg.get("ssh_user", "ben"))
            if client is None:
                return {"name": name, "online": False, "can_wake": bool(cfg.get("wol_mac")), "net": cfg.get("net")}
            # no extra bash -c wrapper: the CMD contains single quotes, and sshd
            # already hands the command line to the login shell (bash here)
            _, stdout, _ = client.exec_command(FLEET_METRICS_CMD, timeout=15)
            out = stdout.read().decode()
    except Exception as e:
        logger.debug(f"fleet metrics {name}: {e}")
        return {"name": name, "online": False, "can_wake": bool(cfg.get("wol_mac")), "net": cfg.get("net")}
    m = dict(kv.split("=") for kv in (out or "").split() if "=" in kv)
    if "cpu" not in m:
        return {"name": name, "online": False, "can_wake": bool(cfg.get("wol_mac")), "net": cfg.get("net")}
    return {
        "name": name, "online": True,
        "net": cfg.get("net"),
        "cpu": int(m.get("cpu", 0)),
        "ram_used_gb": round(int(m.get("ram_used", 0)) / 1024, 1),
        "ram_total_gb": round(int(m.get("ram_total", 0)) / 1024, 1),
        "ram_pct": int(100 * int(m.get("ram_used", 0)) / max(1, int(m.get("ram_total", 1)))),
        "temp_c": round(int(m.get("temp", 0)) / 1000),
        "can_wake": bool(cfg.get("wol_mac")),
    }

# SSH liveness cache for the ComfyUI-independent fallback (2026-07-25).
# Maps target -> epoch seconds of the last PROVEN-alive SSH probe.
_ssh_alive_cache = {}
SSH_ALIVE_TTL = 120  # seconds a proven-alive result stays trusted

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
#
# DISABLED 2026-07-25 at Ben's instruction. He does not use ComfyUI and asked
# for it taken off auto-boot ("I'm not even running comfy") because it holds
# GPU memory. Disabling the systemd unit was NOT enough: this watchdog SSHes
# into the target and runs `sudo systemctl restart comfyui` whenever ComfyUI
# stops answering, so it resurrected the service within a minute every time --
# on gandalf AND frodo -- silently undoing the instruction.
#
# It also caused the dashboard bug Ben reported: liveness was derived from
# ComfyUI's port, so honouring his request made healthy machines read OFFLINE.
#
# Set to True only if ComfyUI is deliberately being used again.
COMFYUI_WATCHDOG_ENABLED = False

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
        client.connect(host, username=user, timeout=5)
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

def get_ssh_metrics(host, user):
    """Get CPU%, GPU power, temperature, utilization, swap, disk I/O, and network I/O via SSH."""
    result = {
        "cpu_percent": None,
        "gpu_watts": None,
        "gpu_power_limit": None,
        "gpu_temp": None,
        "gpu_util": None,
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

        # Get GPU power draw, current limit, temperature, and utilization
        stdin, stdout, stderr = client.exec_command(
            "nvidia-smi --query-gpu=power.draw,power.limit,temperature.gpu,utilization.gpu --format=csv,noheader,nounits"
        )
        gpu_output = stdout.read().decode().strip()
        if gpu_output:
            parts = gpu_output.split(",")
            if len(parts) >= 2:
                result["gpu_watts"] = float(parts[0].strip())
                result["gpu_power_limit"] = float(parts[1].strip())
            if len(parts) >= 3:
                result["gpu_temp"] = float(parts[2].strip())
            if len(parts) >= 4:
                result["gpu_util"] = float(parts[3].strip())

        # Get CPU usage using top (parse idle and subtract from 100)
        stdin, stdout, stderr = client.exec_command(
            "top -bn1 | grep '%Cpu' | sed 's/,/ /g' | awk '{for(i=1;i<=NF;i++) if($i==\"id\") print 100-$(i-1)}'"
        )
        cpu_output = stdout.read().decode().strip()
        if cpu_output:
            result["cpu_percent"] = round(float(cpu_output), 1)

        # Get swap usage
        stdin, stdout, stderr = client.exec_command(
            "free -b | grep Swap | awk '{print $2, $3}'"
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
            "cat /proc/diskstats | awk '/nvme0n1 |sda /{print $6*512/1024, $10*512/1024}' | head -1"
        )
        disk_io_output = stdout.read().decode().strip()
        if disk_io_output:
            parts = disk_io_output.split()
            if len(parts) >= 2:
                result["disk_read_mbps"] = round(float(parts[0]) / 1024, 1)
                result["disk_write_mbps"] = round(float(parts[1]) / 1024, 1)

        # Get network I/O (bytes per second on primary interface)
        stdin, stdout, stderr = client.exec_command(
            "cat /proc/net/dev | grep -E 'eth0|eno|enp' | head -1 | awk '{print $2, $10}'"
        )
        net_output1 = stdout.read().decode().strip()
        if net_output1:
            time.sleep(0.5)
            stdin, stdout, stderr = client.exec_command(
                "cat /proc/net/dev | grep -E 'eth0|eno|enp' | head -1 | awk '{print $2, $10}'"
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
            f"df -B1 {path} | tail -1 | awk '{{print $2, $3, $5}}'"
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

# --- Loaded-model VRAM detection + fleet-gateway route labels (2026-07-25) ---
# Ben: "show what models are loaded in VRAM on gandalf/frodo, segmented on the
# VRAM bar" + "label each GPU machine with the routes it's supposed to serve"
# (route -> machine map is authoritative in ~/.claude/rules/fleet.md).
# gandalf runs ONE model at a time (code-glm/reason/coder/big evict each
# other on gandalf's swapper, ~35s reload) - so gandalf's label set is
# "capable of", not "all loaded simultaneously". frodo/pippin each run one
# dedicated model and are always-loaded.
ROUTE_MODEL_ALIASES = [
    # (substring to match in a process cmdline/alias, route name, friendly model label)
    ("qwen3-235b-a22b", "big", "Qwen3-235B-A22B"),
    ("glm-4.5-air", "code-glm", "GLM-4.5-Air 106B"),
    ("gemma4-26b", "reason", "Gemma4-26B"),
    ("devstral", "coder", "Devstral-Small-2 24B"),
    ("qwen3.6-35b-a3b", "fast", "Qwen3.6-35B-A3B"),
    ("qwen3-coder-next", "code", "Qwen3-Coder-Next 80B"),
    ("glm-edge", "cheap", "GLM-Edge"),
]

FLEET_ROUTE_LABELS = {
    "gandalf": {"routes": ["code-glm", "reason", "coder", "big"],
                "note": "1 model loaded at a time - routes swap in ~35s"},
    "frodo": {"routes": ["fast"], "note": None},
    "pippin": {"routes": ["code"], "note": None},
}

def _match_route(text):
    """Map a process cmdline/alias string to a known fleet-gateway route + friendly label."""
    t = (text or "").lower()
    for substr, route, label in ROUTE_MODEL_ALIASES:
        if substr in t:
            return route, label
    return None, None

def _friendly_process_label(cmdline):
    """Best-effort human label for a GPU process that isn't a recognized route model."""
    if not cmdline:
        return "unknown process"
    low = cmdline.lower()
    if "comfyui" in low:
        return "ComfyUI"
    if "embedding-service" in low:
        return "embedding-service"
    if "llama-server" in low:
        return "llama-server (unrecognized model)"
    if "vllm" in low:
        return "vLLM (unrecognized model)"
    first = cmdline.split()[0] if cmdline.split() else cmdline
    return first.rsplit("/", 1)[-1]

def _parse_compute_apps_csv(csv_text):
    """Parse `nvidia-smi --query-compute-apps=pid,used_memory --format=csv,noheader,nounits`."""
    procs = []
    for line in (csv_text or "").strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2 and parts[0].isdigit():
            try:
                procs.append((int(parts[0]), int(parts[1])))
            except ValueError:
                continue
    return procs

loaded_models_cache = {}  # {target_name: {"data": {...}, "ts": float}}
loaded_models_cache_lock = threading.Lock()
LOADED_MODELS_CACHE_TTL = 20  # seconds - keep the 12s /api/status poll from re-running nvidia-smi/ssh every tick

def get_loaded_models(target_name, target_config):
    """Which model(s) are actually loaded in VRAM on this GPU host right now.

    Prefers real per-process VRAM footprint from `nvidia-smi --query-compute-apps`
    (local subprocess on gandalf, since this service runs there; SSH for frodo).
    Falls back to a process-list-only view (no VRAM sizes, live_breakdown=False)
    when nvidia-smi itself is unreachable (e.g. a driver/library version
    mismatch after an update) - never fabricates a size in that case.
    Never raises: any failure returns available=False.
    """
    now = time.time()
    with loaded_models_cache_lock:
        cached = loaded_models_cache.get(target_name)
        if cached and (now - cached["ts"]) < LOADED_MODELS_CACHE_TTL:
            return cached["data"]

    result = {"available": False, "live_breakdown": False, "total_mb": None, "segments": []}
    try:
        if target_name == "gandalf":
            import subprocess
            proc = subprocess.run(
                ["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=8
            )
            if proc.returncode == 0:
                procs = _parse_compute_apps_csv(proc.stdout)
                total = subprocess.run(
                    ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=8
                )
                if total.returncode == 0 and total.stdout.strip():
                    result["total_mb"] = int(total.stdout.strip().splitlines()[0])
                for pid, mb in procs:
                    cmdline = ""
                    try:
                        with open(f"/proc/{pid}/cmdline", "rb") as f:
                            cmdline = f.read().replace(b"\x00", b" ").decode(errors="ignore")
                    except Exception:
                        pass
                    route, label = _match_route(cmdline)
                    result["segments"].append({
                        "label": label or _friendly_process_label(cmdline),
                        "route": route, "mb": mb, "live": True,
                    })
                result["available"] = True
                result["live_breakdown"] = True
        elif "ssh_host" in target_config:
            client = get_ssh_client(target_config["ssh_host"], target_config.get("ssh_user", "ben"))
            if client is not None:
                _, stdout, stderr = client.exec_command(
                    "nvidia-smi --query-compute-apps=pid,used_memory --format=csv,noheader,nounits", timeout=8
                )
                out = stdout.read().decode(errors="ignore")
                err = stderr.read().decode(errors="ignore")
                # nvidia-smi writes its NVML-failure message to STDOUT (not stderr) on
                # this fleet - the exit status is the only reliable success signal.
                exit_status = stdout.channel.recv_exit_status()
                if exit_status == 0:
                    procs = _parse_compute_apps_csv(out)
                    _, tstdout, tstderr = client.exec_command(
                        "nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits", timeout=8
                    )
                    tout = tstdout.read().decode(errors="ignore").strip()
                    t_exit = tstdout.channel.recv_exit_status()
                    if t_exit == 0 and tout:
                        result["total_mb"] = int(tout.splitlines()[0])
                    for pid, mb in procs:
                        _, cstdout, _ = client.exec_command(
                            f"tr '\\0' ' ' < /proc/{pid}/cmdline 2>/dev/null", timeout=5
                        )
                        cmdline = cstdout.read().decode(errors="ignore")
                        route, label = _match_route(cmdline)
                        result["segments"].append({
                            "label": label or _friendly_process_label(cmdline),
                            "route": route, "mb": mb, "live": True,
                        })
                    result["available"] = True
                    result["live_breakdown"] = True
                else:
                    # nvidia-smi itself unreachable on this host right now - fall
                    # back to a process-list-only view, no fabricated VRAM sizes.
                    logger.debug(f"{target_name}: nvidia-smi unreachable for loaded-model check (exit={exit_status}, {(out or err).strip()[:120]})")
                    _, pstdout, _ = client.exec_command(
                        "ps aux | grep -E 'llama-server|vllm|ollama serve' | grep -v grep", timeout=8
                    )
                    for line in pstdout.read().decode(errors="ignore").splitlines():
                        if "llama-server" not in line and "vllm" not in line:
                            continue
                        route, label = _match_route(line)
                        result["segments"].append({
                            "label": label or _friendly_process_label(line),
                            "route": route, "mb": None, "live": False,
                        })
                    result["available"] = bool(result["segments"])
                    result["live_breakdown"] = False
    except Exception as e:
        logger.debug(f"loaded-model check failed for {target_name}: {e}")

    with loaded_models_cache_lock:
        loaded_models_cache[target_name] = {"data": result, "ts": time.time()}
    return result

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
    """Queued + in-progress GitHub Actions run counts for armbrain-io/armbrain -
    the same queue-depth signal the Shire autoscaler watches to decide whether
    to wake reserve boxes."""
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
            # Only count runs from the last 48h: GitHub sometimes strands
            # "queued" runs forever when a PR branch is deleted mid-queue
            # (uncancellable via API — cancel/force-cancel 500). Counting them
            # would permanently inflate the autoscaler's queue-depth signal.
            from datetime import datetime, timedelta, timezone
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")
            q = requests.get(base, params={"status": "queued", "per_page": 100, "created": f">={cutoff}"}, headers=headers, timeout=8)
            ip = requests.get(base, params={"status": "in_progress", "per_page": 100, "created": f">={cutoff}"}, headers=headers, timeout=8)
            if q.ok and ip.ok:
                result["available"] = True
                result["queued"] = q.json().get("total_count", 0)
                result["in_progress"] = ip.json().get("total_count", 0)

                # RUN count is not WORK count, and reading it as such is what made
                # the dashboard look idle on 2026-07-25 while the fleet was at load
                # average 30. One run holding ten test shards displays as "1", so a
                # fleet chewing through 10 concurrent jobs reads as almost nothing
                # happening. Ben looked at that number and correctly said it did not
                # match what he expected.
                #
                # So also count JOBS actually executing, and name the runners doing
                # them. Deliberately sourced from the jobs API rather than
                # GET /orgs/{org}/actions/runners: that endpoint's `status` and
                # `busy` fields are UNUSABLE for these runners. On 2026-07-25 it
                # reported 0 of 47 online and busy=3 while the jobs API showed work
                # in progress on 13 named runners and 9-10 Runner.Worker processes
                # were live locally. These runners use the broker long-poll
                # transport; the REST status field does not track those sessions
                # reliably. The jobs API agreed with the machines every single time.
                active_jobs = 0
                active_runners = set()
                try:
                    for run in (ip.json().get("workflow_runs") or [])[:12]:
                        jr = requests.get(
                            f"https://api.github.com/repos/{GITHUB_CI_REPO}/actions/runs/{run['id']}/jobs",
                            params={"per_page": 100}, headers=headers, timeout=8)
                        if not jr.ok:
                            continue
                        for job in jr.json().get("jobs", []):
                            if job.get("status") == "in_progress":
                                active_jobs += 1
                                if job.get("runner_name"):
                                    active_runners.add(job["runner_name"])
                    # Orphaned/unclearable runs, surfaced as a NAMED state rather
                    # than left to pollute queue depth (2026-07-25).
                    #
                    # 14 runs have been stuck `queued` since 2026-07-13 on branch
                    # fix/2461-email-connector-batch. PR #2797 merged, the branch was
                    # deleted while runs were queued, and GitHub never reconciled them.
                    # They cannot be cleared: cancel -> HTTP 500, force-cancel -> 500,
                    # delete -> 403, recreate-branch-then-cancel -> 500, check-suite
                    # rerequest -> 404. It is a platform-side bug.
                    #
                    # Ben saw "14 queued" and reasonably concluded CI was starved.
                    # Silently filtering them would repeat today's core mistake of
                    # hiding a thing we do not understand. So: report real queue depth
                    # separately from a labelled orphan count.
                    orphaned = 0
                    try:
                        oq = requests.get(base, params={"status": "queued", "per_page": 100},
                                          headers=headers, timeout=8)
                        if oq.ok:
                            stale_cut = (datetime.now(timezone.utc) - timedelta(hours=48))
                            for run in (oq.json().get("workflow_runs") or []):
                                ca = run.get("created_at") or ""
                                try:
                                    if datetime.strptime(ca, "%Y-%m-%dT%H:%M:%SZ").replace(
                                            tzinfo=timezone.utc) < stale_cut:
                                        orphaned += 1
                                except ValueError:
                                    pass
                    except Exception as e:
                        logger.debug(f"orphaned-run count failed: {e}")
                    result["orphaned_queued"] = orphaned
                    result["active_jobs"] = active_jobs
                    result["active_runners"] = sorted(active_runners)
                    result["active_runner_count"] = len(active_runners)
                except Exception as e:
                    # An unreadable jobs API must not silently read as "no work".
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

    result = {
        "available": available,
        "routes": [{"name": name, "live": routes_live[name]} for name in LOCAL_GATEWAY_ROUTES],
    }
    with route_health_cache_lock:
        route_health_cache["data"] = result
        route_health_cache["ts"] = time.time()
    return result

# ---------------------------------------------------------------------------
# RANGERS - the standing automated patrols.
#
# Why this panel exists, and why it is built THIS way: on 2026-07-25 cron on
# gandalf was found dead for ~6 hours with 30 entries silently not firing -
# including the watchdogs whose entire job was to notice things breaking. A
# panel that listed rangers by name would have shown a happy roster the whole
# time. So health here is derived ONLY from evidence that a ranger actually
# RAN: the mtime of a heartbeat/log file it writes itself. Presence of a
# script proves nothing and is never treated as green.
#
# Each entry declares:
#   heartbeat   - file the ranger touches/writes every run (mtime = last run)
#   max_age_min - how stale that mtime may get before the ranger is DEAD.
#                 Set to roughly 2.5x the schedule interval so one skipped
#                 run is tolerated but a genuinely stopped ranger goes red.
#   alert_grep  - if set and found in the tail of the heartbeat/log, the
#                 ranger ran but is REPORTING A PROBLEM (amber, not red).
#   armed       - True if it can take autonomous action; False = reports only.
# A ranger with heartbeat=None renders GRAY/"unprovable", never green.
_ST = "/home/ben/.local/state"
_FA = "/workspace/fellowship-agents/state"

RANGER_SPECS = [
    # --- Cost & credentials (the "Strider" mandate) ---------------------
    # alert_grep FATAL is essential here: this script's heartbeat updates even
    # when the run dies at the credential step, so mtime alone reported a blind
    # ranger as healthy. It had in fact never once succeeded from cron.
    {"key": "spend-watch", "display": "SPEND", "armed": False,
     "watches": "Month-to-date Anthropic spend against the account cap, so a slow multi-day climb gets caught before it locks the account.",
     "schedule": "hourly", "max_age_min": 150,
     "heartbeat": f"{_ST}/anthropic-spend-watch/check.log", "alert_grep": ["FATAL"]},
    {"key": "credentials", "display": "CREDS", "armed": False,
     "watches": "Logins and API keys quietly expiring - and whether the other rangers' heartbeats have gone stale.",
     "schedule": "daily 04:17 UTC", "max_age_min": 2160,
     "heartbeat": f"{_ST}/credential-inventory/run.log", "alert_grep": ["PAGED NEW"]},
    # --- Ben's lifelines ------------------------------------------------
    {"key": "telegram", "display": "TELEGRAM", "armed": True,
     "watches": "Both Telegram channels to Ben's phone - the bot token, the pollers, and duplicate-poller conflicts.",
     "schedule": "every 5 min", "max_age_min": 15,
     "heartbeat": f"{_ST}/telegram-channels-watchdog/last-run",
     "alert_grep": None},
    {"key": "elrond-bridge", "display": "PHONE-BRIDGE", "armed": True,
     "watches": "The phone-to-Elrond message bridge, its tmux target window, and any messages stuck in the queue.",
     "schedule": "every minute", "max_age_min": 8,
     "heartbeat": "/home/ben/.elrond/watchdog.log", "alert_grep": ["tmux_ok=false"]},
    {"key": "elrond-seat", "display": "SEAT", "armed": True,
     "watches": "Elrond's session lease - re-wakes the coordinator when its lease expires while the pane is still alive.",
     "schedule": "every few min", "max_age_min": 20,
     "heartbeat": f"{_ST}/elrond-supervisor/heartbeat", "alert_grep": None},
    # --- Fleet & routes -------------------------------------------------
    {"key": "gemini-routes", "display": "GEMINI-ROUTE", "armed": False,
     "watches": "Whether the Gemini routes actually answer a real prompt, not merely that the port is open.",
     "schedule": "every 15 min", "max_age_min": 45,
     "heartbeat": f"{_ST}/gemini-route-check/check.log", "alert_grep": ["=FAIL", "=ERR"]},
    {"key": "palantir", "display": "PALANTIR", "armed": False,
     "watches": "Whole-fleet sweep of every machine - failed services, disk, swap, backups, DNS, cron gaps.",
     "schedule": "every 4 hours", "max_age_min": 330,
     "heartbeat": f"{_FA}/palantir/latest.md", "alert_scope": "whole",
     "alert_grep": ["status: RED", "status: WARNING"]},
    {"key": "sentinel", "display": "SENTINEL", "armed": False,
     "watches": "Customer data flowing in - connector and ingestion health across Armbrain.",
     "schedule": "every 2 hours", "max_age_min": 180,
     "heartbeat": f"{_FA}/sentinel/status.json", "alert_scope": "whole",
     "alert_grep": ['"ok": false', '"ok":false']},
    {"key": "needs-ben", "display": "NEEDS-BEN", "armed": False,
     "watches": "Any fleet window that has stalled waiting on Ben, so a blocked lane cannot sit unnoticed.",
     "schedule": "every 5 min", "max_age_min": 15,
     "heartbeat": f"{_FA}/needs-ben/flags.tsv", "alert_grep": None},
    # --- CI & delivery --------------------------------------------------
    # CI-LOAD ranger REMOVED 2026-07-25: Ben ripped out all runner scaling and
    # limiting ("everybody runs without limitations"). The governor no longer exists,
    # so watching for its heartbeat would produce a permanent false red - the exact
    # failure this panel is meant to prevent. A monitor for a deleted system is worse
    # than no monitor.
    {"key": "pr-nanny", "display": "PR-NANNY", "armed": True,
     "watches": "The Armbrain pull-request queue - keeps labels, merge state and stuck reviews moving.",
     "schedule": "every 5 min", "max_age_min": 20,
     "heartbeat": f"{_ST}/armbrain-pr-nanny/run.log", "alert_grep": None},
    {"key": "deploy-tick", "display": "DEPLOY-TICK", "armed": True,
     "watches": "Whether the scheduled deploy actually fired this hour; dispatches it by hand when GitHub skipped it.",
     "schedule": "hourly", "max_age_min": 150,
     "heartbeat": "/home/ben/.deploy-tick-backstop.log", "alert_grep": None},
    {"key": "nightly-deep", "display": "NIGHTLY", "armed": True,
     "watches": "The nightly deep build - re-dispatches it if it was skipped, and escalates on a failure streak.",
     "schedule": "daily 10:20 UTC", "max_age_min": 2160,
     "heartbeat": f"{_ST}/nightly-backstop.log", "alert_grep": ["ESCALATED"]},
    {"key": "lanes", "display": "LANES", "armed": True,
     "watches": "The always-on work lanes - rebuilds any lane whose window has died.",
     "schedule": "every 3 min", "max_age_min": 15,
     "heartbeat": f"{_ST}/lanes-supervisor/heartbeat", "alert_grep": None},
    # --- Standing schedule & cross-substrate (built 2026-07-25) ---------
    # These are registered here on purpose. Both were created tonight to fix
    # silent-failure problems, and an unregistered watcher is the exact thing
    # they exist to prevent - so they get to be visible themselves.
    {"key": "council-schedule", "display": "SCHEDULE", "armed": True,
     "watches": "Whether the ratified post-launch phases are due yet - and fires them without needing to be asked.",
     "schedule": "hourly", "max_age_min": 150,
     "heartbeat": f"{_ST}/council-schedule/last-run", "alert_grep": ["FALSIFICATION", "CANNOT DISPATCH"]},
    # The 13-hour blind spot, made visible. On 2026-07-25 prod sat 13h and 28 commits
    # behind main and nobody noticed - the `deploy-landed` workflow DID detect it and
    # failed five times in 24h, but it went red inside GitHub Actions with no path to a
    # human. This measures the gap directly and puts it on the wall.
    # Watches the outcome of an unattended deploy carrying fix-forward migrations and
    # pages on a failed or half-applied migrate. Registered here because an unwatched
    # watcher is the thing this whole panel exists to prevent.
    {"key": "deploy-outcome", "display": "DEPLOY-WATCH", "armed": False,
     "watches": "The result of each production deploy - pages Ben if a database migration fails or half-applies.",
     "schedule": "every 5 min", "max_age_min": 20,
     "heartbeat": f"{_ST}/deploy-outcome-watch/last-run", "alert_grep": ["ALERT"]},
    {"key": "prod-freshness", "display": "PROD-FRESH", "armed": False,
     "watches": "Whether production is actually running what main says it should - the gap nobody could see.",
     "schedule": "hourly", "max_age_min": 150,
     "heartbeat": f"{_ST}/prod-freshness/last-run", "alert_grep": ["STALE"]},
    # First Move's sms: send is a P2P DEEPLINK - the human taps Send in their own
    # Messages app. There is no carrier, no Twilio, and therefore NO delivery
    # receipt and NO bounce to monitor (SPEC-NETWORK-SPRINT.md 6.3). This ranger
    # deliberately watches the HANDOFF instead: clipboard-fallback rate, customers
    # who never reached a draft, fleet silence, and unroutable (failed-E.164)
    # contacts. Do not rename it to anything containing "delivery" - that would be
    # a monitor reporting on data that does not exist.
    # It stays AMBER (HOLD) until ns_events ships with WP-0; a monitor with no
    # data source must never read as green.
    {"key": "first-move-deeplink", "display": "FM-DEEPLINK", "armed": False,
     "watches": "First Move sms: deeplink handoff - clipboard fallbacks, customers who never reach a draft, and fleet silence. NOT delivery: a P2P deeplink has no receipt or bounce.",
     "schedule": "hourly", "max_age_min": 150,
     "heartbeat": f"{_ST}/first-move-deeplink-watch/check.log",
     # Trailing spaces are load-bearing: the healthy summary line legitimately
     # contains words like "alerted", and a bare "ALERT" substring matched it and
     # pinned this pill permanently amber on first run.
     "alert_scope": "last_line", "alert_grep": ["ALERT ", "FATAL ", "HOLD "]},
    {"key": "cron-fix-verify", "display": "CRON-VERIFY", "armed": False,
     "watches": "Confirms tonight's cron-environment fixes still hold when the scheduler runs them, not just by hand.",
     "schedule": "daily 06:10 UTC", "max_age_min": 2160,
     "heartbeat": f"{_ST}/cron-fix-verification.log", "alert_grep": ["FAIL"]},
    # --- Broad periodic sweeps -----------------------------------------
    # A stable last-run heartbeat was added to this script on 2026-07-25, but it
    # only runs Sundays, so the file will not exist until then. Left unprovable
    # rather than pointed at the missing file, which would render a false red for
    # a ranger that is simply not due yet.
    {"key": "deadman-scan", "display": "DEADMAN", "armed": False,
     "watches": "Weekly fleet-wide sweep for things that have gone quiet - the catch-all for silent failures.",
     "schedule": "Sundays 08:00 UTC", "max_age_min": 11520,
     "heartbeat": None},   # -> _ST/deadman-scan/last-run once it first runs
    {"key": "shire", "display": "SHIRE", "armed": False,
     "watches": "The four Shire CI boxes - firewall, updates, wake-on-LAN, disk and load.",
     "schedule": "daily 06:00 UTC", "max_age_min": 2160,
     "heartbeat": "/var/log/shire-daily-check.log", "alert_grep": None},
    # --- Known-unprovable: deliberately gray, never green ---------------
    # These do real work but write nothing on a clean run, so a quiet log cannot
    # be distinguished from "cron stopped calling it". Listing them as unknown is
    # the honest state; each needs a heartbeat line added before it can go green.
    # Both of these earned their way out of "unprovable" on 2026-07-25 by gaining
    # a trap-based heartbeat that writes on EVERY exit path, including the paths
    # where they find nothing wrong and the paths where they take action.
    {"key": "elrond-deadman", "display": "DEADMAN-SEAT", "armed": True,
     "watches": "Cold-boots a replacement coordinator if Elrond's seat dies outright.",
     "schedule": "every 10 min", "max_age_min": 30,
     "heartbeat": f"{_ST}/elrond-deadman/last-run", "alert_grep": ["cold boot", "FAIL"]},
    # service-watchdog runs as root and its real log is root-only, so it writes
    # this heartbeat to a ben-readable path specifically so the dashboard can see it.
    {"key": "service-watchdog", "display": "SERVICES", "armed": True,
     "watches": "Critical system services, restarting them when they drop.",
     "schedule": "every 10 min", "max_age_min": 30,
     "heartbeat": f"{_ST}/service-watchdog/last-run", "alert_grep": ["restarted 1", "restarted 2",
                                                                     "restarted 3", "FAIL"]},
]

RANGERS = RANGER_SPECS  # alias kept for readability at call sites

ranger_cache = {"data": None, "ts": 0.0}
ranger_cache_lock = threading.Lock()
RANGER_CACHE_TTL = 20  # seconds

def _cron_daemon_alive():
    """cron itself is the single point of failure behind most rangers."""
    import subprocess
    try:
        r = subprocess.run(["systemctl", "is-active", "cron"],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() == "active"
    except Exception:
        return None  # unknown, not a claim of health

def _ranger_state(spec, now):
    """Derive one ranger's live state purely from filesystem evidence."""
    import os
    from zoneinfo import ZoneInfo
    ct = ZoneInfo("America/Chicago")
    hb = spec.get("heartbeat")
    out = {
        "key": spec["key"], "display": spec["display"],
        "watches": spec.get("watches", ""),
        "schedule": spec.get("schedule", ""),
        "armed": bool(spec.get("armed")),
        "age_min": None, "last_run": None, "detail": "",
    }
    if not hb:
        out["state"] = "unknown"
        out["detail"] = "No heartbeat or log file - cannot prove it ran. Status unverifiable."
        return out
    try:
        mtime = os.path.getmtime(hb)
    except OSError:
        out["state"] = "dead"
        out["detail"] = f"Heartbeat file has never appeared ({hb}) - this ranger has likely never run."
        return out

    age_min = (now - mtime) / 60.0
    out["age_min"] = round(age_min, 1)
    out["last_run"] = datetime.fromtimestamp(mtime, ct).strftime("%b %-d %-I:%M%p").lower()
    limit = spec.get("max_age_min") or 0

    if limit and age_min > limit:
        out["state"] = "dead"
        out["detail"] = (f"Last ran {_human_age(age_min)} ago; expected every "
                         f"{spec.get('schedule','?')}. It has stopped running.")
        return out

    alert = spec.get("alert_grep")
    if alert:
        # Scope matters and getting it wrong produces permanent false amber.
        # "last_line" (default) is for APPEND-ONLY LOGS: only the most recent
        # run's verdict counts, otherwise a FATAL from hours ago keeps the pill
        # amber forever even though every run since succeeded - and a ranger
        # that is always amber is exactly as useless as one that is always green.
        # "whole" is for STATUS FILES that are rewritten each run (status.json,
        # latest.md), where the verdict may sit anywhere in the file - including
        # the first line, which a tail-only read would miss entirely.
        scope = spec.get("alert_scope", "last_line")
        try:
            with open(hb, "r", errors="replace") as fh:
                blob = fh.read(262144)
            if scope == "last_line":
                lines = [ln for ln in blob.splitlines() if ln.strip()]
                haystack = lines[-1] if lines else ""
            else:
                haystack = blob
            for pat in ([alert] if isinstance(alert, str) else alert):
                if pat.lower() in haystack.lower():
                    out["state"] = "alert"
                    out["detail"] = (f"Ran {_human_age(age_min)} ago and its latest result "
                                     f"REPORTS A PROBLEM (matched \"{pat}\").")
                    return out
        except OSError:
            pass

    out["state"] = "ok"
    out["detail"] = f"Ran {_human_age(age_min)} ago, on schedule, nothing flagged."
    return out

def _human_age(age_min):
    if age_min < 1:
        return "under a minute"
    if age_min < 90:
        return f"{int(age_min)} min"
    if age_min < 48 * 60:
        return f"{age_min/60:.1f} hr"
    return f"{age_min/1440:.1f} days"

def get_ranger_health():
    now = time.time()
    with ranger_cache_lock:
        if ranger_cache["data"] is not None and (now - ranger_cache["ts"]) < RANGER_CACHE_TTL:
            return ranger_cache["data"]

    rangers = [_ranger_state(s, now) for s in RANGER_SPECS]
    cron_ok = _cron_daemon_alive()
    counts = {}
    for r in rangers:
        counts[r["state"]] = counts.get(r["state"], 0) + 1

    result = {
        "rangers": rangers,
        "counts": counts,
        "total": len(rangers),
        "cron_ok": cron_ok,
        # The meta-warning: if cron is down, every cron-scheduled ranger is
        # down too, whatever its last heartbeat happens to say.
        "cron_warning": (None if cron_ok is not False else
                         "cron daemon is NOT running - every scheduled ranger is stopped"),
    }
    with ranger_cache_lock:
        ranger_cache["data"] = result
        ranger_cache["ts"] = now
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


def _ssh_liveness(target_name, target_config):
    """Is this machine reachable over SSH, regardless of ComfyUI?

    A machine is not offline just because ComfyUI is not running on it. Ben
    asked for ComfyUI to be taken off auto-boot; doing so made frodo and
    gandalf render as OFFLINE on his dashboard while both were healthy.

    A single probe is not reliable enough on its own -- these boxes run heavy
    CI and an occasional slow connect made the check pass on some polls and
    fail on others, which the display hysteresis then counted as consecutive
    failures and flipped the machine back to OFFLINE anyway. So a SUCCESSFUL
    result is cached briefly. Only successes are cached; a genuinely dead box
    stops refreshing it and ages out within SSH_ALIVE_TTL, so a real outage
    still surfaces.
    """
    if not target_config.get("ssh_host"):
        return False
    cached = _ssh_alive_cache.get(target_name)
    if cached and (time.time() - cached) < SSH_ALIVE_TTL:
        return True
    try:
        client = get_ssh_client(target_config["ssh_host"],
                                target_config.get("ssh_user", "ben"))
        if client is not None:
            _, stdout, _ = client.exec_command("echo alive", timeout=8)
            if stdout.read().decode().strip() == "alive":
                _ssh_alive_cache[target_name] = time.time()
                logger.info(
                    f"{target_name}: ComfyUI not answering but SSH is alive "
                    f"- reporting ONLINE (machine up, ComfyUI service down)")
                return True
    except Exception as e:
        logger.debug(f"{target_name} SSH liveness probe failed: {e}")
    return False


def get_target_status(target_name, target_config, fast=False):
    """Check a target's availability and gather live metrics.

    Linux targets are probed via ComfyUI HTTP + SSH (nvidia-smi). Mac targets
    have no ComfyUI here, so they report online via SSH and use native metrics.
    """
    result = {
        "online": False,
        "os": target_config.get("os", "linux"),
        "queue_running": 0,
        "queue_pending": 0,
        "gpu": None,
        "ram": None,
        "cpu_percent": None,
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
        "route_labels": FLEET_ROUTE_LABELS.get(target_name)
    }

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
        return result

    # Try ComfyUI HTTP endpoints
    try:
        # Get queue status
        # Timeout raised 5s -> 10s (2026-07-18): gandalf/frodo run heavy CI jobs
        # and can be slow to answer while busy-but-healthy; 5s was flickering
        # them to false OFFLINE. See apply_display_hysteresis() for the second
        # half of this fix (consecutive-failure hysteresis on the display).
        if not target_config.get("url"):
            raise KeyError("no ComfyUI url configured for this target")
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

            # CUDA version derived from the PyTorch build tag (e.g. "2.9.1+cu128"
            # -> "12.8"). LABEL IT AS SUCH — this is the CUDA that *PyTorch* was
            # compiled against, which is NOT necessarily the box's installed
            # toolkit, nor the driver's max-supported CUDA, nor what the actual
            # inference binary links.
            #
            # 2026-07-25: this bit Ben. The card read "RTX 5090 (CUDA 12.8)" for
            # frodo, so he reasonably concluded frodo was behind gandalf's 13.0
            # and asked for an upgrade. In fact frodo has toolkit 13.0 installed
            # and its llama-server links libcudart.so.13 — only its PyTorch is a
            # cu128 build. Three different true numbers; the bare label "CUDA"
            # implied the wrong one. An unlabelled number that is technically
            # correct but answers a different question than the reader is asking
            # is a wrong number.
            pytorch_ver = system.get("pytorch_version", "")
            cuda_match = re.search(r"\+cu(\d+)", pytorch_ver)
            cuda_version = None
            if cuda_match:
                cuda_num = cuda_match.group(1)
                cuda_version = f"{cuda_num[:-1]}.{cuda_num[-1]}" if len(cuda_num) >= 2 else cuda_num
                # Qualify it so the card cannot be misread as the box's CUDA level.
                cuda_version = f"torch cu{cuda_version}"

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

    # ComfyUI-independent liveness must be evaluated BEFORE this early return.
    # Otherwise the dashboard's fast path (used by /api/status) equates
    # "ComfyUI is down" with "the machine is offline" -- the exact bug Ben
    # reported on 2026-07-25, where healthy boxes vanished from his monitor
    # because he had asked for ComfyUI to be stopped.
    if not result["online"] and _ssh_liveness(target_name, target_config):
        result["online"] = True
        result["comfyui_down"] = True

    if fast and not result["online"]:
        return result

    # Get SSH metrics (CPU, GPU power, temp, util, swap, I/O) - independent of ComfyUI status
    if "ssh_host" in target_config:
        try:
            ssh_metrics = get_ssh_metrics(
                target_config["ssh_host"],
                target_config.get("ssh_user", "ben")
            )
            result["cpu_percent"] = ssh_metrics.get("cpu_percent")
            result["gpu_watts"] = ssh_metrics.get("gpu_watts")
            result["gpu_temp"] = ssh_metrics.get("gpu_temp")
            result["gpu_util"] = ssh_metrics.get("gpu_util")
            if ssh_metrics.get("gpu_power_limit"):
                result["gpu_power_limit"] = ssh_metrics["gpu_power_limit"]

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

            # Loaded-model VRAM breakdown (cached separately, see get_loaded_models)
            result["loaded_models"] = get_loaded_models(target_name, target_config)
        except Exception as e:
            logger.warning(f"{target_name} SSH unreachable: {e}")

    # ------------------------------------------------------------------
    # LIVENESS FALLBACK (2026-07-25). A machine is not "offline" just
    # because ComfyUI is not running on it.
    #
    # Until now `online` was set ONLY when ComfyUI's /queue answered. So
    # when Ben asked for ComfyUI to be taken off auto-boot (he does not use
    # it and it holds GPU memory), doing exactly what he asked made frodo --
    # and later gandalf -- render as OFFLINE on his dashboard while both
    # were perfectly healthy and reachable over SSH. He noticed before I
    # did, which is the point: the panel reported a service outage as a
    # machine outage.
    #
    # SSH reachability is the honest liveness signal, and it is what the
    # Mac target has always used. ComfyUI presence is a separate fact and
    # is still reflected by queue_running/queue_pending being populated.
    # ------------------------------------------------------------------
    return result

@app.route("/api/status", methods=["GET"])
def get_status():
    """Get status of all targets and recent jobs."""
    targets_status = {}
    with ThreadPoolExecutor(max_workers=max(1, len(CONFIG["targets"]))) as executor:
        futures = {
            executor.submit(get_target_status, name, config, True): (name, config)
            for name, config in CONFIG["targets"].items()
        }
        results = {}
        for future in as_completed(futures):
            name, config = futures[future]
            try:
                status = future.result()
            except Exception as e:
                logger.warning(f"{name} status unavailable: {e}")
                status = {
                    "online": False,
                    "os": config.get("os", "linux"),
                    "queue_running": 0,
                    "queue_pending": 0,
                    "gpu": None,
                    "ram": None,
                    "cpu_percent": None,
                    "gpu_watts": None,
                    "gpu_temp": None,
                    "gpu_util": None,
                    "gpu_power_limit": config.get("gpu_power_limit", 300),
                    "gpu_power_max": config.get("gpu_power_max", 400),
                    "disk": None,
                    "swap": None,
                    "disk_io": None,
                    "net_io": None,
                    "loaded_models": None,
                    "route_labels": FLEET_ROUTE_LABELS.get(name),
                }
            results[name] = {
                **status,
                # Display-only smoothing: don't let one slow/busy probe flip
                # the dashboard to a false OFFLINE alarm (see apply_display_hysteresis).
                "online": apply_display_hysteresis(name, status["online"]),
                "vram_gb": config.get("vram_gb"),
                "net": config.get("net"),
                "url": config.get("url")
            }

    # Preserve CONFIG order (gandalf, frodo, pippin) regardless of completion order
    targets_status = {name: results[name] for name in CONFIG["targets"] if name in results}

    conn = sqlite3.connect(CONFIG["db_path"])

    # Get jobs today count per target
    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    cursor = conn.execute(
        "SELECT target, COUNT(*) FROM jobs WHERE submitted_at >= ? GROUP BY target",
        (today_start,)
    )
    jobs_today = {row[0]: row[1] for row in cursor.fetchall()}

    # Add jobs_today to each target
    for name in targets_status:
        targets_status[name]["jobs_today"] = jobs_today.get(name, 0)

    # Max GPU utilization today per target (from collected metrics)
    cursor = conn.execute(
        "SELECT target, MAX(gpu_util) FROM metrics_history WHERE timestamp >= ? GROUP BY target",
        (today_start,)
    )
    max_util_today = {row[0]: row[1] for row in cursor.fetchall()}
    for name in targets_status:
        v = max_util_today.get(name)
        targets_status[name]["max_util_today"] = round(v) if v is not None else None

    cursor = conn.execute(
        "SELECT id, target, status, submitted_at FROM jobs ORDER BY submitted_at DESC LIMIT 10"
    )
    recent_jobs = [
        {"id": row[0], "target": row[1], "status": row[2], "submitted_at": row[3]}
        for row in cursor.fetchall()
    ]
    conn.close()

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


pipeline_cache = {"data": None, "ts": 0.0}
pipeline_cache_lock = threading.Lock()
PIPELINE_CACHE_TTL = 300  # seconds

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
            # deploys today (Gateway Deploy workflow)
            week_start = (now_ct - timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0)
            r = requests.get(f"https://api.github.com/repos/{GITHUB_CI_REPO}/actions/runs",
                             params={"created": f">={week_start.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}", "per_page": 100}, headers=headers, timeout=8)
            dep_ok = dep_fail = dep_live = 0
            live_started_min = None
            dep_days = [0] * 7
            if r.ok:
                for run in r.json().get("workflow_runs", []):
                    if run.get("name") != "Gateway Deploy":
                        continue
                    run_ct = None
                    try:
                        run_ct = datetime.strptime(run.get("created_at"), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone(CT)
                    except Exception:
                        pass
                    is_today = run_ct is not None and run_ct.date() == now_ct.date()
                    if run.get("status") == "completed" and run.get("conclusion") == "success" and run_ct is not None:
                        idx = 6 - (now_ct.date() - run_ct.date()).days
                        if 0 <= idx <= 6:
                            dep_days[idx] += 1
                    if run.get("status") != "completed":
                        if not is_today:
                            continue
                        dep_live += 1
                        try:
                            t = datetime.strptime(run.get("run_started_at") or run.get("created_at"),
                                                  "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                            m = int((datetime.now(timezone.utc) - t).total_seconds() // 60)
                            live_started_min = m if live_started_min is None else min(live_started_min, m)
                        except Exception:
                            pass
                    elif run.get("conclusion") == "success":
                        if is_today:
                            dep_ok += 1
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

@app.route("/api/ci_queue", methods=["GET"])
def api_ci_queue():
    """Queued/running GitHub Actions counts for armbrain-io/armbrain (cached)."""
    return jsonify(get_ci_queue_status())

@app.route("/api/rangers", methods=["GET"])
def api_rangers():
    """Live status of the standing automated patrols, derived from whether each
    one actually RAN recently - not from whether its script exists (cached)."""
    return jsonify(get_ranger_health())

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
            # Hard-disabled 2026-07-21: the dashboard now runs on gandalf, the
            # fleet hub — a "local" reboot would take down the hub. No node
            # should be configured local anymore; SSH-manage everything.
            return jsonify({"error": "Local reboot disabled — this host is the fleet hub"}), 400
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
    return '''<!DOCTYPE html><html><head><title>FLEET MONITOR</title>
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
@media(min-width:1300px){body{display:grid;grid-template-columns:1fr 1fr;gap:0 14px;align-items:start}
h1,#topbar,#monitors,#fleet-hosts,#ci-queue-card,#history{grid-column:1/-1}}
body{font-family:'Rajdhani',sans-serif;background:var(--bg-dark);color:#e0e0e0;padding:10px;margin:0;
background-image:radial-gradient(ellipse at top,#0d1a2d 0%,transparent 50%),
linear-gradient(180deg,transparent 0%,rgba(0,255,242,0.03) 100%);min-height:100vh;font-size:14px}
@media(min-width:768px){body{padding:12px 20px}}
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
h1{font-family:'Orbitron',monospace;color:var(--neon-cyan);font-size:1.3em;font-weight:900;letter-spacing:3px;
text-shadow:var(--glow-cyan);border-bottom:1px solid var(--neon-cyan);padding-bottom:6px;margin:4px 0 10px;
text-transform:uppercase;text-align:center}
h1::before{content:'◈ ';color:var(--neon-magenta)}
h1::after{content:' ◈';color:var(--neon-magenta)}
h3{margin-top:0;color:var(--neon-cyan);font-family:'Orbitron',monospace;font-size:1.1em;letter-spacing:2px;
text-transform:uppercase;text-shadow:0 0 10px var(--neon-cyan)}
.card{background:var(--bg-card);padding:10px;border-radius:4px;margin:6px 0;
border:1px solid #1a2332;box-shadow:0 0 20px rgba(0,255,242,0.1),inset 0 0 60px rgba(0,0,0,0.3)}
.card::before{content:'';position:absolute;top:0;left:0;right:0;height:1px;
background:linear-gradient(90deg,transparent,var(--neon-cyan),transparent)}
.online{color:var(--neon-green);font-weight:bold;text-shadow:var(--glow-green)}
.offline{color:var(--neon-red);font-weight:bold;text-shadow:var(--glow-red);animation:pulse 1s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.7}}
@keyframes glow-pulse{0%,100%{filter:brightness(1)}50%{filter:brightness(1.3)}}
.job{padding:6px 10px;border-left:3px solid var(--neon-cyan);margin:8px 0;background:var(--bg-panel);
border-radius:0 4px 4px 0;font-family:'Rajdhani',sans-serif;transition:all 0.3s}
.job:hover{background:#1a2332;border-left-color:var(--neon-magenta);box-shadow:0 0 15px rgba(0,255,242,0.2)}
.job.video{border-color:var(--neon-magenta)}
.job.completed{opacity:0.7}
pre{background:var(--bg-panel);padding:8px;border-radius:4px;overflow-x:auto;border:1px solid #1a2332;
font-family:'Rajdhani',monospace;color:var(--neon-cyan)}
table{width:100%;border-collapse:collapse}
td,th{padding:5px 8px;text-align:left;border-bottom:1px solid #1a2332}
th{color:var(--neon-cyan);font-family:'Orbitron',monospace;font-size:0.8em;letter-spacing:1px}
.gpu-card{padding:10px;background:linear-gradient(135deg,var(--bg-panel) 0%,var(--bg-card) 100%);
padding:12px;border-radius:8px;margin:6px 0;border:1px solid #1a2332;position:relative;overflow:hidden}
.gpu-card::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;
background:linear-gradient(90deg,var(--neon-cyan),var(--neon-magenta),var(--neon-cyan))}
.gpu-card::after{content:'';position:absolute;top:0;right:0;width:100px;height:100px;
background:radial-gradient(circle,rgba(0,255,242,0.1) 0%,transparent 70%);pointer-events:none}
.gpu-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
.gpu-name{font-family:'Orbitron',monospace;font-size:1.3em;font-weight:700;text-transform:uppercase;
letter-spacing:2px;color:var(--neon-cyan);text-shadow:0 0 10px var(--neon-cyan)}
.net-badge{font-size:0.65em;opacity:0.85;text-shadow:none;vertical-align:middle}
.progress-bar{background:#1a1a2e;border-radius:2px;height:18px;overflow:hidden;margin:5px 0;
border:1px solid #2a2a4e;position:relative}
.progress-bar::before{content:'';position:absolute;top:0;left:0;right:0;bottom:0;
background:repeating-linear-gradient(90deg,transparent,transparent 10px,rgba(255,255,255,0.03) 10px,rgba(255,255,255,0.03) 20px)}
.progress-fill{height:100%;transition:width 2s cubic-bezier(0.25,0.1,0.25,1);position:relative}
.progress-fill::after{content:'';position:absolute;top:0;right:0;width:30px;height:100%;
background:linear-gradient(90deg,transparent,rgba(255,255,255,0.4));opacity:0.3}
.progress-fill.progress-red{animation:bar-breathe 4s ease-in-out infinite}
.progress-fill.progress-red::after{animation:shimmer 2s ease-in-out infinite}
.progress-fill.segmented{display:flex;background:none;box-shadow:none}
.progress-seg{height:100%;flex:0 0 auto}
.bar-legend{display:flex;flex-wrap:wrap;gap:4px 14px;margin:4px 0 2px;font-size:0.78em;color:#9ab}
.bar-legend-item{display:flex;align-items:center;gap:5px;white-space:nowrap}
.bar-legend-dot{width:8px;height:8px;border-radius:50%;flex:0 0 auto;box-shadow:0 0 4px currentColor}
@keyframes bar-breathe{0%,100%{filter:brightness(1)}50%{filter:brightness(1.15)}}
@keyframes shimmer{0%,100%{opacity:0.3}50%{opacity:0.8}}
.progress-vram,.progress-ram,.progress-disk{background:linear-gradient(90deg,#00ff8844,var(--neon-green));box-shadow:0 0 20px #39ff1466}
.progress-swap{background:linear-gradient(90deg,#ff004444,var(--neon-red));box-shadow:0 0 20px #ff004466}
.progress-yellow{background:linear-gradient(90deg,#ffaa0044,var(--neon-yellow));box-shadow:0 0 20px #ffff0066}
.progress-red{background:linear-gradient(90deg,#ff004444,var(--neon-red));box-shadow:0 0 20px #ff004466}
.io-stats{display:flex;gap:12px;margin-top:8px;font-size:1em;padding:8px;background:var(--bg-dark);border-radius:4px;border:1px solid #1a2332}
.io-stat{display:flex;align-items:center;gap:8px}
.io-stat .value{color:var(--neon-cyan);font-weight:bold;font-family:'Orbitron',monospace;text-shadow:0 0 10px var(--neon-cyan)}
.io-stat.warning .value{color:var(--neon-yellow);text-shadow:0 0 10px var(--neon-yellow)}
.io-stat.danger .value{color:var(--neon-red);text-shadow:0 0 10px var(--neon-red)}
.time-range{display:flex;gap:8px;margin-bottom:15px}
.time-range button{background:var(--bg-panel);color:#888;border:1px solid #2a2a4e;padding:5px 10px;
border-radius:4px;cursor:pointer;font-family:'Orbitron',monospace;font-size:0.9em;letter-spacing:1px;
text-transform:uppercase;transition:all 0.3s}
.time-range button:hover{border-color:var(--neon-cyan);color:var(--neon-cyan);box-shadow:0 0 15px rgba(0,255,242,0.3)}
.time-range button.active{background:transparent;border-color:var(--neon-cyan);color:var(--neon-cyan);
box-shadow:var(--glow-cyan)}
.sparkline-container{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}
.sparkline-box{background:var(--bg-panel);padding:12px;border-radius:4px;border:1px solid #1a2332}
.sparkline-label{font-size:0.9em;color:#888;margin-bottom:8px;font-family:'Orbitron',monospace;letter-spacing:1px}
.sparkline{height:34px;display:flex;align-items:end;gap:2px;width:100%;overflow:hidden}
.sparkline-bar{background:var(--neon-cyan);flex:1 1 0;min-width:0;border-radius:1px 1px 0 0;
transition:height 0.5s cubic-bezier(0.4,0,0.2,1);box-shadow:0 0 5px var(--neon-cyan)}
.sparkline-bar.high{background:var(--neon-yellow);box-shadow:0 0 5px var(--neon-yellow)}
.sparkline-bar.critical{background:var(--neon-red);box-shadow:0 0 5px var(--neon-red);animation:spark-glow 1.2s ease-in-out infinite}
.history-machine{padding:10px 12px;border-radius:6px;margin-bottom:8px;border:1px solid #1a2332;overflow:hidden}
.history-machine.alt-0{background:rgba(0,255,242,0.04)}
.history-machine.alt-1{background:rgba(255,0,255,0.05)}
.max-util{color:var(--neon-green);text-shadow:0 0 8px var(--neon-green);font-family:'Orbitron',monospace}
.cost{color:var(--neon-green);font-family:'Orbitron',monospace;text-shadow:0 0 6px var(--neon-green)}
#energy td,#energy-fleet td{font-size:0.95em}
@keyframes spark-glow{0%,100%{opacity:0.85}50%{opacity:1}}
.gauge-arc{}
.progress-label{display:flex;justify-content:space-between;font-size:1em;color:#888;
font-family:'Rajdhani',sans-serif;letter-spacing:1px}
.stat-row{margin:10px 0}
.queue-badge{background:var(--neon-yellow);color:#000;padding:3px 10px;border-radius:2px;font-size:0.8em;
font-family:'Orbitron',monospace;font-weight:bold;box-shadow:0 0 10px var(--neon-yellow)}
.queue-badge.empty{background:#2a2a4e;color:#666;box-shadow:none}
.gauge-row{display:flex;gap:15px;margin:20px 0;justify-content:center;flex-wrap:wrap}
.gauge{text-align:center}
.gauge-dial{position:relative;margin:0 auto;overflow:hidden;background:#1a1a2e;border-radius:999px 999px 0 0}
.gauge-bg{position:absolute;width:100%;height:200%;border-radius:50%}
.gauge-mask{position:absolute;bottom:0;left:10%;width:80%;height:80%;background:var(--bg-card);border-radius:999px 999px 0 0}
.gauge-needle{position:absolute;bottom:0;left:50%;background:linear-gradient(to top,#fff 0%,#fff 60%,var(--neon-cyan) 100%);transform-origin:bottom center;transition:transform 1.5s cubic-bezier(0.4,0,0.2,1);border-radius:2px}
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
.route-pills{display:flex;gap:5px;flex-wrap:wrap;align-items:center}
.route-pill{font-family:'Orbitron',monospace;font-size:0.7em;letter-spacing:1px;padding:2px 9px;
border-radius:999px;border:1px solid #2a2a4e;background:var(--bg-panel);color:#889}
.route-pill.live{color:var(--neon-green);border-color:var(--neon-green);box-shadow:0 0 8px rgba(57,255,20,0.25)}
.pipe-row{display:flex;align-items:stretch;gap:0;flex-wrap:wrap}
.pipe-stage{background:var(--bg-panel);border:1px solid #1a2332;border-radius:6px;padding:5px 10px;text-align:center;min-width:82px}
.pipe-stage .pn{font-family:'Orbitron',monospace;font-size:1.5em;font-weight:700;color:var(--neon-cyan);text-shadow:0 0 8px var(--neon-cyan)}
.pipe-stage .pl{font-size:0.75em;color:#889;letter-spacing:1px;text-transform:uppercase;margin-top:2px}
.pipe-stage.hot .pn{color:var(--neon-yellow);text-shadow:0 0 8px var(--neon-yellow)}
.pipe-stage.live .pn{color:var(--neon-green);text-shadow:0 0 8px var(--neon-green);animation:glow-pulse 1.5s infinite}
.pipe-stage.bad .pn{color:var(--neon-red);text-shadow:0 0 8px var(--neon-red)}
.pipe-arrow{display:flex;align-items:center;color:var(--neon-magenta);padding:0 8px;font-size:1.3em;text-shadow:0 0 8px var(--neon-magenta)}
.pipe-sub{font-size:0.72em;color:#667;margin-top:2px}
.route-pill.missing{color:var(--neon-red);border-color:var(--neon-red);box-shadow:0 0 8px rgba(255,0,68,0.25);animation:pulse 1.5s infinite}
/* RANGERS row - deliberately a squarer, "field patrol" shape so it reads as a
   different KIND of thing than the round ROUTES pills sitting above it. Every
   pill carries its own age, because "exists" is not "ran". */
.ranger-pills{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.ranger-pill{font-family:'Orbitron',monospace;font-size:0.68em;letter-spacing:0.5px;padding:3px 8px 3px 7px;
border-radius:3px;border:1px solid #2a2a4e;border-left-width:3px;background:var(--bg-panel);color:#889;
display:inline-flex;align-items:center;gap:5px;cursor:help;transition:transform 0.12s}
.ranger-pill:hover{transform:translateY(-1px)}
.ranger-pill .rage{font-size:0.85em;opacity:0.65;font-family:monospace;letter-spacing:0}
.ranger-pill.ok{color:var(--neon-green);border-color:#2a4a2a;border-left-color:var(--neon-green)}
.ranger-pill.alert{color:var(--neon-yellow);border-color:#4a4520;border-left-color:var(--neon-yellow);
box-shadow:0 0 8px rgba(255,234,0,0.22)}
.ranger-pill.dead{color:var(--neon-red);border-color:var(--neon-red);border-left-color:var(--neon-red);
box-shadow:0 0 10px rgba(255,0,68,0.35);animation:pulse 1.5s infinite}
/* Gray, never green: a ranger we cannot prove ran must not look reassuring. */
.ranger-pill.unknown{color:#667;border-color:#26263a;border-left-color:#44445e;font-style:italic}
.ranger-pill.disarmed{color:#8899aa;border-color:#26263a;border-left-color:#4a5a6a}
</style></head>
<body><h1>FLEET MONITOR</h1>

<div id="topbar" style="margin:4px 0 8px 0;display:flex;align-items:center;gap:12px;flex-wrap:wrap"><div id="ci-queue-body" style="font-size:0.9em"><p style="color:#667;margin:0">Loading pipeline...</p></div><div id="route-health-body" style="margin-left:auto"></div></div>

<div id="rangers-row" style="margin:0 0 10px 0"></div>

<div class=card id=monitors><p>Loading...</p></div>
<div class=card id=fleet-hosts style="padding:12px 15px;margin:12px 0">
<div id=fleet-row class=fleet-row><p style="grid-column:1/-1;color:#667;margin:0">Scanning fleet hosts...</p></div>
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
GET  /api/health  - Health check</pre>
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


<script>
// Served under a path prefix (tailscale serve --set-path /fleet) as well as at
// the root. Absolute /api/... URLs break under a prefix: the page loads but every
// API call resolves to the ORIGIN root and 404s, which surfaces as "No rangers
// registered yet" and "Object.entries requires input not null". Derive the prefix
// from where this document actually lives so both cases work.
const API_BASE = (function () {
  var p = window.location.pathname.replace(/\/+$/, '');
  return p === '' ? '' : p;
})();
function apiUrl(path) { return API_BASE + path; }

const icons = {gandalf: "🧙", frodo: "🧝", pippin: "🍎", shadowfax: "🐴"};
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

// Connection-type badge: 🔌 = wired ethernet, 📶 = wifi (from config "net" field)
function netBadge(net) {
    if (net === 'eth') return ' <span class="net-badge" title="Wired (Ethernet)">🔌</span>';
    if (net === 'wifi') return ' <span class="net-badge" title="Wi-Fi">📶</span>';
    return '';
}

function fleetTileHtml(name, n) {
    const icon = fleetIcons[name] || '🖥️';
    let html = '<div class="fleet-tile' + (n.online ? '' : ' off') + '" id="fleet-tile-' + name + '">';
    html += '<div class="ft-name">' + icon + ' ' + name + netBadge(n.net) + '</div>';
    if (n.online) {
        html += '<div class="ft-stat"><span>CPU</span><b class="' + pctClass(n.cpu) + '" id="ft-cpu-' + name + '">' + n.cpu + '%</b></div>';
        html += '<div class="ft-stat"><span>TEMP</span><b class="' + tempClass(n.temp_c) + '" id="ft-temp-' + name + '">' + n.temp_c + '°C</b></div>';
        html += '<div class="ft-stat"><span>RAM</span><b class="' + pctClass(n.ram_pct) + '" id="ft-ram-' + name + '">' + n.ram_used_gb + '/' + n.ram_total_gb + 'G</b></div>';
    } else {
        html += '<div class="ft-stat"><span style="color:var(--neon-red)">OFFLINE</span><b></b></div>';
        html += '<div class="ft-stat"><span>&nbsp;</span><b></b></div>';
        html += '<div class="ft-stat"><span>&nbsp;</span><b></b></div>';
    }
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
    fetch(apiUrl('/api/fleet')).then(r => r.json()).then(data => {
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
                if (tile) tile.dataset.shape = n.online + ':' + n.can_wake;
            }
            fleetInitialized = true;
            return;
        }
        // UPDATE PATH: only touch a tile when something about it changed
        // (online/offline flips the button set, so those still get rebuilt;
        // pure stat ticks update text in place).
        for (const [name, n] of Object.entries(data)) {
            const tile = document.getElementById('fleet-tile-' + name);
            const key = n.online + ':' + n.can_wake;
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
    fetch(apiUrl('/api/fleet_power'), {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({node: node, action: action})
    }).then(r => r.json()).then(d => {
        if (d.error) { alert('Failed: ' + d.error); return; }
        if (action === 'wake') alert('Magic packets sent to ' + node + ' — it should appear within ~90s.');
    }).catch(e => alert('Request failed: ' + e));
}

function ciQueueNumClass(n) { return n === 0 ? 'zero' : (n <= 3 ? 'warn' : 'hot'); }

function refreshCiQueue() {
    fetch(apiUrl('/api/pipeline')).then(r => r.json()).then(d => {
        const el = document.getElementById('ci-queue-body');
        if (!el) return;
        if (!d.available) {
            el.innerHTML = '<p class="glance-unavailable">Pipeline signal unavailable right now (token fetch or GitHub API failed) — not necessarily a real outage, just a stale read.</p>';
            return;
        }
        const n = v => (v === null || v === undefined) ? '—' : v;
        const sparkChars = ['▁','▂','▃','▄','▅','▆','▇'];
        const sparkline = arr => {
            if (!arr || !arr.length) return '';
            const mx = Math.max.apply(null, arr.concat([1]));
            return '<div class="pipe-sub" style="letter-spacing:1px;color:var(--neon-cyan);opacity:0.8" title="7-day trend (CT days)">'
                + arr.map(v => sparkChars[Math.min(6, Math.round(v / mx * 6))]).join('') + '</div>';
        };
        const stage = (num, label, cls, sub) =>
            '<div class="pipe-stage ' + (cls || '') + '"><div class="pn">' + n(num) + '</div>' +
            '<div class="pl">' + label + '</div>' + (sub ? '<div class="pipe-sub">' + sub + '</div>' : '') + '</div>';
        const arrow = '<div class="pipe-arrow">▶</div>';
        const ciCls = (d.ci_queued > 5) ? 'hot' : (d.ci_running > 0 ? 'live' : '');
        const depLive = d.deploys_in_flight > 0;
        const lastDep = d.last_deploy_at
            ? new Date(d.last_deploy_at).toLocaleString('en-US', {timeZone: 'America/Chicago',
                month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit'})
            : '—';
        const depSub = depLive ? ('🚀 ' + d.deploys_in_flight + ' in flight · ' + n(d.deploy_started_min) + 'm')
                               : (d.deploys_failed_today > 0 ? ('⚠ ' + d.deploys_failed_today + ' failed') : 'all landed');
        el.innerHTML = '<div class="pipe-row"><div style="display:flex;align-items:center;font-family:Orbitron,monospace;color:var(--neon-cyan);font-size:0.8em;letter-spacing:1px;padding-right:8px">🏭 SHIPPING</div>' +
            stage(d.issues_open, 'Issues open') + arrow +
            stage(d.prs_open, 'PRs open') + arrow +
            stage(n(d.ci_queued) + '/' + n(d.ci_running), 'CI q/run', ciCls) + arrow +
            stage(d.merged_today, 'Merged today', d.merged_today > 0 ? 'live' : '', sparkline(d.merged_spark)) + arrow +
            stage(d.deploys_ok_today, 'Deployed today', depLive ? 'live' : (d.deploys_failed_today > 0 ? 'bad' : ''), depSub + sparkline(d.deploys_spark)) + arrow +
            stage(lastDep, 'Last deploy (CT)', '', d.last_deploy_sha ? ('@ ' + d.last_deploy_sha) : '') +
            '</div>';
    }).catch(() => {
        const el = document.getElementById('ci-queue-body');
        if (el) el.innerHTML = '<p class="glance-unavailable">Pipeline check failed to load.</p>';
    });
}

function refreshRouteHealth() {
    fetch(apiUrl('/api/model_routes')).then(r => r.json()).then(d => {
        const el = document.getElementById('route-health-body');
        if (!el) return;
        if (!d.available) {
            el.innerHTML = '<p class="glance-unavailable">Gateway unreachable right now (key fetch or gateway ping failed) — not necessarily a real outage, just a stale read.</p>';
            return;
        }
        const missing = d.routes.filter(r => !r.live);
        let html = '<div class="route-pills"><span style="color:#667;font-size:0.75em;letter-spacing:1px">🛰️ ROUTES ' +
            (d.routes.length - missing.length) + '/' + d.routes.length + '</span>';
        d.routes.forEach(r => {
            html += '<span class="route-pill ' + (r.live ? 'live' : 'missing') + '">' + (r.live ? '● ' : '✕ ') + r.name + '</span>';
        });
        html += '</div>';
        if (missing.length) {
            html += '<div style="color:var(--neon-red);font-size:0.8em;text-align:right">' + missing.length + ' MISSING: ' + missing.map(r => r.name).join(', ') + '</div>';
        }
        el.innerHTML = html;
    }).catch(() => {
        const el = document.getElementById('route-health-body');
        if (el) el.innerHTML = '<p class="glance-unavailable">Model route check failed to load.</p>';
    });
}

// RANGERS row. Colour + the age printed in each pill carry the status, so the
// row answers "are my patrols actually running" at a glance rather than just
// naming them. Order is worst-first: anything broken sorts to the left where
// the eye lands, so a dead ranger can never hide at the end of a long row.
const RANGER_ORDER = { dead: 0, alert: 1, unknown: 2, ok: 3 };
const RANGER_GLYPH = { ok: '◆', alert: '▲', dead: '✕', unknown: '?' };
function rangerAge(r) {
    if (r.age_min === null || r.age_min === undefined) return '';
    const m = r.age_min;
    if (m < 90) return Math.max(0, Math.round(m)) + 'm';
    if (m < 48 * 60) return (m / 60).toFixed(1) + 'h';
    return (m / 1440).toFixed(1) + 'd';
}
function refreshRangers() {
    fetch(apiUrl('/api/rangers')).then(r => r.json()).then(d => {
        const el = document.getElementById('rangers-row');
        if (!el) return;
        if (!d.rangers || !d.rangers.length) {
            el.innerHTML = '<p class="glance-unavailable">No rangers registered yet.</p>';
            return;
        }
        const c = d.counts || {};
        const broken = (c.dead || 0), alerting = (c.alert || 0), unprovable = (c.unknown || 0);
        // Headline colour reflects the WORST state present, not the average.
        let headColor = 'var(--neon-green)';
        if (broken) headColor = 'var(--neon-red)';
        else if (alerting) headColor = 'var(--neon-yellow)';
        else if (unprovable) headColor = '#889';

        let summary = (c.ok || 0) + '/' + d.total + ' ON PATROL';
        if (broken) summary += ' · ' + broken + ' STOPPED';
        if (alerting) summary += ' · ' + alerting + ' FLAGGING';
        if (unprovable) summary += ' · ' + unprovable + ' UNPROVABLE';

        let html = '<div class="ranger-pills"><span style="color:' + headColor +
            ';font-size:0.75em;letter-spacing:1px;font-family:Orbitron,monospace">⚔️ RANGERS ' +
            summary + '</span>';

        const sorted = d.rangers.slice().sort((a, b) =>
            (RANGER_ORDER[a.state] - RANGER_ORDER[b.state]) || a.display.localeCompare(b.display));
        sorted.forEach(r => {
            const age = rangerAge(r);
            // NOTE: newline escapes below are DOUBLE-backslashed on purpose.
            // This JS lives inside a non-raw Python string, so a single-escaped
            // newline is consumed by Python and emitted as a real line break,
            // leaving an unterminated JS string that kills the ENTIRE dashboard.
            const tip = r.display + ' — ' + r.watches +
                '\\nSchedule: ' + (r.schedule || 'unknown') +
                '\\nLast run: ' + (r.last_run || 'never') +
                '\\n' + r.detail +
                (r.armed ? '\\nARMED: can act on its own.' : '\\nReports only - takes no action.');
            html += '<span class="ranger-pill ' + r.state + '" title="' +
                tip.replace(/"/g, '&quot;') + '">' +
                '<span>' + (RANGER_GLYPH[r.state] || '') + '</span>' +
                '<span>' + r.display + '</span>' +
                (age ? '<span class="rage">' + age + '</span>' : '') +
                (r.armed ? '<span class="rage" style="opacity:0.5">⚡</span>' : '') +
                '</span>';
        });
        html += '</div>';

        // cron is the shared dependency behind most patrols. If it is down,
        // every heartbeat below is frozen and the row above is lying by omission.
        if (d.cron_warning) {
            html += '<div style="color:var(--neon-red);font-size:0.78em;margin-top:4px;animation:pulse 1.5s infinite">' +
                '⚠ ' + d.cron_warning + '</div>';
        }
        const bad = sorted.filter(r => r.state === 'dead' || r.state === 'alert');
        if (bad.length) {
            html += '<div style="font-size:0.76em;color:#99a;margin-top:4px">' +
                bad.map(r => '<b style="color:' + (r.state === 'dead' ? 'var(--neon-red)' : 'var(--neon-yellow)') +
                    '">' + r.display + '</b>: ' + r.detail).join(' &nbsp;·&nbsp; ') + '</div>';
        }
        el.innerHTML = html;
    }).catch(() => {
        const el = document.getElementById('rangers-row');
        if (el) el.innerHTML = '<p class="glance-unavailable">Ranger check failed to load.</p>';
    });
}

function progressBar(percent, cls, id) {
    let barClass = cls;
    if (percent > 90) barClass = 'progress-red';
    else if (percent > 70) barClass = 'progress-yellow';
    return '<div class="progress-bar"><div id="' + id + '" class="progress-fill ' + barClass + '" data-percent="' + percent + '" style="width:' + percent + '%"></div></div>';
}

// Segmented VRAM bar (macOS "Manage Storage" idiom: one bar split into
// colored segments + a dot legend underneath naming each segment + share)
const SEG_PALETTE = ['var(--neon-cyan)', 'var(--neon-magenta)', 'var(--neon-yellow)', 'var(--neon-green)', 'var(--neon-orange)'];
function segColor(i) { return SEG_PALETTE[i % SEG_PALETTE.length]; }

function loadedModelsInline(lm) {
    if (!lm || !lm.available || !lm.segments || !lm.segments.length) return '';
    const sorted = lm.segments.slice().sort((a, b) => (b.mb || 0) - (a.mb || 0));
    const top = sorted.find(s => s.route) || sorted[0];
    const name = top.route ? (top.label + ' [' + top.route + ']') : top.label;
    return ' <span style="color:#667;font-size:0.85em">&middot; loaded: ' + name + (lm.live_breakdown ? '' : ' (nominal)') + '</span>';
}

// Full VRAM stat-row: label+inline-loaded-model, segmented (or plain) bar, dot legend.
function vramBlockHtml(info, name) {
    const percent = info.gpu.vram_percent;
    let barClass = 'progress-vram';
    if (percent > 90) barClass = 'progress-red';
    else if (percent > 70) barClass = 'progress-yellow';
    const lm = info.loaded_models;

    let html = '<div class="progress-label"><span>🎮 VRAM</span><span><span id="vram-label-' + name + '">' +
        info.gpu.vram_used_gb + ' / ' + info.gpu.vram_total_gb + ' GB</span><span id="vram-models-' + name + '">' +
        loadedModelsInline(lm) + '</span></span></div>';

    if (lm && lm.available && lm.live_breakdown && lm.segments.length) {
        const totalSeg = lm.segments.reduce((s, x) => s + (x.mb || 0), 0) || 1;
        const segs = lm.segments.map((s, i) =>
            '<div class="progress-seg" style="width:' + (s.mb / totalSeg * 100).toFixed(2) + '%;background:' + segColor(i) + '"></div>'
        ).join('');
        html += '<div class="progress-bar"><div id="vram-' + name + '" class="progress-fill segmented ' + barClass +
            '" data-percent="' + percent + '" style="width:' + percent + '%">' + segs + '</div></div>';
        html += '<div class="bar-legend">' + lm.segments.map((s, i) => {
            const pctOfGpu = lm.total_mb ? (s.mb / lm.total_mb * 100).toFixed(0) : '?';
            const label = s.route ? (s.label + ' [' + s.route + ']') : s.label;
            return '<span class="bar-legend-item"><span class="bar-legend-dot" style="background:' + segColor(i) + '"></span>' + label + ' ' + pctOfGpu + '%</span>';
        }).join('') + '</div>';
    } else {
        html += progressBar(percent, 'progress-vram', 'vram-' + name);
        if (lm && lm.available && !lm.live_breakdown && lm.segments.length) {
            html += '<div class="bar-legend"><span class="bar-legend-item" style="color:#889">⚠ live VRAM breakdown unavailable on this host (nvidia-smi unreachable) - loaded: ' +
                lm.segments.map(s => s.route ? (s.label + ' [' + s.route + ']') : s.label).join(', ') + '</span></div>';
        }
    }
    return html;
}

// "SERVES" route-label pills for a GPU machine (route -> machine map is
// authoritative in ~/.claude/rules/fleet.md). Highlights the route(s)
// currently loaded; gandalf shows a note that it's one-at-a-time.
function routeLabelsHtml(info) {
    const rl = info.route_labels;
    if (!rl || !rl.routes || !rl.routes.length) return '';
    const loaded = new Set(((info.loaded_models && info.loaded_models.segments) || []).filter(s => s.route).map(s => s.route));
    let html = '<div class="route-pills" style="margin:2px 0 8px 0"><span style="color:#667;font-size:0.7em;letter-spacing:1px">SERVES</span>';
    rl.routes.forEach(r => {
        html += '<span class="route-pill' + (loaded.has(r) ? ' live' : '') + '">' + (loaded.has(r) ? '&#9679; ' : '') + r + '</span>';
    });
    html += '</div>';
    if (rl.note) html += '<div style="color:#667;font-size:0.72em;margin:-4px 0 8px 0">' + rl.note + '</div>';
    return html;
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

function renderGauge(value, min, max, label, unit, showLimit, size, reverseColors, id) {
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

    return '<div class="gauge" id="' + gaugeId + '" style="width:' + w + 'px" data-min="' + min + '" data-max="' + max + '">' +
        '<div class="gauge-dial" style="width:' + w + 'px;height:' + h + 'px">' +
        '<div class="gauge-bg" style="background:' + gradient + '"></div>' +
        '<div class="gauge-mask"></div>' +
        '<div class="gauge-needle" style="width:' + needleW + 'px;height:' + needleH + 'px;margin-left:' + (-needleW/2) + 'px;transform:rotate(' + angle + 'deg);background:linear-gradient(to top,#fff 0%,#fff 60%,' + color + ' 100%)"></div>' +
        '<div class="gauge-center" style="width:' + centerSize + 'px;height:' + centerSize + 'px;margin-left:' + (-centerSize/2) + 'px;border-color:' + color + '"></div>' +
        '</div>' +
        '<div class="gauge-label">' + label + '</div>' +
        '<div class="gauge-value" style="color:' + color + ';text-shadow:0 0 10px ' + color + '">' + displayValue + '</div></div>';
}

function renderSwapGauge(value, max, size, id) {
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

    return '<div class="gauge" id="' + gaugeId + '" style="width:' + w + 'px" data-min="0" data-max="' + max + '">' +
        '<div class="gauge-dial" style="width:' + w + 'px;height:' + h + 'px">' +
        '<div class="gauge-bg" style="background:' + gradient + '"></div>' +
        '<div class="gauge-mask"></div>' +
        '<div class="gauge-needle" style="width:' + needleW + 'px;height:' + needleH + 'px;margin-left:' + (-needleW/2) + 'px;transform:rotate(' + angle + 'deg);background:linear-gradient(to top,#fff 0%,#fff 60%,' + color + ' 100%)"></div>' +
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
        fetch(apiUrl('/api/power_limit'), {
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
        fetch(apiUrl('/api/clear_swap'), {
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
        fetch(apiUrl('/api/power'), {
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
        fetch(apiUrl('/api/power'), {
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
        for (const [name, info] of Object.entries(data.targets)) { lastTargetsOnline[name] = info.online; }
        updateFleetSummary();
        // If not initialized, build the full HTML
        if (!initialized) {
            let html = '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:20px">';

            for (const [name, info] of Object.entries(data.targets)) {
                const icon = icons[name] || "🖥️";
                const status = info.online ? '<span class="online">ONLINE</span>' : '<span class="offline">OFFLINE</span>';

                const isMac = info.os === 'mac';

                html += '<div class="gpu-card" id="card-' + name + '">';
                html += '<div class="gpu-header"><span class="gpu-name">' + icon + ' ' + name + netBadge(info.net) + '</span>';
                html += '<div style="display:flex;align-items:center;gap:10px"><span id="status-' + name + '">' + status + '</span>';
                if (!isMac) {
                    html += '<div class="machine-power">';
                    html += '<button class="reboot-btn" data-target="' + name + '" title="Reboot">&#x21bb;</button>';
                    html += '<button class="shutdown-btn" data-target="' + name + '" title="Shut Down">&#x23FB;</button>';
                    html += '</div>';
                }
                html += '</div></div>';

                const maxUtil = (info.max_util_today === null || info.max_util_today === undefined) ? '--' : info.max_util_today;
                html += '<div class="queue-info">';
                html += '<div class="max-util" id="maxutil-' + name + '">' + maxUtil + '% max utilization today</div>';
                html += '</div>';

                html += '<div id="route-pills-block-' + name + '">' + routeLabelsHtml(info) + '</div>';

                if (info.online && isMac) {
                    html += '<div class="gauge-row">';
                    html += renderGauge(info.gpu_util, 0, 100, "GPU UTIL", "%", false, 1.5, true, 'util-' + name);
                    html += renderGauge(info.cpu_percent, 0, 100, "CPU", "%", false, 1.5, false, 'cpu-' + name);
                    html += '</div>';
                    html += '<div class="gauge-row">';
                    const swapPct = info.swap ? info.swap.percent : 0;
                    html += renderSwapGauge(swapPct, 100, 1, 'swap-' + name);
                    html += '</div>';

                    if (info.ram) {
                        html += '<div class="stat-row">';
                        html += '<div class="progress-label"><span>🧠 Memory</span><span id="ram-label-' + name + '">' + info.ram.used_gb + ' / ' + info.ram.total_gb + ' GB</span></div>';
                        html += progressBar(info.ram.percent, "progress-ram", 'ram-' + name);
                        html += '</div>';
                    }

                    if (info.disk) {
                        const diskUsed = info.disk.total_gb >= 1000 ? (info.disk.used_gb / 1024).toFixed(1) + ' TB' : info.disk.used_gb + ' GB';
                        const diskTotal = info.disk.total_gb >= 1000 ? (info.disk.total_gb / 1024).toFixed(1) + ' TB' : info.disk.total_gb + ' GB';
                        html += '<div class="stat-row">';
                        html += '<div class="progress-label"><span>💿 Disk</span><span id="disk-label-' + name + '">' + diskUsed + ' / ' + diskTotal + '</span></div>';
                        html += progressBar(info.disk.percent, "progress-disk", 'disk-' + name);
                        html += '</div>';
                    }

                    html += '<div style="margin-top:10px;font-size:0.85em;color:#888">Apple M1 Max · 64GB Unified · 32-core GPU</div>';
                } else if (info.online && info.gpu) {
                    html += '<div class="gauge-row">';
                    html += renderGauge(info.gpu_util, 0, 100, "GPU UTIL", "%", false, 1.5, true, 'util-' + name);
                    html += renderGauge(info.gpu_watts, 0, info.gpu_power_max, "POWER", "W", true, 1.5, false, 'power-' + name);
                    html += '</div>';
                    html += '<div class="gauge-row">';
                    html += renderGauge(info.gpu_temp, 24, 90, "TEMP", "°C", false, 1, false, 'temp-' + name);
                    html += renderGauge(info.cpu_percent, 0, 100, "CPU", "%", false, 1, false, 'cpu-' + name);
                    const swapPct = info.swap ? info.swap.percent : 0;
                    html += renderSwapGauge(swapPct, 100, 1, 'swap-' + name);
                    html += '</div>';
                    html += '<button class="power-btn" data-target="' + name + '" data-current="' + info.gpu_power_limit + '" data-max="' + info.gpu_power_max + '">⚡ Set Power Limit</button>';
                    html += '<button class="swap-btn" data-target="' + name + '">🧹 Clear Swap</button>';

                    html += '<div class="stat-row" id="vram-block-' + name + '">' + vramBlockHtml(info, name) + '</div>';

                    if (info.ram) {
                        html += '<div class="stat-row">';
                        html += '<div class="progress-label"><span>💾 RAM</span><span id="ram-label-' + name + '">' + info.ram.used_gb + ' / ' + info.ram.total_gb + ' GB</span></div>';
                        html += progressBar(info.ram.percent, "progress-ram", 'ram-' + name);
                        html += '</div>';
                    }

                    if (info.disk) {
                        const diskUsed = info.disk.total_gb >= 1000 ? (info.disk.used_gb / 1024).toFixed(1) + ' TB' : info.disk.used_gb + ' GB';
                        const diskTotal = info.disk.total_gb >= 1000 ? (info.disk.total_gb / 1024).toFixed(1) + ' TB' : info.disk.total_gb + ' GB';
                        html += '<div class="stat-row">';
                        html += '<div class="progress-label"><span>💿 Disk</span><span id="disk-label-' + name + '">' + diskUsed + ' / ' + diskTotal + '</span></div>';
                        html += progressBar(info.disk.percent, "progress-disk", 'disk-' + name);
                        html += '</div>';
                    }

                // I/O Stats
                    html += '<div class="io-stats">';
                    if (info.disk_io) {
                        html += '<div class="io-stat" id="disk-io-' + name + '">📀 <span class="value">' + info.disk_io.read_mbps + '/' + info.disk_io.write_mbps + '</span> MB/s</div>';
                    }
                    if (info.net_io) {
                        html += '<div class="io-stat" id="net-io-' + name + '">🌐 <span class="value">' + info.net_io.rx_mbps + '/' + info.net_io.tx_mbps + '</span> MB/s</div>';
                    }
                    html += '</div>';

                    // cuda_version already carries its own qualifier (e.g. "torch cu12.8")
                    // — do NOT prepend "CUDA" here. It is the PyTorch build tag, not the
                    // box's toolkit level, and labelling it plain "CUDA" led Ben to
                    // conclude frodo was behind gandalf when it is not. See the
                    // cuda_version comment in get_target_status().
                    html += '<div style="margin-top:10px;font-size:0.85em;color:#888">' + info.gpu.name + (info.gpu.cuda_version ? ' (' + info.gpu.cuda_version + ')' : '') + '</div>';
                }

                html += '</div>';
            }
            html += '</div>';
            document.getElementById("monitors").innerHTML = html;
            initialized = true;

        } else {
            // UPDATE PATH: Just update values in place
            for (const [name, info] of Object.entries(data.targets)) {
                // Update max utilization today
                const maxUtilEl = document.getElementById('maxutil-' + name);
                if (maxUtilEl) {
                    const maxUtil = (info.max_util_today === null || info.max_util_today === undefined) ? '--' : info.max_util_today;
                    maxUtilEl.textContent = maxUtil + '% max utilization today';
                }

                const routePillsEl = document.getElementById('route-pills-block-' + name);
                if (routePillsEl) {
                    const newPillsHtml = routeLabelsHtml(info);
                    if (routePillsEl.dataset.lastVal !== newPillsHtml) {
                        routePillsEl.dataset.lastVal = newPillsHtml;
                        routePillsEl.innerHTML = newPillsHtml;
                    }
                }

                if (info.online && info.os === 'mac') {
                    updateGauge('util-' + name, info.gpu_util, 0, 100, '%', false, getUtilColor);
                    updateGauge('cpu-' + name, info.cpu_percent, 0, 100, '%', false, getNormalColor);
                    const swapPct = info.swap ? info.swap.percent : 0;
                    updateGauge('swap-' + name, swapPct, 0, 100, '%', false, getSwapColor);

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
                    updateGauge('power-' + name, info.gpu_watts, 0, info.gpu_power_max, 'W', true, getNormalColor);
                    updateGauge('temp-' + name, info.gpu_temp, 24, 90, '°C', false, getNormalColor);
                    updateGauge('cpu-' + name, info.cpu_percent, 0, 100, '%', false, getNormalColor);
                    const swapPct = info.swap ? info.swap.percent : 0;
                    updateGauge('swap-' + name, swapPct, 0, 100, '%', false, getSwapColor);

                    // VRAM block: regenerated wholesale (not just width) since loaded-model
                    // segments/legend can change between polls, not just the percent.
                    const vramBlock = document.getElementById('vram-block-' + name);
                    if (vramBlock) {
                        const newHtml = vramBlockHtml(info, name);
                        if (vramBlock.dataset.lastVal !== newHtml) {
                            vramBlock.dataset.lastVal = newHtml;
                            vramBlock.innerHTML = newHtml;
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
    fetch(apiUrl('/api/history?range=') + currentRange).then(r => r.json()).then(data => {
        let html = '';

        let idx = 0;
        for (const [target, points] of Object.entries(data.data)) {
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
    fetch(apiUrl('/api/energy')).then(r => r.json()).then(data => {
        const order = ['gandalf', 'frodo', 'pippin'];
        const names = order.filter(n => n in data.by_machine)
            .concat(Object.keys(data.by_machine).filter(n => !order.includes(n)));

        // Per-machine table
        let rows = '<table><tr><th>Machine</th><th>Today</th><th>This Week</th><th>This Month</th></tr>';
        names.forEach(name => {
            const m = data.by_machine[name];
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

        // Fleet aggregate
        const f = data.fleet;
        let fleet = '<table><tr><th>Period</th><th>GPU Energy</th><th>Cost</th></tr>';
        [['Today', 'day'], ['This Week', 'week'], ['This Month', 'month']].forEach(([lbl, k]) => {
            fleet += '<tr><td><b>' + lbl + '</b></td><td><b style="color:var(--neon-cyan)">'
                  + f[k].kwh.toFixed(2) + '</b> kWh</td><td><b class="cost">$' + f[k].cost.toFixed(2) + '</b></td></tr>';
        });
        fleet += '</table>';
        fleet += '<p style="margin-top:10px;color:#666;font-size:0.8em">Base ' + data.base_per_kwh.toFixed(6)
              + ' $/kWh + PEC time-of-use. ' + data.note + '</p>';
        document.getElementById('energy-fleet-body').innerHTML = fleet;
    }).catch(err => {
        document.getElementById('energy-by-machine').innerHTML = '<p style="color:#d94a4a">Error loading energy: ' + err + '</p>';
    });
}

// --- Polling control: pause everything when the tab isn't visible, resume ---
// --- with an immediate refresh when it becomes visible again.              ---
let statusTimer = null, fleetTimer = null, historyTimer = null, energyTimer = null, ciQueueTimer = null, routeHealthTimer = null, rangerTimer = null;

function startPolling() {
    if (!statusTimer) statusTimer = setInterval(refresh, 12000);     // queue/GPU state: 12s
    if (!fleetTimer) fleetTimer = setInterval(refreshFleet, 45000);  // ssh fleet stats: 45s
    if (!historyTimer) historyTimer = setInterval(refreshHistory, 60000);
    if (!energyTimer) energyTimer = setInterval(refreshEnergy, 60000);
    if (!ciQueueTimer) ciQueueTimer = setInterval(refreshCiQueue, 45000);        // matches backend cache TTL
    if (!routeHealthTimer) routeHealthTimer = setInterval(refreshRouteHealth, 30000);
    if (!rangerTimer) rangerTimer = setInterval(refreshRangers, 30000);
}

function stopPolling() {
    clearInterval(statusTimer); statusTimer = null;
    clearInterval(fleetTimer); fleetTimer = null;
    clearInterval(historyTimer); historyTimer = null;
    clearInterval(energyTimer); energyTimer = null;
    clearInterval(ciQueueTimer); ciQueueTimer = null;
    clearInterval(routeHealthTimer); routeHealthTimer = null;
    clearInterval(rangerTimer); rangerTimer = null;
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
        refreshRouteHealth();
        refreshRangers();
        startPolling();
    }
});

refresh();
refreshHistory();
refreshEnergy();
refreshFleet();
refreshCiQueue();
refreshRouteHealth();
refreshRangers();
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

    conn = sqlite3.connect(CONFIG["db_path"])
    by_machine = {}
    fleet = {w: {"kwh": 0.0, "cost": 0.0} for w in windows}
    for name, cfg in CONFIG["targets"].items():
        # Macs report no wattage (powermetrics needs sudo), so they aren't metered
        if cfg.get("os") == "mac":
            by_machine[name] = {"metered": False}
            continue
        by_machine[name] = {"metered": True}
        for wname, wstart in windows.items():
            ec = energy_cost_since(conn, name, wstart)
            by_machine[name][wname] = ec
            fleet[wname]["kwh"] += ec["kwh"]
            fleet[wname]["cost"] += ec["cost"]
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
        return jsonify({"error": "Invalid range. Use: hour, day, week, month"}), 400

    conn = sqlite3.connect(CONFIG["db_path"])
    conn.row_factory = sqlite3.Row

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

    cursor = conn.execute(query, params)
    rows = cursor.fetchall()
    conn.close()

    # Group and aggregate data
    result = {}
    for row in rows:
        target = row["target"]
        if target not in result:
            result[target] = []

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
                ts = datetime.fromisoformat(point["timestamp"])
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

    # Only surface configured targets, in CONFIG order (gandalf, frodo, pippin)
    result = {t: result[t] for t in CONFIG["targets"] if t in result}

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

    # Start ComfyUI watchdog (auto-restart if down) -- OFF by default, see
    # COMFYUI_WATCHDOG_ENABLED. It used to silently undo Ben's explicit
    # instruction to keep ComfyUI stopped.
    if COMFYUI_WATCHDOG_ENABLED:
        watchdog_thread = threading.Thread(target=comfyui_watchdog_loop, daemon=True)
        watchdog_thread.start()
    else:
        logger.info("ComfyUI watchdog DISABLED (COMFYUI_WATCHDOG_ENABLED=False) "
                    "- ComfyUI will not be auto-restarted")

    logger.info("")
    logger.info("=" * 60)
    logger.info("  SHADOWFAX FLEET MONITOR")
    logger.info("=" * 60)
    logger.info(f"  Listening: http://0.0.0.0:5000")
    logger.info(f"  Dashboard: http://shadowfax.local")
    logger.info(f"  Targets:   {', '.join(CONFIG['targets'].keys())}")
    logger.info(f"  Notifications: Pushover {'enabled' if PUSHOVER_CONFIG['enabled'] else 'disabled'}")
    logger.info("=" * 60)
    logger.info("")
    app.run(host="0.0.0.0", port=5000)
