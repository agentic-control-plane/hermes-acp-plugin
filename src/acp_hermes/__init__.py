"""ACP governance plugin for Hermes Agent.

Routes every tool call through the Agentic Control Plane for audit and policy
enforcement. Mirrors the contract used by the Claude Code ACP plugin:

  - POST /govern/tool-use   — pre-call policy check, can deny / ask / allow
  - POST /govern/tool-output — post-call observation, fires-and-forgets

Behavior:
  - Fails OPEN on network / parse errors so ACP outages don't block work.
  - Reads bearer token from ACP_BEARER_TOKEN or ~/.acp/credentials.
  - Sends X-GS-Client: hermes-plugin/<version> for per-client policy routing.
  - Hermes post_tool_call is observational (cannot block); we only log there.
  - An ACP `ask` decision escalates to Hermes's NATIVE approval gate
    ({"action": "approve"}): the user answers [o]nce/[s]ession/[a]lways/[d]eny
    inline, same as Hermes's own dangerous-shell tier. (0.1.0 wrongly claimed
    Hermes had no ask semantic and hard-blocked with a dashboard detour.)
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

PLUGIN_VERSION = "0.1.1"
CLIENT_ID = f"hermes-plugin/{PLUGIN_VERSION}"

DEFAULT_API_BASE = "https://api.agenticcontrolplane.com"
REQUEST_TIMEOUT_SECONDS = 4.0
POST_HOOK_PAYLOAD_CEILING = 200 * 1024  # 200 KB, matches backend scan ceiling.


def _api_base() -> str:
    return os.environ.get("ACP_API_BASE", DEFAULT_API_BASE).rstrip("/")


def _resolve_token() -> str | None:
    token = os.environ.get("ACP_BEARER_TOKEN")
    if token:
        return token.strip()
    try:
        path = Path.home() / ".acp" / "credentials"
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _post_json(path: str, body: dict[str, Any], token: str) -> dict[str, Any] | None:
    url = f"{_api_base()}{path}"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-GS-Client": CLIENT_ID,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            raw = resp.read()
            if not raw:
                return {}
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return None
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        sys.stderr.write(f"[ACP] gateway unreachable ({exc}); failing open\n")
        return None


def _block(message: str) -> dict[str, str]:
    return {"action": "block", "message": message}


def _pre_tool_call(
    tool_name: str,
    args: dict[str, Any],
    task_id: str = "",
    **_: Any,
) -> dict[str, str] | None:
    token = _resolve_token()
    if not token:
        return None  # Not configured — pass through.

    body = {
        "tool_name": tool_name,
        "tool_input": args,
        "session_id": task_id,
        "hook_event_name": "PreToolUse",
        "agent_tier": "interactive",
    }
    result = _post_json("/govern/tool-use", body, token)
    if result is None:
        return None  # Fail-open on network / parse error.

    decision = result.get("decision")
    reason = result.get("reason") or "policy did not return a reason"
    if decision == "deny":
        return _block(f"[ACP] Denied by policy: {reason}")
    if decision == "ask":
        # Escalate to Hermes's native human-approval gate — the same
        # once/session/always/deny prompt its dangerous-shell tier uses.
        # rule_key scopes an [a]lways answer to this tool under ACP's grain.
        return {
            "action": "approve",
            "message": f"[ACP] Approval required: {reason}",
            "rule_key": f"acp:{tool_name}",
        }
    return None


def _post_tool_call(
    tool_name: str,
    args: dict[str, Any],
    result: str = "",
    task_id: str = "",
    duration_ms: int = 0,
    **_: Any,
) -> None:
    token = _resolve_token()
    if not token:
        return

    output_str = result if isinstance(result, str) else json.dumps(result, default=str)
    if len(output_str.encode("utf-8")) > POST_HOOK_PAYLOAD_CEILING:
        output_str = output_str[:POST_HOOK_PAYLOAD_CEILING]

    body = {
        "tool_name": tool_name,
        "tool_input": args,
        "tool_output": output_str,
        "session_id": task_id,
        "duration_ms": duration_ms,
        "hook_event_name": "PostToolUse",
        "agent_tier": "interactive",
    }
    _post_json("/govern/tool-output", body, token)


def register(ctx: Any) -> None:
    ctx.register_hook("pre_tool_call", _pre_tool_call)
    ctx.register_hook("post_tool_call", _post_tool_call)
