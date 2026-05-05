# Container Image Scanning Service

A FastAPI service that accepts a GitHub repository URL, builds a Docker image from it, and scans it for security vulnerabilities using **Trivy** and/or **Grype** — with real-time host CPU, RAM and disk monitoring per scanner.

---

## Table of Contents

1. [What This Service Does](#what-this-service-does)
2. [Project Structure](#project-structure)
3. [How Every File Fits Together](#how-every-file-fits-together)
4. [Prerequisites](#prerequisites)
5. [Setup & Run](#setup--run)
6. [What run.py Does Step by Step](#what-runpy-does-step-by-step)
7. [Full Request Flow (What Happens When You Call /scan)](#full-request-flow)
8. [API Reference](#api-reference)
9. [Execution Modes Explained](#execution-modes-explained)
10. [Resource Monitoring Explained](#resource-monitoring-explained)
11. [Output Files](#output-files)
12. [Error Handling](#error-handling)
13. [Working Directories](#working-directories)
14. [Common Errors & Fixes](#common-errors--fixes)
15. [Scan Observations & Analysis](#scan-observations--analysis)
16. [External Service Integration](#external-service-integration)

---

## What This Service Does

In plain English:

1. `run.py` sends a hardcoded POST request to `/scan` (POC client)
2. The service wipes `/tmp/repo` and `output/` clean
3. It clones the GitHub repo into `/tmp/repo`
4. It builds a Docker image from the repo's `Dockerfile`
5. It runs Trivy and/or Grype as Docker containers — **while they run**, host CPU, RAM and disk are tracked in real time
6. Results are saved to `output/` inside the project directory as JSON files
7. A structured JSON response is returned to the caller

> **Why Docker containers for scanners?**  
> Trivy and Grype do NOT need to be installed on the host. They run as Docker containers pulled from Docker Hub. This means zero manual tool installation.

### Current implementation notes

The current `/scan` flow also uploads each successful scanner result to Azure Blob Storage after the local JSON files are written. Credentials are loaded from environment variables, including values in a local `.env` file:

```bash
AZURE_STORAGE_CONNECTION_STRING="DefaultEndpointsProtocol=https;AccountName=...;AccountKey=...;EndpointSuffix=core.windows.net"
AZURE_STORAGE_CONTAINER="scan-results"   # optional; defaults to scan-results
```

The request can include:

| Field | Default | Description |
|-------|---------|-------------|
| `service_version` | `v1.0` | Version label used in the Azure blob path. |
| `branch` | repo default branch | Optional Git branch to clone and scan. Omit it to use the repository default branch. |

Blob paths are written as:

```text
<appname>/<service_version>/<branch>/<commit_id>/<scanner>_results.json
```

---

## Project Structure

```
scanner-service/
 ├── main.py           # FastAPI app — defines /health and /scan endpoints
 ├── scanner.py        # Core scan logic — sequential and parallel modes
 ├── monitor.py        # Host CPU, RAM & disk tracking using psutil
 ├── utils.py          # Directory prep, git clone, docker build helpers
 ├── run.py            # One-command setup and launch script (POC client)
 ├── client.py         # Standalone client for external service integration
 ├── setup_scanners.py # Standalone script to pull Trivy/Grype images only
 ├── requirements.txt  # Python dependencies
 ├── output/           # Scan result JSON files (created automatically, git-ignored)
 └── README.md
```

---

## How Every File Fits Together

```
python3 run.py
     │
     ├── validates Python + Docker
     ├── creates venv/, installs requirements.txt
     ├── pulls aquasec/trivy:latest + anchore/grype:latest
     ├── creates /tmp/repo and output/
     ├── checks port availability (auto-selects 8000–8009)
     ├── launches uvicorn → main.py  (server starts as subprocess)
     ├── waits for GET /health to return 200
     └── sends hardcoded POST /scan  ← POC client lives here
                               │
                    POST /scan request arrives
                               │
                          main.py
                          └── ScanRequest validation
                                ├── scanners not empty, deduplicated
                                ├── source must start with https://github.com/
                                └── mode: sequential (default) or parallel
                               │
                          utils.py
                          ├── prepare_directories()   → wipes /tmp/repo and output/
                          ├── clone_repository()      → git clone --depth 1 into /tmp/repo
                          │                              checks Dockerfile exists at root
                          └── build_docker_image()    → docker build -t sample-image:latest
                               │
                          scanner.py
                          ├── sequential mode → run_sequential()
                          │     for each scanner:
                          │       subprocess.Popen(docker run ...)  ← scanner starts
                          │       ProcessMonitor.start(pid)         ← monitoring starts immediately
                          │       proc.communicate()                ← wait for scanner to finish
                          │       ProcessMonitor.stop()             ← monitoring stops
                          │       [monitoring ran the entire time the scanner was alive]
                          │
                          └── parallel mode → run_parallel() → asyncio.gather()
                                both scanners via run_in_executor() simultaneously
                                SystemMonitor tracks whole system for entire window
                                each scanner still gets its own ProcessMonitor
                               │
                          monitor.py
                          ├── ProcessMonitor  → background thread, polls host CPU/RAM/disk every 1s
                          └── SystemMonitor   → background thread, polls whole system every 1s
                               │
                          scanner.py writes output files
                          ├── trivy → writes output/trivy_result.json via -o flag
                          └── grype → stdout captured → written by run_scanner_sync()
                               │
                          utils.py → remove_docker_image()
                          └── docker rmi -f sample-image:latest  ← frees disk after every scan
                               │
                          main.py assembles and returns JSON response
                               │
                          run.py prints response to terminal
```

---

## Prerequisites

These must be installed on your Linux server **before** running `run.py`.

### 1 · Git

```bash
sudo apt install git -y
```

Git is used to clone the target GitHub repository into `/tmp/repo`.

### 2 · Docker

```bash
sudo apt install docker.io -y
sudo systemctl start docker
sudo systemctl enable docker   # auto-start on reboot
sudo usermod -aG docker $USER  # run docker without sudo
newgrp docker                  # apply group change without logout
docker ps                      # verify it works
```

Docker is used for three things:
- Building the image from the cloned repo (`docker build`)
- Running Trivy as a container (`docker run aquasec/trivy`)
- Running Grype as a container (`docker run anchore/grype`)

### 3 · Python 3.8+

```bash
sudo apt install python3 python3-pip python3-venv -y
```

Python 3.8 is the minimum version. The service uses `list[str]` type hints and `asyncio.run()` which require 3.8+.

---

## Setup & Run

One command does everything:

```bash
python3 run.py
```

After it completes, the API is live at:
- API base: `http://localhost:8000`
- Interactive docs (Swagger UI): `http://localhost:8000/docs`

---

## What run.py Does Step by Step

`run.py` is designed to be **idempotent** — safe to run multiple times. It skips steps that are already done.

### Step 1 — Check Python version

```
[*] Checking Python version...
    Python 3.10.x OK
```

Checks `sys.version_info`. Exits immediately if Python < 3.8.

### Step 2 — Check Docker daemon

```
[*] Checking Docker...
    Docker daemon is running OK
```

Runs `docker ps`. If Docker is not installed → exits with install instructions. If Docker is installed but daemon is stopped → exits with `sudo systemctl start docker`.

### Step 3 — Create virtual environment

```
[*] Setting up virtual environment...
    Created 'venv'
    # or: 'venv' already exists, skipping
```

Creates an isolated Python environment in `venv/`. All packages install here, not system-wide. Skipped if `venv/` directory already exists.

### Step 4 — Install Python dependencies

```
[*] Installing dependencies from requirements.txt...
```

Runs `pip install -r requirements.txt` inside the venv. Installs:

| Package | Version | Purpose |
|---------|---------|---------|
| `fastapi` | ≥ 0.111.0 | Web framework for the API |
| `uvicorn[standard]` | ≥ 0.30.0 | ASGI server that runs FastAPI |
| `psutil` | ≥ 5.9.8 | Host CPU, RAM and disk monitoring |
| `azure-storage-blob` | ≥ 12.19.0 | Uploads scan JSON files to Azure Blob Storage |
| `python-dotenv` | ≥ 1.0.1 | Loads local `.env` values for development |

### Step 5 — Pull scanner Docker images

```
[*] Pulling scanner images...
    trivy (aquasec/trivy:latest) already present, skipping
    Pulling anchore/grype:latest ...
    grype ready
```

Only pulls images listed in `POC_REQUEST["scanners"]`. For each, runs `docker image inspect` first — skips the pull if already cached locally.

### Step 6 — Create working directories

```
[*] Ensuring required directories exist...
    /tmp/repo OK
    /home/user/scanner-service/output OK
```

Creates `/tmp/repo` and `output/` inside the project directory. Uses `os.makedirs(..., exist_ok=True)` so it never fails if they already exist. `output/` is resolved as an absolute path from `__file__`.

### Step 6b — Check port availability

```
[*] Checking port availability...
    Using port 8000
```

Uses `ss -tlnp` to check if port 8000 is free. Automatically tries 8001, 8002 ... up to 8009 and picks the first free one. If all are in use, exits with an error.

### Step 7 — Start the API server

```
[*] Starting API server...
    API:   http://localhost:8000
    Docs:  http://localhost:8000/docs
```

Launches `uvicorn main:app --host 0.0.0.0 --port <PORT>` using `subprocess.Popen`, which starts uvicorn as a **child process** and returns immediately. `run.py` stays alive so it can send the POC request and keep the server running after.

### Step 8 — Wait for server to be ready

```
[*] Waiting for server to be ready...
    Server is up
```

Polls `GET /health` every 1 second for up to 30 seconds. Only proceeds to the scan request once the server responds with HTTP 200. This prevents the POC request from firing before uvicorn has finished binding to port 8000.

### Step 9 — Send hardcoded POC scan request

```
[*] Sending POC scan request...
    {
        "scanners": ["trivy", "grype"],
        "source": "https://github.com/docker/welcome-to-docker",
        "mode": "parallel"
    }

[+] Scan response:
{ "status": "completed", ... }
```

`run.py` acts as the POC client — it sends a hardcoded `POST /scan` request using Python's built-in `urllib.request` (no extra dependencies). The `POC_REQUEST` dict at the top of `run.py` is where you change the repo URL, scanners, or mode. The full JSON response is printed to the terminal.

### Step 10 — Keep server running

```
[*] Server still running on port 8000. Press Ctrl+C to stop.
```

After the POC scan completes, the server stays alive so you can send additional requests manually. `server.wait()` blocks until the process exits. Ctrl+C calls `server.terminate()` to shut down uvicorn cleanly.

---

## Full Request Flow

Here is exactly what happens from the moment `run.py` fires the POC request to the moment the response is printed.

### Stage 1 — POC client sends request (`run.py`)

`run.py` sends a hardcoded `POST /scan` using `urllib.request`. The payload is defined in the `POC_REQUEST` dict:

```python
POC_REQUEST = {
    "scanners": ["trivy", "grype"],
    "source": "https://github.com/docker/welcome-to-docker",
    "mode": "parallel",
}
```

Change this dict to point at any GitHub repo with a `Dockerfile`.

### Stage 2 — Request validation (`main.py → ScanRequest`)

FastAPI validates the request body before `scan()` executes:

- `scanners` — must be a non-empty list of `"trivy"` and/or `"grype"`. Duplicates are removed automatically using `dict.fromkeys()`.
- `source` — stripped of whitespace, must start with `https://github.com/`. Anything else → HTTP 422.
- `mode` — must be `"sequential"` or `"parallel"`. Defaults to `"sequential"` if omitted.

If any validation fails, FastAPI returns HTTP 422 immediately and the scan never starts.

### Stage 3 — Directory preparation (`utils.py → prepare_directories`)

Before anything else, both working directories are **wiped clean**:

- `/tmp/repo` — all files and subdirectories deleted
- `output/` (inside the project directory) — all files and subdirectories deleted

This guarantees no leftover files from a previous scan can interfere. Uses `shutil.rmtree()` for directories and `os.remove()` for files. If this step fails → HTTP 500.

### Stage 4 — Clone the repository (`utils.py → clone_repository`)

```bash
git clone --depth 1 <source_url> /tmp/repo
```

`--depth 1` fetches only the latest commit — no history, faster and smaller. After cloning, checks `os.path.isfile("/tmp/repo/Dockerfile")`. If no `Dockerfile` exists at the root → HTTP 422. The repo must have a `Dockerfile` at its root for the next step to work.

### Stage 5 — Build the Docker image (`utils.py → build_docker_image`)

```bash
docker build -t sample-image:latest /tmp/repo
```

Builds a Docker image from the cloned repo and tags it `sample-image:latest`. This is the image Trivy and Grype will scan. Uses `subprocess.run()` with a 600-second timeout. If the build fails → HTTP 500.

### Stage 6 — Scan execution + resource monitoring (simultaneously) (`scanner.py` + `monitor.py`)

> **Important:** Resource monitoring is NOT a separate step that happens after scanning. The monitor attaches to the scanner process immediately when it starts and runs concurrently for the scanner's entire lifetime.

The behaviour depends on `mode`:

#### Sequential mode (`run_sequential`)

```
trivy starts      → ProcessMonitor.start(trivy_pid)  ← monitoring begins
[trivy running]     ProcessMonitor polling every 1s
trivy finishes    → ProcessMonitor.stop()             ← monitoring ends
grype starts      → ProcessMonitor.start(grype_pid)  ← new monitor begins
[grype running]     ProcessMonitor polling every 1s
grype finishes    → ProcessMonitor.stop()             ← monitoring ends
```

- `subprocess.Popen()` launches the scanner — `Popen` (not `run`) is used specifically because it returns the PID immediately so the monitor can attach before the process finishes
- `ProcessMonitor.start(pid)` spins up a background thread that polls **host-level** `psutil.cpu_percent()`, `psutil.virtual_memory()` and `psutil.disk_usage("/")` every 1 second
- `proc.communicate(timeout=600)` blocks until the scanner finishes, collecting stdout/stderr
- If the 600s timeout fires, `docker stop --time 10 <container_name>` is called to stop only our container, then `proc.wait()` waits for the host process to exit naturally. No other process on the server is touched.
- `ProcessMonitor.stop()` signals the thread to stop and records `end_time`
- `merge_metrics()` combines both scanners' sample lists into one `resource_usage` dict

#### Parallel mode (`run_parallel`)

```
SystemMonitor.start()          ← system-wide monitoring begins
trivy starts ─┐  ProcessMonitor.start(trivy_pid)
              ├─ both running simultaneously via asyncio.gather() + thread executor
grype starts ─┘  ProcessMonitor.start(grype_pid)
[both finish]
SystemMonitor.stop()           ← system-wide monitoring ends
```

- `run_parallel()` calls `asyncio.run(run_parallel_async())`
- `run_parallel_async()` starts a `SystemMonitor` then fires all scanners via `asyncio.gather()`
- Each scanner runs in a thread pool via `loop.run_in_executor(None, run_scanner_sync, scanner)` — this is what makes them truly concurrent
- `SystemMonitor` polls `psutil.cpu_percent()`, `psutil.virtual_memory()` and `psutil.disk_usage("/")` for the whole machine every 1 second
- Each scanner still gets its own `ProcessMonitor` for `per_scanner_metrics`

#### How each scanner runs internally (`run_scanner_sync`)

Both scanners run as Docker containers with two volume mounts:

| Mount | Purpose |
|-------|---------|
| `/var/run/docker.sock:/var/run/docker.sock` | Lets the scanner container talk to the host Docker daemon to inspect `sample-image:latest` |
| `<project>/output:/output` | Bind-mounts the project `output/` directory so result files land directly in the project |

**Trivy command:**
```bash
docker run --rm --name trivy-scan-<timestamp> \
  --network host \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /home/user/scanner-service/output:/output \
  aquasec/trivy:latest \
  image -f json -o /output/trivy_result.json sample-image:latest
```
Trivy writes the JSON file directly to `/output/trivy_result.json` inside the container, which maps to `output/trivy_result.json` in the project directory on the host via the bind mount.

**Grype command:**
```bash
docker run --rm --name grype-scan-<timestamp> \
  --network host \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /home/user/scanner-service/output:/output \
  anchore/grype:latest \
  sample-image:latest -o json
```
Grype writes JSON to stdout. `proc.communicate()` captures it, then `run_scanner_sync` writes it to `output/grype_result.json` manually.

Both use `--rm` (auto-remove container on exit) and a timestamped `--name` to avoid name collisions in parallel mode.

### Stage 7 — Store JSON outputs (`scanner.py → run_scanner_sync`)

Result files land at:

```
output/trivy_result.json   ← written by the Trivy container itself
output/grype_result.json   ← written by run_scanner_sync from captured stdout
```

These files are overwritten on every scan because Stage 3 wipes `output/` at the start of each request.

### Stage 8 — Remove built image (`utils.py → remove_docker_image`)

After scanning completes (or if the scanner raises an exception), `sample-image:latest` is removed from Docker:

```bash
docker rmi -f sample-image:latest
```

This frees the disk space used by the built image after every scan. The scanner images (`aquasec/trivy:latest` and `anchore/grype:latest`) are **not** removed — they are reused across scans.

### Stage 9 — Build and return response (`main.py`)

`main.py` assembles the final response dict and returns `JSONResponse(status_code=200)`:

- `status` → `"completed"` if errors dict is empty, `"partial"` if any scanner failed
- `results` → map of scanner name to output file path
- `resource_usage` → combined cpu/mem/disk (merged ProcessMonitor samples in sequential, SystemMonitor in parallel)
- `per_scanner_metrics` → per-scanner pid, start/end time (UTC), duration, cpu avg/peak, memory avg/peak, disk avg/peak
- `errors` → only included when `status` is `"partial"`

### Stage 10 — run.py prints response to terminal

`run.py` receives the HTTP response, parses the JSON, and prints it with `json.dumps(result, indent=2)`.

---

## API Reference

### `GET /health`

Simple liveness check. Use this to verify the service is running.

```bash
curl http://localhost:8000/health
```

Response:
```json
{"status": "ok"}
```

---

### `POST /scan`

**Request body fields:**

In addition to the original `source`, `scanners`, and `mode` fields, the current service accepts two optional metadata fields:

| Field | Default | Description |
|-------|---------|-------------|
| `service_version` | `v1.0` | Version label used in Azure Blob Storage paths. |
| `branch` | repo default branch | Optional Git branch to clone and scan. Omit it to scan the repository default branch. |

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `source` | string | ✅ | — | GitHub repo URL. Must start with `https://github.com/`. The repo must have a `Dockerfile` at its root. |
| `scanners` | array | ✅ | — | Which scanners to use. Valid values: `"trivy"`, `"grype"`, or both. Duplicates are ignored. |
| `mode` | string | ❌ | `"sequential"` | `"sequential"` runs scanners one after another. `"parallel"` runs them at the same time. |

**Example — single scanner:**
```bash
curl -X POST http://localhost:8000/scan \
  -H "Content-Type: application/json" \
  -d '{
    "scanners": ["trivy"],
    "source": "https://github.com/docker/welcome-to-docker",
    "mode": "sequential"
  }'
```

**Example — both scanners in parallel:**
```bash
curl -X POST http://localhost:8000/scan \
  -H "Content-Type: application/json" \
  -d '{
    "scanners": ["trivy", "grype"],
    "source": "https://github.com/docker/welcome-to-docker",
    "mode": "parallel"
  }'
```

**Success response (`status: completed`) — parallel mode:**
```json
{
  "status": "completed",
  "mode": "parallel",
  "image": "sample-image:latest",
  "source": "https://github.com/docker/welcome-to-docker",
  "total_duration_seconds": 142.03,
  "results": {
    "trivy": "scanner-service/output/trivy_result.json",
    "grype": "scanner-service/output/grype_result.json"
  },
  "resource_usage": {
    "cpu_avg": "32.3%",
    "cpu_peak": "57.3%",
    "memory_avg": "3664.3MB",
    "memory_peak": "3871.2MB",
    "disk_used_avg": "83.24GB",
    "disk_used_peak": "84.34GB"
  },
  "per_scanner_metrics": {
    "trivy": {
      "pid": 12345,
      "start_time": "2026-05-05 12:46:56 UTC",
      "end_time": "2026-05-05 12:47:54 UTC",
      "duration_seconds": 58.22,
      "cpu_avg": "41.9%",
      "cpu_peak": "57.4%",
      "memory_avg": "3633.8MB",
      "memory_peak": "3871.2MB",
      "disk_used_avg": "83.45GB",
      "disk_used_peak": "84.34GB"
    },
    "grype": {
      "pid": 12346,
      "start_time": "2026-05-05 12:46:56 UTC",
      "end_time": "2026-05-05 12:49:14 UTC",
      "duration_seconds": 137.54,
      "cpu_avg": "32.4%",
      "cpu_peak": "57.3%",
      "memory_avg": "3667.8MB",
      "memory_peak": "3871.2MB",
      "disk_used_avg": "83.25GB",
      "disk_used_peak": "84.34GB"
    }
  }
}
```

**Success response (`status: completed`) — sequential mode:**
```json
{
  "status": "completed",
  "mode": "sequential",
  "image": "sample-image:latest",
  "source": "https://github.com/docker/welcome-to-docker",
  "total_duration_seconds": 181.23,
  "results": {
    "trivy": "scanner-service/output/trivy_result.json",
    "grype": "scanner-service/output/grype_result.json"
  },
  "resource_usage": {
    "cpu_avg": "27.1%",
    "cpu_peak": "54.0%",
    "memory_avg": "3578.7MB",
    "memory_peak": "3817.5MB",
    "disk_used_avg": "82.78GB",
    "disk_used_peak": "83.23GB"
  },
  "per_scanner_metrics": {
    "trivy": {
      "pid": 2305616,
      "start_time": "2026-05-05 12:58:58 UTC",
      "end_time": "2026-05-05 12:59:38 UTC",
      "duration_seconds": 39.13,
      "cpu_avg": "30.4%",
      "cpu_peak": "54.0%",
      "memory_avg": "3288.8MB",
      "memory_peak": "3425.9MB",
      "disk_used_avg": "82.52GB",
      "disk_used_peak": "82.94GB"
    },
    "grype": {
      "pid": 2305946,
      "start_time": "2026-05-05 12:59:38 UTC",
      "end_time": "2026-05-05 13:01:56 UTC",
      "duration_seconds": 138.54,
      "cpu_avg": "26.2%",
      "cpu_peak": "36.3%",
      "memory_avg": "3660.7MB",
      "memory_peak": "3817.5MB",
      "disk_used_avg": "82.86GB",
      "disk_used_peak": "83.23GB"
    }
  }
}
```

**Partial response (`status: partial`) — one scanner failed:**
```json
{
  "status": "partial",
  "results": {
    "trivy": "scanner-service/output/trivy_result.json"
  },
  "errors": {
    "grype": "grype failed: ..."
  }
}
```

**Error response:**
```json
{
  "status": "failed",
  "error": "Docker build failed: ..."
}
```

**Response field reference:**

| Field | Description |
|-------|-------------|
| `status` | `completed` = all scanners succeeded. `partial` = at least one scanner failed. `failed` = the whole request failed before scanning. |
| `mode` | The execution mode that was used. |
| `image` | Always `sample-image:latest` — the Docker image that was scanned. |
| `source` | The GitHub URL that was provided. |
| `total_duration_seconds` | Wall-clock time from request received to response sent. |
| `results` | Map of scanner name → path to the JSON result file on disk. |
| `resource_usage` | Host CPU, RAM and disk stats across the full scan window. |
| `per_scanner_metrics` | Per-scanner breakdown including PID, UTC start/end times, duration, CPU, RAM and disk. |
| `errors` | Only present when `status` is `partial`. Maps scanner name → error message. |

---

Current response additions:

| Field | Description |
|-------|-------------|
| `service_version` | Version label supplied in the request or defaulted to `v1.0`. |
| `branch` | Branch that was scanned, either detected from the clone or supplied in the request. |
| `commit_id` | Short Git commit SHA from the cloned repository. |
| `blob_urls` | Map of scanner name to uploaded Azure Blob URL for each successful scanner result. |
| `blob_errors` | Only present if local scanning succeeded but one or more Azure uploads failed. |

## Execution Modes Explained

### Sequential mode

```
Timeline:  [── trivy ──][── grype ──]
Monitoring: ProcessMonitor(trivy PID) then ProcessMonitor(grype PID)
```

- Trivy runs completely, then Grype starts
- Each scanner gets its own `ProcessMonitor` that polls the `docker run` process PID every 1 second
- Child processes of `docker run` (e.g. the actual scanner binary inside the container) are also included in the metrics
- `per_scanner_metrics` shows individual stats for each scanner
- `resource_usage` is a merged average/peak across both scanners' samples

### Parallel mode

```
Timeline:  [────── trivy ──────]
           [──────── grype ────────]
Monitoring: SystemMonitor (whole system, every 1s)
```

- Both scanners start simultaneously using `asyncio.gather()` + a thread pool executor
- A `SystemMonitor` tracks overall system CPU, RAM and disk for the duration
- `resource_usage` reflects system-wide CPU, RAM and disk usage during the parallel window
- `per_scanner_metrics` still shows per-scanner PID and duration from their individual `ProcessMonitor`

> **Which mode should I use?**  
> Use `sequential` when you want accurate per-scanner resource attribution.  
> Use `parallel` when you want faster total scan time and only care about overall system load.

---

## Resource Monitoring Explained

Resource monitoring is handled entirely in `monitor.py` using the `psutil` library.

### ProcessMonitor

Used in both sequential and parallel modes. Attached per scanner — each scanner gets its own monitor window.

- Starts a background thread when `start(pid)` is called
- Every 1 second, reads **host-level** `psutil.cpu_percent()`, `psutil.virtual_memory()` and `psutil.disk_usage("/")`
- Stops when `stop()` is called
- Computes `avg` and `peak` from all collected samples

> **Why host-level and not per-process?** The scanners run inside Docker containers. The `docker run` host process is just a thin wrapper — all the actual CPU and memory work happens inside the container kernel namespace which psutil cannot see directly. Polling the host gives accurate real-world server load numbers.

### SystemMonitor

Used only in parallel mode to track the whole machine for the entire parallel window.

- Polls `psutil.cpu_percent()`, `psutil.virtual_memory()` and `psutil.disk_usage("/")` every 1 second
- Measures used memory as `total - available`
- Measures used disk on the root `/` partition

### ResourceMetrics dataclass

Both monitors populate a `ResourceMetrics` object with:

| Field | Description |
|-------|-------------|
| `pid` | Process ID of the `docker run` host process |
| `start_time` / `end_time` | Human-readable UTC timestamps (e.g. `2026-05-05 12:46:56 UTC`) |
| `duration_seconds` | `end_time - start_time` |
| `cpu_avg` / `cpu_peak` | Average and peak host CPU percentage |
| `memory_avg` / `memory_peak` | Average and peak host RAM used in MB |
| `disk_used_avg` / `disk_used_peak` | Average and peak host disk used on `/` in GB |

---

## Output Files

Scan results are saved as JSON files inside the project directory:

```
scanner-service/
 └── output/
      ├── trivy_result.json
      └── grype_result.json
```

The `output/` directory is created automatically on first run and is listed in `.gitignore` so results are never committed to the repository.

These files are **overwritten on every scan** (the directory is wiped at the start of each `/scan` request).

Inspect results:
```bash
# Pretty-print and show first 60 lines
cat output/trivy_result.json | python3 -m json.tool | head -60
cat output/grype_result.json | python3 -m json.tool | head -60
```

> **Note:** The API response returns the absolute file paths, not the file contents. The files persist on disk between runs — they are only wiped when a new `/scan` request starts.

---

## Error Handling

| Scenario | HTTP Status | `status` field | Where it's caught |
|----------|-------------|----------------|-------------------|
| `scanners` is empty | 422 | `failed` | `main.py` — Pydantic validator |
| `source` is not a GitHub URL | 422 | `failed` | `main.py` — Pydantic validator |
| `git clone` fails | 422 | `failed` | `utils.py → clone_repository` |
| No `Dockerfile` in repo root | 422 | `failed` | `utils.py → clone_repository` |
| `docker build` fails | 500 | `failed` | `utils.py → build_docker_image` |
| Scanner timeout (600s exceeded) | 200 | `partial` | `scanner.py` — `docker stop` our container, `proc.wait()`, returns timeout error |
| All scanners fail | 200 | `partial` | `scanner.py` |
| Unexpected exception in scanner | 500 | `failed` | `main.py` — try/except around scanner call |
| Directory preparation fails | 500 | `failed` | `main.py` — try/except around prepare_directories |

> **Why is scanner failure HTTP 200 and not 500?**  
> Because the service itself worked correctly — it cloned, built, and attempted the scan. A scanner failure is a result, not a service crash. The `partial` status tells you to check the `errors` field.

---

## Working Directories

| Path | Purpose | Lifecycle |
|------|---------|-----------|
| `/tmp/repo` | Cloned GitHub repository | Wiped at the start of every `/scan` request |
| `output/` (inside project) | Scanner JSON result files | Wiped at the start of every `/scan` request |

Both directories are created by `run.py` on startup and re-created (if missing) by `utils.prepare_directories()` on each request.

`output/` uses an absolute path resolved from `__file__` so it always points to the correct location regardless of the working directory you run the script from.

## Common Errors & Fixes

| Error | Cause | Fix |
|-------|-------|-----|
| `[Errno 98] address already in use` | Port 8000 is taken by another process | `run.py` automatically tries ports 8001–8009 and picks the first free one |
| `HTTP 403 Forbidden` on `/scan` | `output/` directory does not exist or wrong permissions when Docker tries to mount it | `run.py` creates `output/` before starting the server. If it persists: `mkdir -p output && chmod 755 output` |
| `git clone` fails | Repo URL is wrong or network issue | Check the URL starts with `https://github.com/` and the repo is public |
| `No Dockerfile at root` | The repo doesn't have a `Dockerfile` at its root level | Use a repo that has a `Dockerfile` at the root, not in a subdirectory |

---

## Scan Observations & Analysis

This section documents what was observed on the host server during real scans of `docker/welcome-to-docker`.

---

### Sequential vs Parallel — Side by Side

| Metric | Sequential | Parallel |
|--------|-----------|---------|
| Total duration | 181.23s | 142.03s |
| Time saved | — | ~39s (22% faster) |
| CPU avg | 27.1% | 32.3% |
| CPU peak | 54.0% | 57.3% |
| RAM avg | 3578.7MB | 3664.3MB |
| RAM peak | 3817.5MB | 3871.2MB |
| Disk avg | 82.78GB | 83.24GB |
| Disk peak | 83.23GB | 84.34GB |

---

### Sequential Mode — Observations

**Trivy (12:58:58 → 12:59:38, 39s)**

- CPU spiked to **54.0%** — the highest point of the entire sequential run
- RAM started at ~3288MB and climbed to **3425.9MB** during the scan
- Disk grew from ~82.52GB to **82.94GB** — Trivy downloads its vulnerability database on first run and caches it inside the container layer

**Grype (12:59:38 → 13:01:56, 138s)**

- Grype took **3.5× longer** than Trivy (138s vs 39s) — Grype downloads and processes a larger vulnerability database
- CPU dropped to an average of **26.2%** and peaked at only **36.3%** — Grype is more I/O bound than CPU bound
- RAM climbed significantly to **3817.5MB peak** — Grype loads its full database into memory for matching
- Disk grew to **83.23GB peak** — Grype's database download accounts for the additional ~0.3GB

**Combined resource_usage (sequential)**

- CPU avg **27.1%** — lower than Trivy alone because Grype's long idle I/O wait pulls the average down
- RAM peak **3817.5MB** — driven entirely by Grype's in-memory database
- Disk peak **83.23GB** — net ~0.45GB increase from scanner database downloads

---

### Parallel Mode — Observations

- Both scanners started at the same timestamp (`12:46:56 UTC`) — confirmed truly concurrent
- CPU peak **57.3%** — slightly higher than sequential (54%) because both scanners competed for CPU simultaneously
- RAM peak **3871.2MB** — slightly higher than sequential (3817.5MB) because both databases were loaded at the same time
- Disk peak **84.34GB** — higher than sequential (83.23GB) because both scanners wrote their databases concurrently
- Total time **142s** vs sequential **181s** — parallel saved 39 seconds because Trivy (58s) ran entirely within Grype's window (137s)

---

### Key Takeaways

- **Grype is the bottleneck** — it takes 3–4× longer than Trivy regardless of mode. This is expected: Grype's vulnerability database is larger and it does more thorough matching.
- **Parallel mode is faster but uses more resources** — peak RAM and disk are higher because both scanners load their databases simultaneously. On a memory-constrained server, sequential is safer.
- **CPU is not the bottleneck** — peak CPU never exceeded 58% even in parallel mode. The bottleneck is network I/O (database downloads) and memory (database loading).
- **Disk grows during scans** — each scanner downloads its vulnerability database into the Docker layer cache. This is a one-time cost per scanner image version. Subsequent scans reuse the cached database and disk usage stabilises.
- **RAM returns to baseline after scan** — the memory spike is temporary. Once the scanner containers exit, the RAM is released back to the OS.

---

## External Service Integration

`client.py` is a standalone script that lets any external service trigger a scan dynamically without touching `run.py`.

`run.py` remains the setup and launch script with its hardcoded `POC_REQUEST`. `client.py` is the integration point for everything else.

### Prerequisites

The scanner service must already be running before calling `client.py`:
```bash
python3 run.py   # starts the server
```

### Usage

**Override source only (scanners and mode use defaults):**
```bash
python3 client.py --source https://github.com/org/repo
```

**Specify everything explicitly:**
```bash
python3 client.py \
  --source https://github.com/org/repo \
  --scanners trivy grype \
  --mode parallel \
  --service-version v1.2 \
  --branch develop
```

**Single scanner:**
```bash
python3 client.py --source https://github.com/org/repo --scanners trivy
```

**Full JSON payload — for service-to-service calls:**
```bash
python3 client.py --payload '{"source": "https://github.com/org/repo", "scanners": ["trivy", "grype"], "mode": "sequential"}'
```

**Custom host and port — if the service runs on a different machine or port:**
```bash
python3 client.py --host 192.168.1.10 --port 8001 --source https://github.com/org/repo
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--source` | — | GitHub repo URL. Required unless `--payload` is used. |
| `--scanners` | `trivy grype` | One or both of `trivy`, `grype`. |
| `--mode` | `sequential` | `sequential` or `parallel`. |
| `--payload` | — | Full JSON string. Overrides all other flags. |
| `--host` | `localhost` | Host where the scanner service is running. |
| `--port` | `8000` | Port the scanner service is listening on. |
| `--timeout` | `900` | Request timeout in seconds. |

Current `client.py` also supports `--service-version` (default `v1.0`) and `--branch` (optional) for Azure blob path metadata and branch-specific scans.

### Payload priority

```
--payload JSON  →  highest priority, overrides everything
--source / --scanners / --mode flags  →  used if --payload not provided
built-in defaults  →  fallback for any flag not specified
```

### Calling from another Python service

```python
import subprocess, json

result = subprocess.run(
    [
        "python3", "client.py",
        "--source", "https://github.com/org/repo",
        "--scanners", "trivy", "grype",
        "--mode", "parallel",
    ],
    capture_output=True, text=True
)
response = json.loads(result.stdout.split("[+] Scan response:\n")[1])
```

Or call the API directly without `client.py`:

```python
import urllib.request, json

payload = {
    "source": "https://github.com/org/repo",
    "scanners": ["trivy", "grype"],
    "mode": "sequential",
}
req = urllib.request.Request(
    "http://localhost:8000/scan",
    data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(req, timeout=900) as resp:
    result = json.loads(resp.read())
```
