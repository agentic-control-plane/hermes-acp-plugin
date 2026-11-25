"""Attended claim and native ask.

Every PreToolUse says whether a person can answer Hermes's approval prompt
(`attended`), and a gateway `ask` is shown through that prompt only when one
can. Hermes is not installed here: its modules are faked in sys.modules and
the terminal check goes through the plugin's `_has_tty` helper.
"""

from __future__ import annotations

import io
import json
import re
import sys
import types
import urllib.error
from pathlib import Path
from unittest.mock import patch

import pytest

import acp_hermes as plugin

# The fixture replaces plugin._has_tty; keep the real one for its own test.
_REAL_HAS_TTY = plugin._has_tty

ASK_REASON = (
    "ACP (Agentic Control Plane) asks: run terminal `deploy prod`?\n"
    "Policy: production deploys need a person\n"
    "Console: https://cloud.agenticcontrolplane.com/a/apr_1"
)
ENV_VARS = (
    "HERMES_INTERACTIVE",
    "HERMES_YOLO_MODE",
    "HERMES_CRON_SESSION",
    "HERMES_SINGLE_QUERY_SESSION",
    "HERMES_GATEWAY_SESSION",
    "HERMES_SESSION_PLATFORM",
    "CI",
)
MODES = {
    "cli", "yolo", "approvals-off", "cron", "single-query", "gateway", "ci",
    "no-tty", "not-interactive", "unknown",
}


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _gateway(reply: dict, recorded: list, before_reply=None):
    def _impl(req, timeout):
        recorded.append((req, timeout))
        if before_reply is not None:
            before_reply()
        return FakeResponse(json.dumps(reply).encode())

    return _impl


class Hermes:
    """Fake Hermes state the plugin reads."""

    def __init__(self) -> None:
        self.yolo: object = False
        self.mode: object = "manual"


@pytest.fixture(autouse=True)
def hermes(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ACP_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("ACP_API_BASE", raising=False)
    (tmp_path / ".acp").mkdir()
    (tmp_path / ".acp" / "credentials").write_text("acp_test_token")
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")

    state = Hermes()
    tools = types.ModuleType("tools")
    approval = types.ModuleType("tools.approval")
    approval.is_current_session_yolo_enabled = lambda: state.yolo
    approval_context = types.ModuleType("tools.approval_context")
    approval_context._get_approval_mode = lambda: state.mode
    tools.approval = approval
    tools.approval_context = approval_context
    monkeypatch.setitem(sys.modules, "tools", tools)
    monkeypatch.setitem(sys.modules, "tools.approval", approval)
    monkeypatch.setitem(sys.modules, "tools.approval_context", approval_context)
    # No session env helper: the plugin falls back to os.environ.
    monkeypatch.setitem(sys.modules, "gateway", None)
    monkeypatch.setitem(sys.modules, "gateway.session_context", None)
    monkeypatch.setattr(plugin, "_has_tty", lambda: True)

    plugin._reset_ledger_for_tests()
    yield state
    plugin._reset_ledger_for_tests()


def _run(reply: dict, tool_call_id: str = "call-1", before_reply=None):
    recorded: list = []
    with patch("urllib.request.urlopen", _gateway(reply, recorded, before_reply)):
        result = plugin._pre_tool_call(
            "terminal", {"command": "deploy prod"}, task_id="t1", tool_call_id=tool_call_id
        )
    bodies = [json.loads(req.data) for req, _t in recorded]
    return result, bodies


def _pre_body(reply: dict | None = None) -> dict:
    _result, bodies = _run(reply or {"decision": "allow"})
    (body,) = bodies
    assert body["hook_event_name"] == "PreToolUse"
    return body


def _assert_no_person(body: dict, mode: str, auto_approve: bool = False) -> None:
    expected = {"claim": False, "source": "env", "mode": mode}
    if auto_approve:
        expected["auto_approve"] = True
    assert body["attended"] == expected
    assert "native_ask" not in body.get("capabilities", [])


def _assert_blocked(result, why: str | None = None) -> None:
    assert result is not None
    assert result["action"] == "block"
    assert result["action"] != "approve"
    assert ASK_REASON in result["message"]
    assert result["message"].startswith("[ACP] This call needs a person's approval.")
    assert "Approve it in the ACP console, then run it again." in result["message"]
    if why is not None:
        assert f"Hermes cannot ask here ({why})" in result["message"]
    assert "rule_key" not in result


# --- attended on the wire -----------------------------------------------------


def test_attended_terminal_claims_a_person_and_declares_native_ask():
    body = _pre_body()
    assert body["attended"] == {"claim": True, "source": "env", "mode": "cli"}
    assert body["capabilities"] == ["native_ask"]
    assert body["agent_tier"] == "interactive"


def _yolo_env(mp, h):
    mp.setenv("HERMES_YOLO_MODE", "1")


def _session_yolo(mp, h):
    h.yolo = True


def _approvals_off(mp, h):
    h.mode = "off"


def _cron(mp, h):
    mp.setenv("HERMES_CRON_SESSION", "1")


def _single_query(mp, h):
    mp.setenv("HERMES_SINGLE_QUERY_SESSION", "1")


def _gateway_session(mp, h):
    mp.setenv("HERMES_GATEWAY_SESSION", "1")


def _session_platform(mp, h):
    mp.setenv("HERMES_SESSION_PLATFORM", "telegram")


def _ci(mp, h):
    mp.setenv("CI", "true")


def _no_tty(mp, h):
    mp.setattr(plugin, "_has_tty", lambda: False)


def _not_interactive(mp, h):
    mp.delenv("HERMES_INTERACTIVE", raising=False)


def _interactive_off(mp, h):
    mp.setenv("HERMES_INTERACTIVE", "0")


def _no_tools_approval(mp, h):
    mp.setitem(sys.modules, "tools.approval", None)


NO_PERSON = [
    pytest.param(_yolo_env, "yolo", True, "--yolo skips approval prompts", id="yolo-env"),
    pytest.param(_session_yolo, "yolo", True, "--yolo skips approval prompts", id="session-yolo"),
    pytest.param(_approvals_off, "approvals-off", True, "approvals are switched off", id="approvals-off"),
    pytest.param(_cron, "cron", False, "this is a cron run", id="cron"),
    pytest.param(_single_query, "single-query", False, "this is a single-query run", id="single-query"),
    pytest.param(_gateway_session, "gateway", False, "this session runs through a messaging gateway", id="gateway"),
    pytest.param(_session_platform, "gateway", False, "this session runs through a messaging gateway", id="platform"),
    pytest.param(_ci, "ci", False, None, id="ci"),
    pytest.param(_no_tty, "no-tty", False, "there is no terminal", id="no-tty"),
    pytest.param(_not_interactive, "not-interactive", False, None, id="not-interactive"),
    pytest.param(_interactive_off, "not-interactive", False, None, id="interactive-0"),
    pytest.param(_no_tools_approval, "unknown", False, None, id="tools-approval-import-fails"),
]


@pytest.mark.parametrize("setup,mode,auto_approve,why", NO_PERSON)
def test_each_condition_flips_the_claim_and_blocks_an_ask(hermes, monkeypatch, setup, mode, auto_approve, why):
    setup(monkeypatch, hermes)
    result, (body,) = _run({"decision": "ask", "reason": ASK_REASON})
    assert mode in MODES
    _assert_no_person(body, mode, auto_approve)
    assert "capabilities" not in body
    _assert_blocked(result, why)


def test_yolo_wins_over_every_other_mode(monkeypatch):
    monkeypatch.setenv("HERMES_YOLO_MODE", "1")
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    monkeypatch.setattr(plugin, "_has_tty", lambda: False)
    _assert_no_person(_pre_body(), "yolo", auto_approve=True)


@pytest.mark.parametrize("value", ["1", "true", "YES", "On", "2", "enabled", " x "])
def test_any_yolo_value_but_an_explicit_off_is_yolo(monkeypatch, value):
    monkeypatch.setenv("HERMES_YOLO_MODE", value)
    _assert_no_person(_pre_body(), "yolo", auto_approve=True)


@pytest.mark.parametrize("value", ["0", "false", "No", "OFF", ""])
def test_explicit_off_yolo_values_are_not_yolo(monkeypatch, value):
    monkeypatch.setenv("HERMES_YOLO_MODE", value)
    assert _pre_body()["attended"]["claim"] is True


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_interactive_truthy_values(monkeypatch, value):
    monkeypatch.setenv("HERMES_INTERACTIVE", value)
    assert _pre_body()["attended"]["mode"] == "cli"


@pytest.mark.parametrize("value", ["", "0", "false", "2", "maybe"])
def test_interactive_non_truthy_values(monkeypatch, value):
    monkeypatch.setenv("HERMES_INTERACTIVE", value)
    _assert_no_person(_pre_body(), "not-interactive")


def test_session_env_helper_is_used_when_importable(monkeypatch):
    """A cron or gateway flag scoped to the session counts even when the
    process env does not carry it."""
    gateway = types.ModuleType("gateway")
    session_context = types.ModuleType("gateway.session_context")
    session_vars = {"HERMES_INTERACTIVE": "1", "HERMES_CRON_SESSION": "1"}
    session_context.get_session_env = lambda name: session_vars.get(name, "")
    monkeypatch.setitem(sys.modules, "gateway", gateway)
    monkeypatch.setitem(sys.modules, "gateway.session_context", session_context)
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    _assert_no_person(_pre_body(), "cron")
    del session_vars["HERMES_CRON_SESSION"]
    assert _pre_body()["attended"] == {"claim": True, "source": "env", "mode": "cli"}


def test_session_env_helper_that_raises_falls_back_to_process_env(monkeypatch):
    session_context = types.ModuleType("gateway.session_context")

    def _boom(name):
        raise RuntimeError("no session")

    session_context.get_session_env = _boom
    monkeypatch.setitem(sys.modules, "gateway.session_context", session_context)
    assert _pre_body()["attended"]["mode"] == "cli"
    monkeypatch.setenv("HERMES_SINGLE_QUERY_SESSION", "1")
    _assert_no_person(_pre_body(), "single-query")


# --- reads the plugin cannot make never claim a person -----------------------


def _approval_context_import_fails(mp, h):
    mp.setitem(sys.modules, "tools.approval_context", None)


def _approval_mode_raises(mp, h):
    def _boom():
        raise RuntimeError("config unreadable")

    mp.setattr(sys.modules["tools.approval_context"], "_get_approval_mode", _boom)


def _approval_mode_returns(value):
    def _setup(mp, h):
        h.mode = value

    return _setup


def _session_yolo_raises(mp, h):
    def _boom():
        raise RuntimeError("no session")

    mp.setattr(sys.modules["tools.approval"], "is_current_session_yolo_enabled", _boom)


def _session_yolo_returns(value):
    def _setup(mp, h):
        h.yolo = value

    return _setup


UNREADABLE = [
    pytest.param(_approval_context_import_fails, id="approval-context-import-fails"),
    pytest.param(_approval_mode_raises, id="approval-mode-raises"),
    pytest.param(_approval_mode_returns(None), id="approval-mode-None"),
    pytest.param(_approval_mode_returns(False), id="approval-mode-False"),
    pytest.param(_approval_mode_returns(""), id="approval-mode-empty"),
    pytest.param(_approval_mode_returns("   "), id="approval-mode-blank"),
    pytest.param(_approval_mode_returns(0), id="approval-mode-int"),
    pytest.param(_approval_mode_returns(["off"]), id="approval-mode-list"),
    pytest.param(_approval_mode_returns(b"manual"), id="approval-mode-bytes"),
    pytest.param(_no_tools_approval, id="tools-approval-import-fails"),
    pytest.param(_session_yolo_raises, id="session-yolo-raises"),
    pytest.param(_session_yolo_returns(None), id="session-yolo-None"),
    pytest.param(_session_yolo_returns("yes"), id="session-yolo-non-bool"),
]


@pytest.mark.parametrize("setup", UNREADABLE)
def test_unreadable_hermes_state_never_claims_a_person(hermes, monkeypatch, setup):
    setup(monkeypatch, hermes)
    result, (body,) = _run({"decision": "ask", "reason": ASK_REASON})
    _assert_no_person(body, "unknown")
    assert "capabilities" not in body
    _assert_blocked(result, "the plugin cannot confirm that Hermes would ask a person")


# --- approvals modes: only one that puts the prompt in front of a person claims -


@pytest.mark.parametrize("mode", ["manual", "MANUAL", " Manual ", "smart", "SMART"])
def test_manual_or_smart_approvals_mode_claims_a_person(hermes, mode):
    """Smart mode only lets a model answer dangerous shell commands; a plugin
    ask still goes to the person."""
    hermes.mode = mode
    assert _pre_body()["attended"] == {"claim": True, "source": "env", "mode": "cli"}
    result, _ = _run({"decision": "ask", "reason": ASK_REASON})
    assert result["action"] == "approve"


@pytest.mark.parametrize("mode", ["auto", "guardian", "on", "ask"])
def test_unknown_approvals_mode_never_claims_a_person(hermes, mode):
    """A mode the plugin does not know might let something other than a
    person answer, so it never claims one."""
    hermes.mode = mode
    result, (body,) = _run({"decision": "ask", "reason": ASK_REASON})
    _assert_no_person(body, "unknown")
    assert "capabilities" not in body
    _assert_blocked(result, "the plugin cannot confirm that Hermes would ask a person")


# --- unattended turns (`hermes -z`) approve like yolo ---------------------------


def _session_finite(monkeypatch, unattended_turn=lambda: False, finite_turn_required=lambda: None) -> None:
    gateway = types.ModuleType("gateway")
    session_finite = types.ModuleType("gateway.session_finite")
    session_finite.unattended_turn = unattended_turn
    session_finite.finite_turn_required = finite_turn_required
    monkeypatch.setitem(sys.modules, "gateway", gateway)
    monkeypatch.setitem(sys.modules, "gateway.session_finite", session_finite)


def test_unattended_turn_is_yolo_and_blocks_an_ask(monkeypatch):
    _session_finite(monkeypatch, lambda: True)
    result, (body,) = _run({"decision": "ask", "reason": ASK_REASON})
    _assert_no_person(body, "yolo", auto_approve=True)
    _assert_blocked(result)


def test_attended_turn_with_session_finite_claims_a_person(monkeypatch):
    _session_finite(monkeypatch, lambda: False)
    assert _pre_body()["attended"]["claim"] is True


def _boom():
    raise RuntimeError("no turn")


@pytest.mark.parametrize("unattended_turn", [_boom, lambda: None, lambda: "yes"])
def test_unreadable_unattended_turn_never_claims_a_person(monkeypatch, unattended_turn):
    _session_finite(monkeypatch, unattended_turn)
    result, (body,) = _run({"decision": "ask", "reason": ASK_REASON})
    _assert_no_person(body, "unknown")
    _assert_blocked(result)


def test_session_finite_that_fails_to_import_never_claims_a_person(monkeypatch):
    """A Hermes without the module has no unattended turns; a module that is
    there but breaks on import is unreadable."""
    import importlib as _importlib

    real_import = _importlib.import_module

    def _import(name, *a, **k):
        if name == "gateway.session_finite":
            raise ImportError("broken")
        return real_import(name, *a, **k)

    monkeypatch.setattr(plugin.importlib, "import_module", _import)
    _assert_no_person(_pre_body(), "unknown")


# --- finite turns (single-query) never ask -----------------------------------------


def test_finite_turn_is_single_query_and_blocks_an_ask(monkeypatch):
    _session_finite(monkeypatch, finite_turn_required=lambda: True)
    result, (body,) = _run({"decision": "ask", "reason": ASK_REASON})
    _assert_no_person(body, "single-query")
    assert "capabilities" not in body
    _assert_blocked(result, "this is a single-query run")


@pytest.mark.parametrize("value", [None, False], ids=["no-marker", "not-finite"])
def test_turn_that_is_not_finite_claims_a_person(monkeypatch, value):
    _session_finite(monkeypatch, finite_turn_required=lambda: value)
    assert _pre_body()["attended"] == {"claim": True, "source": "env", "mode": "cli"}


@pytest.mark.parametrize("finite_turn_required", [_boom, lambda: "yes"], ids=["raises", "non-bool"])
def test_unreadable_finite_turn_never_claims_a_person(monkeypatch, finite_turn_required):
    _session_finite(monkeypatch, finite_turn_required=finite_turn_required)
    result, (body,) = _run({"decision": "ask", "reason": ASK_REASON})
    _assert_no_person(body, "unknown")
    _assert_blocked(result)


def test_missing_session_finite_module_is_not_finite_and_claims_a_person(monkeypatch):
    """The fixture leaves gateway unimportable: a Hermes without the module."""
    monkeypatch.setitem(sys.modules, "gateway.session_finite", None)
    assert plugin._finite_turn() is False
    assert plugin._unattended_turn() is False
    assert _pre_body()["attended"]["claim"] is True


# --- the source carries no raw bidi or format characters ------------------------


def test_plugin_source_has_no_raw_bidi_or_format_characters():
    source = Path(plugin.__file__).read_text(encoding="utf-8")
    # Built with chr() so this file carries no raw bidi characters either.
    hidden = [0x061C, *range(0x200B, 0x2010), *range(0x202A, 0x202F), *range(0x2066, 0x206A), 0xFEFF]
    for path in (Path(plugin.__file__), Path(__file__)):
        text = path.read_text(encoding="utf-8")
        found = sorted({hex(c) for c in hidden if chr(c) in text})
        assert not found, f"{path.name} has raw {found}"
    # The pattern still removes them from a reason.
    assert plugin._safe_reason("a" + chr(0x202E) + "b" + chr(0x2066) + "c" + chr(0x061C) + "d" + chr(0x200F) + "e") == "abcde"


# --- the reason shown in the prompt carries no terminal control characters ------


def test_control_characters_are_removed_from_the_prompt():
    reason = ASK_REASON.replace("deploy prod", "deploy\x1b\r prod\x07" + chr(0x202E))
    result, _ = _run({"decision": "ask", "reason": reason})
    assert result["action"] == "approve"
    assert result["message"] == ASK_REASON  # the three lines stay three lines


def test_control_characters_are_removed_from_a_plain_reason_and_the_block(monkeypatch):
    result, _ = _run({"decision": "ask", "reason": "needs\x1b]0;owned\x07 review\tnow\x00"})
    assert result["message"] == "[ACP] Approval required: needs]0;owned review\tnow"
    monkeypatch.setenv("HERMES_YOLO_MODE", "1")
    blocked, _ = _run({"decision": "ask", "reason": ASK_REASON + "\x1b\x9b\x7f"})
    _assert_blocked(blocked, "--yolo skips approval prompts")
    assert "\x1b" not in blocked["message"] and "\x9b" not in blocked["message"]
    assert blocked["message"].endswith(ASK_REASON)


def test_approval_mode_helper_is_also_read_from_tools_approval(hermes, monkeypatch):
    monkeypatch.setitem(sys.modules, "tools.approval_context", None)
    monkeypatch.setattr(sys.modules["tools.approval"], "_get_approval_mode", lambda: "off", raising=False)
    result, (body,) = _run({"decision": "ask", "reason": ASK_REASON})
    _assert_no_person(body, "approvals-off", auto_approve=True)
    _assert_blocked(result, "approvals are switched off")


def test_approval_mode_from_tools_approval_that_is_on_claims_a_person(monkeypatch):
    monkeypatch.setitem(sys.modules, "tools.approval_context", None)
    monkeypatch.setattr(sys.modules["tools.approval"], "_get_approval_mode", lambda: "manual", raising=False)
    assert _pre_body()["attended"]["claim"] is True


def test_approvals_off_is_read_case_insensitively(hermes):
    hermes.mode = "OFF"
    _assert_no_person(_pre_body(), "approvals-off", auto_approve=True)


def test_has_tty_needs_both_stdin_and_stdout():
    class Stream:
        def __init__(self, tty: bool) -> None:
            self._tty = tty

        def isatty(self) -> bool:
            return self._tty

    for stdin, stdout, expected in ((True, True, True), (True, False, False), (False, True, False)):
        with patch.object(sys, "stdin", Stream(stdin)), patch.object(sys, "stdout", Stream(stdout)):
            assert _REAL_HAS_TTY() is expected
    with patch.object(sys, "stdin", None):
        assert _REAL_HAS_TTY() is False


# --- ask through Hermes's prompt ----------------------------------------------


def test_ask_with_a_person_passes_the_gateway_reason_unchanged():
    result, _ = _run({"decision": "ask", "reason": ASK_REASON})
    assert result["action"] == "approve"
    assert result["message"] == ASK_REASON


def test_ask_reason_already_marked_acp_is_unchanged():
    result, _ = _run({"decision": "ask", "reason": "[ACP] hold: needs review"})
    assert result["message"] == "[ACP] hold: needs review"


def test_ask_plain_reason_gets_the_acp_prefix():
    result, _ = _run({"decision": "ask", "reason": "needs review"})
    assert result["message"] == "[ACP] Approval required: needs review"


_HEX32 = re.compile(r"[0-9a-f]{32}")


def test_rule_key_uses_approval_id_as_prefix_and_is_fresh_every_ask():
    reply = {"decision": "ask", "reason": ASK_REASON, "approval_id": "apr_1"}
    first, _ = _run(reply, tool_call_id="call-1")
    second, _ = _run(reply, tool_call_id="call-1")
    for r in (first, second):
        assert r["rule_key"].startswith("acp:terminal:apr_1:")
        assert _HEX32.fullmatch(r["rule_key"].rsplit(":", 1)[1])
    assert first["rule_key"] != second["rule_key"]


def test_rule_key_falls_back_to_tool_call_id_prefix():
    first, _ = _run({"decision": "ask", "reason": ASK_REASON}, tool_call_id="call-7")
    second, _ = _run({"decision": "ask", "reason": ASK_REASON}, tool_call_id="call-7")
    assert first["rule_key"].startswith("acp:terminal:call-7:")
    assert second["rule_key"].startswith("acp:terminal:call-7:")
    assert first["rule_key"] != second["rule_key"]


def test_rule_key_without_ids_is_tool_plus_uuid():
    first, _ = _run({"decision": "ask", "reason": ASK_REASON}, tool_call_id="")
    second, _ = _run({"decision": "ask", "reason": ASK_REASON}, tool_call_id="")
    for r in (first, second):
        prefix, _sep, suffix = r["rule_key"].rpartition(":")
        assert prefix == "acp:terminal"
        assert _HEX32.fullmatch(suffix)
    assert first["rule_key"] != second["rule_key"]


def test_rule_key_ends_in_uuid4_hex(monkeypatch):
    class _U:
        hex = "0123456789abcdef0123456789abcdef"

    monkeypatch.setattr(plugin.uuid, "uuid4", lambda: _U())
    result, _ = _run({"decision": "ask", "reason": ASK_REASON, "approval_id": "apr_1"})
    assert result["rule_key"] == "acp:terminal:apr_1:0123456789abcdef0123456789abcdef"


def test_yolo_typed_while_the_request_was_out_blocks_the_ask(hermes):
    def _toggle():
        hermes.yolo = True

    result, (body,) = _run({"decision": "ask", "reason": ASK_REASON}, before_reply=_toggle)
    assert body["attended"]["claim"] is True  # sent before /yolo
    _assert_blocked(result, "--yolo skips approval prompts")


def test_allow_and_deny_are_unchanged_without_a_person(monkeypatch):
    monkeypatch.setenv("HERMES_YOLO_MODE", "1")
    allowed, _ = _run({"decision": "allow"})
    assert allowed is None
    reason = "rm -rf is not allowed. See https://cloud.agenticcontrolplane.com/d/abc123?x=<1>&y=2"
    denied, _ = _run({"decision": "deny", "reason": reason})
    assert denied == {"action": "block", "message": f"[ACP] Denied by policy: {reason}"}


# --- gateway failures never approve --------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        urllib.error.URLError("connection refused"),
        TimeoutError("slow"),
        ConnectionResetError("reset"),
        urllib.error.HTTPError("http://x", 500, "boom", {}, io.BytesIO(b"")),
        urllib.error.HTTPError("http://x", 429, "slow down", {}, io.BytesIO(b"")),
        urllib.error.HTTPError("http://x", 401, "revoked", {}, io.BytesIO(b"")),
    ],
)
def test_gateway_failure_with_a_person_never_approves(exc):
    timeouts: list = []

    def _raise(req, timeout):
        timeouts.append(timeout)
        raise exc

    with patch("urllib.request.urlopen", _raise):
        result = plugin._pre_tool_call("terminal", {"command": "deploy"}, task_id="t", tool_call_id="c")
    assert result is None
    assert timeouts == [plugin.REQUEST_TIMEOUT_SECONDS] and plugin.REQUEST_TIMEOUT_SECONDS == 4.0


@pytest.mark.parametrize("raw", [b"not json", b"[\"ask\"]", b"\"ask\"", b"null", b"{\"decision\": \"maybe\"}"])
def test_malformed_gateway_reply_with_a_person_never_approves(raw):
    def _reply(req, timeout):
        return FakeResponse(raw)

    with patch("urllib.request.urlopen", _reply):
        result = plugin._pre_tool_call("terminal", {"command": "deploy"}, task_id="t", tool_call_id="c")
    assert result is None or result.get("action") != "approve"


# --- PostToolUse carries neither -------------------------------------------------


def test_post_tool_use_carries_no_attended_and_no_capabilities():
    recorded: list = []
    with patch("urllib.request.urlopen", _gateway({"action": "pass"}, recorded)):
        plugin._pre_tool_call("terminal", {"command": "ls"}, task_id="t", tool_call_id="c1")
        plugin._post_tool_call("terminal", {"command": "ls"}, result="ok", task_id="t", tool_call_id="c1")
    pre, post = (json.loads(req.data) for req, _t in recorded)
    assert pre["capabilities"] == ["native_ask"] and pre["attended"]["claim"] is True
    assert post["hook_event_name"] == "PostToolUse"
    assert "attended" not in post and "capabilities" not in post


# --- version -------------------------------------------------------------------


def test_version_is_the_same_in_all_three_places():
    root = Path(__file__).resolve().parents[1]
    pyproject = re.search(r'^version = "([^"]+)"', (root / "pyproject.toml").read_text(), re.M).group(1)
    manifest = re.search(
        r"^version:\s*(\S+)", (root / "src" / "acp_hermes" / "plugin.yaml").read_text(), re.M
    ).group(1)
    assert pyproject == manifest == plugin._FALLBACK_VERSION
    # Bumped past the release before attended claims (0.4.0).
    assert tuple(int(p) for p in pyproject.split(".")[:3]) > (0, 4, 0)


def test_readme_says_when_the_prompt_appears_yolo_blocks_and_always_does_not_carry_over():
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
    assert "The prompt appears only when a person is there to answer it" in readme
    assert "the plugin turns a held call into a block" in readme
    assert "It does not carry over to later asks" in readme
