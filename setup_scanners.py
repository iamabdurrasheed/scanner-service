"""
setup_scanners.py
─────────────────
Pulls the official Trivy and Grype Docker images from Docker Hub.
Run this once before starting the FastAPI service:

    python3 setup_scanners.py

No manual apt / dpkg / curl needed.
"""

import subprocess
import sys
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ── Image pinning ──────────────────────────────────────────────────────────────
# Use a specific tag in production; "latest" is fine for a POC.
SCANNER_IMAGES = {
    "trivy": "aquasec/trivy:latest",
    "grype":  "anchore/grype:latest",
}


def docker_available() -> bool:
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0
    except FileNotFoundError:
        return False


def pull_image(name: str, image: str) -> bool:
    """
    Pull `image` from Docker Hub.
    Returns True on success, False on failure.
    """
    logger.info(f"Pulling {name} image  →  {image}")
    result = subprocess.run(
        ["docker", "pull", image],
        # Stream output live so the user sees download progress
        stdout=sys.stdout,
        stderr=sys.stderr,
        text=True,
        timeout=600,
    )
    if result.returncode != 0:
        logger.error(f"Failed to pull {image}")
        return False
    logger.info(f"✓  {name} ({image}) ready")
    return True


def verify_image(image: str) -> bool:
    """Check the image exists locally after pulling."""
    result = subprocess.run(
        ["docker", "image", "inspect", image],
        capture_output=True, text=True,
    )
    return result.returncode == 0


def main():
    logger.info("=== Scanner image setup ===")

    if not docker_available():
        logger.error(
            "Docker daemon is not running or 'docker' is not on PATH.\n"
            "Install Docker:  sudo apt install docker.io -y\n"
            "Start Docker:    sudo systemctl start docker\n"
            "Add yourself:    sudo usermod -aG docker $USER && newgrp docker"
        )
        sys.exit(1)

    failed = []
    for name, image in SCANNER_IMAGES.items():
        ok = pull_image(name, image)
        if ok:
            if not verify_image(image):
                logger.error(f"Pull reported success but image not found locally: {image}")
                failed.append(name)
        else:
            failed.append(name)

    print()
    if failed:
        logger.error(f"Setup FAILED for: {', '.join(failed)}")
        sys.exit(1)
    else:
        logger.info("All scanner images pulled successfully. You can now start the service:")
        logger.info("    uvicorn main:app --host 0.0.0.0 --port 8000")


if __name__ == "__main__":
    main()
