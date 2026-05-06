# Technical Documentation

This document explains the internal design of the scanner service in detail. The
`README.md` is intentionally user friendly; this file is for developers,
reviewers, operators, and future maintainers who need to understand how the
project works under the hood.

## Project Summary

The project is a FastAPI service that scans container images built from source
repositories. A client sends a scan request to the API. The service prepares a
clean workspace, clones or copies a repository, builds a Docker image from its
root `Dockerfile`, runs one or more vulnerability scanners against that image,
records resource usage during the scan, uploads scanner JSON output files to
Azure Blob Storage, and returns a structured response.

The current scanner engines are:

- Trivy, executed through the Docker image `aquasec/trivy:latest`
- Grype, executed through the Docker image `anchore/grype:latest`

The project does not require Trivy or Grype to be installed directly on the
host. Scanner binaries run inside Docker containers. The host does need Docker,
Git, Python, and access to the Docker daemon.

## High-Level Architecture

At runtime, the service has these major layers:

1. API layer: `main.py`
   Accepts HTTP requests, validates payloads, orchestrates the scan pipeline,
   and formats HTTP responses.

2. Repository and Docker utility layer: `utils.py`
   Handles filesystem preparation, Git clone/local copy, Docker image build,
   repository metadata extraction, and cleanup of the built image.

3. Scanner execution layer: `scanner.py`
   Builds scanner Docker commands and runs Trivy/Grype either sequentially or in
   parallel.

4. Resource monitoring layer: `monitor.py`
   Samples CPU, memory, and disk usage during scanner execution.

5. Blob storage layer: `blob_storage.py`
   Uploads JSON scan outputs to Azure Blob Storage.

6. CLI/helper scripts:
   `run.py` bootstraps and launches a demo service flow.
   `client.py` sends requests to an already-running service.
   `setup_scanners.py` pulls scanner images.

## Runtime Flow

The main `/scan` request follows this sequence:

1. Validate JSON payload with `ScanRequest`.
2. Clean and recreate runtime directories with `prepare_directories(clean=True)`.
3. Clone a GitHub repository or copy a local repository into `/tmp/repo`.
4. Validate that `/tmp/repo/Dockerfile` exists.
5. Extract metadata: app name, branch, commit ID.
6. Build Docker image `sample-image:latest` from `/tmp/repo`.
7. Run requested scanners:
   - sequential mode: one scanner after another
   - parallel mode: scanner jobs concurrently via `asyncio` thread executor
8. Remove `sample-image:latest` from the local Docker daemon.
9. Upload successful scanner result JSON files to Azure Blob Storage.
10. Return one JSON response containing status, result file paths, resource
    usage, per-scanner metrics, blob URLs, and any errors.

The built image tag is fixed today:

```text
sample-image:latest
```

That makes the implementation simple for a proof of concept, but it also means
concurrent API requests can collide because multiple requests would try to use
the same local image tag and workspace. See "Concurrency and Isolation" below.

## Repository Layout

```text
.
|-- .env
|-- .gitignore
|-- README.md
|-- TECHNICAL.md
|-- blob_storage.py
|-- client.py
|-- main.py
|-- monitor.py
|-- requirements.txt
|-- run.py
|-- scanner.py
|-- setup_scanners.py
`-- utils.py
```

Runtime-generated paths:

```text
/tmp/repo          cloned/copied repository workspace
./output           scanner JSON output files
./venv             local Python virtual environment created by run.py
```

`venv/`, `output/`, `.env`, Python bytecode, and `.DS_Store` are ignored by
Git.

## Configuration

### Environment Variables

`AZURE_STORAGE_CONNECTION_STRING`

Required for successful blob uploads. Loaded by `blob_storage.py`. If missing,
the scan can still run locally, but upload attempts fail and the API response
will include `blob_errors`.

`AZURE_STORAGE_CONTAINER`

Optional. Defaults to:

```text
scan-results
```

`PORT`

Used by `run.py` as an initial port value, although `run.py` then searches for
an available port between `8000` and `8009`.

### Python Dependencies

The dependencies are listed in `requirements.txt`:

- `fastapi`: HTTP API framework.
- `uvicorn[standard]`: ASGI server for FastAPI.
- `psutil`: CPU, memory, and disk sampling.
- `azure-storage-blob`: Azure Blob Storage client.
- `python-dotenv`: loads `.env` values during local development.

### External System Dependencies

The service also depends on tools outside Python:

- Docker CLI and Docker daemon.
- Git CLI.
- Network access to GitHub for remote repositories.
- Network access to Docker Hub for pulling scanner images.
- Network access to Azure Blob Storage for uploads.

## API Design

### FastAPI App

Defined in `main.py`:

```python
app = FastAPI(
    title="Container Image Scanning Service",
    description="Scans Docker images with Trivy and/or Grype with resource monitoring.",
    version="1.0.0",
)
```

The application exposes two endpoints:

- `GET /health`
- `POST /scan`

### `GET /health`

Function:

```python
def health()
```

Returns:

```json
{"status": "ok"}
```

Purpose:

- Simple readiness check for scripts and operators.
- Used by `run.py` to wait until the Uvicorn server is available.

### `POST /scan`

Function:

```python
def scan(request: ScanRequest)
```

This is the primary orchestration endpoint. It is synchronous from FastAPI's
point of view. The scanner layer may run work sequentially or in parallel, but
the HTTP request remains open until the scan completes.

Request model:

```python
class ScanRequest(BaseModel):
    scanners: list[Literal["trivy", "grype"]]
    source: str
    mode: Literal["sequential", "parallel"] = "sequential"
    service_version: str = "v1.0"
    branch: Optional[str] = None
    token: Optional[str] = None
```

Fields:

- `scanners`: list of scanner names. Only `trivy` and `grype` are accepted.
- `source`: GitHub URL or local path.
- `mode`: `sequential` or `parallel`.
- `service_version`: logical service version included in the blob path.
- `branch`: optional Git branch passed to `git clone --branch`.
- `token`: optional GitHub personal access token for private repos.

Validation behavior:

- `scanners` cannot be empty.
- Duplicate scanner names are removed while preserving first-seen order.
- `source` is stripped.
- Local paths are accepted if they start with `/`, `./`, or `../`.
- Non-local sources must start with `https://github.com/`.
- `service_version` cannot be empty after trimming.
- `branch` cannot be empty if provided.

Response status behavior:

- Directory preparation failure: HTTP `500`.
- Clone/copy/source validation failure: HTTP `422`.
- Docker build failure: HTTP `500`.
- Unexpected scanner execution exception: HTTP `500`.
- Scanner-level failures: HTTP `200` with `status: "partial"`.
- Successful scan execution: HTTP `200` with `status: "completed"`.
- Blob upload failures do not fail the scan response; they are recorded in
  `blob_errors`.

Example successful response shape:

```json
{
  "status": "completed",
  "mode": "parallel",
  "image": "sample-image:latest",
  "source": "https://github.com/docker/welcome-to-docker",
  "service_version": "v1.0",
  "branch": "main",
  "commit_id": "abc1234",
  "total_duration_seconds": 42.5,
  "results": {
    "trivy": "C:\\path\\to\\project\\output\\trivy_result.json",
    "grype": "C:\\path\\to\\project\\output\\grype_result.json"
  },
  "resource_usage": {
    "cpu_avg": "18.4%",
    "cpu_peak": "71.2%",
    "memory_avg": "8200.3MB",
    "memory_peak": "9001.5MB",
    "disk_used_avg": "123.45GB",
    "disk_used_peak": "123.70GB"
  },
  "per_scanner_metrics": {
    "trivy": {},
    "grype": {}
  },
  "blob_urls": {
    "trivy": "https://...",
    "grype": "https://..."
  }
}
```

## `main.py`

`main.py` is the API entry point.

### Constants

`IMAGE_TAG`

```python
IMAGE_TAG = "sample-image:latest"
```

The built image is always tagged with this name. The scanners are also hardcoded
to scan this same image tag in `scanner.py`. If this tag changes in one module,
it must be changed in the other or centralized into shared configuration.

### Imports

`main.py` imports:

- FastAPI and JSONResponse for HTTP behavior.
- Pydantic `BaseModel` and `field_validator` for request validation.
- Utility functions from `utils.py`.
- scanner runners from `scanner.py`.
- `upload_scan_results` from `blob_storage.py`.

### `ScanRequest.scanners_not_empty`

```python
@field_validator("scanners")
def scanners_not_empty(cls, v)
```

Responsibilities:

- Rejects an empty list.
- Deduplicates scanner names using `dict.fromkeys`, which preserves order.

Design reason:

- If the request accidentally includes `["trivy", "trivy"]`, the scanner does
  not run twice and overwrite the same output file.

### `ScanRequest.source_valid`

```python
@field_validator("source")
def source_valid(cls, v)
```

Responsibilities:

- Strips leading/trailing whitespace.
- Allows local paths identified by `_is_local_path`.
- Rejects non-GitHub URLs.

Design reason:

- The service is intentionally scoped to GitHub URLs and local paths.
- Local path existence is not checked here because validation should only verify
  request shape; filesystem copy is handled by `clone_repository`.

### `ScanRequest.version_not_empty`

Rejects blank `service_version`.

Design reason:

- `service_version` becomes part of the Azure blob path. Empty values would
  create confusing or malformed paths.

### `ScanRequest.branch_not_empty`

Allows `None`, rejects blank strings.

Design reason:

- `None` means "use repository default branch".
- Empty strings usually mean a client mistake.

### `health`

Returns a small static health payload.

### `scan`

Primary workflow:

1. Capture start time for total duration.
2. Log scanners, mode, source, and branch.
3. Prepare clean directories.
4. Clone/copy repository.
5. Get repository metadata.
6. Build Docker image.
7. Run scanners using `run_sequential` or `run_parallel`.
8. Remove the built image.
9. Upload each successful scanner result to Azure Blob Storage.
10. Build and return the JSON response.

Important error-handling decisions:

- Directory preparation is fatal.
- Clone/copy failure is treated as a bad/unprocessable request.
- Docker build failure is fatal.
- Scanner exceptions are fatal, but individual scanner failures are partial.
- Docker image cleanup is attempted after scanner execution.
- Blob upload failure is nonfatal because scan results may still exist locally.

Cleanup caveat:

- If Docker build succeeds but later blob upload fails, the image has already
  been removed.
- If Docker build succeeds and scan runners return normally, cleanup happens.
- If a failure happens after build but before the explicit cleanup call, only
  the scanner exception block removes the image. Future refactoring should use
  `try/finally` around build/scan/upload to make cleanup more consistent.

## `utils.py`

`utils.py` owns local workspace preparation, repository staging, Docker build,
metadata extraction, and image cleanup.

### Constants

`BASE_DIR`

Absolute path to the directory containing `utils.py`.

`REPO_DIR`

```python
REPO_DIR = "/tmp/repo"
```

This is the canonical working repository path. Whether the input was GitHub or
local, the rest of the service expects the repository here.

On Windows, `/tmp/repo` resolves according to the active environment. This can
work under Git Bash/WSL-like environments, but pure Windows deployments may
prefer a project-local temp path or `tempfile.gettempdir()`.

`OUTPUT_DIR`

Project-local `output` directory used for scanner JSON files.

### `prepare_directories`

```python
def prepare_directories(clean: bool = True)
```

Responsibilities:

- Ensures `/tmp/repo` exists.
- Ensures `./output` exists.
- Optionally wipes contents from both directories.

Design reason:

- Each scan starts from a clean workspace.
- Scanner output does not accidentally mix with previous scan files.

Operational impact:

- This function is destructive inside `REPO_DIR` and `OUTPUT_DIR`.
- It assumes those directories are dedicated to this service.

### `_clean_dir`

```python
def _clean_dir(path: str)
```

Responsibilities:

- Iterates over the direct contents of `path`.
- Removes child directories with `shutil.rmtree`.
- Removes files with `os.remove`.
- Logs a warning if an item cannot be removed.

Design reason:

- Cleaning child entries instead of deleting the root keeps the root directory
  stable and avoids needing to recreate permissions each time.

### `_is_local_path`

```python
def _is_local_path(source: str) -> bool
```

Returns true when `source` starts with:

- `/`
- `./`
- `../`

Design reason:

- Keeps API validation simple.
- Differentiates local filesystem input from GitHub URL input.

Limitations:

- Windows absolute paths like `C:\path\repo` are not considered local by this
  function.
- UNC paths are not recognized.
- If Windows-native paths are required, this function should be extended.

### `_inject_token`

```python
def _inject_token(url: str, token: str) -> str
```

Transforms:

```text
https://github.com/org/repo
```

into:

```text
https://<token>@github.com/org/repo
```

Purpose:

- Allows `git clone` to access private GitHub repositories.

Security behavior:

- The tokenized URL is not logged directly.
- Clone errors are sanitized by replacing the token with `***` if needed.

Security caveat:

- Embedding tokens in command arguments can still expose tokens to process
  listings on some systems. A safer production approach is to use Git credential
  helpers, deploy keys, short-lived credentials, or `GIT_ASKPASS`.

### `clone_repository`

```python
def clone_repository(source: str, branch: str = None, token: str = None) -> tuple[bool, str]
```

Responsibilities:

- Accepts either local path or GitHub URL.
- For local paths:
  - Converts to absolute path.
  - Checks directory existence.
  - Copies tree into `REPO_DIR` with `dirs_exist_ok=True`.
- For GitHub URLs:
  - Builds `git clone --depth 1`.
  - Adds `--branch <branch>` when branch is provided.
  - Injects token into clone URL if provided.
  - Runs clone with a 300 second timeout.
  - Sanitizes token from clone errors.
- Verifies a root-level `Dockerfile` exists in `REPO_DIR`.

Returns:

- `(True, "Repository ready.")` on success.
- `(False, "...")` on failure.

Design decisions:

- Shallow clone is used to reduce time and bandwidth.
- Root-level Dockerfile is required because `build_docker_image` builds from
  `REPO_DIR` without a custom Dockerfile path.
- Local repositories are copied rather than built in place so later pipeline
  steps do not need separate paths.

Limitations:

- Repositories with Dockerfiles in subdirectories are not supported.
- Git submodules are not initialized.
- Shallow clone may not include history required by some builds.
- Local copy can include large ignored/build artifacts unless the source path is
  already clean.

### `build_docker_image`

```python
def build_docker_image(tag: str = "sample-image:latest") -> tuple[bool, str]
```

Responsibilities:

- Runs:

```text
docker build -t <tag> /tmp/repo
```

- Captures stdout/stderr.
- Enforces a 600 second timeout.
- Returns success boolean and message.

Design reason:

- Scanner tools operate on Docker images, so repository source is first
  transformed into a local image.

Limitations:

- No build arguments are supported.
- No target stage selection is supported.
- No custom Dockerfile path is supported.
- No platform override is supported.
- Build timeout is fixed at 600 seconds.

### `get_repo_info`

```python
def get_repo_info(source: str, branch: str = None) -> dict
```

Responsibilities:

- Determines `appname`.
- Determines short `commit_id`.
- Determines branch name.

App name logic:

- Local path: basename of the absolute source path.
- URL: final URL segment, with `.git` removed.

Commit ID logic:

- Runs:

```text
git -C /tmp/repo rev-parse HEAD
```

- Uses first seven characters.
- Falls back to `unknown` if Git metadata is unavailable.

Branch logic:

- Runs:

```text
git -C /tmp/repo rev-parse --abbrev-ref HEAD
```

- Uses detected branch unless result is empty or `HEAD`.
- Falls back to request branch or `main`.

Design reason:

- Blob paths should include enough metadata to group scan results by app,
  version, branch, and commit.

### `remove_docker_image`

```python
def remove_docker_image(tag: str = "sample-image:latest")
```

Responsibilities:

- Runs:

```text
docker rmi -f <tag>
```

- Logs success or warning.
- Does not raise on failure.

Design reason:

- Cleanup should free disk space but should not crash the API response after the
  scan has already completed.

## `scanner.py`

`scanner.py` owns scanner command construction and execution.

### Constants

`BASE_DIR`

Absolute project directory.

`OUTPUT_DIR`

Project-local output folder. This path is bind-mounted into scanner containers.

`IMAGE_TAG`

The image scanned by Trivy and Grype:

```text
sample-image:latest
```

`TRIVY_IMAGE`

```text
aquasec/trivy:latest
```

`GRYPE_IMAGE`

```text
anchore/grype:latest
```

These values must stay aligned with `setup_scanners.py` and `run.py`.

### Scanner Container Model

Both scanners run as containers using `docker run`.

Shared options:

```text
docker run --rm --name <container_name> --network host \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v <project>/output:/output \
  <scanner_image> ...
```

Mounts:

- `/var/run/docker.sock:/var/run/docker.sock`
  Lets the scanner container communicate with the host Docker daemon and inspect
  `sample-image:latest`.
- `<project>/output:/output`
  Lets scan result files land on the host filesystem.

Security implications:

- Mounting the Docker socket gives scanner containers powerful access to the
  host Docker daemon. This is common for Docker-based scanner workflows but
  should be treated as privileged in production threat models.

### `_docker_run_base`

```python
def _docker_run_base(scanner_image: str, container_name: str) -> list[str]
```

Responsibilities:

- Builds the common `docker run` argument list.
- Adds `--rm` so the scanner container is removed after completion.
- Assigns a unique container name.
- Uses host networking.
- Mounts Docker socket and output directory.

Design reason:

- Trivy and Grype share the same container execution envelope, so centralizing
  it avoids command drift.

### `trivy_cmd`

```python
def trivy_cmd(output_filename: str, container_name: str) -> list[str]
```

Builds the full Trivy command:

```text
trivy image -f json -o /output/<output_filename> sample-image:latest
```

Output behavior:

- Trivy writes JSON directly to `/output/<file>` inside the container.
- Because `/output` is bind-mounted, the file appears under project `output/`.

### `grype_cmd`

```python
def grype_cmd(container_name: str) -> list[str]
```

Builds the full Grype command:

```text
grype sample-image:latest -o json
```

Output behavior:

- Grype writes JSON to stdout.
- `run_scanner_sync` captures stdout and writes it to
  `output/grype_result.json`.

### `_stop_container`

```python
def _stop_container(container_name: str)
```

Responsibilities:

- Runs:

```text
docker stop --time 10 <container_name>
```

- Used when scanner execution exceeds the 600 second timeout.

Design reason:

- Stops only the container launched for that scanner instead of killing unrelated
  Docker workloads.

### `run_scanner_sync`

```python
def run_scanner_sync(scanner: Literal["trivy", "grype"]) -> tuple[bool, str, ResourceMetrics]
```

Responsibilities:

- Creates output path:

```text
output/<scanner>_result.json
```

- Creates unique container name:

```text
<scanner>-scan-<unix_timestamp>
```

- Starts a `ProcessMonitor`.
- Launches the scanner Docker container with `subprocess.Popen`.
- Waits for completion with a 600 second timeout.
- Stops the scanner container on timeout.
- Returns scanner success/failure, output path/error string, and metrics.

For Trivy:

- Uses `trivy_cmd`.
- Trivy writes output file itself.
- Nonzero return code is treated as scanner failure.

For Grype:

- Uses `grype_cmd`.
- Captures stdout.
- Writes stdout bytes to `output/grype_result.json`.
- Nonzero return code is treated as scanner failure.

Return tuple:

```python
(success, output_file_path_or_error_message, metrics)
```

Design decisions:

- A single function handles both scanners because their control flow is similar.
- Output path naming is stable and predictable.
- Timeout protects the API from hanging forever on a stuck scanner.

Concurrency caveat:

- Container names use `int(time.time())`, which has one-second precision. In
  unusual cases, concurrent runs of the same scanner in different API requests
  could collide. Parallel mode within one request runs different scanners, so
  `trivy` and `grype` names do not collide with each other.

### `run_sequential`

```python
def run_sequential(scanners: list[str]) -> dict
```

Responsibilities:

- Runs each scanner in request order.
- Collects successful result paths in `results`.
- Collects failures in `errors`.
- Stores each scanner's metrics in `per_scanner_metrics`.
- Merges all scanner metric samples with `merge_metrics`.

Return shape:

```python
{
    "results": {...},
    "errors": {...},
    "resource_usage": {...},
    "per_scanner_metrics": {...}
}
```

Design reason:

- Sequential mode gives cleaner per-scanner timing and avoids scanners competing
  for CPU, memory, disk, network, and Docker daemon resources.

### `_run_scanner_async`

```python
async def _run_scanner_async(scanner: Literal["trivy", "grype"], sys_monitor: SystemMonitor)
```

Responsibilities:

- Gets the current event loop.
- Runs blocking `run_scanner_sync` in a thread executor.
- Returns scanner name plus the sync scanner result tuple.

Note:

- The `sys_monitor` argument is currently not used inside this function. System
  monitoring is started/stopped by `run_parallel_async`.

### `run_parallel_async`

```python
async def run_parallel_async(scanners: list[str]) -> dict
```

Responsibilities:

- Starts `SystemMonitor`.
- Creates one async task per scanner.
- Runs tasks concurrently using `asyncio.gather`.
- Stops `SystemMonitor`.
- Collects results, errors, and per-scanner metrics.
- Uses system-level monitor samples for aggregate `resource_usage`.

Design reason:

- In parallel mode, per-process monitor samples can overlap and double-count
  host resource usage. The overall system monitor gives a more realistic
  aggregate picture during the concurrent scan window.

Exception behavior:

- `asyncio.gather(..., return_exceptions=True)` prevents one task exception from
  cancelling all other scanner tasks.
- Exceptions are logged and skipped.

### `run_parallel`

```python
def run_parallel(scanners: list[str]) -> dict
```

Synchronous wrapper around:

```python
asyncio.run(run_parallel_async(scanners))
```

Design reason:

- Lets `main.py` call parallel scanning from a normal synchronous endpoint.

## `monitor.py`

`monitor.py` provides resource sampling using `psutil`.

Metrics are host-level samples. Despite `ProcessMonitor` receiving a PID, it
does not currently sample only that process. It samples total host CPU, total
used memory, and root disk usage while the scanner process is running.

### `ResourceMetrics`

```python
@dataclass
class ResourceMetrics:
    pid: Optional[int] = None
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    cpu_samples: list = field(default_factory=list)
    mem_samples: list = field(default_factory=list)
    disk_samples: list = field(default_factory=list)
```

Stored values:

- `pid`: process ID associated with the monitor.
- `start_time`: Unix timestamp when monitor starts.
- `end_time`: Unix timestamp when monitor stops.
- `cpu_samples`: host CPU percent samples.
- `mem_samples`: used RAM in MB.
- `disk_samples`: used disk on root partition in GB.

Computed properties:

- `duration_seconds`
- `cpu_avg`
- `cpu_peak`
- `mem_avg`
- `mem_peak`
- `disk_avg`
- `disk_peak`

All averages and peaks are returned as formatted strings, such as `12.4%`,
`1024.8MB`, or `70.31GB`.

### `ResourceMetrics.to_dict`

```python
def to_dict(self) -> dict
```

Responsibilities:

- Converts timestamps to UTC strings.
- Includes duration, CPU, memory, and disk summaries.
- Produces API-ready metric dictionaries.

Timestamp format:

```text
YYYY-MM-DD HH:MM:SS UTC
```

### `ProcessMonitor`

```python
class ProcessMonitor
```

Despite the name, it currently measures host-level resources during a process
window, not per-process resource usage.

Constructor:

```python
def __init__(self, interval: float = 1.0)
```

Creates:

- polling interval
- empty `ResourceMetrics`
- background thread placeholder
- stop event

`start(pid)`

- Resets metrics with PID and start time.
- Clears stop event.
- Starts daemon polling thread.

`stop()`

- Signals stop event.
- Joins polling thread with a 5 second limit.
- Records end time.

`_poll()`

- Initializes psutil CPU counter.
- Sleeps for `interval`.
- Samples:
  - `psutil.cpu_percent`
  - `psutil.virtual_memory`
  - `psutil.disk_usage("/")`
- Appends values to metrics sample lists.

### `SystemMonitor`

```python
class SystemMonitor
```

Same sampling model as `ProcessMonitor`, but no PID is associated with it.

Used by:

- `run_parallel_async`

Purpose:

- Captures aggregate host resource usage during all concurrent scanner work.

### `merge_metrics`

```python
def merge_metrics(metrics_list: list[ResourceMetrics]) -> dict
```

Responsibilities:

- Flattens CPU samples from multiple monitors.
- Flattens memory samples from multiple monitors.
- Flattens disk samples from multiple monitors.
- Computes aggregate average and peak values.

Used by:

- `run_sequential`

Design reason:

- In sequential mode, scanner windows do not overlap, so merging all samples into
  one combined scan summary is reasonable.

## `blob_storage.py`

`blob_storage.py` uploads scanner output JSON files to Azure Blob Storage.

### `.env` Loading

At import time:

```python
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
```

Purpose:

- Allows local developers to put Azure configuration into `.env`.
- Keeps the module usable even if `python-dotenv` is absent, though it is listed
  in `requirements.txt`.

### `CONTAINER_NAME`

```python
CONTAINER_NAME = os.environ.get("AZURE_STORAGE_CONTAINER", "scan-results")
```

The target Azure Blob container. Defaults to `scan-results`.

### `_get_client`

```python
def _get_client()
```

Responsibilities:

- Reads `AZURE_STORAGE_CONNECTION_STRING`.
- Raises `EnvironmentError` if missing.
- Imports `BlobServiceClient`.
- Returns a client created from the connection string.

Design reason:

- Azure import and client creation are isolated to one place.
- Missing configuration fails with a clear message.

### `upload_scan_results`

```python
def upload_scan_results(
    local_file_path: str,
    appname: str,
    service_version: str,
    branch: str,
    commit_id: str,
    scanner: str,
) -> str
```

Blob path:

```text
<appname>/<service_version>/<branch>/<commit_id>/<scanner>_results.json
```

Example:

```text
welcome-to-docker/v1.0/main/abc1234/trivy_results.json
```

Responsibilities:

- Build blob name.
- Get Azure Blob service client.
- Create target container if it does not already exist.
- Get blob client for the result path.
- Upload local JSON file with `overwrite=True`.
- Return public/client URL from `blob_client.url`.

Error behavior:

- Logs upload failure.
- Re-raises exception to caller.
- `main.py` catches this and records it in `blob_errors`.

Design reason:

- Scan execution should be separable from storage persistence. Upload failures
  are visible but do not erase the local result paths from the API response.

## `run.py`

`run.py` is a convenience bootstrapper and proof-of-concept runner. It performs
environment checks, creates a virtual environment, installs dependencies, pulls
scanner images, starts the API server, sends a hardcoded scan request, then keeps
the server running.

It is intentionally more operational than library-like. Much of its work happens
at module top level, so importing `run.py` would execute setup. It should be run
as a script.

### Constants

`VENV_DIR`

```text
venv
```

Local virtual environment directory.

`ALL_SCANNER_IMAGES`

Maps scanner names to Docker image references.

`POC_REQUEST`

Hardcoded scan request:

```python
{
    "scanners": ["trivy", "grype"],
    "source": "https://github.com/docker/welcome-to-docker",
    "mode": "parallel",
}
```

`PORT`

Initially read from env, but later reassigned while scanning ports `8000` to
`8009`.

### `step`

```python
def step(msg: str)
```

Prints a formatted progress message.

### `fail`

```python
def fail(msg: str)
```

Prints an error to stderr and exits with status `1`.

### Environment Validation

Top-level logic:

- Verifies Python version is at least `3.8`.
- Runs `docker ps`.
- Fails if Docker is missing or daemon is not running.

### Virtual Environment Setup

Creates `venv` if missing.

Computes platform-specific paths for:

- `pip`
- `python`
- `uvicorn`

Windows paths use `venv/Scripts`; Unix-like paths use `venv/bin`.

### Dependency Installation

Reads `requirements.txt`, computes SHA-256 hash, and compares it to:

```text
venv/.deps_installed
```

If the hash differs or marker is missing:

- Runs `pip install -r requirements.txt`.
- Writes the new hash to `.deps_installed`.

Design reason:

- Avoids reinstalling dependencies on every `run.py` invocation when
  requirements are unchanged.

### Scanner Image Pulling

For each scanner in `POC_REQUEST["scanners"]`:

- Checks if the image exists locally using `docker image inspect`.
- Pulls the image if missing.
- Fails on pull errors.

### Directory Setup

Ensures:

- `/tmp/repo`
- project `output`

exist before starting the server.

### Port Selection

`_port_in_use(port)`

Uses a TCP socket connection attempt to `127.0.0.1:<port>`.

The script selects the first free port from `8000` through `8009`.

### Server Startup

Starts Uvicorn:

```text
uvicorn main:app --host 0.0.0.0 --port <PORT>
```

The process is started with `subprocess.Popen` and kept alive.

### Readiness Loop

For up to 30 seconds:

- Checks if server process exited.
- Calls `GET /health`.
- Sleeps one second between attempts.

If the server does not become ready, the script terminates it and exits.

### POC Request

Sends `POC_REQUEST` to:

```text
http://localhost:<PORT>/scan
```

with a 900 second timeout.

The response is printed as pretty JSON.

### Long-Running Server

After the POC request, the server keeps running until Ctrl+C. On keyboard
interrupt, `run.py` terminates the Uvicorn process.

## `client.py`

`client.py` is a standalone CLI client for sending scan requests to an existing
service instance.

It uses Python standard library HTTP tooling (`urllib`) and does not depend on
the `requests` package.

### Defaults

```python
DEFAULT_HOST = "localhost"
DEFAULT_PORT = 8000
DEFAULT_SCANNERS = ["trivy", "grype"]
DEFAULT_MODE = "sequential"
DEFAULT_SERVICE_VERSION = "v1.0"
DEFAULT_TIMEOUT = 900
```

### `parse_args`

```python
def parse_args()
```

Defines CLI options:

- `--host`
- `--port`
- `--source`
- `--token`
- `--scanners`
- `--mode`
- `--service-version`
- `--branch`
- `--payload`
- `--timeout`

`--payload` accepts a full JSON string and overrides other request-building
flags.

### `build_payload`

```python
def build_payload(args) -> dict
```

Responsibilities:

- If `--payload` is provided:
  - Parse it as JSON.
  - Exit with error if invalid.
- If `--payload` is absent:
  - Require `--source`.
  - Build payload from flags.
  - Apply defaults for scanners, mode, and service version.
  - Include branch and token only when provided.

Design reason:

- Supports both easy CLI usage and exact payload testing.

### `send_scan`

```python
def send_scan(host: str, port: int, payload: dict, timeout: int)
```

Responsibilities:

- Builds URL:

```text
http://<host>:<port>/scan
```

- Prints payload with token redacted.
- Sends POST request with JSON body.
- Parses JSON response.
- Prints pretty JSON response.
- Exits with useful error messages on HTTP, connection, or unexpected failures.

Security behavior:

- `token` is displayed as `***` in console output.

## `setup_scanners.py`

`setup_scanners.py` pulls scanner images ahead of time.

### `SCANNER_IMAGES`

```python
{
    "trivy": "aquasec/trivy:latest",
    "grype": "anchore/grype:latest",
}
```

Production note:

- The code comments correctly mention that `latest` is suitable for POC usage.
  Production should pin known-good versions or digests.

### `docker_available`

```python
def docker_available() -> bool
```

Runs:

```text
docker info
```

Returns true if Docker is available and daemon responds.

### `pull_image`

```python
def pull_image(name: str, image: str) -> bool
```

Responsibilities:

- Runs:

```text
docker pull <image>
```

- Streams stdout/stderr live.
- Enforces 600 second timeout.
- Returns success boolean.

### `verify_image`

```python
def verify_image(image: str) -> bool
```

Runs:

```text
docker image inspect <image>
```

Returns true if Docker can inspect the local image.

### `main`

Workflow:

1. Log setup header.
2. Validate Docker availability.
3. Pull each scanner image.
4. Verify image exists.
5. Report failed scanner names and exit `1` if any failed.
6. Print next command if all images are ready.

## `requirements.txt`

The requirements file is intentionally small. It contains only API, runtime,
monitoring, Azure upload, and local environment loading dependencies.

Current contents:

```text
fastapi>=0.111.0
uvicorn[standard]>=0.30.0
psutil>=5.9.8
azure-storage-blob>=12.19.0
python-dotenv>=1.0.1
```

Version strategy:

- Uses lower bounds rather than exact pins.
- This is convenient during development but can introduce drift in production.
- A production deployment should consider a lock file or pinned versions.

## `.env`

`.env` is ignored by Git and may contain local secrets/configuration such as:

```text
AZURE_STORAGE_CONNECTION_STRING=...
AZURE_STORAGE_CONTAINER=scan-results
```

Never commit `.env`.

## `.gitignore`

Current ignored paths:

```text
venv/
output/
__pycache__/
*.pyc
.env
.DS_Store
```

Rationale:

- `venv/`: local dependency environment.
- `output/`: generated scan JSON files.
- `__pycache__/` and `*.pyc`: generated Python bytecode.
- `.env`: local secrets.
- `.DS_Store`: macOS metadata.

## Data Model

### Request Data

Main request fields:

- scanners
- source
- mode
- service_version
- branch
- token

The request is validated before the scan pipeline starts. Invalid scanner names
or mode values are rejected by Pydantic because the fields use `Literal`.

### Internal Scan Output

Scanner runners return:

```python
{
    "results": {
        "trivy": "<local_path>",
        "grype": "<local_path>"
    },
    "errors": {
        "trivy": "<error>"
    },
    "resource_usage": {
        "cpu_avg": "...",
        "cpu_peak": "...",
        "memory_avg": "...",
        "memory_peak": "...",
        "disk_used_avg": "...",
        "disk_used_peak": "..."
    },
    "per_scanner_metrics": {
        "trivy": {...},
        "grype": {...}
    }
}
```

### Blob Storage Naming

Blob naming is deterministic:

```text
<appname>/<service_version>/<branch>/<commit_id>/<scanner>_results.json
```

This makes scans naturally browsable by application, version, branch, commit,
and scanner.

## Sequential vs Parallel Scanning

### Sequential Mode

Function:

```python
run_sequential(scanners)
```

Behavior:

- Runs scanner A to completion.
- Then runs scanner B.
- Merges all samples for total usage.

Benefits:

- Easier debugging.
- Lower resource contention.
- Cleaner attribution per scanner.

Tradeoffs:

- Slower total wall-clock time when multiple scanners are requested.

### Parallel Mode

Function:

```python
run_parallel(scanners)
```

Behavior:

- Starts all scanner jobs concurrently.
- Uses a system monitor for combined resource usage.
- Still records per-scanner metrics from each scanner's monitor window.

Benefits:

- Faster wall-clock time when scanners can run at the same time.

Tradeoffs:

- Higher CPU, memory, disk, network, and Docker daemon contention.
- Shared output directory and shared image tag are not request-isolated.

## Security Considerations

### Docker Socket Mount

Scanner containers mount:

```text
/var/run/docker.sock:/var/run/docker.sock
```

This gives the scanner container access to the host Docker daemon. In production,
treat this as privileged access.

Possible hardening options:

- Run scanner service on isolated worker nodes.
- Use ephemeral VMs/runners.
- Restrict network and credentials available to scanner containers.
- Avoid scanning untrusted Dockerfiles on shared hosts.

### Building Untrusted Dockerfiles

The service builds Dockerfiles from user-provided repositories. Docker builds can
execute arbitrary commands during image build.

Production controls should include:

- Trusted source allowlists.
- Isolated build workers.
- Resource limits.
- Build timeouts.
- Network restrictions where possible.
- Audit logging.

### GitHub Token Handling

The token is accepted in request body and embedded into clone URL when present.
Logging attempts to redact it.

Remaining risks:

- Tokens may be visible in process arguments on some systems.
- Tokens may appear in upstream Git errors not covered by simple replacement.
- Tokens sent over HTTP to the API should only be used behind TLS in production.

### Azure Credentials

Azure connection string is read from environment variables. It should be stored
using proper secret management in production, not plain files.

## Concurrency and Isolation

The current implementation is best understood as a single-scan-at-a-time proof
of concept.

Shared mutable resources:

- `/tmp/repo`
- `./output`
- Docker image tag `sample-image:latest`
- fixed scanner output filenames

Impact:

- Two simultaneous `/scan` requests can delete each other's workspaces.
- One request can overwrite another request's scanner JSON files.
- Docker image tag collisions can cause scanners to inspect the wrong image.

Recommended production design:

- Generate a unique scan ID per request.
- Use per-request repo directory, such as `/tmp/scanner-service/<scan_id>/repo`.
- Use per-request output directory.
- Tag built images as `scanner-service:<scan_id>`.
- Include scan ID in scanner container names.
- Clean up with `try/finally`.
- Optionally queue scans with a worker system instead of doing long work inside
  HTTP request handlers.

## Error Handling Model

Fatal errors:

- Directory preparation failure.
- Repository clone/copy failure.
- Missing root Dockerfile.
- Docker image build failure.
- Unexpected scanner orchestration exception.

Partial errors:

- Individual scanner failure when other scanners succeeded.
- Blob upload failures.

Why blob upload is nonfatal:

- The primary scan produced useful local artifacts.
- Storage failure can be retried or investigated separately.

## Observability

The project uses Python's standard `logging` module.

Log coverage includes:

- API scan requests.
- Directory preparation failures.
- Clone operations and clone errors.
- Docker build errors.
- Scanner command execution.
- Scanner failures/timeouts.
- Docker image cleanup.
- Blob upload success/failure.

Metrics coverage includes:

- Total scan duration from API layer.
- Per-scanner duration and resource usage.
- Aggregate CPU, memory, and disk usage.

Current metric limitations:

- CPU/memory/disk are host-level samples, not true container-level resource
  accounting.
- Disk usage is root partition usage, not per-scan disk delta.
- Sampling interval is fixed at one second.

## Timeouts

Current timeout values:

- `git clone`: 300 seconds.
- Docker image build: 600 seconds.
- Scanner execution: 600 seconds per scanner.
- Scanner image pull in setup scripts: 600 seconds.
- `client.py` request timeout: 900 seconds by default.
- `run.py` POC request timeout: 900 seconds.
- `run.py` server readiness wait: 30 seconds.

These are hardcoded. Production configuration should move them to settings.

## Platform Notes

The project appears to be developed on Windows, but parts of the runtime assume
Unix-like paths and Docker behavior:

- `REPO_DIR = "/tmp/repo"`
- Docker socket mount uses `/var/run/docker.sock`.
- Docker run uses `--network host`.
- Local path detection does not recognize `C:\...`.

These choices are common for Linux Docker hosts. On Docker Desktop for Windows,
the Docker socket and host networking behavior may differ. For reliable
production use, Linux is the simpler target environment.

## Known Limitations

- No authentication on the FastAPI service itself.
- No request queue or background job model.
- No unique per-request workspace.
- Fixed image tag can collide across concurrent scans.
- Fixed output file names can collide across concurrent scans.
- Root-level Dockerfile only.
- No custom Docker build args.
- No support for Git submodules.
- No scanner version pinning.
- No SBOM mode.
- No severity filtering.
- No result normalization across scanners.
- No tests currently present in the repository.
- No structured configuration object.
- No container-level metrics.

## Design Decisions

### Why Run Scanners Through Docker?

Running Trivy and Grype as Docker images avoids requiring scanner binaries to be
installed on the host. It also makes setup reproducible: pull known images, run
containers, write JSON output.

### Why Build the Repository Into an Image First?

Both Trivy and Grype can scan container images. The service is designed around
container image vulnerability scanning, so repository source is first converted
to a local Docker image.

### Why Use JSON Scanner Output?

JSON is machine-readable and can be stored as a raw artifact in Azure Blob
Storage. It preserves scanner-native detail for later analysis.

### Why Keep README Simple?

The README is aimed at users who want to install, run, and call the service. Deep
internal details live here to avoid making the first-use path intimidating.

### Why Upload Raw Results Instead of Normalizing?

Trivy and Grype have different schemas. Storing raw outputs avoids losing detail
and keeps the proof of concept simple. A later analytics layer can normalize
results into a common schema.

## Extension Guide

### Add a New Scanner

1. Add scanner name to the `ScanRequest.scanners` `Literal` in `main.py`.
2. Add scanner Docker image constant in `scanner.py`.
3. Add scanner image to `setup_scanners.py`.
4. Add scanner image to `run.py` if demo flow should pull it.
5. Add command builder in `scanner.py`.
6. Extend `run_scanner_sync` with scanner-specific execution behavior.
7. Ensure output path uses a unique `<scanner>_result.json` file.
8. Update README and this document.

### Support Per-Request Isolation

Recommended changes:

1. Generate `scan_id = uuid.uuid4().hex`.
2. Change `prepare_directories` to accept repo/output paths.
3. Pass paths through `main.py`, `utils.py`, and `scanner.py`.
4. Tag image as `scanner-service:<scan_id>`.
5. Use container names like `<scanner>-scan-<scan_id>`.
6. Upload from per-scan output directory.
7. Clean per-scan paths in `finally`.

### Support Custom Dockerfile Path

Recommended changes:

1. Add `dockerfile_path` to `ScanRequest`.
2. Validate it does not escape `REPO_DIR`.
3. Change `clone_repository` Dockerfile check.
4. Change `build_docker_image` to run:

```text
docker build -f <dockerfile_path> -t <tag> <context>
```

5. Document API behavior.

### Add Tests

Suggested test layers:

- Unit tests for validators in `ScanRequest`.
- Unit tests for `_is_local_path`, `_inject_token`, and `get_repo_info`.
- Unit tests for command builders in `scanner.py`.
- Unit tests for `merge_metrics`.
- Integration tests with Docker mocked.
- Optional end-to-end tests against a tiny fixture repository with a simple
  Dockerfile.

## Production Hardening Checklist

- Add API authentication and authorization.
- Serve only over TLS or behind a trusted TLS proxy.
- Avoid passing GitHub tokens in process arguments.
- Isolate each scan request by unique workspace and image tag.
- Pin scanner images by version or digest.
- Add request queue/background workers.
- Add rate limits and max payload size.
- Add structured logs with scan IDs.
- Add container-level resource metrics.
- Add lifecycle cleanup with `try/finally`.
- Add tests and CI.
- Add explicit configuration management.
- Add Docker build resource limits.
- Add allowlist or trust policy for sources.
- Add retention policy for blob artifacts.

