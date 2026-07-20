"""hermes-acp CLI.

Subcommands:
  login    Open browser, exchange auth token for an API key, write
           ~/.acp/credentials. Mirrors the claude-code installer wire
           protocol so the same workspace is reachable from any harness.
  status   Print whether ~/.acp/credentials is present and reachable.
  logout   Remove ~/.acp/credentials.
  report   Local cost & quality X-ray from ~/.acp/hermes-local.db.
           No account needed; --json for machine-readable output.

The login flow:
  1. Open <dashboard>/plugin/authorize in browser.
  2. User pastes the one-time auth token.
  3. POST <api>/plugin/provision with Bearer auth → {apiKey, workspace, isNew}.
  4. Write apiKey to ~/.acp/credentials (chmod 600).
  5. Verify via <api>/govern/health.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any

from . import PLUGIN_VERSION

DEFAULT_API_BASE = "https://api.agenticcontrolplane.com"
DEFAULT_DASHBOARD = "https://cloud.agenticcontrolplane.com"
REQUEST_TIMEOUT_SECONDS = 10.0


def _api_base() -> str:
    return os.environ.get("ACP_API_BASE", DEFAULT_API_BASE).rstrip("/")


def _dashboard_base() -> str:
    return os.environ.get("ACP_DASHBOARD_BASE", DEFAULT_DASHBOARD).rstrip("/")


def _credentials_path() -> Path:
    return Path.home() / ".acp" / "credentials"


def _write_credentials(api_key: str) -> Path:
    path = _credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(api_key.strip() + "\n", encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return path


def _post_json(url: str, body: dict[str, Any], token: str) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-GS-Client": f"hermes-plugin/{PLUGIN_VERSION}",
        },
    )
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
        return json.loads(resp.read() or b"{}")


def _get(url: str) -> int:
    req = urllib.request.Request(
        url,
        method="GET",
        headers={"X-GS-Client": f"hermes-plugin/{PLUGIN_VERSION}"},
    )
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
        return resp.status


def _prompt(message: str) -> str:
    sys.stderr.write(message)
    sys.stderr.flush()
    return input().strip()


def cmd_login(args: argparse.Namespace) -> int:
    creds = _credentials_path()
    if creds.exists() and not args.force:
        sys.stderr.write(
            f"Credentials already at {creds}. Re-run with --force to reconfigure.\n"
        )
        return 0

    auth_url = f"{_dashboard_base()}/plugin/authorize"
    sys.stderr.write(f"Opening browser: {auth_url}\n")
    try:
        webbrowser.open(auth_url)
    except webbrowser.Error:
        sys.stderr.write("Could not open browser automatically. Open the URL above.\n")

    token = _prompt("Paste your token: ")
    if not token:
        sys.stderr.write("No token provided. Aborting.\n")
        return 1

    sys.stderr.write("Provisioning workspace...\n")
    try:
        result = _post_json(f"{_api_base()}/plugin/provision", {}, token)
    except urllib.error.HTTPError as exc:
        sys.stderr.write(f"Provision failed: HTTP {exc.code} {exc.reason}\n")
        return 1
    except (urllib.error.URLError, TimeoutError) as exc:
        sys.stderr.write(f"Provision failed: {exc}\n")
        return 1

    api_key = result.get("apiKey")
    workspace = result.get("workspace")
    is_new = bool(result.get("isNew"))
    if not api_key or not workspace:
        sys.stderr.write(f"Malformed provision response: {result!r}\n")
        return 1

    path = _write_credentials(api_key)
    verb = "Created" if is_new else "Connected to"
    sys.stderr.write(f"{verb} workspace: {workspace}\n")
    sys.stderr.write(f"Credentials written to {path}\n")

    try:
        _get(f"{_api_base()}/govern/health")
        sys.stderr.write("Governance endpoint verified.\n")
    except Exception as exc:
        sys.stderr.write(f"Health check failed (non-fatal): {exc}\n")

    sys.stderr.write(f"\nDashboard: {_dashboard_base()}/logs\n")
    return 0


def cmd_status(_: argparse.Namespace) -> int:
    creds = _credentials_path()
    if not creds.exists():
        sys.stderr.write("Not configured. Run `hermes-acp login`.\n")
        return 1
    sys.stderr.write(f"Credentials present at {creds}\n")
    try:
        status = _get(f"{_api_base()}/govern/health")
        sys.stderr.write(f"Gateway reachable (HTTP {status}).\n")
        return 0
    except Exception as exc:
        sys.stderr.write(f"Gateway unreachable: {exc}\n")
        return 2


def cmd_logout(_: argparse.Namespace) -> int:
    creds = _credentials_path()
    if not creds.exists():
        sys.stderr.write("Already logged out.\n")
        return 0
    creds.unlink()
    sys.stderr.write(f"Removed {creds}\n")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    from . import report

    try:
        print(report.build_report(days=args.days, as_json=args.json))
        return 0
    except Exception as exc:
        sys.stderr.write(f"Report failed: {exc}\n")
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="acp-hermes", description="ACP CLI for Hermes Agent")
    parser.add_argument("--version", action="version", version=f"hermes-acp {PLUGIN_VERSION}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_login = sub.add_parser("login", help="Authenticate and write ~/.acp/credentials")
    p_login.add_argument("--force", action="store_true", help="Overwrite existing credentials")
    p_login.set_defaults(func=cmd_login)

    p_status = sub.add_parser("status", help="Show credential and gateway status")
    p_status.set_defaults(func=cmd_status)

    p_logout = sub.add_parser("logout", help="Remove ~/.acp/credentials")
    p_logout.set_defaults(func=cmd_logout)

    p_report = sub.add_parser(
        "report", help="Local cost & quality X-ray (no account needed)"
    )
    p_report.add_argument("--days", type=int, default=7, help="Window in days (default 7)")
    p_report.add_argument(
        "--json", action="store_true", help="Machine-readable output for self-optimizing agents"
    )
    p_report.set_defaults(func=cmd_report)

    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
