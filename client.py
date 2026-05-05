"""
client.py
─────────
Standalone client for the Container Image Scanning Service.
Intended to be called by an external service to trigger a scan dynamically.

Usage examples:

  # Explicit flags
  python3 client.py --source https://github.com/org/repo --scanners trivy grype --mode parallel

  # Single scanner
  python3 client.py --source https://github.com/org/repo --scanners trivy

  # Full JSON payload (for service-to-service calls)
  python3 client.py --payload '{"source": "https://github.com/org/repo", "scanners": ["trivy"], "mode": "sequential"}'

  # Override just the source, keep defaults for everything else
  python3 client.py --source https://github.com/org/repo

Options:
  --host      API host (default: localhost)
  --port      API port (default: 8000)
  --source    GitHub repo URL (must have a Dockerfile at root)
  --scanners  One or both of: trivy grype
  --mode      sequential or parallel (default: sequential)
  --payload   Full JSON string — overrides all other flags
  --timeout   Request timeout in seconds (default: 900)
"""

import sys
import json
import argparse
import urllib.request
import urllib.error

# ── Defaults ──────────────────────────────────────────────────────────────────
DEFAULT_HOST     = "localhost"
DEFAULT_PORT     = 8000
DEFAULT_SCANNERS = ["trivy", "grype"]
DEFAULT_MODE     = "sequential"
DEFAULT_SERVICE_VERSION = "v1.0"
DEFAULT_TIMEOUT  = 900


def parse_args():
    parser = argparse.ArgumentParser(
        description="Send a scan request to the Container Image Scanning Service.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--host",     default=DEFAULT_HOST,    help=f"API host (default: {DEFAULT_HOST})")
    parser.add_argument("--port",     default=DEFAULT_PORT,    type=int, help=f"API port (default: {DEFAULT_PORT})")
    parser.add_argument("--source",   default=None,            help="GitHub repo URL")
    parser.add_argument("--scanners", default=None,            nargs="+", choices=["trivy", "grype"], help="Scanners to run")
    parser.add_argument("--mode",     default=None,            choices=["sequential", "parallel"], help="Execution mode")
    parser.add_argument("--service-version", default=DEFAULT_SERVICE_VERSION, help=f"Version label for blob path (default: {DEFAULT_SERVICE_VERSION})")
    parser.add_argument("--branch",   default=None,            help="Optional Git branch to clone")
    parser.add_argument("--payload",  default=None,            help="Full JSON payload string — overrides all other flags")
    parser.add_argument("--timeout",  default=DEFAULT_TIMEOUT, type=int, help=f"Request timeout seconds (default: {DEFAULT_TIMEOUT})")
    return parser.parse_args()


def build_payload(args) -> dict:
    """
    Build the request payload.
    Priority: --payload > individual flags > defaults.
    """
    if args.payload:
        try:
            return json.loads(args.payload)
        except json.JSONDecodeError as e:
            print(f"[ERROR] --payload is not valid JSON: {e}", file=sys.stderr)
            sys.exit(1)

    if not args.source:
        print("[ERROR] --source is required unless --payload is provided.", file=sys.stderr)
        print("  Example: python3 client.py --source https://github.com/org/repo", file=sys.stderr)
        sys.exit(1)

    payload = {
        "source":   args.source,
        "scanners": args.scanners or DEFAULT_SCANNERS,
        "mode":     args.mode     or DEFAULT_MODE,
        "service_version": args.service_version,
    }
    if args.branch:
        payload["branch"] = args.branch
    return payload


def send_scan(host: str, port: int, payload: dict, timeout: int):
    url = f"http://{host}:{port}/scan"
    print(f"\n[*] Sending scan request to {url}")
    print(f"    {json.dumps(payload, indent=4)}\n")

    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read())
        print("[+] Scan response:")
        print(json.dumps(result, indent=2))
        return result

    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"[ERROR] HTTP {e.code}: {body}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"[ERROR] Could not reach {url}: {e.reason}", file=sys.stderr)
        print(f"  Is the service running? Start it with: python3 run.py", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"[ERROR] Unexpected error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    args = parse_args()
    payload = build_payload(args)
    send_scan(args.host, args.port, payload, args.timeout)
