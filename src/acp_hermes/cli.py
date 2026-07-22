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
import time
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

    if getattr(args, "device", False):
        return _login_device(creds)

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


def _post_json_noauth(url: str, body: dict[str, Any]) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-GS-Client": f"hermes-plugin/{PLUGIN_VERSION}",
        },
    )
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
        return json.loads(resp.read() or b"{}")


def _login_device(creds: Path) -> int:
    """Device-code login for headless boxes (#303): no browser needed HERE —
    a short code is approved from any browser, and the key lands directly in
    this machine's credentials file. The device_code never leaves this
    process; the human-facing user_code can't redeem anything by itself."""
    try:
        grant = _post_json_noauth(f"{_api_base()}/device/code", {"client": "acp-hermes"})
    except (urllib.error.URLError, TimeoutError) as exc:
        sys.stderr.write(f"Could not reach the gateway: {exc}\n")
        return 1
    user_code = grant.get("user_code")
    device_code = grant.get("device_code")
    verify_url = grant.get("verification_uri_complete") or grant.get("verification_uri")
    interval = max(2, int(grant.get("interval", 5)))
    expires_in = int(grant.get("expires_in", 900))
    if not user_code or not device_code or not verify_url:
        sys.stderr.write(f"Malformed device grant: {grant!r}\n")
        return 1

    sys.stderr.write("\n  On any browser (this machine or your phone), open:\n")
    sys.stderr.write(f"    {verify_url}\n")
    sys.stderr.write(f"  and approve code:  {user_code}\n\n")
    sys.stderr.write("  Waiting for approval")
    sys.stderr.flush()

    deadline = time.time() + expires_in
    while time.time() < deadline:
        time.sleep(interval)
        try:
            result = _post_json_noauth(f"{_api_base()}/device/token", {"device_code": device_code})
        except urllib.error.HTTPError as exc:
            try:
                err = json.loads(exc.read() or b"{}").get("error", "")
            except Exception:
                err = ""
            if err == "authorization_pending":
                sys.stderr.write(".")
                sys.stderr.flush()
                continue
            if err == "slow_down":
                interval += 2
                continue
            sys.stderr.write(f"\nDevice login failed: {err or exc}\n")
            return 1
        except (urllib.error.URLError, TimeoutError):
            sys.stderr.write("x")  # transient — keep polling until expiry
            sys.stderr.flush()
            continue
        api_key = result.get("apiKey")
        workspace = result.get("workspace")
        if not api_key or not workspace:
            sys.stderr.write(f"\nMalformed token response: {result!r}\n")
            return 1
        path = _write_credentials(api_key)
        verb = "Created" if result.get("isNew") else "Connected to"
        sys.stderr.write(f"\n{verb} workspace: {workspace}\n")
        sys.stderr.write(f"Credentials written to {path}\n")
        sys.stderr.write(f"\nDashboard: {_dashboard_base()}/logs\n")
        return 0

    sys.stderr.write("\nCode expired before approval. Re-run `acp-hermes login --device`.\n")
    return 1


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


# ── coverage ─────────────────────────────────────────────────────────

def _get_json(url: str, token: str) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Authorization": f"Bearer {token}",
            "X-GS-Client": f"hermes-plugin/{PLUGIN_VERSION}",
        },
    )
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
        return json.loads(resp.read() or b"{}")


def _slug_from_key(token: str) -> str | None:
    # gsk_{slug}_{random} — same parse the gateway uses (parts[1]).
    parts = token.split("_")
    if not token.startswith("gsk_") or len(parts) < 3:
        return None
    return parts[1]


def cmd_coverage(args: argparse.Namespace) -> int:
    """Two-plane coverage doctor: which plane(s) does each client's traffic
    have, and the exact missing step. Prints the workspace's
    /admin/coverage report — the same contract the console Coverage card
    renders, so terminal and dashboard never disagree."""
    creds = _credentials_path()
    if not creds.exists():
        print("No ACP credentials. Run `acp-hermes login` first.")
        return 1
    token = creds.read_text(encoding="utf-8").strip()
    slug = _slug_from_key(token)
    if not slug:
        print("Credential is not a gsk_ workspace key — can't derive the workspace. Re-run `acp-hermes login`.")
        return 1

    try:
        data = _get_json(f"{_api_base()}/{slug}/admin/coverage", token)
    except urllib.error.HTTPError as exc:
        print(f"Coverage check failed: HTTP {exc.code} {exc.reason}")
        return 1
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"Coverage check failed: {exc}")
        return 1

    if args.json:
        print(json.dumps(data, indent=2))
        return 0

    clients = data.get("clients") or []
    ws = data.get("workspace") or {}
    mark = lambda b: "✓" if b else "✗"  # noqa: E731
    print(f"Coverage — workspace '{slug}', last {data.get('windowDays', 30)} days")
    if not clients:
        print("  No governed traffic yet. Both planes come up with one setup step per stack:")
        print(f"  {data.get('setupUrl', 'https://agenticcontrolplane.com/docs/setup')}")
        return 0
    for c in clients:
        i, p = c.get("interception", {}), c.get("proxy", {})
        print(
            f"  {c.get('client', '?'):24} {c.get('family', ''):14} "
            f"interception {mark(i.get('active'))} ({i.get('calls', 0)} calls)   "
            f"proxy {mark(p.get('active'))} ({p.get('calls', 0)} calls)"
        )
        fix = c.get("fix")
        if fix:
            print(f"    → {fix.get('run')}")
            if fix.get("docs"):
                print(f"      docs: {fix['docs']}")
        elif c.get("note"):
            print(f"    note: {c['note']}")
    print(f"  Workspace: interception {mark(ws.get('interception'))} · proxy {mark(ws.get('proxy'))}")
    full = all(
        c.get("interception", {}).get("active") and c.get("proxy", {}).get("active")
        for c in clients
    )
    if not full:
        print(f"  Full guide: {data.get('setupUrl', 'https://agenticcontrolplane.com/docs/setup')}")
    return 0




# ── proxy-setup ──────────────────────────────────────────────────────

def cmd_proxy_setup(args: argparse.Namespace) -> int:
    """Route Hermes's model calls through the ACP proxy (#1).

    Registers a named `acp` provider (OpenAI-compatible endpoint) in
    ~/.hermes/config.yaml pointing at the proxy, keeps the user's current
    model, and puts the ACP key in ~/.hermes/.env as ACP_BEARER_TOKEN.
    The proxy routes any model id (gpt-*/claude-*/gemini-*) to the real
    provider, so this is provider-agnostic by design. Deterministic file
    edits only; --print shows the plan, --undo restores the pre-setup
    config, --verify makes one tiny completion through the proxy.
    """
    try:
        import yaml  # hermes itself is YAML-configured, so this is present
    except ImportError:
        print("PyYAML not found — run this inside the environment Hermes is installed in.")
        return 1

    hermes_dir = Path.home() / ".hermes"
    cfg_path = hermes_dir / "config.yaml"
    backup_path = hermes_dir / "config.yaml.acp-backup"
    env_path = hermes_dir / ".env"

    if args.undo:
        if backup_path.exists():
            cfg_path.write_text(backup_path.read_text(encoding="utf-8"), encoding="utf-8")
            backup_path.unlink()
            print(f"Restored {cfg_path} from backup. (ACP_BEARER_TOKEN left in {env_path}; remove manually if unwanted.)")
            return 0
        print("No backup found — nothing to undo.")
        return 1

    cred_path = Path.home() / ".acp" / "credentials"
    if not cred_path.exists():
        print("No ACP credentials. Run `acp-hermes login` first (cloud account is what prices your calls).")
        return 1
    acp_key = cred_path.read_text(encoding="utf-8").strip()

    cfg = {}
    if cfg_path.exists():
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}

    model_block = cfg.get("model") or {}
    model_id = args.model or (model_block.get("default") if isinstance(model_block, dict) else None)
    if not model_id:
        print("Couldn't determine your current model from ~/.hermes/config.yaml.")
        print("Re-run with:  acp-hermes proxy-setup --model <model-id>   (e.g. gemini-flash-latest)")
        return 1

    base = _api_base() + "/v1"
    providers = cfg.get("providers") or {}
    providers["acp"] = {
        "base_url": base,
        "key_env": "ACP_BEARER_TOKEN",
        "type": "openai",
        "default_model": model_id,
    }
    new_cfg = dict(cfg)
    new_cfg["providers"] = providers
    new_cfg["model"] = {**(model_block if isinstance(model_block, dict) else {}), "default": model_id, "provider": "acp"}

    if args.print_only:
        print("Would write to ~/.hermes/config.yaml:")
        print(yaml.safe_dump({"model": new_cfg["model"], "providers": {"acp": providers["acp"]}}, sort_keys=False))
        print(f"Would ensure ACP_BEARER_TOKEN is set in {env_path}")
        return 0

    if not backup_path.exists() and cfg_path.exists():
        backup_path.write_text(cfg_path.read_text(encoding="utf-8"), encoding="utf-8")

    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(yaml.safe_dump(new_cfg, sort_keys=False), encoding="utf-8")

    env_text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    if "ACP_BEARER_TOKEN=" not in env_text:
        with open(env_path, "a", encoding="utf-8") as f:
            if env_text and not env_text.endswith("\n"):
                f.write("\n")
            f.write(f"ACP_BEARER_TOKEN={acp_key}\n")
        os.chmod(env_path, 0o600)

    print(f"✓ Hermes now routes model calls through the ACP proxy ({base})")
    print(f"  provider: acp · model: {model_id} · key: ACP_BEARER_TOKEN in {env_path}")
    print(f"  Undo any time: acp-hermes proxy-setup --undo")

    if args.verify:
        import json as _json
        import urllib.request
        req = urllib.request.Request(
            base + "/chat/completions",
            data=_json.dumps({"model": model_id, "messages": [{"role": "user", "content": "say ok"}], "max_tokens": 5}).encode(),
            headers={"Authorization": f"Bearer {acp_key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                _json.loads(resp.read())
                print("✓ Verified: a governed, metered completion just went through the proxy.")
                print("  See it in the console: https://cloud.agenticcontrolplane.com")
        except Exception as e:  # noqa: BLE001 — report, never crash setup
            body = getattr(e, "read", lambda: b"")()
            print(f"✗ Verify call failed: {e}")
            if b"Unknown model" in body:
                print("  The proxy doesn't route this model id — check the model name.")
            elif getattr(e, "code", None) == 401:
                print("  Auth failed — re-run `acp-hermes login`.")
            else:
                print("  Likely missing provider key for this model's route — add it in the console: Settings → Model keys.")
            return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="acp-hermes", description="ACP CLI for Hermes Agent")
    parser.add_argument("--version", action="version", version=f"hermes-acp {PLUGIN_VERSION}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_login = sub.add_parser("login", help="Authenticate and write ~/.acp/credentials")
    p_login.add_argument("--force", action="store_true", help="Overwrite existing credentials")
    p_login.add_argument("--device", action="store_true", help="Device-code flow for headless machines: approve a short code from any browser; no browser needed here")
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

    p_proxy = sub.add_parser(
        "proxy-setup",
        help="Route Hermes's model calls through the ACP proxy (cloud cost X-ray)",
    )
    p_proxy.add_argument("--model", help="Model id to keep (default: current config.yaml model)")
    p_proxy.add_argument("--print", dest="print_only", action="store_true", help="Show the plan without writing")
    p_proxy.add_argument("--undo", action="store_true", help="Restore the pre-setup config.yaml")
    p_proxy.add_argument("--verify", action="store_true", help="Send one tiny completion through the proxy after setup")
    p_proxy.set_defaults(func=cmd_proxy_setup)

    p_cov = sub.add_parser(
        "coverage",
        help="Two-plane coverage doctor: interception ✓/✗ · proxy ✓/✗ per client, with the exact missing step",
    )
    p_cov.add_argument(
        "--json", action="store_true", help="Machine-readable output (the raw /admin/coverage contract)"
    )
    p_cov.set_defaults(func=cmd_coverage)

    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
