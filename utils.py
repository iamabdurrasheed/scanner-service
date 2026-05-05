"""
utils.py - Helper utilities: directory prep, git clone, docker build.
"""

import os
import shutil
import subprocess
import logging

logger = logging.getLogger(__name__)

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
REPO_DIR   = "/tmp/repo"
OUTPUT_DIR = os.path.join(BASE_DIR, "output")


def prepare_directories(clean: bool = True):
    """Create /tmp/repo and project output/, optionally wiping previous runs."""
    os.makedirs(REPO_DIR, exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    if clean:
        _clean_dir(REPO_DIR)
        _clean_dir(OUTPUT_DIR)


def _clean_dir(path: str):
    for item in os.listdir(path):
        full = os.path.join(path, item)
        try:
            if os.path.isdir(full):
                shutil.rmtree(full)
            else:
                os.remove(full)
        except Exception as e:
            logger.warning(f"Could not remove {full}: {e}")


def clone_repository(url: str) -> tuple[bool, str]:
    """
    Clone `url` into REPO_DIR.
    Returns (success: bool, message: str).
    """
    logger.info(f"Cloning {url} → {REPO_DIR}")
    result = subprocess.run(
        ["git", "clone", "--depth", "1", url, REPO_DIR],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        msg = result.stderr.strip() or result.stdout.strip()
        logger.error(f"git clone failed: {msg}")
        return False, f"git clone failed: {msg}"

    # Verify a Dockerfile exists
    dockerfile = os.path.join(REPO_DIR, "Dockerfile")
    if not os.path.isfile(dockerfile):
        return False, "Repository does not contain a Dockerfile at the root level."

    return True, "Repository cloned successfully."


def build_docker_image(tag: str = "sample-image:latest") -> tuple[bool, str]:
    """
    Build a Docker image from REPO_DIR.
    Returns (success: bool, message: str).
    """
    logger.info(f"Building Docker image {tag} from {REPO_DIR}")
    result = subprocess.run(
        ["docker", "build", "-t", tag, REPO_DIR],
        capture_output=True,
        text=True,
        timeout=600,
    )
    if result.returncode != 0:
        msg = result.stderr.strip() or result.stdout.strip()
        logger.error(f"docker build failed: {msg}")
        return False, f"Docker build failed: {msg}"
    return True, f"Docker image {tag} built successfully."
