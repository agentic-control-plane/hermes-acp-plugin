"""Unit tests for pre/post tool-call hooks.

Strategy: monkeypatch urllib.request.urlopen with a fake that records the
request and returns a canned JSON response. No real network.
"""

from __future__ import annotations

import io
import json
import urllib.error
from unittest.mock import patch

import pytest

from acp_hermes import _post_tool_call, _pre_tool_call, register


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self._body = body
        self.status = status

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _fake_urlopen(payload: dict, recorded: list | None = None):
    body = json.dumps(payload).encode()

    def _impl(req, timeout):  # noqa: ARG001 — match urlopen signature
        if recorded is not None:
            recorded.append(req)
        return FakeResponse(body)

    return _impl


@pytest.fixture(autouse=True)
def _isolate_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ACP_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("ACP_API_BASE", raising=False)
    yield


def _write_token(tmp_path, token: str = "acp_test_token") -> None:
    acp_dir = tmp_path / ".acp"
    acp_dir.mkdir()
    (acp_dir / "credentials").write_text(token)


def test_pre_no_token_passes_through(tmp_path):
    # No env var, no credentials file: hook must be a no-op.
    assert _pre_tool_call("terminal", {"command": "ls"}, task_id="t1") is None


def test_pre_allow_returns_none(tmp_path):
    _write_token(tmp_path)
    with patch("urllib.request.urlopen", _fake_urlopen({"decision": "allow"})):
        assert _pre_tool_call("terminal", {"command": "ls"}, task_id="t1") is None


def test_pre_deny_returns_block(tmp_path):
    _write_token(tmp_path)
    with patch(
        "urllib.request.urlopen",
        _fake_urlopen({"decision": "deny", "reason": "destructive command"}),
    ):
        result = _pre_tool_call("terminal", {"command": "rm -rf /"}, task_id="t1")
    assert result is not None
    assert result["action"] == "block"
    assert "destructive command" in result["message"]
    assert "[ACP]" in result["message"]


def _attended_terminal(monkeypatch) -> None:
    """A person at an interactive Hermes terminal, no auto-approve mode."""
    import acp_hermes

    for name in ("HERMES_YOLO_MODE", "HERMES_CRON_SESSION", "HERMES_SINGLE_QUERY_SESSION",
                 "HERMES_GATEWAY_SESSION", "HERMES_SESSION_PLATFORM", "CI"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")
    monkeypatch.setattr(acp_hermes, "_session_yolo", lambda: False)
    monkeypatch.setattr(acp_hermes, "_approval_mode", lambda: "manual")
    monkeypatch.setattr(acp_hermes, "_unattended_turn", lambda: False)
    monkeypatch.setattr(acp_hermes, "_has_tty", lambda: True)


def test_pre_ask_escalates_to_native_approval_gate(tmp_path, monkeypatch):
    """ACP `ask` returns Hermes's approve directive — the native
    once/session/always/deny prompt — not a hard block (0.1.1), when a
    person is at the terminal to answer it."""
    _write_token(tmp_path)
    _attended_terminal(monkeypatch)
    with patch(
        "urllib.request.urlopen",
        _fake_urlopen({"decision": "ask", "reason": "needs review"}),
    ):
        result = _pre_tool_call("terminal", {"command": "deploy"}, task_id="t1")
    assert result is not None
    assert result["action"] == "approve"
    assert "needs review" in result["message"]
    # Unique per ask, so a remembered answer never matches a later ask.
    assert result["rule_key"].startswith("acp:terminal:")
    assert result["rule_key"] != "acp:terminal"


def test_pre_network_error_fails_open(tmp_path):
    _write_token(tmp_path)

    def _raise(req, timeout):  # noqa: ARG001
        raise urllib.error.URLError("connection refused")

    with patch("urllib.request.urlopen", _raise):
        assert _pre_tool_call("terminal", {"command": "ls"}, task_id="t1") is None


def test_pre_timeout_fails_open(tmp_path):
    _write_token(tmp_path)

    def _raise(req, timeout):  # noqa: ARG001
        raise TimeoutError("slow")

    with patch("urllib.request.urlopen", _raise):
        assert _pre_tool_call("terminal", {"command": "ls"}, task_id="t1") is None


def test_pre_sends_correct_headers_and_body(tmp_path):
    _write_token(tmp_path, "tok_abc")
    recorded: list = []
    with patch("urllib.request.urlopen", _fake_urlopen({"decision": "allow"}, recorded)):
        _pre_tool_call("terminal", {"command": "ls"}, task_id="session-42")
    assert len(recorded) == 1
    req = recorded[0]
    assert req.full_url.endswith("/govern/tool-use")
    assert req.headers["Authorization"] == "Bearer tok_abc"
    assert req.headers["X-gs-client"].startswith("hermes-plugin/")
    body = json.loads(req.data)
    assert body["tool_name"] == "terminal"
    assert body["tool_input"] == {"command": "ls"}
    assert body["session_id"] == "session-42"
    assert body["hook_event_name"] == "PreToolUse"


def test_post_observational_no_return(tmp_path):
    _write_token(tmp_path)
    with patch("urllib.request.urlopen", _fake_urlopen({"action": "redact"})):
        # post hook returns None even when the server says redact —
        # Hermes can't act on it, so we don't propagate.
        result = _post_tool_call(
            "terminal", {"command": "ls"}, result="output", task_id="t", duration_ms=42
        )
    assert result is None


def test_post_shows_gateway_notice_on_stderr(tmp_path, capsys):
    _write_token(tmp_path)
    with patch("urllib.request.urlopen", _fake_urlopen({"action": "pass", "notice": "[ACP cost] Context is 400k tokens."})):
        result = _post_tool_call("terminal", {"command": "ls"}, result="ok", task_id="t", duration_ms=1)
    assert result is None
    assert "[ACP cost] Context is 400k tokens." in capsys.readouterr().err


def test_post_notice_silenced_by_acp_shadow_off(tmp_path, capsys, monkeypatch):
    _write_token(tmp_path)
    monkeypatch.setenv("ACP_SHADOW", "off")
    with patch("urllib.request.urlopen", _fake_urlopen({"action": "pass", "notice": "[ACP cost] x"})):
        _post_tool_call("terminal", {}, result="ok", task_id="t", duration_ms=1)
    assert "[ACP cost] x" not in capsys.readouterr().err


def test_post_truncates_large_payload(tmp_path):
    _write_token(tmp_path)
    big = "x" * (300 * 1024)  # 300 KB, over the 200 KB ceiling
    recorded: list = []
    with patch("urllib.request.urlopen", _fake_urlopen({}, recorded)):
        _post_tool_call("terminal", {}, result=big, task_id="t", duration_ms=1)
    body = json.loads(recorded[0].data)
    assert len(body["tool_output"]) == 200 * 1024


def test_post_no_token_skips_request(tmp_path):
    recorded: list = []
    with patch("urllib.request.urlopen", _fake_urlopen({}, recorded)):
        _post_tool_call("terminal", {}, result="x", task_id="t", duration_ms=1)
    assert recorded == []


def test_register_wires_both_hooks():
    calls: list = []

    class FakeCtx:
        def register_hook(self, name, fn):
            calls.append((name, fn))

    register(FakeCtx())
    names = [c[0] for c in calls]
    assert "pre_tool_call" in names
    assert "post_tool_call" in names


# ---------------------------------------------------------------------------
# Pre-lapse ledger (0.3.0): a call that ran without a pre-call verdict must
# reach the gateway as `pre_lapse` on the next PostToolUse, never as a clean
# pass. Same wire contract as the Claude Code plugin (#902) + call_id (#681).
# ---------------------------------------------------------------------------

import acp_hermes as _plugin  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_ledger():
    _plugin._reset_ledger_for_tests()
    yield
    _plugin._reset_ledger_for_tests()


def _bodies(recorded: list) -> list[dict]:
    return [json.loads(r.data) for r in recorded]


def test_pre_then_post_pairs_by_call_id_and_carries_no_lapse(tmp_path):
    _write_token(tmp_path)
    recorded: list = []
    with patch("urllib.request.urlopen", _fake_urlopen({"decision": "allow"}, recorded)):
        _pre_tool_call("terminal", {"command": "ls"}, task_id="t", tool_call_id="call-1")
        _post_tool_call("terminal", {"command": "ls"}, result="ok", task_id="t", tool_call_id="call-1")
    pre, post = _bodies(recorded)
    assert pre["call_id"] == "call-1" and post["call_id"] == "call-1"
    assert "pre_lapse" not in post


def test_post_without_pre_reports_missing_hook_lapse(tmp_path):
    """The production 2026-09-22 shape: Hermes emitted post_tool_call only."""
    _write_token(tmp_path)
    recorded: list = []
    with patch("urllib.request.urlopen", _fake_urlopen({"action": "pass"}, recorded)):
        _post_tool_call("terminal", {"command": "test_canary"}, result="x", task_id="t", tool_call_id="call-9")
    (post,) = _bodies(recorded)
    assert post["hook_event_name"] == "PostToolUse"
    assert len(post["pre_lapse"]) == 1
    lapse = post["pre_lapse"][0]
    assert lapse["tool"] == "terminal"
    assert lapse["detail"].startswith("pre-hook-missing:")
    assert len(lapse["at"]) <= 40 and len(lapse["detail"]) <= 200


def test_pre_network_failure_is_carried_on_next_post_then_cleared(tmp_path):
    _write_token(tmp_path)

    def _raise(req, timeout):  # noqa: ARG001
        raise urllib.error.URLError("connection refused")

    with patch("urllib.request.urlopen", _raise):
        assert _pre_tool_call("terminal", {"command": "ls"}, task_id="t", tool_call_id="c1") is None

    recorded: list = []
    with patch("urllib.request.urlopen", _fake_urlopen({"action": "pass"}, recorded)):
        _post_tool_call("terminal", {"command": "ls"}, result="ok", task_id="t", tool_call_id="c1")
        _post_tool_call("terminal", {"command": "ls"}, result="ok", task_id="t", tool_call_id="c2")
    first, second = _bodies(recorded)
    assert len(first["pre_lapse"]) == 1
    assert first["pre_lapse"][0]["detail"].startswith("gateway unreachable at PreToolUse:")
    assert "connection refused" in first["pre_lapse"][0]["detail"]
    # c2 had no pre either, so the second post reports exactly that, not the old lapse.
    assert len(second["pre_lapse"]) == 1
    assert second["pre_lapse"][0]["detail"].startswith("pre-hook-missing:")


def test_lapses_requeue_when_the_carrying_post_fails(tmp_path):
    _write_token(tmp_path)

    def _raise(req, timeout):  # noqa: ARG001
        raise TimeoutError("slow")

    with patch("urllib.request.urlopen", _raise):
        _pre_tool_call("terminal", {"command": "a"}, task_id="t", tool_call_id="a")
        _post_tool_call("terminal", {"command": "a"}, result="ok", task_id="t", tool_call_id="a")
    recorded: list = []
    with patch("urllib.request.urlopen", _fake_urlopen({"action": "pass"}, recorded)):
        _pre_tool_call("terminal", {"command": "b"}, task_id="t", tool_call_id="b")
        _post_tool_call("terminal", {"command": "b"}, result="ok", task_id="t", tool_call_id="b")
    post = _bodies(recorded)[-1]
    assert [l["tool"] for l in post["pre_lapse"]] == ["terminal"]
    assert post["pre_lapse"][0]["detail"].startswith("gateway unreachable at PreToolUse:")


def test_call_key_falls_back_to_args_digest_without_tool_call_id(tmp_path):
    _write_token(tmp_path)
    recorded: list = []
    with patch("urllib.request.urlopen", _fake_urlopen({"decision": "allow"}, recorded)):
        _pre_tool_call("terminal", {"command": "ls"}, task_id="t")
        _post_tool_call("terminal", {"command": "ls"}, result="ok", task_id="t")
    pre, post = _bodies(recorded)
    assert "call_id" not in pre and "call_id" not in post
    assert "pre_lapse" not in post


def test_http_error_on_pre_is_reported_as_http_not_unreachable(tmp_path):
    _write_token(tmp_path)

    def _raise(req, timeout):  # noqa: ARG001
        raise urllib.error.HTTPError(req.full_url, 401, "revoked", {}, io.BytesIO(b""))

    with patch("urllib.request.urlopen", _raise):
        assert _pre_tool_call("terminal", {"command": "ls"}, task_id="t", tool_call_id="h") is None
    recorded: list = []
    with patch("urllib.request.urlopen", _fake_urlopen({"action": "pass"}, recorded)):
        _post_tool_call("terminal", {"command": "ls"}, result="ok", task_id="t", tool_call_id="h")
    (post,) = _bodies(recorded)
    assert "http 401" in post["pre_lapse"][0]["detail"]


# ---------------------------------------------------------------------------
# Personal data removal (0.4.0): transform_tool_result sends the PostToolUse
# and hands Hermes the gateway's rewritten result.
# ---------------------------------------------------------------------------


class _Ctx:
    def __init__(self, reject: tuple[str, ...] = ()) -> None:
        self.hooks: dict = {}
        self._reject = reject

    def register_hook(self, name, fn):
        if name in self._reject:
            raise ValueError(f"unknown hook {name}")
        self.hooks[name] = fn


def _registered() -> dict:
    ctx = _Ctx()
    register(ctx)
    return ctx.hooks


def _raw_urlopen(raw: bytes, recorded: list | None = None):
    def _impl(req, timeout):  # noqa: ARG001
        if recorded is not None:
            recorded.append(req)
        return FakeResponse(raw)

    return _impl


def _tool_output_bodies(recorded: list) -> list[dict]:
    return [json.loads(r.data) for r in recorded if r.full_url.endswith("/govern/tool-output")]


def _run_call(hooks: dict, result, call_id: str = "c1"):
    args = {"command": "cat contacts.txt"}
    hooks["post_tool_call"]("terminal", args, result=result, task_id="t", tool_call_id=call_id, duration_ms=3)
    return hooks["transform_tool_result"](
        tool_name="terminal", args=args, result=result, task_id="t", tool_call_id=call_id
    )


def test_register_adds_transform_tool_result():
    hooks = _registered()
    assert hooks["transform_tool_result"] is _plugin._transform_tool_result
    assert _plugin._transform_registered is True


def test_older_hermes_without_transform_hook_still_loads_with_todays_behaviour(tmp_path):
    _write_token(tmp_path)
    ctx = _Ctx(reject=("transform_tool_result",))
    register(ctx)
    assert {"pre_tool_call", "post_tool_call", "post_api_request", "post_llm_call"} <= set(ctx.hooks)
    assert _plugin._transform_registered is False
    recorded: list = []
    with patch("urllib.request.urlopen", _fake_urlopen({"action": "pass"}, recorded)):
        ctx.hooks["post_tool_call"]("terminal", {}, result="ok", task_id="t", tool_call_id="c1")
    (post,) = _tool_output_bodies(recorded)
    assert "capabilities" not in post


def test_one_gateway_request_per_call_and_rewrite_returned(tmp_path):
    _write_token(tmp_path)
    hooks = _registered()
    recorded: list = []
    reply = {
        "action": "pass",
        "modified_tool_response": "mail [REDACTED:email]",
        "pii": {"types": ["email"], "count": 1, "action": "redact", "applied": True},
    }
    with patch("urllib.request.urlopen", _fake_urlopen(reply, recorded)):
        args = {"command": "cat contacts.txt"}
        hooks["post_tool_call"]("terminal", args, result="mail a@b.co", task_id="t", tool_call_id="c1")
        assert recorded == []  # post_tool_call skipped its gateway post
        out = hooks["transform_tool_result"](
            tool_name="terminal", args=args, result="mail a@b.co", task_id="t", tool_call_id="c1"
        )
    assert out == "mail [REDACTED:email]"
    assert len(recorded) == 1
    (post,) = _tool_output_bodies(recorded)
    assert post["capabilities"] == ["output_rewrite"]
    assert post["call_id"] == "c1" and post["tool_output"] == "mail a@b.co"
    assert post["hook_event_name"] == "PostToolUse"


def test_post_tool_call_still_meters_locally_when_transform_registered(tmp_path, monkeypatch):
    _write_token(tmp_path)
    hooks = _registered()
    rows: list = []
    monkeypatch.setattr(_plugin.local_store, "metering_enabled", lambda: True)
    monkeypatch.setattr(_plugin.local_store, "record_tool_call", rows.append)
    with patch("urllib.request.urlopen", _fake_urlopen({"action": "pass"})):
        _run_call(hooks, "x" * 10)
    assert len(rows) == 1 and rows[0]["result_bytes"] == 10


def test_legacy_modified_output_used_when_tool_response_not_a_string(tmp_path):
    _write_token(tmp_path)
    hooks = _registered()
    reply = {"modified_tool_response": {"not": "a string"}, "modified_output": "[REDACTED:phone]"}
    with patch("urllib.request.urlopen", _fake_urlopen(reply)):
        assert _run_call(hooks, "call 555-0100") == "[REDACTED:phone]"


def test_no_rewrite_in_reply_returns_none(tmp_path):
    _write_token(tmp_path)
    hooks = _registered()
    recorded: list = []
    with patch("urllib.request.urlopen", _fake_urlopen({"action": "pass"}, recorded)):
        assert _run_call(hooks, "plain output") is None
    assert len(_tool_output_bodies(recorded)) == 1


def test_truncated_result_declares_no_capability_and_is_never_replaced(tmp_path):
    _write_token(tmp_path)
    hooks = _registered()
    big = "x" * (300 * 1024)
    recorded: list = []
    with patch("urllib.request.urlopen", _fake_urlopen({"modified_tool_response": "short"}, recorded)):
        assert _run_call(hooks, big) is None
    (post,) = _tool_output_bodies(recorded)
    assert "capabilities" not in post
    assert len(post["tool_output"]) == 200 * 1024


def test_non_string_result_declares_no_capability(tmp_path):
    _write_token(tmp_path)
    hooks = _registered()
    recorded: list = []
    with patch("urllib.request.urlopen", _fake_urlopen({"modified_tool_response": "r"}, recorded)):
        assert _run_call(hooks, {"rows": ["a@b.co"]}) is None
    (post,) = _tool_output_bodies(recorded)
    assert "capabilities" not in post


def test_result_at_ceiling_still_declares_capability(tmp_path):
    _write_token(tmp_path)
    hooks = _registered()
    exact = "x" * (200 * 1024)
    recorded: list = []
    with patch("urllib.request.urlopen", _fake_urlopen({}, recorded)):
        _run_call(hooks, exact)
    (post,) = _tool_output_bodies(recorded)
    assert post["capabilities"] == ["output_rewrite"]


@pytest.mark.parametrize(
    "exc",
    [
        urllib.error.URLError("connection refused"),
        TimeoutError("slow"),
        ConnectionResetError("reset"),
    ],
)
def test_transform_fails_open_on_network_error_and_timeout(tmp_path, exc):
    _write_token(tmp_path)
    hooks = _registered()

    def _raise(req, timeout):  # noqa: ARG001
        raise exc

    with patch("urllib.request.urlopen", _raise):
        assert _run_call(hooks, "mail a@b.co") is None


def test_transform_fails_open_on_http_error(tmp_path):
    _write_token(tmp_path)
    hooks = _registered()

    def _raise(req, timeout):  # noqa: ARG001
        raise urllib.error.HTTPError(req.full_url, 500, "boom", {}, io.BytesIO(b""))

    with patch("urllib.request.urlopen", _raise):
        assert _run_call(hooks, "mail a@b.co") is None


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b'["modified_tool_response"]',
        b'"just a string"',
        b'{"modified_tool_response": 42}',
        b'{"modified_tool_response": null, "modified_output": ["x"]}',
        b'{"pii": "nonsense", "notice": 7}',
    ],
)
def test_transform_fails_open_on_malformed_reply(tmp_path, raw):
    _write_token(tmp_path)
    hooks = _registered()
    with patch("urllib.request.urlopen", _raw_urlopen(raw)):
        assert _run_call(hooks, "mail a@b.co") is None


def test_transform_never_raises_on_unexpected_error(tmp_path, monkeypatch):
    _write_token(tmp_path)
    hooks = _registered()

    def _boom(*_a, **_k):
        raise RuntimeError("unexpected")

    with patch("urllib.request.urlopen", _fake_urlopen({})):
        hooks["post_tool_call"]("terminal", {}, result="x", task_id="t", tool_call_id="c1")
    monkeypatch.setattr(_plugin, "_post_json_detail", _boom)
    assert hooks["transform_tool_result"](tool_name="terminal", args={}, result="x", tool_call_id="c1") is None


def test_transform_without_token_is_a_no_op(tmp_path):
    hooks = _registered()
    with patch("urllib.request.urlopen", side_effect=AssertionError("no network expected")):
        assert _run_call(hooks, "mail a@b.co") is None


def test_removed_notice_only_when_string_returned(tmp_path, capsys):
    _write_token(tmp_path)
    hooks = _registered()
    reply = {
        "modified_tool_response": "mail [REDACTED:email]",
        "pii": {"types": ["email"], "count": 1, "action": "redact", "applied": True},
    }
    with patch("urllib.request.urlopen", _fake_urlopen(reply)):
        _run_call(hooks, "mail a@b.co")
    err = capsys.readouterr().err
    assert "Personal data removed" in err and "email" in err


def test_gateway_removed_line_shown_when_string_returned(tmp_path, capsys):
    _write_token(tmp_path)
    hooks = _registered()
    reply = {
        "modified_tool_response": "mail [REDACTED:email]",
        "pii": {"types": ["email"], "count": 1, "applied": True},
        "notice": "[ACP] Removed 1 email address from this tool result.",
    }
    with patch("urllib.request.urlopen", _fake_urlopen(reply)):
        _run_call(hooks, "mail a@b.co")
    assert "[ACP] Removed 1 email address from this tool result." in capsys.readouterr().err


def test_no_removed_wording_when_nothing_returned(tmp_path, capsys):
    """Truncated result: the gateway may still report what it found, but the
    model sees the original, so the person must not read "removed"."""
    _write_token(tmp_path)
    hooks = _registered()
    reply = {
        "pii": {"types": ["email"], "count": 2, "action": "redact", "applied": False},
        "notice": "[ACP] Removed 2 email addresses from this tool result.",
    }
    with patch("urllib.request.urlopen", _fake_urlopen(reply)):
        assert _run_call(hooks, "x" * (300 * 1024)) is None
    err = capsys.readouterr().err
    assert "emov" not in err and "edact" not in err
    assert "Personal data found" in err and "unchanged" in err


def test_personal_data_notice_silenced_by_acp_shadow_off(tmp_path, capsys, monkeypatch):
    _write_token(tmp_path)
    monkeypatch.setenv("ACP_SHADOW", "off")
    hooks = _registered()
    reply = {
        "modified_tool_response": "[REDACTED:email]",
        "pii": {"types": ["email"], "count": 1, "applied": True},
    }
    with patch("urllib.request.urlopen", _fake_urlopen(reply)):
        assert _run_call(hooks, "a@b.co") == "[REDACTED:email]"
    assert "Personal data" not in capsys.readouterr().err


def test_unclaimed_parked_post_is_sent_on_next_call(tmp_path):
    """A Hermes that accepts the hook name but never calls it must not lose
    the audit row: the next post_tool_call sends the parked one first."""
    _write_token(tmp_path)
    hooks = _registered()
    recorded: list = []
    with patch("urllib.request.urlopen", _fake_urlopen({"action": "pass"}, recorded)):
        hooks["post_tool_call"]("terminal", {}, result="one", task_id="t", tool_call_id="c1")
        hooks["post_tool_call"]("terminal", {}, result="two", task_id="t", tool_call_id="c2")
        assert [b["call_id"] for b in _tool_output_bodies(recorded)] == ["c1"]
        _plugin._flush_pending_posts()
    bodies = _tool_output_bodies(recorded)
    assert [b["call_id"] for b in bodies] == ["c1", "c2"]
    assert all("capabilities" not in b for b in bodies)


def test_pre_deny_unchanged_when_transform_registered(tmp_path):
    _write_token(tmp_path)
    hooks = _registered()
    with patch("urllib.request.urlopen", _fake_urlopen({"decision": "deny", "reason": "nope"})):
        result = hooks["pre_tool_call"]("terminal", {"command": "rm -rf /"}, task_id="t")
    assert result == {"action": "block", "message": "[ACP] Denied by policy: nope"}


def test_pre_lapse_rides_the_transform_post(tmp_path):
    _write_token(tmp_path)
    hooks = _registered()
    recorded: list = []
    with patch("urllib.request.urlopen", _fake_urlopen({"action": "pass"}, recorded)):
        _run_call(hooks, "ok", call_id="no-pre")
    (post,) = _tool_output_bodies(recorded)
    assert post["pre_lapse"][0]["detail"].startswith("pre-hook-missing:")


REJECTED_REWRITE_REPLY = {
    # Redact mode always carries modified_output; the gateway's own re-check
    # rejected the rewrite, so modified_tool_response is absent and applied is false.
    "action": "redact",
    "modified_output": "mail [X] and [REDACTED:email]",
    "pii": {"types": ["email"], "count": 2, "action": "detected", "applied": False},
}


def test_applied_false_with_modified_output_is_not_a_rewrite(tmp_path, capsys):
    _write_token(tmp_path)
    hooks = _registered()
    with patch("urllib.request.urlopen", _fake_urlopen(REJECTED_REWRITE_REPLY)):
        assert _run_call(hooks, "mail a@b.co and [REDACTED:email]") is None
    err = capsys.readouterr().err
    assert "emov" not in err
    assert "Personal data found" in err and "unchanged" in err


def test_applied_false_keeps_gateway_not_removed_notice(tmp_path, capsys):
    _write_token(tmp_path)
    hooks = _registered()
    reply = dict(
        REJECTED_REWRITE_REPLY,
        notice="[ACP] Personal data found (2 email); the model saw it unchanged.",
    )
    with patch("urllib.request.urlopen", _fake_urlopen(reply)):
        assert _run_call(hooks, "mail a@b.co and [REDACTED:email]") is None
    err = capsys.readouterr().err
    assert "the model saw it unchanged" in err and "emov" not in err


def test_applied_true_rewrites_from_modified_output(tmp_path, capsys):
    _write_token(tmp_path)
    hooks = _registered()
    reply = dict(REJECTED_REWRITE_REPLY, pii={"types": ["email"], "count": 2, "applied": True})
    with patch("urllib.request.urlopen", _fake_urlopen(reply)):
        out = _run_call(hooks, "mail a@b.co and [REDACTED:email]")
    assert out == "mail [X] and [REDACTED:email]"
    assert "Personal data removed" in capsys.readouterr().err


@pytest.mark.parametrize("pii", [{"types": ["email"], "count": 1}, {"applied": "true"}, None, "x"])
def test_pii_present_without_applied_true_is_not_a_rewrite(tmp_path, pii):
    _write_token(tmp_path)
    hooks = _registered()
    reply = {"modified_tool_response": "[REDACTED:email]", "pii": pii}
    with patch("urllib.request.urlopen", _fake_urlopen(reply)):
        assert _run_call(hooks, "a@b.co") is None


def _fake_hermes_hooks(monkeypatch, names):
    import sys
    import types

    pkg = types.ModuleType("hermes_cli")
    mod = types.ModuleType("hermes_cli.plugins")
    mod.VALID_HOOKS = set(names)
    pkg.plugins = mod
    monkeypatch.setitem(sys.modules, "hermes_cli", pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.plugins", mod)


def test_hermes_hook_list_without_transform_keeps_todays_behaviour(tmp_path, monkeypatch):
    """Hermes warns about an unknown hook name but stores it anyway, so
    register_hook succeeding is no proof. Its hook list decides."""
    _write_token(tmp_path)
    _fake_hermes_hooks(monkeypatch, {"pre_tool_call", "post_tool_call"})
    ctx = _Ctx()  # accepts every name, like Hermes's warn-and-store
    register(ctx)
    assert "transform_tool_result" not in ctx.hooks
    assert _plugin._transform_registered is False
    recorded: list = []
    with patch("urllib.request.urlopen", _fake_urlopen({"action": "pass"}, recorded)):
        ctx.hooks["post_tool_call"]("terminal", {}, result="ok", task_id="t", tool_call_id="c1")
    (post,) = _tool_output_bodies(recorded)
    assert "capabilities" not in post


def test_hermes_hook_list_with_transform_registers_it(tmp_path, monkeypatch):
    _write_token(tmp_path)
    _fake_hermes_hooks(monkeypatch, {"pre_tool_call", "post_tool_call", "transform_tool_result"})
    ctx = _Ctx()
    register(ctx)
    assert ctx.hooks["transform_tool_result"] is _plugin._transform_tool_result
    assert _plugin._transform_registered is True


def test_hermes_hook_list_with_transform_but_register_raises(tmp_path, monkeypatch):
    _fake_hermes_hooks(monkeypatch, {"transform_tool_result"})
    register(_Ctx(reject=("transform_tool_result",)))
    assert _plugin._transform_registered is False


@pytest.mark.parametrize(
    "status,error_type",
    [
        ("blocked", "policy"),
        ("timeout", "tool_timeout"),
        ("cancelled", "keyboard_interrupt"),
        ("error", "RuntimeError"),
        ("error", "invalid_tool_arguments"),
    ],
)
def test_calls_with_no_transform_are_sent_at_once(tmp_path, status, error_type):
    _write_token(tmp_path)
    hooks = _registered()
    recorded: list = []
    with patch("urllib.request.urlopen", _fake_urlopen({"action": "pass"}, recorded)):
        hooks["post_tool_call"](
            "terminal", {}, result="nope", task_id="t", tool_call_id="c1",
            status=status, error_type=error_type,
        )
        (post,) = _tool_output_bodies(recorded)
        assert post["call_id"] == "c1" and "capabilities" not in post
        assert _plugin._pending_posts == {}
        # A transform that does arrive finds nothing and asks for nothing.
        assert hooks["transform_tool_result"](
            tool_name="terminal", args={}, result="nope", tool_call_id="c1"
        ) is None
    assert len(_tool_output_bodies(recorded)) == 1


@pytest.mark.parametrize("status,error_type", [("ok", ""), ("", ""), ("error", "tool_error")])
def test_rewritable_results_are_parked_for_the_transform(tmp_path, status, error_type):
    _write_token(tmp_path)
    hooks = _registered()
    recorded: list = []
    with patch("urllib.request.urlopen", _fake_urlopen({"action": "pass"}, recorded)):
        hooks["post_tool_call"](
            "terminal", {}, result="out", task_id="t", tool_call_id="c1",
            status=status, error_type=error_type,
        )
        assert recorded == []
        hooks["transform_tool_result"](tool_name="terminal", args={}, result="out", tool_call_id="c1")
    (post,) = _tool_output_bodies(recorded)
    assert post["capabilities"] == ["output_rewrite"]


def test_exit_flush_is_bounded_and_drops_the_rest_with_one_warning(tmp_path, capsys, monkeypatch):
    _write_token(tmp_path)
    hooks = _registered()
    for i in range(10):
        with _plugin._ledger_lock:
            _plugin._pending_posts[f"k{i}"] = {
                "body": {"tool_name": "terminal", "call_id": f"c{i}"},
                "lapses": [], "token": "t", "tool": "terminal", "thread": -1, "original": None,
            }
    clock = [0.0]
    monkeypatch.setattr(_plugin.time, "monotonic", lambda: clock[0])
    timeouts: list = []

    def slow(req, timeout):
        timeouts.append(timeout)
        clock[0] += timeout  # each send uses its whole allowance
        raise TimeoutError("timed out")

    with patch("urllib.request.urlopen", slow):
        _plugin._flush_pending_posts()
    assert sum(timeouts) <= _plugin.EXIT_FLUSH_BUDGET_SECONDS + 1e-9
    assert all(t <= _plugin.REQUEST_TIMEOUT_SECONDS for t in timeouts)
    assert len(timeouts) < 10
    err = capsys.readouterr().err
    assert err.count("not sent within") == 1
    assert f"{10 - len(timeouts)} tool-call audit record(s)" in err
    assert _plugin._pending_posts == {}
    del hooks


def test_exit_flush_sends_everything_when_fast(tmp_path):
    _write_token(tmp_path)
    hooks = _registered()
    recorded: list = []
    with patch("urllib.request.urlopen", _fake_urlopen({"action": "pass"}, recorded)):
        for i in range(3):
            with _plugin._ledger_lock:
                _plugin._pending_posts[f"k{i}"] = {
                    "body": {"call_id": f"c{i}"}, "lapses": [], "token": "t",
                    "tool": "terminal", "thread": -1, "original": None,
                }
        _plugin._flush_pending_posts()
    assert [b["call_id"] for b in _tool_output_bodies(recorded)] == ["c0", "c1", "c2"]
    del hooks


APPLIED_REPLY = {
    "action": "redact",
    "modified_tool_response": "mail [REDACTED:email]",
    "modified_output": "mail [REDACTED:email]",
    "pii": {"types": ["email"], "count": 1, "applied": True},
}


def test_transform_first_order_rewrites_and_post_does_not_send_again(tmp_path):
    """Hermes's sequential registry path runs transform_tool_result before
    post_tool_call. The transform sends the request itself."""
    _write_token(tmp_path)
    hooks = _registered()
    recorded: list = []
    args = {"command": "cat contacts.txt"}
    with patch("urllib.request.urlopen", _fake_urlopen(APPLIED_REPLY, recorded)):
        hooks["pre_tool_call"]("terminal", args, task_id="t", tool_call_id="c1")
        out = hooks["transform_tool_result"](
            tool_name="terminal", args=args, result="mail a@b.co", task_id="t",
            tool_call_id="c1", duration_ms=7,
        )
        hooks["post_tool_call"](
            "terminal", args, result=out, task_id="t", tool_call_id="c1", status="ok"
        )
    assert out == "mail [REDACTED:email]"
    (post,) = _tool_output_bodies(recorded)
    assert post["capabilities"] == ["output_rewrite"]
    assert post["call_id"] == "c1" and post["tool_output"] == "mail a@b.co"
    assert post["session_id"] == "t" and post["duration_ms"] == 7
    assert "pre_lapse" not in post  # pre ran; no spurious lapse from either hook
    assert _plugin._pending_posts == {} and _plugin._handled_posts == {}


def test_post_first_order_still_rewrites_once(tmp_path):
    _write_token(tmp_path)
    hooks = _registered()
    recorded: list = []
    with patch("urllib.request.urlopen", _fake_urlopen(APPLIED_REPLY, recorded)):
        out = _run_call(hooks, "mail a@b.co")
    assert out == "mail [REDACTED:email]"
    (post,) = _tool_output_bodies(recorded)
    assert post["capabilities"] == ["output_rewrite"]
    assert _plugin._pending_posts == {} and _plugin._handled_posts == {}


def test_transform_first_orders_alternate_across_calls(tmp_path):
    _write_token(tmp_path)
    hooks = _registered()
    recorded: list = []
    with patch("urllib.request.urlopen", _fake_urlopen(APPLIED_REPLY, recorded)):
        for cid in ("c1", "c2"):
            out = hooks["transform_tool_result"](
                tool_name="terminal", args={}, result="mail a@b.co", tool_call_id=cid
            )
            hooks["post_tool_call"]("terminal", {}, result=out, task_id="t", tool_call_id=cid)
            assert out == "mail [REDACTED:email]"
        _run_call(hooks, "mail a@b.co", call_id="c3")
    assert [b["call_id"] for b in _tool_output_bodies(recorded)] == ["c1", "c2", "c3"]
    assert all(b["capabilities"] == ["output_rewrite"] for b in _tool_output_bodies(recorded))


def test_parked_post_is_sent_when_next_call_runs_transform_first(tmp_path):
    """A row parked by one call (its transform never ran) must not wait for
    exit when the following calls take the transform-first order, where
    post_tool_call never parks."""
    _write_token(tmp_path)
    hooks = _registered()
    recorded: list = []
    with patch("urllib.request.urlopen", _fake_urlopen({"action": "pass"}, recorded)):
        hooks["post_tool_call"]("terminal", {}, result="one", task_id="t", tool_call_id="c1")
        assert _tool_output_bodies(recorded) == []
        out = hooks["transform_tool_result"](
            tool_name="terminal", args={}, result="two", tool_call_id="c2"
        )
        hooks["post_tool_call"]("terminal", {}, result=out, task_id="t", tool_call_id="c2")
        assert sorted(b["call_id"] for b in _tool_output_bodies(recorded)) == ["c1", "c2"]
        assert _plugin._pending_posts == {}
        _plugin._flush_pending_posts()
    assert len(_tool_output_bodies(recorded)) == 2


def test_transform_with_no_post_following_sends_once_and_stays_bounded(tmp_path, monkeypatch):
    _write_token(tmp_path)
    hooks = _registered()
    monkeypatch.setattr(_plugin, "HANDLED_POSTS_MAX", 5)
    recorded: list = []
    with patch("urllib.request.urlopen", _fake_urlopen({"action": "pass"}, recorded)):
        for i in range(20):
            assert hooks["transform_tool_result"](
                tool_name="terminal", args={}, result="out", tool_call_id=f"c{i}"
            ) is None
        _plugin._flush_pending_posts()
    assert len(_tool_output_bodies(recorded)) == 20
    assert len(_plugin._handled_posts) == 5
    assert _plugin._pending_posts == {}


def test_transform_first_non_rewritable_result_declares_no_capability(tmp_path):
    _write_token(tmp_path)
    hooks = _registered()
    recorded: list = []
    with patch("urllib.request.urlopen", _fake_urlopen(APPLIED_REPLY, recorded)):
        out = hooks["transform_tool_result"](
            tool_name="terminal", args={}, result="x" * (300 * 1024), tool_call_id="c1"
        )
    assert out is None
    (post,) = _tool_output_bodies(recorded)
    assert "capabilities" not in post


def test_transform_first_without_token_sends_nothing(tmp_path):
    hooks = _registered()
    with patch("urllib.request.urlopen", side_effect=AssertionError("no network expected")):
        assert hooks["transform_tool_result"](
            tool_name="terminal", args={}, result="x", tool_call_id="c1"
        ) is None
    assert _plugin._handled_posts == {}


def test_client_id_comes_from_package_metadata_or_pinned_fallback():
    """Wire version == installed dist version (issue #6). From a bare source
    tree there is no dist metadata, so the pinned fallback must equal
    pyproject's version — that is the drift check."""
    import re
    from importlib.metadata import PackageNotFoundError, version
    from pathlib import Path

    try:
        expected = version("acp-hermes")
    except PackageNotFoundError:
        pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
        expected = re.search(r'^version = "([^"]+)"', pyproject.read_text(), re.M).group(1)
        assert _plugin._FALLBACK_VERSION == expected, "bump _FALLBACK_VERSION with pyproject"
    assert _plugin.CLIENT_ID == f"hermes-plugin/{expected}"
