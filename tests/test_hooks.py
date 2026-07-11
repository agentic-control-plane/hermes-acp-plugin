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


def test_pre_ask_renders_as_block(tmp_path):
    _write_token(tmp_path)
    with patch(
        "urllib.request.urlopen",
        _fake_urlopen({"decision": "ask", "reason": "needs review"}),
    ):
        result = _pre_tool_call("terminal", {"command": "deploy"}, task_id="t1")
    assert result is not None
    assert result["action"] == "block"
    assert "Approval required" in result["message"]
    assert "dashboard" in result["message"].lower()


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
