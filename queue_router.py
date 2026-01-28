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

# Set up logging with clear formatting
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("QueueRouter")

app = Flask(__name__)
CORS(app)

# Configuration
CONFIG = {
    "targets": {
        "gandalf": {
            "url": "http://192.168.1.122:8188",
            "ssh_host": "gandalf.local",
            "ssh_user": "ben",
            "vram_gb": 96,
            "gpu_power_limit": 450,
            "gpu_power_max": 600,
            "priority": 1,
            "capabilities": ["video", "flux", "sdxl", "sd15", "llm"],
            "disk_path": "/workspace"
        },
        "frodo": {
            "url": "http://frodo.local:8188",
            "ssh_host": "frodo.local",
            "ssh_user": "ben",
            "vram_gb": 16,
            "gpu_power_limit": 360,
            "gpu_power_max": 400,
            "priority": 2,
            "capabilities": ["sdxl", "sd15", "flux-schnell"]
        }
    },
    "db_path": "/var/lib/queue-router/jobs.db",
    "vram_thresholds": {
        "video": 48,
        "flux": 24,
        "sdxl": 12,
        "sd15": 6
    }
}

# Track active jobs for completion monitoring
active_jobs = {}  # {prompt_id: {"job_id": ..., "target": ..., "start_time": ...}}

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

def check_job_completion():
    """Background thread to check for completed jobs."""
    while True:
        try:
            jobs_to_remove = []

            for prompt_id, job_info in list(active_jobs.items()):
                target = job_info["target"]
                target_url = CONFIG["targets"].get(target, {}).get("url")

                if not target_url:
                    continue

                try:
                    # Check ComfyUI history for this prompt
                    response = requests.get(f"{target_url}/history/{prompt_id}", timeout=5)
                    if response.ok:
                        history = response.json()
                        if prompt_id in history:
                            # Job completed!
                            elapsed = datetime.now() - job_info["start_time"]
                            duration = f"{int(elapsed.total_seconds() // 60)}m{int(elapsed.total_seconds() % 60)}s"

                            # Update database
                            conn = sqlite3.connect(CONFIG["db_path"])
                            conn.execute(
                                "UPDATE jobs SET status = 'completed', completed_at = ? WHERE id = ?",
                                (datetime.now().isoformat(), job_info["job_id"])
                            )
                            conn.commit()
                            conn.close()

                            # Send notification
                            send_notification(
                                "🎬 Render Complete!",
                                f"Job: {job_info['job_id']}\nGPU: {target.capitalize()}\nTime: {duration}"
                            )

                            logger.info(f"  ✅ Job {job_info['job_id']} completed on {target} in {duration}")
                            jobs_to_remove.append(prompt_id)

                except Exception as e:
                    logger.debug(f"  Error checking job {prompt_id}: {e}")

            # Remove completed jobs from tracking
            for prompt_id in jobs_to_remove:
                active_jobs.pop(prompt_id, None)

        except Exception as e:
            logger.error(f"Job completion checker error: {e}")

        time.sleep(5)  # Check every 5 seconds

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

# SSH connection cache
ssh_clients = {}

# Disk usage cache (updates every 5 minutes)
disk_cache = {}  # {host: {"data": {...}, "updated_at": datetime}}
DISK_CACHE_TTL = 300  # 5 minutes

# Historical metrics collection interval
METRICS_INTERVAL = 60  # Collect every 60 seconds

def get_ssh_client(host, user):
    """Get or create cached SSH client for a host."""
    key = f"{user}@{host}"
    if key in ssh_clients:
        client = ssh_clients[key]
        if client.get_transport() and client.get_transport().is_active():
            return client

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host, username=user, timeout=5)
    ssh_clients[key] = client
    return client

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

def get_target_status(target_name, target_config):
    """Check if a target ComfyUI instance is available and get queue depth + GPU stats."""
    result = {
        "online": False,
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
        "net_io": None
    }

    # Try ComfyUI HTTP endpoints
    try:
        # Get queue status
        response = requests.get(f"{target_config['url']}/queue", timeout=5)
        if response.ok:
            data = response.json()
            result["online"] = True
            result["queue_running"] = len(data.get("queue_running", []))
            result["queue_pending"] = len(data.get("queue_pending", []))

        # Get system stats (GPU, RAM)
        stats_response = requests.get(f"{target_config['url']}/system_stats", timeout=5)
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
        except Exception as e:
            logger.warning(f"{target_name} SSH unreachable: {e}")

    return result

def choose_target(routing_hints):
    """Choose the best target based on VRAM needs and queue depth."""
    estimated_vram = routing_hints.get("estimated_vram", 8)
    is_video = routing_hints.get("is_video", False)
    model_types = routing_hints.get("model_types", [])
    node_count = routing_hints.get("node_count", 0)

    logger.info(f"  Workflow: vram_needed={estimated_vram}GB, video={is_video}, models={model_types}, nodes={node_count}")

    # Force video to Gandalf (needs 96GB)
    if is_video or estimated_vram > 20:
        target = "gandalf"
        status = get_target_status(target, CONFIG["targets"][target])
        if status["online"]:
            reason = f"Video/large model -> requires {CONFIG['targets'][target]['vram_gb']}GB VRAM"
            return target, reason
        else:
            logger.error("  ERROR: Gandalf offline but required for this workflow")
            return None, "Gandalf is offline and this workflow requires it"

    # For smaller jobs, check queue depths
    candidates = []
    for name, config in CONFIG["targets"].items():
        if config["vram_gb"] >= estimated_vram:
            status = get_target_status(name, config)
            if status["online"]:
                total_queue = status["queue_running"] + status["queue_pending"]
                candidates.append({
                    "name": name,
                    "config": config,
                    "queue_depth": total_queue,
                    "priority": config["priority"]
                })
                logger.info(f"  Checking {name}: {total_queue} queued, {config['vram_gb']}GB VRAM")

    if not candidates:
        logger.error("  ERROR: No suitable targets available")
        return None, "No suitable targets available"

    # Sort by queue depth, then priority
    candidates.sort(key=lambda x: (x["queue_depth"], x["priority"]))
    chosen = candidates[0]
    reason = f"Least busy ({chosen['queue_depth']} queued), {chosen['config']['vram_gb']}GB VRAM"

    return chosen["name"], reason

def submit_to_target(target_name, workflow_data):
    """Submit the workflow to the chosen ComfyUI instance."""
    target_config = CONFIG["targets"][target_name]

    response = requests.post(
        f"{target_config['url']}/prompt",
        json={
            "prompt": workflow_data["prompt"],
            "client_id": workflow_data.get("client_id", str(uuid.uuid4()))
        },
        timeout=30
    )

    if response.ok:
        return response.json()
    else:
        raise Exception(f"Target returned {response.status_code}: {response.text}")

@app.route("/api/queue", methods=["POST"])
def queue_job():
    """Receive a workflow and route it to the best target."""
    try:
        data = request.json

        if not data or "prompt" not in data:
            return jsonify({"error": "Missing prompt data"}), 400

        routing_hints = data.get("routing_hints", {})
        submitted_from = data.get("submitted_from", "unknown")

        logger.info("")
        logger.info("=" * 60)
        logger.info(f"NEW JOB from {submitted_from}")
        logger.info("=" * 60)

        # Choose target
        target, reason = choose_target(routing_hints)

        if not target:
            return jsonify({"error": reason}), 503

        # Generate job ID
        job_id = str(uuid.uuid4())[:8]

        # Store in database
        conn = sqlite3.connect(CONFIG["db_path"])
        conn.execute(
            """INSERT INTO jobs (id, target, submitted_at, workflow_json, routing_hints)
               VALUES (?, ?, ?, ?, ?)""",
            (job_id, target, datetime.now().isoformat(),
             json.dumps(data.get("workflow", {})),
             json.dumps(routing_hints))
        )
        conn.commit()
        conn.close()

        # Submit to target
        result = submit_to_target(target, data)
        prompt_id = result.get("prompt_id", "unknown")

        # Update job status
        conn = sqlite3.connect(CONFIG["db_path"])
        conn.execute(
            "UPDATE jobs SET status = 'submitted', started_at = ?, result = ? WHERE id = ?",
            (datetime.now().isoformat(), json.dumps({"prompt_id": prompt_id}), job_id)
        )
        conn.commit()
        conn.close()

        # Track job for completion monitoring
        if prompt_id != "unknown":
            active_jobs[prompt_id] = {
                "job_id": job_id,
                "target": target,
                "start_time": datetime.now()
            }

        target_config = CONFIG["targets"][target]

        logger.info("")
        logger.info(f"  >>> ROUTED: Job {job_id} --> {target.upper()}")
        logger.info(f"      Reason: {reason}")
        logger.info(f"      ComfyUI URL: {target_config['url']}")
        logger.info(f"      Prompt ID: {prompt_id}")
        logger.info(f"      Output: {target}'s ComfyUI output folder")
        logger.info("")

        return jsonify({
            "job_id": job_id,
            "target": target,
            "target_url": target_config["url"],
            "reason": reason,
            "prompt_id": prompt_id,
            "output_location": f"Output will appear in {target}'s ComfyUI output folder"
        })

    except Exception as e:
        logger.error(f"Error processing job: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/status", methods=["GET"])
def get_status():
    """Get status of all targets and recent jobs."""
    targets_status = {}
    for name, config in CONFIG["targets"].items():
        targets_status[name] = {
            **get_target_status(name, config),
            "vram_gb": config["vram_gb"],
            "url": config["url"]
        }

    conn = sqlite3.connect(CONFIG["db_path"])
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

@app.route("/")
def index():
    """Dashboard home page."""
    return '''<!DOCTYPE html><html><head><title>SHADOWFAX // QUEUE ROUTER</title>
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
linear-gradient(180deg,transparent 0%,rgba(0,255,242,0.03) 100%);min-height:100vh}
@media(min-width:768px){body{padding:20px 40px}}
@media(max-width:767px){
h1{font-size:1.2em;letter-spacing:2px}
.gauge-row{gap:10px}
.gauge{transform:scale(0.85)}
.gpu-card{padding:15px}
.io-stats{flex-wrap:wrap;gap:10px}
.time-range{flex-wrap:wrap}
.sparkline-container{grid-template-columns:1fr 1fr}
}
body::before{content:'';position:fixed;top:0;left:0;right:0;bottom:0;
background:repeating-linear-gradient(0deg,transparent,transparent 2px,rgba(0,0,0,0.1) 2px,rgba(0,0,0,0.1) 4px);
pointer-events:none;z-index:9999;opacity:0.3}
h1{font-family:'Orbitron',monospace;color:var(--neon-cyan);font-size:1.8em;font-weight:900;letter-spacing:4px;
text-shadow:var(--glow-cyan);border-bottom:2px solid var(--neon-cyan);padding-bottom:15px;margin-bottom:30px;
text-transform:uppercase}
h1::before{content:'◈ ';color:var(--neon-magenta)}
h1::after{content:' ◈';color:var(--neon-magenta)}
h3{margin-top:0;color:var(--neon-cyan);font-family:'Orbitron',monospace;font-size:0.9em;letter-spacing:2px;
text-transform:uppercase;text-shadow:0 0 10px var(--neon-cyan)}
h3::before{content:'▸ '}
.card{background:var(--bg-card);padding:20px;border-radius:4px;margin:20px 0;
border:1px solid #1a2332;box-shadow:0 0 20px rgba(0,255,242,0.1),inset 0 0 60px rgba(0,0,0,0.3)}
.card::before{content:'';position:absolute;top:0;left:0;right:0;height:1px;
background:linear-gradient(90deg,transparent,var(--neon-cyan),transparent)}
.online{color:var(--neon-green);font-weight:bold;text-shadow:var(--glow-green);animation:pulse 2s infinite}
.offline{color:var(--neon-red);font-weight:bold;text-shadow:var(--glow-red);animation:pulse 1s infinite}
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
.gpu-name{font-family:'Orbitron',monospace;font-size:1.1em;font-weight:700;text-transform:uppercase;
letter-spacing:2px;color:var(--neon-cyan);text-shadow:0 0 10px var(--neon-cyan)}
.progress-bar{background:#1a1a2e;border-radius:2px;height:24px;overflow:hidden;margin:5px 0;
border:1px solid #2a2a4e;position:relative}
.progress-bar::before{content:'';position:absolute;top:0;left:0;right:0;bottom:0;
background:repeating-linear-gradient(90deg,transparent,transparent 10px,rgba(255,255,255,0.03) 10px,rgba(255,255,255,0.03) 20px)}
.progress-fill{height:100%;transition:width 1s cubic-bezier(0.4,0,0.2,1);position:relative;animation:bar-breathe 3s ease-in-out infinite}
.progress-fill::after{content:'';position:absolute;top:0;right:0;width:30px;height:100%;
background:linear-gradient(90deg,transparent,rgba(255,255,255,0.4));animation:shimmer 2s ease-in-out infinite}
@keyframes bar-breathe{0%,100%{filter:brightness(1)}50%{filter:brightness(1.15)}}
@keyframes shimmer{0%,100%{opacity:0.3}50%{opacity:0.8}}
.progress-vram,.progress-ram,.progress-disk{background:linear-gradient(90deg,#00ff8844,var(--neon-green));box-shadow:0 0 20px #39ff1466}
.progress-swap{background:linear-gradient(90deg,#ff004444,var(--neon-red));box-shadow:0 0 20px #ff004466}
.progress-yellow{background:linear-gradient(90deg,#ffaa0044,var(--neon-yellow));box-shadow:0 0 20px #ffff0066}
.progress-red{background:linear-gradient(90deg,#ff004444,var(--neon-red));box-shadow:0 0 20px #ff004466}
.io-stats{display:flex;gap:20px;margin-top:15px;font-size:0.9em;padding:10px;background:var(--bg-dark);border-radius:4px;border:1px solid #1a2332}
.io-stat{display:flex;align-items:center;gap:8px}
.io-stat .value{color:var(--neon-cyan);font-weight:bold;font-family:'Orbitron',monospace;text-shadow:0 0 10px var(--neon-cyan)}
.io-stat.warning .value{color:var(--neon-yellow);text-shadow:0 0 10px var(--neon-yellow)}
.io-stat.danger .value{color:var(--neon-red);text-shadow:0 0 10px var(--neon-red)}
.time-range{display:flex;gap:8px;margin-bottom:15px}
.time-range button{background:var(--bg-panel);color:#888;border:1px solid #2a2a4e;padding:8px 16px;
border-radius:4px;cursor:pointer;font-family:'Orbitron',monospace;font-size:0.75em;letter-spacing:1px;
text-transform:uppercase;transition:all 0.3s}
.time-range button:hover{border-color:var(--neon-cyan);color:var(--neon-cyan);box-shadow:0 0 15px rgba(0,255,242,0.3)}
.time-range button.active{background:transparent;border-color:var(--neon-cyan);color:var(--neon-cyan);
box-shadow:var(--glow-cyan)}
.sparkline-container{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}
.sparkline-box{background:var(--bg-panel);padding:12px;border-radius:4px;border:1px solid #1a2332}
.sparkline-label{font-size:0.75em;color:#666;margin-bottom:8px;font-family:'Orbitron',monospace;letter-spacing:1px}
.sparkline{height:45px;display:flex;align-items:end;gap:2px}
.sparkline-bar{background:var(--neon-cyan);min-width:3px;border-radius:1px 1px 0 0;
transition:height 0.5s cubic-bezier(0.4,0,0.2,1);box-shadow:0 0 5px var(--neon-cyan);animation:spark-glow 2s ease-in-out infinite}
.sparkline-bar.high{background:var(--neon-yellow);box-shadow:0 0 5px var(--neon-yellow)}
.sparkline-bar.critical{background:var(--neon-red);box-shadow:0 0 5px var(--neon-red)}
@keyframes spark-glow{0%,100%{opacity:0.85}50%{opacity:1}}
.gauge-arc{animation:arc-glow 2s ease-in-out infinite}
@keyframes arc-glow{0%,100%{filter:brightness(1) drop-shadow(0 0 3px currentColor)}50%{filter:brightness(1.2) drop-shadow(0 0 8px currentColor)}}
.progress-label{display:flex;justify-content:space-between;font-size:0.85em;color:#888;
font-family:'Rajdhani',sans-serif;letter-spacing:1px}
.stat-row{margin:10px 0}
.queue-badge{background:var(--neon-yellow);color:#000;padding:3px 10px;border-radius:2px;font-size:0.8em;
font-family:'Orbitron',monospace;font-weight:bold;box-shadow:0 0 10px var(--neon-yellow)}
.queue-badge.empty{background:#2a2a4e;color:#666;box-shadow:none}
.gauge-row{display:flex;gap:15px;margin:20px 0;justify-content:center;flex-wrap:wrap}
.gauge{text-align:center;position:relative}
.gauge svg{filter:drop-shadow(0 0 10px var(--neon-cyan))}
.gauge-label{font-family:'Orbitron',monospace;font-size:0.7em;color:#666;margin-top:8px;
letter-spacing:2px;text-transform:uppercase}
.gauge-value{font-family:'Orbitron',monospace;font-size:1.1em;font-weight:bold;margin-top:4px;
color:var(--neon-cyan);text-shadow:0 0 10px var(--neon-cyan)}
.power-btn{background:transparent;color:var(--neon-yellow);border:1px solid var(--neon-yellow);
padding:8px 16px;border-radius:4px;cursor:pointer;font-family:'Orbitron',monospace;font-size:0.75em;
letter-spacing:1px;text-transform:uppercase;transition:all 0.3s;margin-top:10px}
.power-btn:hover{background:var(--neon-yellow);color:#000;box-shadow:var(--glow-yellow)}
.job-time{color:#666;font-size:0.9em}
.job-duration{color:var(--neon-green);font-weight:bold;font-family:'Orbitron',monospace;text-shadow:0 0 5px var(--neon-green)}
</style></head>
<body><h1>SHADOWFAX // QUEUE ROUTER</h1>

<div class=card id=monitors><h3>📊 Live Monitoring</h3><p>Loading...</p></div>
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
<div class=card id=jobs><h3>📋 Recent Jobs</h3><p>Loading...</p></div>

<div class=card><h3>🗺️ Routing Logic</h3>
<table>
<tr><th>Workflow Type</th><th>VRAM Needed</th><th>Routes To</th><th>Reason</th></tr>
<tr><td>🎬 Video (AnimateDiff, Wan, etc)</td><td>&gt;48GB</td><td><b style="color:#d9a54a">GANDALF</b></td><td>Only GPU with enough VRAM</td></tr>
<tr><td>🌊 Flux (full)</td><td>~24GB</td><td><b style="color:#d9a54a">GANDALF</b></td><td>Exceeds Frodo's 16GB</td></tr>
<tr><td>⚡ Flux Schnell</td><td>~12GB</td><td><b style="color:#5a8a4a">Least busy</b></td><td>Load balanced</td></tr>
<tr><td>🎨 SDXL</td><td>~12GB</td><td><b style="color:#5a8a4a">Least busy</b></td><td>Load balanced</td></tr>
<tr><td>🖼️ SD 1.5</td><td>~6GB</td><td><b style="color:#5a8a4a">Least busy</b></td><td>Load balanced</td></tr>
</table>
<p style="margin-top:15px;color:#888"><b>Load Balancing:</b> For jobs that fit on either GPU, Shadowfax picks the target with the shortest queue. If queues are equal, Gandalf (priority 1) is preferred.</p>
</div>

<div class=card><h3>📡 API Endpoints</h3><pre>POST /api/queue   - Submit workflow from ComfyUI
GET  /api/status  - Get target status and recent jobs
GET  /api/logs    - Get detailed job history
GET  /api/health  - Health check</pre>
</div>

<script>
const icons = {gandalf: "🧙", frodo: "🧝", shadowfax: "🐴"};

function progressBar(percent, cls, id) {
    let barClass = cls;
    if (percent > 90) barClass = 'progress-red';
    else if (percent > 70) barClass = 'progress-yellow';
    const barId = id || 'bar-' + Math.random().toString(36).substr(2,9);
    return '<div class="progress-bar"><div id="' + barId + '" class="progress-fill ' + barClass + '" style="width:' + percent + '%"></div></div>';
}

function renderGauge(value, min, max, label, unit, showLimit, size) {
    const s = size || 1;
    const w = Math.round(120 * s);
    const h = Math.round(70 * s);
    const r = 45 * s;
    const strokeW = 8 * s;
    const cx = w / 2;
    const cy = h - 5;

    if (value === null) {
        return '<div class="gauge" style="width:' + w + 'px"><svg width="' + w + '" height="' + h + '" viewBox="0 0 ' + w + ' ' + h + '">' +
            '<path d="M ' + (cx - r) + ' ' + cy + ' A ' + r + ' ' + r + ' 0 0 1 ' + (cx + r) + ' ' + cy + '" fill="none" stroke="#1a2332" stroke-width="' + strokeW + '" stroke-linecap="round"/>' +
            '</svg><div class="gauge-label">' + label + '</div><div class="gauge-value">--</div></div>';
    }

    const clampedValue = Math.max(min, Math.min(max, value));
    const percent = ((clampedValue - min) / (max - min)) * 100;
    const arcLength = Math.PI * r;
    const dashOffset = arcLength * (1 - percent / 100);

    // Color based on percentage
    let color = '#39ff14';  // neon green
    let glow = '#39ff14';
    if (percent > 90) { color = '#ff0044'; glow = '#ff0044'; }
    else if (percent > 70) { color = '#ffff00'; glow = '#ffff00'; }

    const displayValue = showLimit ? Math.round(value) + '/' + max + unit : Math.round(value) + unit;

    return '<div class="gauge" style="width:' + w + 'px">' +
        '<svg width="' + w + '" height="' + h + '" viewBox="0 0 ' + w + ' ' + h + '" style="filter:drop-shadow(0 0 10px ' + glow + ')">' +
        '<defs><linearGradient id="grad-' + label.replace(/\\s/g,'') + '" x1="0%" y1="0%" x2="100%" y2="0%">' +
        '<stop offset="0%" style="stop-color:#1a2332"/>' +
        '<stop offset="100%" style="stop-color:#2a3342"/></linearGradient></defs>' +
        '<path d="M ' + (cx - r) + ' ' + cy + ' A ' + r + ' ' + r + ' 0 0 1 ' + (cx + r) + ' ' + cy + '" fill="none" stroke="url(#grad-' + label.replace(/\\s/g,'') + ')" stroke-width="' + (strokeW + 4) + '" stroke-linecap="round"/>' +
        '<path class="gauge-arc" d="M ' + (cx - r) + ' ' + cy + ' A ' + r + ' ' + r + ' 0 0 1 ' + (cx + r) + ' ' + cy + '" fill="none" stroke="' + color + '" stroke-width="' + strokeW + '" stroke-linecap="round" stroke-dasharray="' + arcLength + '" stroke-dashoffset="' + dashOffset + '" style="transition:stroke-dashoffset 0.8s ease-out,stroke 0.5s"/>' +
        '<circle cx="' + cx + '" cy="' + cy + '" r="' + (6 * s) + '" fill="#0a0a0f" stroke="' + color + '" stroke-width="2"/>' +
        '</svg>' +
        '<div class="gauge-label">' + label + '</div>' +
        '<div class="gauge-value" style="color:' + color + ';text-shadow:0 0 10px ' + glow + '">' + displayValue + '</div></div>';
}

function setupPowerButtons() {
    document.querySelectorAll('.power-btn').forEach(btn => {
        btn.onclick = function() {
            const target = this.dataset.target;
            const current = this.dataset.current;
            const max = this.dataset.max;
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
                if (data.success) {
                    refresh();
                } else {
                    alert('Failed: ' + (data.error || 'Unknown error'));
                }
            }).catch(err => alert('Error: ' + err));
        };
    });
}

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


function refresh() {
    fetch("/api/status").then(r => r.json()).then(data => {
        let html = '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:20px">';

        for (const [name, info] of Object.entries(data.targets)) {
            const icon = icons[name] || "🖥️";
            const status = info.online ? '<span class="online">ONLINE</span>' : '<span class="offline">OFFLINE</span>';
            const queueBadge = info.queue_pending > 0
                ? '<span class="queue-badge">' + info.queue_pending + ' queued</span>'
                : '<span class="queue-badge empty">idle</span>';

            html += '<div class="gpu-card">';
            html += '<div class="gpu-header"><span class="gpu-name">' + icon + ' ' + name + '</span>' + status + '</div>';

            if (info.online && info.gpu) {
                // Gauges in 2x2 grid: GPU Util + CPU on top, GPU Temp + GPU Power below
                html += '<div class="gauge-row">';
                html += renderGauge(info.gpu_util, 0, 100, "GPU Util", "%", false, 1.2);
                html += renderGauge(info.cpu_percent, 0, 100, "CPU Util", "%", false);
                html += '</div>';
                html += '<div class="gauge-row">';
                html += renderGauge(info.gpu_temp, 24, 90, "GPU Temp", "°C", false);
                html += renderGauge(info.gpu_watts, 0, info.gpu_power_max, "GPU Power", "W", true);
                html += '</div>';
                html += '<button class="power-btn" data-target="' + name + '" data-current="' + info.gpu_power_limit + '" data-max="' + info.gpu_power_max + '">⚡ Set Power Limit</button>';

                // VRAM bar
                html += '<div class="stat-row">';
                html += '<div class="progress-label"><span>🎮 VRAM</span><span>' + info.gpu.vram_used_gb + ' / ' + info.gpu.vram_total_gb + ' GB</span></div>';
                html += progressBar(info.gpu.vram_percent, "progress-vram");
                html += '</div>';

                // RAM bar
                if (info.ram) {
                    html += '<div class="stat-row">';
                    html += '<div class="progress-label"><span>💾 RAM</span><span>' + info.ram.used_gb + ' / ' + info.ram.total_gb + ' GB</span></div>';
                    html += progressBar(info.ram.percent, "progress-ram");
                    html += '</div>';
                }

                // Swap bar (only show if > 0% used - indicates potential issue)
                if (info.swap && info.swap.percent > 0) {
                    html += '<div class="stat-row">';
                    html += '<div class="progress-label"><span>⚠️ Swap</span><span>' + info.swap.used_gb + ' / ' + info.swap.total_gb + ' GB</span></div>';
                    html += progressBar(info.swap.percent, "progress-swap");
                    html += '</div>';
                }

                // Disk bar
                if (info.disk) {
                    html += '<div class="stat-row">';
                    const diskUsed = info.disk.total_gb >= 1000 ? (info.disk.used_gb / 1024).toFixed(1) + ' TB' : info.disk.used_gb + ' GB';
                    const diskTotal = info.disk.total_gb >= 1000 ? (info.disk.total_gb / 1024).toFixed(1) + ' TB' : info.disk.total_gb + ' GB';
                    html += '<div class="progress-label"><span>💿 Disk</span><span>' + diskUsed + ' / ' + diskTotal + '</span></div>';
                    html += progressBar(info.disk.percent, "progress-disk");
                    html += '</div>';
                }

                // I/O Stats
                html += '<div class="io-stats">';
                if (info.disk_io) {
                    const diskClass = (info.disk_io.read_mbps + info.disk_io.write_mbps) > 500 ? 'warning' : '';
                    html += '<div class="io-stat ' + diskClass + '">📀 <span class="value">' + info.disk_io.read_mbps + '/' + info.disk_io.write_mbps + '</span> MB/s</div>';
                }
                if (info.net_io) {
                    const netClass = (info.net_io.rx_mbps + info.net_io.tx_mbps) > 100 ? 'warning' : '';
                    html += '<div class="io-stat ' + netClass + '">🌐 <span class="value">' + info.net_io.rx_mbps + '/' + info.net_io.tx_mbps + '</span> MB/s</div>';
                }
                html += '</div>';

                html += '<div style="margin-top:10px;font-size:0.85em;color:#888">' + info.gpu.name + (info.gpu.cuda_version ? ' (CUDA ' + info.gpu.cuda_version + ')' : '') + '</div>';
            } else if (!info.online) {
                // Show SSH metrics even if ComfyUI is offline
                if (info.cpu_percent !== null || info.gpu_watts !== null) {
                    html += '<div style="color:#d9a54a;font-size:0.9em;margin-bottom:10px">ComfyUI offline - showing system metrics</div>';
                    html += '<div class="gauge-row">';
                    html += renderGauge(info.gpu_util, 0, 100, "GPU Util", "%", false, 1.2);
                    html += renderGauge(info.cpu_percent, 0, 100, "CPU Util", "%", false);
                    html += '</div>';
                    html += '<div class="gauge-row">';
                    html += renderGauge(info.gpu_temp, 24, 90, "GPU Temp", "°C", false);
                    html += renderGauge(info.gpu_watts, 0, info.gpu_power_max, "GPU Power", "W", true);
                    html += '</div>';
                    if (info.gpu_watts !== null) {
                        html += '<button class="power-btn" data-target="' + name + '" data-current="' + info.gpu_power_limit + '" data-max="' + info.gpu_power_max + '">⚡ Set Power Limit</button>';
                    }
                    // Disk bar even when offline
                    if (info.disk) {
                        html += '<div class="stat-row">';
                        const diskUsed = info.disk.total_gb >= 1000 ? (info.disk.used_gb / 1024).toFixed(1) + ' TB' : info.disk.used_gb + ' GB';
                        const diskTotal = info.disk.total_gb >= 1000 ? (info.disk.total_gb / 1024).toFixed(1) + ' TB' : info.disk.total_gb + ' GB';
                        html += '<div class="progress-label"><span>💿 Disk</span><span>' + diskUsed + ' / ' + diskTotal + '</span></div>';
                        html += progressBar(info.disk.percent, "progress-disk");
                        html += '</div>';
                    }
                } else {
                    html += '<div style="color:#666;padding:20px 0">Offline or unreachable</div>';
                }
            }

            html += '<div style="margin-top:10px">' + queueBadge + ' <a href="' + info.url + '" style="color:#5a8a4a;font-size:0.85em;margin-left:10px">Open ComfyUI →</a></div>';
            html += '</div>';
        }
        html += '</div>';
        document.getElementById("monitors").innerHTML = "<h3>📊 Live Monitoring</h3>" + html;
        setupPowerButtons();
    });

    fetch("/api/logs").then(r => r.json()).then(data => {
        let html = "";
        if (data.jobs.length === 0) {
            html = "<p style='color:#888'>No jobs yet - use the Router button in ComfyUI!</p>";
        } else {
            data.jobs.slice(0, 10).forEach(j => {
                const cls = j.is_video ? "job video" : (j.status === "completed" ? "job completed" : "job");
                const models = j.model_types.length ? j.model_types.join(", ") : "standard";
                const timeStr = formatJobTime(j.submitted_at);
                const icon = icons[j.target] || "🖥️";
                let timing = '<span class="job-time">' + timeStr + '</span>';
                if (j.status === "completed" && j.duration) {
                    timing += ' <span class="job-duration">⏱ ' + j.duration + '</span>';
                }
                html += '<div class="' + cls + '"><b>' + j.id + '</b> → ' + icon + ' <b style="text-transform:capitalize">' + j.target + '</b> | ' + models + ' | ' + timing + '</div>';
            });
        }
        document.getElementById("jobs").innerHTML = "<h3>📋 Recent Jobs</h3>" + html;
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

        for (const [target, points] of Object.entries(data.data)) {
            const icon = icons[target] || '🖥️';
            html += '<div style="margin-bottom:20px"><h4 style="margin:0 0 10px 0;color:#ccc">' + icon + ' ' + target.charAt(0).toUpperCase() + target.slice(1) + '</h4>';
            html += '<div class="sparkline-container">';
            html += renderSparkline(points, 'gpu_util', 100, '🎮 GPU Util %');
            html += renderSparkline(points, 'cpu_percent', 100, '💻 CPU %');
            html += renderSparkline(points, 'gpu_temp', 90, '🌡️ Temp °C');
            html += renderSparkline(points, 'vram_percent', 100, '🎮 VRAM %');
            html += renderSparkline(points, 'swap_percent', 100, '⚠️ Swap %');
            html += renderSparkline(points, 'queue_depth', null, '📋 Queue');
            html += '</div></div>';
        }

        if (!html) {
            html = '<p style="color:#888">No historical data yet. Metrics are collected every minute.</p>';
        }

        document.getElementById('sparklines').innerHTML = html;
    }).catch(err => {
        document.getElementById('sparklines').innerHTML = '<p style="color:#d94a4a">Error loading history: ' + err + '</p>';
    });
}

refresh();
refreshHistory();
setInterval(refresh, 3000);
setInterval(refreshHistory, 60000);  // Refresh history every minute
</script></body></html>'''

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

    # Start background job completion checker
    completion_thread = threading.Thread(target=check_job_completion, daemon=True)
    completion_thread.start()

    # Start background metrics collector
    metrics_thread = threading.Thread(target=collect_metrics, daemon=True)
    metrics_thread.start()

    logger.info("")
    logger.info("=" * 60)
    logger.info("  SHADOWFAX QUEUE ROUTER")
    logger.info("=" * 60)
    logger.info(f"  Listening: http://0.0.0.0:5000")
    logger.info(f"  Dashboard: http://shadowfax.local")
    logger.info(f"  Targets:   {', '.join(CONFIG['targets'].keys())}")
    logger.info(f"  Notifications: Pushover {'enabled' if PUSHOVER_CONFIG['enabled'] else 'disabled'}")
    logger.info("=" * 60)
    logger.info("")
    app.run(host="0.0.0.0", port=5000)
