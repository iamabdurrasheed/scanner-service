"""
main.py - FastAPI Container Image Scanning Service
POST /scan → clone repo → build image → run trivy/grype → upload to blob → return metrics
"""

import logging
import time
from typing import Literal, Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

from utils import prepare_directories, clone_repository, build_docker_image, remove_docker_image, get_repo_info, _is_local_path
from scanner import run_sequential, run_parallel
from blob_storage import upload_scan_results

# ──────────────────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# FastAPI app
# ──────────────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Container Image Scanning Service",
    description="Scans Docker images with Trivy and/or Grype with resource monitoring.",
    version="1.0.0",
)

IMAGE_TAG = "sample-image:latest"

# ──────────────────────────────────────────────────────────────────────────────
# Request / Response models
# ──────────────────────────────────────────────────────────────────────────────

class ScanRequest(BaseModel):
    scanners: list[Literal["trivy", "grype"]]
    source: str                                        # GitHub URL or local path
    mode: Literal["sequential", "parallel"] = "sequential"
    service_version: str = "v1.0"
    branch: Optional[str] = None
    token: Optional[str] = None                       # GitHub PAT for private repos

    @field_validator("scanners")
    @classmethod
    def scanners_not_empty(cls, v):
        if not v:
            raise ValueError("At least one scanner must be specified.")
        return list(dict.fromkeys(v))

    @field_validator("source")
    @classmethod
    def source_valid(cls, v):
        v = v.strip()
        if _is_local_path(v):
            return v  # local path — no further validation here
        if not v.startswith("https://github.com/"):
            raise ValueError(
                "source must be a GitHub URL (https://github.com/...) or a local path (/path/to/repo)"
            )
        return v

    @field_validator("service_version")
    @classmethod
    def version_not_empty(cls, v):
        v = v.strip()
        if not v:
            raise ValueError("service_version cannot be empty.")
        return v

    @field_validator("branch")
    @classmethod
    def branch_not_empty(cls, v):
        if v is None:
            return v
        v = v.strip()
        if not v:
            raise ValueError("branch cannot be empty when provided.")
        return v


# ──────────────────────────────────────────────────────────────────────────────
# Endpoints
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/scan")
def scan(request: ScanRequest):
    start = time.time()
    logger.info(
        f"Scan request: scanners={request.scanners}, mode={request.mode}, "
        f"source={request.source}, branch={request.branch or 'default'}"
    )

    # ── Step 1: Prepare directories ──────────────────────────────────────────
    try:
        prepare_directories(clean=True)
    except Exception as e:
        logger.error(f"Directory prep failed: {e}")
        return JSONResponse(
            status_code=500,
            content={"status": "failed", "error": f"Directory preparation failed: {str(e)}"},
        )

    # ── Step 2: Clone repository ─────────────────────────────────────────────
    ok, msg = clone_repository(request.source, request.branch, request.token)
    if not ok:
        return JSONResponse(
            status_code=422,
            content={"status": "failed", "error": msg},
        )

    # ── Step 3: Extract repo metadata (appname, branch, commit_id) ───────────
    repo_info = get_repo_info(request.source, request.branch)
    logger.info(f"Repo info: {repo_info}")

    # ── Step 4: Build Docker image ───────────────────────────────────────────
    ok, msg = build_docker_image(IMAGE_TAG)
    if not ok:
        return JSONResponse(
            status_code=500,
            content={"status": "failed", "error": msg},
        )

    # ── Step 5: Run scanners ─────────────────────────────────────────────────
    try:
        if request.mode == "sequential":
            scan_output = run_sequential(request.scanners)
        else:
            scan_output = run_parallel(request.scanners)
    except Exception as e:
        logger.exception("Scanner execution raised an unexpected error")
        remove_docker_image(IMAGE_TAG)
        return JSONResponse(
            status_code=500,
            content={"status": "failed", "error": f"Scanner error: {str(e)}"},
        )

    # ── Step 6: Remove built image to free disk ──────────────────────────────
    remove_docker_image(IMAGE_TAG)

    # ── Step 7: Upload results to Azure Blob Storage ─────────────────────────
    blob_urls = {}
    blob_errors = {}

    for scanner, local_path in scan_output["results"].items():
        try:
            url = upload_scan_results(
                local_file_path=local_path,
                appname=repo_info["appname"],
                service_version=request.service_version,
                branch=repo_info["branch"],
                commit_id=repo_info["commit_id"],
                scanner=scanner,
            )
            blob_urls[scanner] = url
        except Exception as e:
            logger.error(f"Blob upload failed for {scanner}: {e}")
            blob_errors[scanner] = str(e)

    # ── Step 8: Build response ───────────────────────────────────────────────
    total_duration = round(time.time() - start, 2)

    response = {
        "status": "completed" if not scan_output["errors"] else "partial",
        "mode": request.mode,
        "image": IMAGE_TAG,
        "source": request.source,
        "service_version": request.service_version,
        "branch": repo_info["branch"],
        "commit_id": repo_info["commit_id"],
        "total_duration_seconds": total_duration,
        "results": scan_output["results"],
        "resource_usage": scan_output["resource_usage"],
        "per_scanner_metrics": scan_output.get("per_scanner_metrics", {}),
        "blob_urls": blob_urls,
    }

    if scan_output["errors"]:
        response["errors"] = scan_output["errors"]

    if blob_errors:
        response["blob_errors"] = blob_errors

    return JSONResponse(status_code=200, content=response)
