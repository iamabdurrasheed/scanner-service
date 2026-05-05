"""
scanner.py - Trivy and Grype scan logic with sequential / parallel execution modes.

Both scanners run as Docker containers (no host installation needed).
Pull the images once with:  python3 setup_scanners.py
"""

import asyncio
import subprocess
import time
import logging
import os
from typing import Literal

from monitor import ProcessMonitor, SystemMonitor, ResourceMetrics, merge_metrics

logger = logging.getLogger(__name__)

OUTPUT_DIR = "/tmp/output"
IMAGE_TAG  = "sample-image:latest"

# Docker Hub image references (kept in sync with setup_scanners.py)
TRIVY_IMAGE = "aquasec/trivy:latest"
GRYPE_IMAGE = "anchore/grype:latest"

# ──────────────────────────────────────────────────────────────────────────────
# Command builders
# ──────────────────────────────────────────────────────────────────────────────
#
# Both commands share the same two mounts:
#   /tmp/output  → where JSON results are written
#   /var/run/docker.sock → lets the scanner container reach the host Docker
#                          daemon so it can inspect sample-image:latest
#
# Trivy:
#   aquasec/trivy image -f json -o /output/<file> <image>
#   Output goes directly to the mounted /output directory.
#
# Grype:
#   anchore/grype <image> -o json
#   Grype writes to stdout; we capture it and save it ourselves.
# ──────────────────────────────────────────────────────────────────────────────

def _docker_run_base(scanner_image: str, container_name: str) -> list[str]:
    """
    Shared docker run prefix used by both scanner commands.
    --rm          : auto-remove the container when it exits
    --network host: avoids NAT overhead and lets the container resolve the
                    host Docker socket without extra flags on most setups
    """
    return [
        "docker", "run",
        "--rm",
        "--name", container_name,
        "--network", "host",
        # Mount Docker socket so the container can talk to the host daemon
        "-v", "/var/run/docker.sock:/var/run/docker.sock",
        # Mount output directory so result files land on the host
        "-v", f"{OUTPUT_DIR}:/output",
        scanner_image,
    ]


def trivy_cmd(output_filename: str, container_name: str) -> list[str]:
    """
    Full docker run command for Trivy.
    Trivy writes the JSON file directly to /output inside the container,
    which is bind-mounted to OUTPUT_DIR on the host.
    """
    return _docker_run_base(TRIVY_IMAGE, container_name) + [
        "image",
        "-f", "json",
        "-o", f"/output/{output_filename}",
        IMAGE_TAG,
    ]


def grype_cmd(container_name: str) -> list[str]:
    """
    Full docker run command for Grype.
    Grype writes JSON to stdout; the caller captures it and saves the file.
    """
    return _docker_run_base(GRYPE_IMAGE, container_name) + [
        IMAGE_TAG,
        "-o", "json",
    ]


# ──────────────────────────────────────────────────────────────────────────────
# Synchronous (blocking) single-scanner runner
# ──────────────────────────────────────────────────────────────────────────────

def run_scanner_sync(scanner: Literal["trivy", "grype"]) -> tuple[bool, str, ResourceMetrics]:
    """
    Run a single scanner inside a Docker container, monitoring resource usage
    of the `docker run` host process (which drives the container workload).
    Returns (success, output_file_path_or_error_msg, metrics).
    """
    output_file = os.path.join(OUTPUT_DIR, f"{scanner}_result.json")
    # Unique container name avoids collisions in parallel mode
    container_name = f"{scanner}-scan-{int(time.time())}"
    monitor = ProcessMonitor(interval=1.0)

    if scanner == "trivy":
        cmd = trivy_cmd(os.path.basename(output_file), container_name)
        logger.info(f"[trivy] docker run command: {' '.join(cmd)}")
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        monitor.start(proc.pid)
        stdout, stderr = proc.communicate(timeout=600)
        monitor.stop()

        if proc.returncode != 0:
            err = stderr.decode().strip() or stdout.decode().strip()
            logger.error(f"trivy container failed (rc={proc.returncode}): {err}")
            return False, f"trivy failed: {err}", monitor.metrics

    elif scanner == "grype":
        cmd = grype_cmd(container_name)
        logger.info(f"[grype] docker run command: {' '.join(cmd)}")
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        monitor.start(proc.pid)
        stdout, stderr = proc.communicate(timeout=600)
        monitor.stop()

        if proc.returncode != 0:
            err = stderr.decode().strip() or stdout.decode().strip()
            logger.error(f"grype container failed (rc={proc.returncode}): {err}")
            return False, f"grype failed: {err}", monitor.metrics

        # Grype outputs JSON to stdout → save it to the host output directory
        with open(output_file, "wb") as f:
            f.write(stdout)

    else:
        raise ValueError(f"Unknown scanner: {scanner}")

    logger.info(f"{scanner} completed → {output_file}")
    return True, output_file, monitor.metrics


# ──────────────────────────────────────────────────────────────────────────────
# Sequential mode
# ──────────────────────────────────────────────────────────────────────────────

def run_sequential(scanners: list[str]) -> dict:
    """
    Run each scanner one after another.
    Returns a results dict suitable for the API response.
    """
    results = {}
    per_scanner_metrics = {}
    errors = {}
    all_metrics: list[ResourceMetrics] = []

    for scanner in scanners:
        logger.info(f"[sequential] Starting {scanner}")
        success, path_or_err, metrics = run_scanner_sync(scanner)
        all_metrics.append(metrics)
        per_scanner_metrics[scanner] = metrics.to_dict()
        if success:
            results[scanner] = path_or_err
        else:
            errors[scanner] = path_or_err

    return {
        "results": results,
        "errors": errors,
        "resource_usage": merge_metrics(all_metrics),
        "per_scanner_metrics": per_scanner_metrics,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Parallel mode
# ──────────────────────────────────────────────────────────────────────────────

async def _run_scanner_async(scanner: Literal["trivy", "grype"], sys_monitor: SystemMonitor) -> tuple[str, bool, str, ResourceMetrics]:
    """
    Async wrapper: launches scanner in a thread pool so the event loop stays free.
    """
    loop = asyncio.get_event_loop()
    success, path_or_err, metrics = await loop.run_in_executor(
        None, run_scanner_sync, scanner
    )
    return scanner, success, path_or_err, metrics


async def run_parallel_async(scanners: list[str]) -> dict:
    """
    Run all scanners concurrently using asyncio + thread executor.
    """
    sys_monitor = SystemMonitor(interval=1.0)
    sys_monitor.start()

    tasks = [_run_scanner_async(s, sys_monitor) for s in scanners]
    scan_results = await asyncio.gather(*tasks, return_exceptions=True)

    sys_monitor.stop()

    results = {}
    errors = {}
    per_scanner_metrics = {}
    individual_metrics: list[ResourceMetrics] = []

    for item in scan_results:
        if isinstance(item, Exception):
            logger.error(f"Parallel scan raised: {item}")
            continue
        scanner, success, path_or_err, metrics = item
        per_scanner_metrics[scanner] = metrics.to_dict()
        individual_metrics.append(metrics)
        if success:
            results[scanner] = path_or_err
        else:
            errors[scanner] = path_or_err

    # Use system-level metrics for combined usage (more accurate in parallel)
    combined = sys_monitor.metrics
    resource_usage = {
        "cpu_avg": combined.cpu_avg,
        "cpu_peak": combined.cpu_peak,
        "memory_avg": combined.mem_avg,
        "memory_peak": combined.mem_peak,
    }

    return {
        "results": results,
        "errors": errors,
        "resource_usage": resource_usage,
        "per_scanner_metrics": per_scanner_metrics,
    }


def run_parallel(scanners: list[str]) -> dict:
    """Synchronous entry-point for parallel mode (runs the async event loop)."""
    return asyncio.run(run_parallel_async(scanners))
