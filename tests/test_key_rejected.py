"""A 401/403 from the policy check is a credential failure, not an outage, and
a 401 with `code: "workspace_deleted"` says so once per session
(gatewaystack-connect#1693; mirrors codex-acp-plugin / pi-acp-plugin
test/key-rejected). Hermes has no unattended tier — every call is
"interactive" and the plugin fails open — so there is no deny case here.
"""

from __future__ import annotations

import io
import json
import urllib.error
from unittest.mock import patch

import pytest

import acp_hermes
from acp_hermes import _post_tool_call, _pre_tool_call

DELETED = {"error": "workspace deleted", "code": "workspace_deleted"}


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ACP_BEARER_TOKEN", "gsk_test")
    monkeypatch.delenv("ACP_API_BASE", raising=False)
    acp_hermes._reset_ledger_for_tests()
    yield
    acp_hermes._reset_ledger_for_tests()


def _http_error(status: int, payload):
    raw = b"" if payload is None else json.dumps(payload).encode()

    def _impl(req, timeout):  # noqa: ARG001
        raise urllib.error.HTTPError(req.full_url, status, "err", {}, io.BytesIO(raw))

    return _impl


def _lapses(tmp_path) -> list[dict]:
    path = tmp_path / ".acp" / "lapse.log"
    return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []


def test_401_proceeds_with_key_rejected_naming_key_and_fix_and_logs_lapse(tmp_path, capsys):
    with patch("urllib.request.urlopen", _http_error(401, {"error": "invalid key"})):
        assert _pre_tool_call("terminal", {"command": "ls"}, task_id="s1") is None
    err = capsys.readouterr().err
    assert err.startswith(
        "[ACP] ⚠ KEY REJECTED (HTTP 401): the key in ~/.acp/credentials is revoked or invalid "
        "— this call proceeded UNGOVERNED and unlogged, and every call will until it is fixed. Fix:"
    )
    assert "install.sh" in err
    assert "settings/api-keys" in err
    assert "unreachable" not in err.lower()
    assert "returned HTTP" not in err
    assert _lapses(tmp_path)[0]["detail"] == "key rejected (HTTP 401)"
    # The lapse also rides the next PostToolUse as pre_lapse, like an outage.
    assert acp_hermes._drain_lapses()[0]["detail"].startswith("key rejected")


def test_401_with_no_body_still_reads_as_key_rejected(capsys):
    with patch("urllib.request.urlopen", _http_error(401, None)):
        assert _pre_tool_call("terminal", {"command": "ls"}, task_id="s1") is None
    assert "KEY REJECTED (HTTP 401)" in capsys.readouterr().err


def test_403_is_treated_like_401(capsys):
    with patch("urllib.request.urlopen", _http_error(403, {"error": "forbidden"})):
        assert _pre_tool_call("terminal", {"command": "ls"}, task_id="s1") is None
    assert "KEY REJECTED (HTTP 403)" in capsys.readouterr().err


def test_5xx_keeps_the_outage_path(tmp_path, capsys):
    with patch("urllib.request.urlopen", _http_error(503, {"error": "down"})):
        assert _pre_tool_call("terminal", {"command": "ls"}, task_id="s1") is None
    err = capsys.readouterr().err
    assert "gateway returned HTTP 503; failing open" in err
    assert "KEY REJECTED" not in err
    assert _lapses(tmp_path) == []


def test_network_error_keeps_the_outage_path(capsys):
    def _boom(req, timeout):  # noqa: ARG001
        raise urllib.error.URLError("ECONNREFUSED")

    with patch("urllib.request.urlopen", _boom):
        assert _pre_tool_call("terminal", {"command": "ls"}, task_id="s1") is None
    err = capsys.readouterr().err
    assert "gateway unreachable" in err
    assert "KEY REJECTED" not in err


def test_post_hook_401_is_silent(capsys):
    with patch("urllib.request.urlopen", _http_error(401, {"error": "invalid key"})):
        _post_tool_call("terminal", {"command": "ls"}, result="ok", task_id="s1")
    assert "HTTP 401" not in capsys.readouterr().err


def test_workspace_deleted_once_per_session_then_silent_lapse_every_call(tmp_path, capsys):
    with patch("urllib.request.urlopen", _http_error(401, DELETED)):
        assert _pre_tool_call("terminal", {"command": "ls"}, task_id="sess-a") is None
        first = capsys.readouterr().err
        assert "workspace this machine was connected to has been deleted" in first
        assert "acp-uninstall" in first
        assert "uninstall.sh" in first
        assert (tmp_path / ".acp" / "notices" / "workspace-deleted.sess-a").exists()

        assert _pre_tool_call("terminal", {"command": "ls"}, task_id="sess-a") is None
        assert capsys.readouterr().err == ""

        assert _pre_tool_call("terminal", {"command": "ls"}, task_id="sess-b") is None
        assert "has been deleted" in capsys.readouterr().err
    assert len(_lapses(tmp_path)) == 3


def test_workspace_deleted_no_session_at_most_once_per_24h(tmp_path, capsys):
    with patch("urllib.request.urlopen", _http_error(401, DELETED)):
        _pre_tool_call("terminal", {"command": "ls"}, task_id="")
        assert "has been deleted" in capsys.readouterr().err
        assert (tmp_path / ".acp" / "notices" / "workspace-deleted.no-session").exists()
        assert _pre_tool_call("terminal", {"command": "ls"}, task_id="") is None
        assert capsys.readouterr().err == ""


def test_no_session_marker_written_by_a_js_plugin_is_honoured(tmp_path, capsys):
    # The marker is shared with the Node plugins, which write toISOString().
    import datetime as dt

    notices = tmp_path / ".acp" / "notices"
    notices.mkdir(parents=True)
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    (notices / "workspace-deleted.no-session").write_text(now)
    with patch("urllib.request.urlopen", _http_error(401, DELETED)):
        _pre_tool_call("terminal", {"command": "ls"}, task_id="")
    assert capsys.readouterr().err == ""
