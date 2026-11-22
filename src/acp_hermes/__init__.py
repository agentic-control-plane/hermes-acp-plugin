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

Personal data removal (0.4.0): Hermes's `transform_tool_result` hook runs
after post_tool_call and before the result reaches the conversation; the
first string it returns replaces the result. When that hook registers,
post_tool_call parks its PostToolUse body instead of sending it and
transform_tool_result sends it (one request per call), declaring
`capabilities: ["output_rewrite"]` when the whole result is a string the
gateway saw untruncated. A rewritten string in the reply is returned; any
failure returns None so Hermes keeps the original. A parked body that no
transform claims (a Hermes that accepts the hook name but never calls it) is
sent on the next post_tool_call from the same thread, or at exit.
"""

from __future__ import annotations

import atexit
import datetime as _dt
import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from pathlib import Path
from typing import Any

from . import local_store, pricing


# Only used when the package is imported from a source tree without dist
# metadata (tests, `python -m` from a checkout). tests/test_cli.py pins it
# to pyproject's version so a release bump can't leave it behind again.
_FALLBACK_VERSION = "0.4.0"


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

# transform_tool_result pairing. Set by register() once Hermes accepted the
# hook; post_tool_call then parks its PostToolUse here for the transform.
_transform_registered = False
_atexit_armed = False
PENDING_POSTS_MAX = 64
# call key -> {"body", "lapses", "token", "tool", "thread", "original"}
_pending_posts: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
# Keys whose PostToolUse transform_tool_result already sent because it ran
# before post_tool_call (Hermes's sequential registry path). Bounded so a
# transform with no post after it cannot grow this without limit.
HANDLED_POSTS_MAX = 256
_handled_posts: "OrderedDict[str, str]" = OrderedDict()
REWRITE_CAPABILITY = "output_rewrite"
_NO_TRANSFORM_STATUSES = frozenset({"blocked", "timeout", "cancelled"})
# About how long the exit flush may spend sending parked posts. Not a hard
# wall clock: urlopen's timeout applies per socket operation.
EXIT_FLUSH_BUDGET_SECONDS = 5.0


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
    global _transform_registered
    with _ledger_lock:
        _pre_ledger.clear()
        _pending_lapses.clear()
        _pending_posts.clear()
        _handled_posts.clear()
        _transform_registered = False


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


KEY_REJECTED_FIX = (
    "Fix: re-run the installer (curl -sf https://agenticcontrolplane.com/install.sh | bash) "
    "or create a key at https://cloud.agenticcontrolplane.com/settings/api-keys and save it: "
    "echo 'gsk_...' > ~/.acp/credentials"
)
WORKSPACE_DELETED_FIX = (
    "The ACP workspace this machine was connected to has been deleted. To remove ACP from "
    "this machine: acp-uninstall (older installs without it: curl -sf "
    "https://agenticcontrolplane.com/uninstall.sh | bash). To keep using ACP, re-run the "
    "installer: curl -sf https://agenticcontrolplane.com/install.sh | bash"
)
_DELETED_NOTICE_WINDOW_SECONDS = 24 * 60 * 60


def _lapse_line(fields: dict[str, Any]) -> None:
    """Append to ~/.acp/lapse.log, the local record shared by every ACP harness
    plugin. Best-effort — never block a call on it."""
    try:
        acp_dir = Path.home() / ".acp"
        acp_dir.mkdir(parents=True, exist_ok=True)
        line = {
            "at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "client": "hermes-plugin",
            **fields,
        }
        with open(acp_dir / "lapse.log", "a", encoding="utf-8") as f:
            f.write(json.dumps(line) + "\n")
    except Exception:
        pass


def _first_deleted_notice(session_id: str) -> bool:
    """True the first time the workspace-deleted notice is asked for in a
    session, False after (~/.acp/notices/workspace-deleted.<session>, created
    exclusively — the same marker the other harness plugins use). With no
    session id, at most once per 24h via a timestamp marker. Fails toward
    showing: an unwritable marker repeats, never vanishes."""
    sid = re.sub(r"[^A-Za-z0-9._-]", "_", str(session_id or ""))[:120]
    notices = Path.home() / ".acp" / "notices"
    try:
        notices.mkdir(parents=True, exist_ok=True)
        now = _dt.datetime.now(_dt.timezone.utc)
        stamp = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
        if not sid or sid == "unknown":
            marker = notices / "workspace-deleted.no-session"
            last = 0.0
            try:
                raw = marker.read_text(encoding="utf-8").strip()
                last = _dt.datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
            except (OSError, ValueError):
                pass
            if now.timestamp() - last < _DELETED_NOTICE_WINDOW_SECONDS:
                return False
            marker.write_text(stamp, encoding="utf-8")
            return True
        fd = os.open(notices / f"workspace-deleted.{sid}", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        try:
            os.write(fd, stamp.encode("utf-8"))
        finally:
            os.close(fd)
        return True
    except FileExistsError:
        return False
    except Exception:
        return True


def _post_json_full(
    path: str, body: dict[str, Any], token: str, timeout: float | None = None
) -> tuple[dict[str, Any] | None, str | None, int | None, str | None]:
    """POST and return (parsed_json, failure_detail, http_status, body_code).
    Exactly one of parsed_json / failure_detail is None. http_status and
    body_code are set only for an HTTP error answer; body_code is a 4xx
    body's machine-readable `code` (e.g. "workspace_deleted")."""
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
        with urllib.request.urlopen(
            req, timeout=REQUEST_TIMEOUT_SECONDS if timeout is None else timeout
        ) as resp:
            raw = resp.read()
            if not raw:
                return {}, None, None, None
            try:
                return json.loads(raw), None, None, None
            except json.JSONDecodeError:
                return None, "unparseable gateway response", None, None
    except urllib.error.HTTPError as exc:
        code: str | None = None
        if 400 <= exc.code < 500:
            try:
                parsed = json.loads(exc.read() or b"null")
                if isinstance(parsed, dict) and isinstance(parsed.get("code"), str):
                    code = parsed["code"]
            except Exception:
                pass
        if exc.code in (401, 403):
            # A rejected key, not an outage — the caller names it (#1693).
            return None, f"key rejected (HTTP {exc.code})", exc.code, code
        # 4xx/5xx: the gateway answered but did not decide (429, 5xx).
        # Report it as what it is, not as "unreachable".
        sys.stderr.write(f"[ACP] gateway returned HTTP {exc.code}; failing open\n")
        return None, f"http {exc.code}", exc.code, code
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        sys.stderr.write(f"[ACP] gateway unreachable ({exc}); failing open\n")
        reason = getattr(exc, "reason", None) or exc
        return None, f"{type(exc).__name__}: {reason}", None, None


def _post_json_detail(
    path: str, body: dict[str, Any], token: str, timeout: float | None = None
) -> tuple[dict[str, Any] | None, str | None]:
    """POST and return (parsed_json, failure_detail). Exactly one is None.
    The detail is what a pre-lapse report carries, so it names the cause."""
    result, failure, _status, _code = _post_json_full(path, body, token, timeout)
    return result, failure


def _key_rejected(tool_name: str, status: int, code: str | None, session_id: str) -> None:
    """A 401/403 is NOT the gateway being down — it is this machine's key being
    revoked, mistyped, or from a deleted workspace. Reading it as "gateway
    returned HTTP 401" left a user running ungoverned believing it was our
    outage. Hermes has no unattended tier (every call is "interactive" and the
    plugin fails open everywhere), so the call proceeds; the lapse is logged
    and the message names the cause and the fix. `code: "workspace_deleted"`:
    the owner deleted the workspace on purpose — say so once per session, then
    proceed silently while still logging the lapse. (gatewaystack-connect#1693,
    ported from codex-acp-plugin / pi-acp-plugin.)"""
    detail = f"key rejected (HTTP {status})"
    _note_lapse(tool_name, f"key rejected at PreToolUse: http {status}")
    _lapse_line({"kind": "UNGOVERNED", "tool": tool_name, "session": session_id, "detail": detail})
    deleted = code == "workspace_deleted"
    if deleted and not _first_deleted_notice(session_id):
        return
    fix = WORKSPACE_DELETED_FIX if deleted else KEY_REJECTED_FIX
    sys.stderr.write(
        f"[ACP] ⚠ KEY REJECTED (HTTP {status}): the key in ~/.acp/credentials is revoked or "
        f"invalid — this call proceeded UNGOVERNED and unlogged, and every call will until it "
        f"is fixed. {fix}\n"
    )


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
    result, failure, status, code = _post_json_full("/govern/tool-use", body, token)
    if result is None and status in (401, 403):
        _key_rejected(tool_name, status, code, task_id)
        return None
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


def _build_entry(
    key: str,
    tool_name: str,
    args: Any,
    result: Any,
    task_id: str,
    duration_ms: Any,
    tool_call_id: str,
    token: str,
) -> dict[str, Any]:
    """Build one PostToolUse request. `original` is the result when it may be
    rewritten: a whole, untruncated string."""
    output_str = result if isinstance(result, str) else json.dumps(result, default=str)
    # Ledger check: did pre_tool_call run for THIS call? If Hermes dispatched
    # the tool without invoking it, the call ran with no policy check and the
    # gateway must not record a clean pass.
    if not _pop_pre(key):
        _note_lapse(
            tool_name,
            "pre-hook-missing: Hermes did not invoke pre_tool_call for this call; "
            "it ran without a policy check",
        )

    truncated = len(output_str.encode("utf-8")) > POST_HOOK_PAYLOAD_CEILING
    if truncated:
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
    return {
        "body": body,
        "lapses": lapses,
        "token": token,
        "tool": tool_name,
        "original": result if isinstance(result, str) and not truncated else None,
    }


def _note_handled(key: str, by: str) -> None:
    """Record that `by` ("transform" or "post") already sent this call's
    PostToolUse, so the other hook for the same call does not send again."""
    with _ledger_lock:
        _handled_posts[key] = by
        _handled_posts.move_to_end(key)
        while len(_handled_posts) > HANDLED_POSTS_MAX:
            _handled_posts.popitem(last=False)


def _take_handled(key: str, by: str) -> bool:
    with _ledger_lock:
        if _handled_posts.get(key) != by:
            return False
        del _handled_posts[key]
        return True


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

    key = _call_key(tool_name, args, tool_call_id)
    if _take_handled(key, "transform"):
        # Transform-first order: transform_tool_result already sent this
        # call's PostToolUse, so the gateway sees it once. Anything this
        # thread parked earlier was never claimed: send it now.
        _flush_thread_parked()
        return

    entry = _build_entry(key, tool_name, args, result, task_id, duration_ms, tool_call_id, token)
    if _transform_registered and _transform_follows(status, error_type):
        # Post-first order: transform_tool_result sends this one, so the
        # gateway sees the call once.
        entry["thread"] = threading.get_ident()
        _park_post(key, entry)
        return
    if _transform_registered and tool_call_id:
        # Sent now; a transform that still arrives for this call must not
        # send it a second time.
        _note_handled(key, "post")
    _flush_post(entry)


def _transform_follows(status: Any, error_type: Any) -> bool:
    """Whether Hermes will run transform_tool_result after this post_tool_call.

    Hermes fires post_tool_call with no transform for blocked, timed-out and
    cancelled calls, and for calls whose dispatch raised (status "error" with
    the exception's type). Those rows are sent at once instead of parked. A
    tool that returned an error result (error_type "tool_error") still goes
    through the transform, so it stays parked and can be rewritten."""
    s = status if isinstance(status, str) else ""
    if s in _NO_TRANSFORM_STATUSES:
        return False
    if s == "error" and error_type != "tool_error":
        return False
    return True


def _show_notice(text: Any) -> None:
    # Gateway notices (cost advisories, shadow) ride `notice`. Hermes has no
    # message channel on the post hook, so they go to stderr, the surface this
    # plugin already uses for the person. Same contract as the Claude Code
    # plugin: ACP_SHADOW=off silences them (gatewaystack-connect#1334).
    if isinstance(text, str) and text.strip() and not _notices_off():
        sys.stderr.write(text.strip()[:2000] + "\n")


def _send_post(entry: dict[str, Any], timeout: float | None = None) -> dict[str, Any] | None:
    posted, _failure = _post_json_detail(
        "/govern/tool-output", entry["body"], entry["token"], timeout
    )
    if posted is None:
        _requeue_lapses(entry["lapses"])
    return posted if isinstance(posted, dict) else None


def _flush_post(entry: dict[str, Any], timeout: float | None = None) -> None:
    """Send a PostToolUse with no rewrite asked for — today's observe path."""
    posted = _send_post(entry, timeout)
    _show_notice(posted.get("notice") if posted else None)


def _park_post(key: str, entry: dict[str, Any]) -> None:
    """Hold a PostToolUse for transform_tool_result (post-first order).

    Hermes runs the two hooks for one call back to back on one thread, in
    either order: post then transform on the concurrent path, transform then
    post on the sequential registry path. In the transform-first order the
    transform has already sent the request and post_tool_call never parks.
    So anything still parked from this thread when a new post arrives was
    never claimed: send it now rather than lose the audit row."""
    stale: list[dict[str, Any]] = []
    with _ledger_lock:
        old = _pending_posts.pop(key, None)
        if old is not None:
            stale.append(old)
        stale.extend(_pop_thread_parked_locked(entry["thread"]))
        _pending_posts[key] = entry
        while len(_pending_posts) > PENDING_POSTS_MAX:
            stale.append(_pending_posts.popitem(last=False)[1])
    for e in stale:
        _flush_post(e)


def _pop_thread_parked_locked(thread: int) -> list[dict[str, Any]]:
    """Remove and return this thread's parked posts. Caller holds _ledger_lock."""
    keys = [k for k, e in _pending_posts.items() if e["thread"] == thread]
    return [_pending_posts.pop(k) for k in keys]


def _flush_thread_parked() -> None:
    """Send whatever this thread left parked and unclaimed (see _park_post).
    Called from the transform-first path too, where post_tool_call never
    parks, so a row parked earlier is not left waiting until exit."""
    with _ledger_lock:
        stale = _pop_thread_parked_locked(threading.get_ident())
    for e in stale:
        _flush_post(e)


def _flush_pending_posts(budget: float | None = None) -> None:
    """Send what is still parked at exit within an overall budget of about
    EXIT_FLUSH_BUDGET_SECONDS, so a down gateway cannot hold up exit for
    long. Whatever does not fit is dropped with one warning."""
    with _ledger_lock:
        stale = list(_pending_posts.values())
        _pending_posts.clear()
    limit = EXIT_FLUSH_BUDGET_SECONDS if budget is None else budget
    deadline = time.monotonic() + limit
    for i, e in enumerate(stale):
        remaining = min(REQUEST_TIMEOUT_SECONDS, deadline - time.monotonic())
        if remaining <= 0.05:
            try:
                sys.stderr.write(
                    f"[ACP] exit: {len(stale) - i} tool-call audit record(s) not sent "
                    f"within {limit:g}s; dropped\n"
                )
            except Exception:
                pass
            return
        try:
            _flush_post(e, remaining)
        except Exception:
            pass


def _types_suffix(pii: dict[str, Any]) -> str:
    types = pii.get("types")
    if isinstance(types, list):
        names = [t for t in types if isinstance(t, str) and t]
        if names:
            return f" ({', '.join(names[:10])})"
    return ""


def _personal_data_notice(tool_name: str, posted: dict[str, Any], removed: bool) -> None:
    """Say what happened to personal data, and only what happened: "removed"
    only when this hook handed Hermes the rewritten string."""
    notice = posted.get("notice")
    pii = posted.get("pii") if isinstance(posted.get("pii"), dict) else {}
    count = pii.get("count")
    found = (isinstance(count, int) and not isinstance(count, bool) and count > 0) or bool(
        _types_suffix(pii)
    )
    if removed:
        if isinstance(notice, str) and notice.strip():
            _show_notice(notice)
        else:
            _show_notice(
                f"[ACP] Personal data removed from the {tool_name} result before the model "
                f"saw it{_types_suffix(pii)}."
            )
        return
    claims_removal = isinstance(notice, str) and any(
        w in notice.lower() for w in ("remov", "redact")
    )
    if not claims_removal:
        _show_notice(notice)
    if found and (claims_removal or not (isinstance(notice, str) and notice.strip())):
        _show_notice(
            f"[ACP] Personal data found in the {tool_name} result{_types_suffix(pii)}; "
            f"the model saw it unchanged."
        )


def _rewrite_applied(posted: dict[str, Any]) -> bool:
    """True when the gateway says its rewrite may replace the result.

    A reply with a `pii` object is authoritative: only `pii.applied is True`
    counts. In redact mode the gateway always sends `modified_output`, even
    when its own re-check rejected the rewrite (then `applied` is false), so
    `modified_output` alone is not proof. A legacy reply with no `pii` key at
    all keeps the old behaviour and its rewrite fields are trusted."""
    if "pii" not in posted:
        return True
    pii = posted.get("pii")
    return isinstance(pii, dict) and pii.get("applied") is True


def _transform(
    tool_name: str,
    args: Any,
    result: Any,
    tool_call_id: str,
    task_id: str = "",
    duration_ms: Any = 0,
) -> str | None:
    key = _call_key(tool_name, args, tool_call_id)
    with _ledger_lock:
        entry = _pending_posts.pop(key, None)
    if entry is None and _take_handled(key, "post"):
        return None  # post_tool_call already sent this call (no rewrite asked).
    if entry is None:
        # Transform-first order (Hermes's sequential registry path runs this
        # hook before post_tool_call): build and send the request here, and
        # mark the key so the post_tool_call that follows does not send again.
        token = _resolve_token()
        if not token:
            return None  # Not logged in: nothing to do.
        entry = _build_entry(key, tool_name, args, result, task_id, duration_ms, tool_call_id, token)
        _note_handled(key, "transform")
    original = entry.get("original")
    # Rewrite only what the gateway saw, whole: the same string Hermes is
    # about to append to the conversation.
    rewritable = isinstance(original, str) and isinstance(result, str) and result == original
    if rewritable:
        entry["body"]["capabilities"] = [REWRITE_CAPABILITY]
    posted = _send_post(entry)
    if posted is None:
        return None
    rewritten: str | None = None
    if rewritable and _rewrite_applied(posted):
        candidate = posted.get("modified_tool_response")
        if not isinstance(candidate, str):
            candidate = posted.get("modified_output")
        if isinstance(candidate, str) and candidate != original:
            rewritten = candidate
    _personal_data_notice(tool_name, posted, removed=rewritten is not None)
    return rewritten


def _transform_tool_result(
    tool_name: str = "",
    args: Any = None,
    result: Any = None,
    tool_call_id: str = "",
    task_id: str = "",
    duration_ms: Any = 0,
    **_: Any,
) -> str | None:
    """Return the gateway's rewritten result, or None to keep the original.
    Runs on the conversation path: never raises, never waits past the
    request timeout."""
    try:
        return _transform(tool_name, args, result, tool_call_id, task_id, duration_ms)
    except Exception:
        return None


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


def _host_supports_transform() -> bool | None:
    """True or False when Hermes's hook list (`hermes_cli.plugins.VALID_HOOKS`)
    says whether transform_tool_result exists; None when that list cannot be
    read, in which case register_hook raising is the only signal left."""
    try:
        from hermes_cli.plugins import VALID_HOOKS  # type: ignore[import-not-found]
    except Exception:
        return None
    try:
        if isinstance(VALID_HOOKS, (set, frozenset, list, tuple, dict)):
            return "transform_tool_result" in VALID_HOOKS
    except Exception:
        pass
    return None


def register(ctx: Any) -> None:
    global _transform_registered, _atexit_armed
    ctx.register_hook("pre_tool_call", _pre_tool_call)
    ctx.register_hook("post_tool_call", _post_tool_call)
    # Personal data removal. Hermes warns about an unknown hook name but still
    # stores it, so a successful register_hook call does not prove the hook
    # will ever run. Ask Hermes's own list of hook names first; an older
    # Hermes that does not list transform_tool_result keeps today's
    # behaviour: post_tool_call sends its own PostToolUse.
    supported = _host_supports_transform()
    if supported is not False:
        try:
            ctx.register_hook("transform_tool_result", _transform_tool_result)
        except Exception:
            supported = False
    if supported is not False:
        _transform_registered = True
        if not _atexit_armed:
            atexit.register(_flush_pending_posts)
            _atexit_armed = True
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
