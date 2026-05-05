"""
run.py
──────
Single-command setup and launch for the container scanning service.
Also acts as the POC client — fires a hardcoded /scan request once the
server is ready and prints the response.

    python3 run.py
"""

import os
import sys
import subprocess
import platform
import time
import json
import urllib.request
import urllib.error

VENV_DIR = "venv"
ALL_SCANNER_IMAGES = {
    "trivy": "aquasec/trivy:latest",
    "grype": "anchore/grype:latest",
}

POC_REQUEST = {
    "scanners": ["trivy", "grype"],
    "source": "https://github.com/docker/getting-started",
    "mode": "sequential",
}


def step(msg: str):
    print(f"\n[*] {msg}")


def fail(msg: str):
    print(f"\n[ERROR] {msg}", file=sys.stderr)
    sys.exit(1)


# ── 1. Validate environment ────────────────────────────────────────────────────

step("Checking Python version...")
if sys.version_info < (3, 8):
    fail(f"Python 3.8+ required, found {platform.python_version()}")
print(f"    Python {platform.python_version()} OK")

step("Checking Docker...")
try:
    result = subprocess.run(["docker", "ps"], capture_output=True, timeout=10)
    if result.returncode != 0:
        fail("Docker is installed but the daemon is not running.\n"
             "    Start it with: sudo systemctl start docker")
except FileNotFoundError:
    fail("Docker not found on PATH.\n"
         "    Install with: sudo apt install docker.io -y")
print("    Docker daemon is running OK")


# ── 2. Virtual environment ─────────────────────────────────────────────────────

step("Setting up virtual environment...")
if not os.path.isdir(VENV_DIR):
    subprocess.run([sys.executable, "-m", "venv", VENV_DIR], check=True)
    print(f"    Created '{VENV_DIR}'")
else:
    print(f"    '{VENV_DIR}' already exists, skipping")

pip = os.path.join(VENV_DIR, "Scripts" if sys.platform == "win32" else "bin", "pip")
python = os.path.join(VENV_DIR, "Scripts" if sys.platform == "win32" else "bin", "python")


# ── 3. Install dependencies ────────────────────────────────────────────────────

step("Installing dependencies from requirements.txt...")
_marker = os.path.join(VENV_DIR, ".deps_installed")
if not os.path.isfile(_marker):
    subprocess.run([pip, "install", "-r", "requirements.txt"], check=True)
    open(_marker, "w").close()
    print("    Dependencies installed")
else:
    print("    Dependencies already installed, skipping")


# ── 4. Pull scanner images (only those requested in POC_REQUEST) ──────────────

step("Pulling scanner images...")
for name in POC_REQUEST["scanners"]:
    image = ALL_SCANNER_IMAGES[name]
    check = subprocess.run(
        ["docker", "image", "inspect", image],
        capture_output=True,
    )
    if check.returncode == 0:
        print(f"    {name} ({image}) already present, skipping")
        continue

    print(f"    Pulling {image} ...")
    result = subprocess.run(["docker", "pull", image], timeout=600)
    if result.returncode != 0:
        fail(f"Failed to pull {image}")
    print(f"    {name} ready")


# ── 5. Ensure required directories ────────────────────────────────────────────

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")

step("Ensuring required directories exist...")
for d in ["/tmp/repo", OUTPUT_DIR]:
    os.makedirs(d, exist_ok=True)
    print(f"    {d} OK")


# ── 6. Start FastAPI service ───────────────────────────────────────────────────

uvicorn = os.path.join(VENV_DIR, "Scripts" if sys.platform == "win32" else "bin", "uvicorn")

# Kill any process already holding port 8000 so re-runs never fail with EADDRINUSE
subprocess.run(["fuser", "-k", "8000/tcp"], capture_output=True)
time.sleep(1)

step("Starting API server...")
print("    API:   http://localhost:8000")
print("    Docs:  http://localhost:8000/docs\n")

server = subprocess.Popen(
    [uvicorn, "main:app", "--host", "0.0.0.0", "--port", "8000"]
)

# ── 7. Wait for server to be ready ────────────────────────────────────────────

step("Waiting for server to be ready...")
for _ in range(30):
    try:
        urllib.request.urlopen("http://localhost:8000/health", timeout=2)
        print("    Server is up")
        break
    except Exception:
        time.sleep(1)
else:
    server.terminate()
    fail("Server did not start within 30 seconds")

# ── 8. POC hardcoded scan request ─────────────────────────────────────────────

step("Sending POC scan request...")
print(f"    {json.dumps(POC_REQUEST, indent=4)}\n")

try:
    req = urllib.request.Request(
        "http://localhost:8000/scan",
        data=json.dumps(POC_REQUEST).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=900) as resp:
        result = json.loads(resp.read())
    print("[+] Scan response:")
    print(json.dumps(result, indent=2))
except urllib.error.HTTPError as e:
    print(f"[ERROR] HTTP {e.code}: {e.read().decode()}", file=sys.stderr)
except Exception as e:
    print(f"[ERROR] Request failed: {e}", file=sys.stderr)

# ── Keep server running ────────────────────────────────────────────────────────

print("\n[*] Server still running. Press Ctrl+C to stop.")
try:
    server.wait()
except KeyboardInterrupt:
    print("\n[*] Shutting down...")
    server.terminate()
