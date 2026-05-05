"""
client.py
─────────
Standalone client for the Container Image Scanning Service.

Usage examples:

  # Public GitHub repo
  python3 client.py --source https://github.com/org/repo

  # Private GitHub repo (pass token)
  python3 client.py --source https://github.com/org/private-repo --token ghp_xxxx

  # Local path
  python3 client.py --source /path/to/local/repo

  # Full control
  python3 client.py --source https://github.com/org/repo --scanners trivy grype --mode parallel --service-version v1.2 --branch develop

  # Full JSON payload
  python3 client.py --payload '{"source": "https://github.com/org/repo", "scanners": ["trivy"], "mode": "sequential"}'

  # Custom host/port
  python3 client.py --host 192.168.1.10 --port 8001 --source https://github.com/org/repo
"""

import sys
import json
import argparse
import urllib.request
import urllib.error

DEFAULT_HOST            = "localhost"
DEFAULT_PORT            = 8000
DEFAULT_SCANNERS        = ["trivy", "grype"]
DEFAULT_MODE            = "sequential"
DEFAULT_SERVICE_VERSION = "v1.0"
DEFAULT_TIMEOUT         = 900


def parse_args():
    parser = argparse.ArgumentParser(
        description="Send a scan request to the Container Image Scanning Service.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--host",            default=DEFAULT_HOST,            help=f"API host (default: {DEFAULT_HOST})")
    parser.add_argument("--port",            default=DEFAULT_PORT, type=int,  help=f"API port (default: {DEFAULT_PORT})")
    parser.add_argument("--source",          default=None,                    help="GitHub URL or local path to repo")
    parser.add_argument("--token",           default=None,                    help="GitHub PAT for private repos")
    parser.add_argument("--scanners",        default=None, nargs="+",         choices=["trivy", "grype"], help="Scanners to run")
    parser.add_argument("--mode",            default=None,                    choices=["sequential", "parallel"], help="Execution mode")
    parser.add_argument("--service-version", default=DEFAULT_SERVICE_VERSION, help=f"Version label for blob path (default: {DEFAULT_SERVICE_VERSION})")
    parser.add_argument("--branch",          default=None,                    help="Git branch to clone (optional)")
    parser.add_argument("--payload",         default=None,                    help="Full JSON string — overrides all other flags")
    parser.add_argument("--timeout",         default=DEFAULT_TIMEOUT, type=int, help=f"Request timeout seconds (default: {DEFAULT_TIMEOUT})")
    return parser.parse_args()


def build_payload(args) -> dict:
    if args.payload:
        try:
            return json.loads(args.payload)
        except json.JSONDecodeError as e:
            print(f"[ERROR] --payload is not valid JSON: {e}", file=sys.stderr)
            sys.exit(1)

    if not args.source:
        print("[ERROR] --source is required unless --payload is provided.", file=sys.stderr)
        print("  Examples:", file=sys.stderr)
        print("    python3 client.py --source https://github.com/org/repo", file=sys.stderr)
        print("    python3 client.py --source https://github.com/org/private-repo --token ghp_xxxx", file=sys.stderr)
        print("    python3 client.py --source /path/to/local/repo", file=sys.stderr)
        sys.exit(1)

    payload = {
        "source":          args.source,
        "scanners":        args.scanners or DEFAULT_SCANNERS,
        "mode":            args.mode     or DEFAULT_MODE,
        "service_version": args.service_version,
    }
    if args.branch:
        payload["branch"] = args.branch
    if args.token:
        payload["token"] = args.token
    return payload


def send_scan(host: str, port: int, payload: dict, timeout: int):
    url = f"http://{host}:{port}/scan"

    # Print payload without exposing the token
    display = {k: ("***" if k == "token" else v) for k, v in payload.items()}
    print(f"\n[*] Sending scan request to {url}")
    print(f"    {json.dumps(display, indent=4)}\n")

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
        print(f"[ERROR] HTTP {e.code}: {e.read().decode()}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"[ERROR] Could not reach {url}: {e.reason}", file=sys.stderr)
        print("  Is the service running? Start it with: python3 run.py", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"[ERROR] Unexpected error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    args = parse_args()
    payload = build_payload(args)
    send_scan(args.host, args.port, payload, args.timeout)
