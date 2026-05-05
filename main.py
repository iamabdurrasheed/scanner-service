"""
main.py - FastAPI Container Image Scanning Service
POST /scan → clone repo → build image → run trivy/grype → return metrics
"""

import logging
import time
from typing import Literal

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

from utils import prepare_directories, clone_repository, build_docker_image, remove_docker_image
from scanner import run_sequential, run_parallel

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
    source: str  # GitHub repository URL
    mode: Literal["sequential", "parallel"] = "sequential"

    @field_validator("scanners")
    @classmethod
    def scanners_not_empty(cls, v):
        if not v:
            raise ValueError("At least one scanner must be specified.")
        return list(dict.fromkeys(v))  # deduplicate while preserving order

    @field_validator("source")
    @classmethod
    def source_is_github(cls, v):
        v = v.strip()
        if not v.startswith("https://github.com/"):
            raise ValueError("source must be a GitHub repository URL (https://github.com/...)")
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
    logger.info(f"Scan request: scanners={request.scanners}, mode={request.mode}, source={request.source}")

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
    ok, msg = clone_repository(request.source)
    if not ok:
        return JSONResponse(
            status_code=422,
            content={"status": "failed", "error": msg},
        )

    # ── Step 3: Build Docker image ───────────────────────────────────────────
    ok, msg = build_docker_image(IMAGE_TAG)
    if not ok:
        return JSONResponse(
            status_code=500,
            content={"status": "failed", "error": msg},
        )

    # ── Step 4: Run scanners ─────────────────────────────────────────────────
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

    # ── Step 5: Remove built image to free disk ──────────────────────────────
    remove_docker_image(IMAGE_TAG)

    # ── Step 6: Build response ───────────────────────────────────────────────
    total_duration = round(time.time() - start, 2)

    response = {
        "status": "completed" if not scan_output["errors"] else "partial",
        "mode": request.mode,
        "image": IMAGE_TAG,
        "source": request.source,
        "total_duration_seconds": total_duration,
        "results": scan_output["results"],
        "resource_usage": scan_output["resource_usage"],
        "per_scanner_metrics": scan_output.get("per_scanner_metrics", {}),
    }

    if scan_output["errors"]:
        response["errors"] = scan_output["errors"]

    return JSONResponse(status_code=200, content=response)
