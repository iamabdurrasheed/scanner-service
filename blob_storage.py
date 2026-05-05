"""
blob_storage.py
───────────────
Handles uploading scan result JSON files to Azure Blob Storage.

Blob path structure:
    <appname>/<service_version>/<branch>/<commit_id>/<scanner>_results.json

Example:
    welcome-to-docker/v1.0/main/abc1234/trivy_results.json
    welcome-to-docker/v1.0/main/abc1234/grype_results.json

Credentials are read from the environment variable:
    AZURE_STORAGE_CONNECTION_STRING

Set it before running:
    export AZURE_STORAGE_CONNECTION_STRING="DefaultEndpointsProtocol=https;AccountName=...;AccountKey=...;EndpointSuffix=core.windows.net"
"""

import os
import logging

logger = logging.getLogger(__name__)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

CONTAINER_NAME = os.environ.get("AZURE_STORAGE_CONTAINER", "scan-results")


def _get_client():
    """Return a BlobServiceClient using the connection string from env."""
    conn_str = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
    if not conn_str:
        raise EnvironmentError(
            "AZURE_STORAGE_CONNECTION_STRING environment variable is not set.\n"
            "Export it before running:\n"
            "  export AZURE_STORAGE_CONNECTION_STRING='your_connection_string'"
        )
    from azure.storage.blob import BlobServiceClient
    return BlobServiceClient.from_connection_string(conn_str)


def upload_scan_results(
    local_file_path: str,
    appname: str,
    service_version: str,
    branch: str,
    commit_id: str,
    scanner: str,
) -> str:
    """
    Upload a scan result JSON file to Azure Blob Storage.

    Returns the blob URL on success.
    Raises on failure — caller decides whether to treat as fatal.

    Blob path: <appname>/<service_version>/<branch>/<commit_id>/<scanner>_results.json
    """
    blob_name = f"{appname}/{service_version}/{branch}/{commit_id}/{scanner}_results.json"

    try:
        client = _get_client()
        from azure.core.exceptions import ResourceExistsError
        try:
            client.create_container(CONTAINER_NAME)
        except ResourceExistsError:
            pass

        blob_client = client.get_blob_client(container=CONTAINER_NAME, blob=blob_name)

        with open(local_file_path, "rb") as f:
            blob_client.upload_blob(f, overwrite=True)

        url = blob_client.url
        logger.info(f"Uploaded {scanner} results → {url}")
        return url

    except Exception as e:
        logger.error(f"Failed to upload {scanner} results to blob storage: {e}")
        raise
