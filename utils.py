"""
utils.py - Helper utilities: directory prep, git clone, docker build.
"""

import os
import shutil
import subprocess
import logging
from typing import Optional

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


def _is_local_path(source: str) -> bool:
    """Return True if source is a local filesystem path rather than a URL."""
    return source.startswith("/") or source.startswith("./") or source.startswith("../")


def _inject_token(url: str, token: str) -> str:
    """
    Embed a GitHub personal access token into an HTTPS URL.
    https://github.com/org/repo  →  https://<token>@github.com/org/repo
    The token never appears in logs — only the sanitised URL is logged.
    """
    return url.replace("https://", f"https://{token}@")


def clone_repository(source: str, branch: str = None, token: str = None) -> tuple[bool, str]:
    """
    Prepare REPO_DIR from `source`, which can be:
      - A public GitHub URL   (https://github.com/org/repo)
      - A private GitHub URL  (https://github.com/org/repo + token)
      - A local path          (/path/to/repo  or  ./relative/path)

    For local paths the directory is copied into REPO_DIR so the rest of
    the pipeline always works from the same location.
    Returns (success: bool, message: str).
    """
    if _is_local_path(source):
        abs_path = os.path.abspath(source)
        logger.info(f"Local path detected: {abs_path}")
        if not os.path.isdir(abs_path):
            return False, f"Local path does not exist: {abs_path}"
        # Copy into REPO_DIR so the rest of the pipeline is unchanged
        try:
            shutil.copytree(abs_path, REPO_DIR, dirs_exist_ok=True)
        except Exception as e:
            return False, f"Failed to copy local repo: {e}"
    else:
        # Build clone URL — embed token for private repos
        clone_url = _inject_token(source, token) if token else source
        safe_url  = source  # never log the token
        logger.info(f"Cloning {safe_url} → {REPO_DIR}")

        cmd = ["git", "clone", "--depth", "1"]
        if branch:
            cmd.extend(["--branch", branch])
        cmd.extend([clone_url, REPO_DIR])

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            # Sanitise error output — strip token if it leaked into stderr
            msg = (result.stderr.strip() or result.stdout.strip())
            if token:
                msg = msg.replace(token, "***")
            logger.error(f"git clone failed: {msg}")
            return False, f"git clone failed: {msg}"

    dockerfile = os.path.join(REPO_DIR, "Dockerfile")
    if not os.path.isfile(dockerfile):
        return False, "Repository does not contain a Dockerfile at the root level."

    return True, "Repository ready."


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


def get_repo_info(source: str, branch: str = None) -> dict:
    """
    Extract repo metadata after the repo is ready in REPO_DIR.
    Works for both URLs and local paths.
    """
    # appname: last segment of URL or directory name for local paths
    appname = os.path.basename(os.path.abspath(source)) if _is_local_path(source) \
              else source.rstrip("/").split("/")[-1].replace(".git", "")

    # commit_id from the repo in REPO_DIR
    result = subprocess.run(
        ["git", "-C", REPO_DIR, "rev-parse", "HEAD"],
        capture_output=True, text=True,
    )
    commit_id = result.stdout.strip()[:7] if result.returncode == 0 else "unknown"

    # branch detection
    result = subprocess.run(
        ["git", "-C", REPO_DIR, "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True,
    )
    detected = result.stdout.strip()
    resolved_branch = detected if detected and detected != "HEAD" else branch or "main"

    return {
        "appname": appname,
        "branch": resolved_branch,
        "commit_id": commit_id,
    }


def remove_docker_image(tag: str = "sample-image:latest"):
    """Remove the scanned image from Docker to free disk space."""
    result = subprocess.run(
        ["docker", "rmi", "-f", tag],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        logger.info(f"Removed Docker image {tag}")
    else:
        logger.warning(f"Could not remove image {tag}: {result.stderr.strip()}")
