# Container Image Scanning Service

A FastAPI service that clones a GitHub repository, builds a Docker image, and scans it for vulnerabilities using **Trivy** and/or **Grype** — with real-time host CPU, RAM and disk monitoring.

---

## Prerequisites

Install these on your Linux server before anything else:

```bash
sudo apt install git docker.io python3 python3-pip python3-venv -y
sudo systemctl start docker
sudo usermod -aG docker $USER
newgrp docker
```

---

## Start the Service

```bash
python3 run.py
```

That's it. `run.py` handles everything — venv, dependencies, scanner image pulls, directories, and starts the server. Watch for the port it selects:

```
[*] Checking port availability...
    Using port 8001

[*] Starting API server...
    API:   http://localhost:8001
    Docs:  http://localhost:8001/docs
```

It also fires a built-in test scan automatically and prints the result. To change what it scans, edit `POC_REQUEST` at the top of `run.py`.

---

## Azure Blob Storage Setup

Set your connection string before running:

```bash
export AZURE_STORAGE_CONNECTION_STRING="DefaultEndpointsProtocol=https;AccountName=...;AccountKey=...;EndpointSuffix=core.windows.net"
export AZURE_STORAGE_CONTAINER="scan-results"   # optional, this is the default
```

Results are uploaded to:
```
<appname>/<service_version>/<branch>/<commit_id>/<scanner>_results.json
```

---

## Sending Scan Requests

### Using client.py (recommended)

The server must be running first (`python3 run.py` in another terminal).

**Public repo:**
```bash
python3 client.py --source https://github.com/docker/welcome-to-docker
```

**Private repo:**
```bash
python3 client.py \
  --source https://github.com/your-org/private-repo \
  --token ghp_xxxxxxxxxxxxxxxxxxxx \
  --branch main
```

**Local path:**
```bash
python3 client.py --source /path/to/local/repo
```

**Full control:**
```bash
python3 client.py \
  --port 8001 \
  --source https://github.com/docker/welcome-to-docker \
  --scanners trivy grype \
  --mode sequential \
  --service-version v1.2 \
  --branch main
```

**client.py options:**

| Flag | Default | Description |
|------|---------|-------------|
| `--source` | — | GitHub URL, private URL, or local path |
| `--token` | — | GitHub PAT for private repos |
| `--scanners` | `trivy grype` | `trivy`, `grype`, or both |
| `--mode` | `sequential` | `sequential` or `parallel` |
| `--service-version` | `v1.0` | Version label for blob path |
| `--branch` | repo default | Git branch to scan |
| `--host` | `localhost` | API host |
| `--port` | `8000` | API port |
| `--payload` | — | Full JSON string, overrides all flags |
| `--timeout` | `900` | Request timeout in seconds |

### Using curl

```bash
# Public repo
curl -X POST http://localhost:8001/scan \
  -H "Content-Type: application/json" \
  -d '{"scanners": ["trivy", "grype"], "source": "https://github.com/docker/welcome-to-docker", "mode": "sequential"}'

# Private repo
curl -X POST http://localhost:8001/scan \
  -H "Content-Type: application/json" \
  -d '{"scanners": ["trivy"], "source": "https://github.com/org/private-repo", "token": "ghp_xxx", "branch": "main"}'

# Local path
curl -X POST http://localhost:8001/scan \
  -H "Content-Type: application/json" \
  -d '{"scanners": ["trivy", "grype"], "source": "/path/to/local/repo", "mode": "parallel"}'
```

### Using Swagger UI

Open `http://localhost:<PORT>/docs` in your browser.

1. Click `POST /scan` → **Try it out**
2. Paste your JSON payload
3. Click **Execute**
4. See the full response below

---

## Request Fields

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `source` | ✅ | — | GitHub URL, private URL (+ `token`), or local path |
| `scanners` | ✅ | — | `["trivy"]`, `["grype"]`, or `["trivy", "grype"]` |
| `mode` | ❌ | `sequential` | `sequential` or `parallel` |
| `service_version` | ❌ | `v1.0` | Version label for Azure blob path |
| `branch` | ❌ | repo default | Git branch to clone |
| `token` | ❌ | — | GitHub PAT for private repos |

---

## Response

```json
{
  "status": "completed",
  "mode": "sequential",
  "image": "sample-image:latest",
  "source": "https://github.com/docker/welcome-to-docker",
  "service_version": "v1.0",
  "branch": "main",
  "commit_id": "abc1234",
  "total_duration_seconds": 181.23,
  "results": {
    "trivy": "/path/to/scanner-service/output/trivy_result.json",
    "grype": "/path/to/scanner-service/output/grype_result.json"
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
      "pid": 12345,
      "start_time": "2026-05-05 12:58:58 UTC",
      "end_time": "2026-05-05 12:59:38 UTC",
      "duration_seconds": 39.13,
      "cpu_avg": "30.4%", "cpu_peak": "54.0%",
      "memory_avg": "3288.8MB", "memory_peak": "3425.9MB",
      "disk_used_avg": "82.52GB", "disk_used_peak": "82.94GB"
    },
    "grype": {
      "pid": 12346,
      "start_time": "2026-05-05 12:59:38 UTC",
      "end_time": "2026-05-05 13:01:56 UTC",
      "duration_seconds": 138.54,
      "cpu_avg": "26.2%", "cpu_peak": "36.3%",
      "memory_avg": "3660.7MB", "memory_peak": "3817.5MB",
      "disk_used_avg": "82.86GB", "disk_used_peak": "83.23GB"
    }
  },
  "blob_urls": {
    "trivy": "https://account.blob.core.windows.net/scan-results/welcome-to-docker/v1.0/main/abc1234/trivy_results.json",
    "grype": "https://account.blob.core.windows.net/scan-results/welcome-to-docker/v1.0/main/abc1234/grype_results.json"
  }
}
```

`status` values: `completed` | `partial` (one scanner failed) | `failed` (request failed before scanning)

---

## Execution Modes

| Mode | Behaviour | Best for |
|------|-----------|----------|
| `sequential` | Trivy runs, then Grype. Each gets its own resource window. | Accurate per-scanner metrics |
| `parallel` | Both run simultaneously. System-wide metrics tracked. | Faster total scan time |

---

## Output Files

Results are saved locally in `output/` inside the project directory and uploaded to Azure Blob Storage:

```
scanner-service/output/trivy_result.json
scanner-service/output/grype_result.json
```

Files are wiped at the start of every new scan.

---

## Common Errors

| Error | Fix |
|-------|-----|
| Port already in use | `run.py` auto-selects next free port (8000–8009) |
| `HTTP 403` on `/scan` | Wrong port — check what `run.py` printed and pass `--port <PORT>` |
| `No Dockerfile at root` | Repo must have `Dockerfile` at root, not in a subfolder |
| `git clone` fails | Check URL is correct and repo is accessible |
| Azure upload fails | Check `AZURE_STORAGE_CONNECTION_STRING` is set. Scan still completes — check `blob_errors` in response |

---

## Health Check

```bash
curl http://localhost:8000/health
# {"status": "ok"}
```

---

> For full technical details — architecture, file-by-file breakdown, design decisions — see [TECHNICAL.md](TECHNICAL.md)
