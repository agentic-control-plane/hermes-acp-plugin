"""ACP plugin for Hermes Agent: governance + local self-optimization data.

Two independent planes, one plugin:

Cloud governance (needs `hermes-acp login`):
  - POST /govern/tool-use   — pre-call policy check, can deny / ask / allow
  - POST /govern/tool-output — post-call observation, fire-and-forget
  - An ACP `ask` decision escalates to Hermes's NATIVE approval gate
    ({"action": "approve"}): the user answers [o]nce/[s]ession/[a]lways/[d]eny
    inline, same as Hermes's own dangerous-shell tier.

Local metering (works with ZERO credentials, nothing leaves the machine):
  - post_api_request → model calls (tokens, cache buckets, cost via Hermes's
    own pricing engine) into ~/.acp/hermes-local.db
  - post_llm_call → context composition by role per turn
  - post_tool_call → tool durations, status, result sizes
  Read it back with `hermes-acp report` (or `report --json` for agents that
  want to optimize themselves). ACP_LOCAL_METERING=off disables.

Everything fails OPEN: an ACP outage, a full disk, or a locked SQLite file
must never block a Hermes run.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from . import local_store, pricing

PLUGIN_VERSION = "0.2.3"
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
    session_id: str = "",
    turn_id: str = "",
    duration_ms: int = 0,
    status: str = "",
    error_type: str = "",
    **_: Any,
) -> None:
    output_str = result if isinstance(result, str) else json.dumps(result, default=str)

    if local_store.metering_enabled():
        try:
            local_store.record_tool_call(
                {
                    "session_id": session_id or "",
                    "task_id": task_id or "",
                    "turn_id": turn_id or "",
                    "tool_name": tool_name,
                    "duration_ms": int(duration_ms or 0),
                    "status": status or "",
                    "error_type": error_type or "",
                    "result_bytes": len(output_str.encode("utf-8", errors="replace")),
                }
            )
        except Exception:
            pass

    token = _resolve_token()
    if not token:
        return

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


def _post_api_request(
    model: str = "",
    provider: str = "",
    base_url: str = "",
    api_mode: str = "",
    usage: dict[str, Any] | None = None,
    session_id: str = "",
    task_id: str = "",
    turn_id: str = "",
    api_request_id: str = "",
    api_duration: float = 0.0,
    finish_reason: str = "",
    message_count: int = 0,
    **_: Any,
) -> None:
    """Meter one LLM API request into the local store. Never raises."""
    if not local_store.metering_enabled():
        return
    try:
        u = usage or {}
        cost_usd, cost_status = pricing.estimate_cost_usd(
            model, u, provider=provider, base_url=base_url
        )
        local_store.record_model_call(
            {
                "session_id": session_id or "",
                "task_id": task_id or "",
                "turn_id": turn_id or "",
                "api_request_id": api_request_id or "",
                "model": model or "",
                "provider": provider or "",
                "api_mode": api_mode or "",
                "input_tokens": int(u.get("input_tokens") or 0),
                "output_tokens": int(u.get("output_tokens") or 0),
                "cache_read_tokens": int(u.get("cache_read_tokens") or 0),
                "cache_write_tokens": int(u.get("cache_write_tokens") or 0),
                "reasoning_tokens": int(u.get("reasoning_tokens") or 0),
                "request_count": int(u.get("request_count") or 1),
                "api_duration_ms": int((api_duration or 0) * 1000),
                "finish_reason": finish_reason or "",
                "message_count": int(message_count or 0),
                "cost_usd": cost_usd,
                "cost_status": cost_status,
            }
        )
    except Exception:
        pass


def _bucket_for(role: str, part: Any) -> str:
    if isinstance(part, dict):
        ptype = str(part.get("type") or "")
        if ptype in ("tool_result", "tool_response", "function_call_output"):
            return "tool"
    if role in ("tool", "function"):
        return "tool"
    if role in ("system", "developer"):
        return "system"
    if role in ("user", "assistant"):
        return role
    return "other"


def _part_chars(part: Any) -> int:
    if part is None:
        return 0
    if isinstance(part, str):
        return len(part)
    if isinstance(part, dict):
        for key in ("text", "content", "output"):
            if key in part:
                return _part_chars(part[key])
        try:
            return len(json.dumps(part, default=str))
        except Exception:
            return len(str(part))
    if isinstance(part, list):
        return sum(_part_chars(p) for p in part)
    return len(str(part))


def _post_llm_call(
    conversation_history: list[Any] | None = None,
    session_id: str = "",
    task_id: str = "",
    turn_id: str = "",
    model: str = "",
    **_: Any,
) -> None:
    """Record context composition by role for one completed turn. Never raises."""
    if not local_store.metering_enabled():
        return
    try:
        buckets = {"system": 0, "user": 0, "assistant": 0, "tool": 0, "other": 0}
        history = conversation_history or []
        for msg in history:
            role = str(
                (msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", "")) or ""
            ).lower()
            content = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", None)
            parts = content if isinstance(content, list) else [content]
            for part in parts:
                buckets[_bucket_for(role, part)] += _part_chars(part)
        local_store.record_turn(
            {
                "session_id": session_id or "",
                "task_id": task_id or "",
                "turn_id": turn_id or "",
                "model": model or "",
                "message_count": len(history),
                "system_chars": buckets["system"],
                "user_chars": buckets["user"],
                "assistant_chars": buckets["assistant"],
                "tool_chars": buckets["tool"],
                "other_chars": buckets["other"],
            }
        )
    except Exception:
        pass


def register(ctx: Any) -> None:
    ctx.register_hook("pre_tool_call", _pre_tool_call)
    ctx.register_hook("post_tool_call", _post_tool_call)
    # Metering hooks — registered individually and fail-open so an older
    # Hermes without these hook names still loads the governance plane.
    for hook, fn in (
        ("post_api_request", _post_api_request),
        ("post_llm_call", _post_llm_call),
    ):
        try:
            ctx.register_hook(hook, fn)
        except Exception:
            pass
