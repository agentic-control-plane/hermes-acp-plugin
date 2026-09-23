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

Pre-lapse ledger (0.3.0): a tool call that ran WITHOUT a pre-call policy
check is a coverage gap ACP must be able to see. Two ways it happens:
  - the gateway was unreachable at pre_tool_call (we fail open), or
  - Hermes never invoked pre_tool_call for the call at all (seen in
    production 2026-09-22: one PostToolUse, no PreToolUse, decision "pass").
The pre hook records every call it handled; the post hook checks the
ledger and carries any lapse to the gateway as `pre_lapse: [{at, tool,
detail}]` on the next PostToolUse — the same wire contract the Claude Code
plugin uses (#902) — so the console shows "ran ungoverned" instead of a
clean pass. Both hooks also send `call_id` (#681) so the rows pair.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from collections import OrderedDict
from pathlib import Path
from typing import Any

from . import local_store, pricing


# Only used when the package is imported from a source tree without dist
# metadata (tests, `python -m` from a checkout). tests/test_cli.py pins it
# to pyproject's version so a release bump can't leave it behind again.
_FALLBACK_VERSION = "0.3.0"


def _dist_version() -> str:
    """Version from package metadata so the wire `X-GS-Client` can never
    drift from what pip installed (issue #6: 0.2.4 on the wire, 0.2.6 dist)."""
    try:
        from importlib.metadata import version

        return version("acp-hermes")
    except Exception:
        return _FALLBACK_VERSION


PLUGIN_VERSION = _dist_version()
CLIENT_ID = f"hermes-plugin/{PLUGIN_VERSION}"

DEFAULT_API_BASE = "https://api.agenticcontrolplane.com"
REQUEST_TIMEOUT_SECONDS = 4.0
POST_HOOK_PAYLOAD_CEILING = 200 * 1024  # 200 KB, matches backend scan ceiling.

# Pre-lapse ledger bounds. Gateway caps: 20 items, at<=40, tool<=120,
# detail<=200 chars (hookGovernance.ts PRE_LAPSE_*); we stay inside them.
PRE_LEDGER_MAX = 256
PRE_LAPSE_MAX_ITEMS = 20
_PRE_LAPSE_TOOL_MAX = 120
_PRE_LAPSE_DETAIL_MAX = 200

_ledger_lock = threading.Lock()
# call key -> True once pre_tool_call handled that call (any outcome).
_pre_ledger: "OrderedDict[str, bool]" = OrderedDict()
# Lapses waiting to ride the next PostToolUse.
_pending_lapses: list[dict[str, str]] = []


def _call_key(tool_name: str, args: Any, tool_call_id: str) -> str:
    """Stable identity for one tool call. Hermes passes tool_call_id to both
    hooks (0.19 and main); fall back to tool + args digest when absent."""
    if tool_call_id:
        return f"id:{tool_call_id}"
    try:
        blob = json.dumps(args, sort_keys=True, default=str)
    except Exception:
        blob = str(args)
    return f"{tool_name}:{hashlib.sha256(blob.encode('utf-8', 'replace')).hexdigest()[:16]}"


def _note_pre(key: str) -> None:
    with _ledger_lock:
        _pre_ledger[key] = True
        _pre_ledger.move_to_end(key)
        while len(_pre_ledger) > PRE_LEDGER_MAX:
            _pre_ledger.popitem(last=False)


def _pop_pre(key: str) -> bool:
    with _ledger_lock:
        return _pre_ledger.pop(key, None) is not None


def _note_lapse(tool_name: str, detail: str) -> None:
    entry = {
        "at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tool": tool_name[:_PRE_LAPSE_TOOL_MAX] or "unknown",
        "detail": " ".join(detail.split())[:_PRE_LAPSE_DETAIL_MAX],
    }
    with _ledger_lock:
        if len(_pending_lapses) < PRE_LAPSE_MAX_ITEMS:
            _pending_lapses.append(entry)


def _drain_lapses() -> list[dict[str, str]]:
    with _ledger_lock:
        out = list(_pending_lapses)
        _pending_lapses.clear()
        return out


def _requeue_lapses(entries: list[dict[str, str]]) -> None:
    """The PostToolUse carrying them failed — keep them for the next one."""
    if not entries:
        return
    with _ledger_lock:
        room = PRE_LAPSE_MAX_ITEMS - len(_pending_lapses)
        if room > 0:
            _pending_lapses[:0] = entries[:room]


def _reset_ledger_for_tests() -> None:
    with _ledger_lock:
        _pre_ledger.clear()
        _pending_lapses.clear()


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


def _post_json_detail(
    path: str, body: dict[str, Any], token: str
) -> tuple[dict[str, Any] | None, str | None]:
    """POST and return (parsed_json, failure_detail). Exactly one is None.
    The detail is what a pre-lapse report carries, so it names the cause."""
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
                return {}, None
            try:
                return json.loads(raw), None
            except json.JSONDecodeError:
                return None, "unparseable gateway response"
    except urllib.error.HTTPError as exc:
        # 4xx/5xx: the gateway answered but did not decide (401 revoked key,
        # 429, 5xx). Report it as what it is, not as "unreachable".
        sys.stderr.write(f"[ACP] gateway returned HTTP {exc.code}; failing open\n")
        return None, f"http {exc.code}"
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        sys.stderr.write(f"[ACP] gateway unreachable ({exc}); failing open\n")
        reason = getattr(exc, "reason", None) or exc
        return None, f"{type(exc).__name__}: {reason}"


def _post_json(path: str, body: dict[str, Any], token: str) -> dict[str, Any] | None:
    return _post_json_detail(path, body, token)[0]


def _notices_off() -> bool:
    return os.environ.get("ACP_SHADOW", "").strip().lower() in ("off", "0", "false")


def _block(message: str) -> dict[str, str]:
    return {"action": "block", "message": message}


def _pre_tool_call(
    tool_name: str,
    args: dict[str, Any],
    task_id: str = "",
    tool_call_id: str = "",
    **_: Any,
) -> dict[str, str] | None:
    token = _resolve_token()
    if not token:
        return None  # Not configured — pass through.

    # Ledger first: whatever happens next, this call HAD its pre hook run.
    _note_pre(_call_key(tool_name, args, tool_call_id))

    body: dict[str, Any] = {
        "tool_name": tool_name,
        "tool_input": args,
        "session_id": task_id,
        "hook_event_name": "PreToolUse",
        "agent_tier": "interactive",
    }
    if tool_call_id:
        body["call_id"] = tool_call_id
    result, failure = _post_json_detail("/govern/tool-use", body, token)
    if result is None:
        # Fail-open (never-brick) — but record that this call ran with no
        # verdict, so the next PostToolUse tells the gateway.
        _note_lapse(tool_name, f"gateway unreachable at PreToolUse: {failure or 'unknown'}")
        return None

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
    tool_call_id: str = "",
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

    # Ledger check: did pre_tool_call run for THIS call? If Hermes dispatched
    # the tool without invoking it, the call ran with no policy check and the
    # gateway must not record a clean pass.
    if not _pop_pre(_call_key(tool_name, args, tool_call_id)):
        _note_lapse(
            tool_name,
            "pre-hook-missing: Hermes did not invoke pre_tool_call for this call; "
            "it ran without a policy check",
        )

    if len(output_str.encode("utf-8")) > POST_HOOK_PAYLOAD_CEILING:
        output_str = output_str[:POST_HOOK_PAYLOAD_CEILING]

    body: dict[str, Any] = {
        "tool_name": tool_name,
        "tool_input": args,
        "tool_output": output_str,
        "session_id": task_id,
        "duration_ms": duration_ms,
        "hook_event_name": "PostToolUse",
        "agent_tier": "interactive",
    }
    if tool_call_id:
        body["call_id"] = tool_call_id
    lapses = _drain_lapses()
    if lapses:
        body["pre_lapse"] = lapses
    posted, _failure = _post_json_detail("/govern/tool-output", body, token)
    if posted is None:
        _requeue_lapses(lapses)
    # Gateway notices (cost advisories, shadow) ride `notice`. Hermes has no
    # message channel on the post hook, so they go to stderr, the surface this
    # plugin already uses for the person. Same contract as the Claude Code
    # plugin: ACP_SHADOW=off silences them (gatewaystack-connect#1334).
    notice = posted.get("notice") if isinstance(posted, dict) else None
    if isinstance(notice, str) and notice.strip() and not _notices_off():
        sys.stderr.write(notice.strip()[:2000] + "\n")


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
