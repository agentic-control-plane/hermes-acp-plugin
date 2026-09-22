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


def test_pre_ask_escalates_to_native_approval_gate(tmp_path):
    """ACP `ask` returns Hermes's approve directive — the native
    once/session/always/deny prompt — not a hard block (0.1.1)."""
    _write_token(tmp_path)
    with patch(
        "urllib.request.urlopen",
        _fake_urlopen({"decision": "ask", "reason": "needs review"}),
    ):
        result = _pre_tool_call("terminal", {"command": "deploy"}, task_id="t1")
    assert result is not None
    assert result["action"] == "approve"
    assert "needs review" in result["message"]
    assert result["rule_key"] == "acp:terminal"


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
