# Container Image Scanning Service

A FastAPI service that accepts a GitHub repository URL, builds a Docker image from it, and scans it for security vulnerabilities using **Trivy** and/or **Grype** — with real-time CPU & memory monitoring per scanner.

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
13. [Temporary Directories](#temporary-directories)

---

## What This Service Does

In plain English:

1. `run.py` sends a hardcoded POST request to `/scan` (POC client)
2. The service wipes `/tmp/repo` and `output/` clean
3. It clones the GitHub repo into `/tmp/repo`
4. It builds a Docker image from the repo's `Dockerfile`
5. It runs Trivy and/or Grype as Docker containers — **while they run**, CPU & memory are tracked in real time
6. Results are saved to `output/` inside the project directory as JSON files
7. A structured JSON response is returned to the caller

> **Why Docker containers for scanners?**  
> Trivy and Grype do NOT need to be installed on the host. They run as Docker containers pulled from Docker Hub. This means zero manual tool installation.

---

## Project Structure

```
scanner-service/
 ├── main.py           # FastAPI app — defines /health and /scan endpoints
 ├── scanner.py        # Core scan logic — sequential and parallel modes
 ├── monitor.py        # CPU & memory tracking using psutil
 ├── utils.py          # Directory prep, git clone, docker build helpers
 ├── run.py            # One-command setup and launch script
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
     ├── kills any process on port 8000 (prevents EADDRINUSE on re-runs)
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
                          ├── ProcessMonitor  → background thread, polls PID every 1s
                          │                     includes child processes of docker run
                          └── SystemMonitor   → background thread, polls whole system every 1s
                               │
                          scanner.py writes output files
                          ├── trivy → writes output/trivy_result.json via -o flag
                          └── grype → stdout captured → written by run_scanner_sync()
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
| `psutil` | ≥ 5.9.8 | CPU and memory monitoring |

### Step 5 — Pull scanner Docker images

```
[*] Pulling scanner images...
    trivy (aquasec/trivy:latest) already present, skipping
    Pulling anchore/grype:latest ...
    grype ready
```

For each scanner image, it first runs `docker image inspect` to check if it's already cached locally. Only pulls if not present. This avoids re-downloading large images on every restart.

| Scanner | Docker Image |
|---------|-------------|
| Trivy | `aquasec/trivy:latest` |
| Grype | `anchore/grype:latest` |

### Step 6 — Create working directories

```
[*] Ensuring required directories exist...
    /tmp/repo OK
    /home/user/scanner-service/output OK
```

Creates `/tmp/repo` (where repos are cloned) and `output/` inside the project directory (where scan results are saved). Uses `os.makedirs(..., exist_ok=True)` so it never fails if they already exist. The `output/` path is resolved as an absolute path using `os.path.abspath(__file__)` so it always points to the right place regardless of where you run the script from.

### Step 6b — Kill any existing process on port 8000

Before starting uvicorn, `run.py` runs `fuser -k 8000/tcp` to kill any process already holding port 8000. This prevents the `[Errno 98] address already in use` error on re-runs. A 1-second sleep follows to give the OS time to release the port.

### Step 7 — Start the API server

```
[*] Starting API server...
    API:   http://localhost:8000
    Docs:  http://localhost:8000/docs
```

Launches `uvicorn main:app --host 0.0.0.0 --port 8000` using `subprocess.Popen`, which starts uvicorn as a **child process** and returns immediately. `run.py` stays alive so it can send the POC request and keep the server running after.

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
        "source": "https://github.com/docker/getting-started",
        "mode": "sequential"
    }

[+] Scan response:
{ "status": "completed", ... }
```

`run.py` acts as the POC client — it sends a hardcoded `POST /scan` request using Python's built-in `urllib.request` (no extra dependencies). The `POC_REQUEST` dict at the top of `run.py` is where you change the repo URL, scanners, or mode. The full JSON response is printed to the terminal.

### Step 10 — Keep server running

```
[*] Server still running. Press Ctrl+C to stop.
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
    "source": "https://github.com/docker/getting-started",
    "mode": "sequential",
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
- `ProcessMonitor.start(pid)` spins up a background thread that polls `psutil.Process(pid)` every 1 second
- `proc.communicate(timeout=600)` blocks until the scanner finishes, collecting stdout/stderr
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
- `SystemMonitor` polls `psutil.cpu_percent()` and `psutil.virtual_memory()` for the whole machine every 1 second
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

### Stage 8 — Build and return response (`main.py`)

`main.py` assembles the final response dict and returns `JSONResponse(status_code=200)`:

- `status` → `"completed"` if errors dict is empty, `"partial"` if any scanner failed
- `results` → map of scanner name to output file path
- `resource_usage` → combined cpu/mem (merged ProcessMonitor samples in sequential, SystemMonitor in parallel)
- `per_scanner_metrics` → per-scanner pid, duration, cpu avg/peak, memory avg/peak
- `errors` → only included when `status` is `"partial"`

### Stage 9 — run.py prints response to terminal

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
    "source": "https://github.com/docker/getting-started",
    "mode": "sequential"
  }'
```

**Example — both scanners in parallel:**
```bash
curl -X POST http://localhost:8000/scan \
  -H "Content-Type: application/json" \
  -d '{
    "scanners": ["trivy", "grype"],
    "source": "https://github.com/docker/getting-started",
    "mode": "parallel"
  }'
```

**Success response (`status: completed`):**
```json
{
  "status": "completed",
  "mode": "parallel",
  "image": "sample-image:latest",
  "source": "https://github.com/docker/getting-started",
  "total_duration_seconds": 142.5,
  "results": {
    "trivy": "/home/user/scanner-service/output/trivy_result.json",
    "grype": "/home/user/scanner-service/output/grype_result.json"
  },
  "resource_usage": {
    "cpu_avg": "45.2%",
    "cpu_peak": "80.1%",
    "memory_avg": "312.4MB",
    "memory_peak": "498.7MB"
  },
  "per_scanner_metrics": {
    "trivy": {
      "pid": 12345,
      "duration_seconds": 38.1,
      "cpu_avg": "42.0%",
      "cpu_peak": "75.3%",
      "memory_avg": "280.0MB",
      "memory_peak": "420.0MB"
    },
    "grype": {
      "pid": 12346,
      "duration_seconds": 41.7,
      "cpu_avg": "48.5%",
      "cpu_peak": "80.1%",
      "memory_avg": "344.8MB",
      "memory_peak": "498.7MB"
    }
  }
}
```

**Partial response (`status: partial`) — one scanner failed:**
```json
{
  "status": "partial",
  "results": {
    "trivy": "/home/user/scanner-service/output/trivy_result.json"
  },
  "errors": {
    "grype": "grype failed: ..."
  },
  ...
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
| `resource_usage` | Combined CPU/memory stats across all scanners. |
| `per_scanner_metrics` | Per-scanner breakdown including PID, duration, CPU, and memory. |
| `errors` | Only present when `status` is `partial`. Maps scanner name → error message. |

---

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
- A `SystemMonitor` tracks overall system CPU and memory (not per-process) for the duration
- `resource_usage` reflects system-wide usage during the parallel window
- `per_scanner_metrics` still shows per-scanner PID and duration from their individual `ProcessMonitor`

> **Which mode should I use?**  
> Use `sequential` when you want accurate per-scanner resource attribution.  
> Use `parallel` when you want faster total scan time and only care about overall system load.

---

## Resource Monitoring Explained

Resource monitoring is handled entirely in `monitor.py` using the `psutil` library.

### ProcessMonitor

Used in both sequential and parallel modes to track a specific scanner process.

- Starts a background thread when `start(pid)` is called
- Every 1 second, reads `cpu_percent` and `memory_info().rss` from the process
- Also walks all child processes (recursive) and adds their CPU/memory to the sample
- Stops when `stop()` is called
- Computes `avg` and `peak` from all collected samples

### SystemMonitor

Used only in parallel mode to track the whole machine.

- Polls `psutil.cpu_percent()` and `psutil.virtual_memory()` every 1 second
- Measures used memory as `total - available`
- Gives a system-wide view of load during the parallel scan window

### ResourceMetrics dataclass

Both monitors populate a `ResourceMetrics` object with:

| Field | Description |
|-------|-------------|
| `pid` | Process ID being monitored (None for SystemMonitor) |
| `start_time` / `end_time` | Unix timestamps |
| `duration_seconds` | `end_time - start_time` |
| `cpu_avg` / `cpu_peak` | Average and peak CPU percentage |
| `memory_avg` / `memory_peak` | Average and peak RSS memory in MB |

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
| One scanner fails, others succeed | 200 | `partial` | `scanner.py → run_scanner_sync` |
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
| `[Errno 98] address already in use` | A previous uvicorn process is still holding port 8000 | `run.py` handles this automatically with `fuser -k 8000/tcp`. If it persists: `pkill -f "uvicorn main:app"` |
| `HTTP 403 Forbidden` on `/scan` | `output/` directory does not exist or wrong permissions when Docker tries to mount it | `run.py` creates `output/` before starting the server. If it persists: `mkdir -p output && chmod 755 output` |
| `git clone` fails | Repo URL is wrong or network issue | Check the URL starts with `https://github.com/` and the repo is public |
| `No Dockerfile at root` | The repo doesn't have a `Dockerfile` at its root level | Use a repo that has a `Dockerfile` at the root, not in a subdirectory |
