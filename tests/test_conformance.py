"""ACP plugin conformance adapter for hermes-acp-plugin.

Drives the REAL entry point (acp_hermes._post_tool_call, the function that
acp_hermes.register() wires onto Hermes's native "post_tool_call" hook name)
against a fake gateway, using the shared corpus vendored at
tests/fixtures/plugin-corpus.json (see davidcrowe/gatewaystack-connect#1344).

Canonical field mapping (declared per the corpus's post-tool contract):
  - tool_name: hermes has NO tool-name canonicalisation table. The native
    tool name Hermes hands the hook is forwarded byte for byte as
    body["tool_name"]. So case.call["tool"] ("shell") is fed straight in as
    the hook's tool_name positional arg and expected back unchanged.
  - session_id: acp_hermes._post_tool_call's outgoing wire body sets
    body["session_id"] = task_id — NOT the hook's separate `session_id=`
    kwarg (see src/acp_hermes/__init__.py). So case.call["sessionId"] is fed
    in as this hook's `task_id=` kwarg, which is what a real Hermes session
    id would occupy on the wire.

Run with PYTHONPATH pointing at THIS WORKTREE's src/ so the code under test
is the worktree's, not any installed copy:

    PYTHONPATH=<this worktree>/src <python> -m pytest tests/ -q

test_worktree_module_is_under_test asserts acp_hermes.__file__ resolves
under this worktree's path, so a wrong PYTHONPATH fails loudly instead of
silently testing the wrong code.
"""

from __future__ import annotations

import contextlib
import hashlib
import http.server
import json
import threading
from pathlib import Path

import pytest

import acp_hermes

FIXTURE = Path(__file__).parent / "fixtures" / "plugin-corpus.json"
PINNED_FINGERPRINT = "aa186d3fb3e7d18c"


def _load_corpus() -> dict:
    raw = FIXTURE.read_bytes()
    fp = hashlib.sha256(raw).hexdigest()[:16]
    if fp != PINNED_FINGERPRINT:
        raise AssertionError(
            f"vendored plugin-corpus.json fingerprint {fp} != pinned "
            f"{PINNED_FINGERPRINT}; re-vendor from "
            "davidcrowe/gatewaystack-connect:conformance/plugin-corpus.json"
        )
    return json.loads(raw)


CORPUS = _load_corpus()
MARKER = CORPUS["marker"]
CASES = {c["id"]: c for c in CORPUS["cases"]}
PLUGIN_ROWS = {
    row["capability"]: row
    for row in CORPUS["harnesses"]
    if row["plugin"] == "hermes-acp-plugin"
}

# A case listed here is EXPECTED to currently fail the corpus contract.
# Each entry is asserted to actually fail (so a fix turns this file red
# until the entry is removed), and the full list is checked against every
# case's actual outcome (so an undeclared NEW failure also turns this file
# red instead of silently passing).
EXPECTED_DIVERGENCES = [
    {
        "case": "notice-shown",
        "issue": "#1334",
        "detail": (
            "_post_tool_call() in src/acp_hermes/__init__.py calls "
            "_post_json('/govern/tool-output', ...) and discards the return "
            "value entirely — no code path reads result.get('notice'), so "
            "the marker never reaches stdout, stderr, or any logger."
        ),
    },
]
_DIVERGENT_CASE_IDS = {d["case"] for d in EXPECTED_DIVERGENCES}


def test_fingerprint_pinned() -> None:
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest()[:16] == PINNED_FINGERPRINT


def test_plugin_rows_present_and_supported() -> None:
    assert PLUGIN_ROWS["notice"]["status"] == "supported"
    assert PLUGIN_ROWS["post-tool"]["status"] == "supported"


def test_worktree_module_is_under_test() -> None:
    # Guards against a bad PYTHONPATH silently exercising an installed copy
    # (e.g. the primary checkout's .venv site-packages) instead of this
    # worktree's src/.
    # Must hold in any checkout (a CI clone, a worktree), so compare against
    # this repo's own src/ rather than a folder name.
    from pathlib import Path

    repo_src = (Path(__file__).resolve().parents[1] / "src").resolve()
    module = Path(acp_hermes.__file__).resolve()
    assert repo_src in module.parents, (
        f"acp_hermes imported from {str(module)!r}, not {str(repo_src)!r} — "
        "check PYTHONPATH"
    )


# --- fake gateway ------------------------------------------------------------


@contextlib.contextmanager
def fake_gateway(reply_for_tool_output: dict):
    """Real HTTP server on 127.0.0.1:0. /govern/tool-output -> reply_for_tool_output.
    Every other path -> {"decision": "allow"}. Records every request."""
    requests: list[dict] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib method name
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw) if raw else None
            except json.JSONDecodeError:
                body = raw.decode("utf-8", "replace")
            requests.append({"method": "POST", "path": self.path, "body": body})
            reply = reply_for_tool_output if self.path == "/govern/tool-output" else {"decision": "allow"}
            payload = json.dumps(reply).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args: object) -> None:  # silence stderr noise
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, requests
    finally:
        server.shutdown()
        thread.join(timeout=2)


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    # Isolate HOME so no first-per-session marker or real credential on the
    # developer's machine can influence the plugin.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ACP_SHADOW", raising=False)
    monkeypatch.delenv("ACP_BEARER_TOKEN", raising=False)
    acp_dir = tmp_path / ".acp"
    acp_dir.mkdir()
    # Dummy credential in the plugin's own expected shape/prefix. Never real.
    (acp_dir / "credentials").write_text("gsk_conformance_dummy_not_real\n")
    yield


def _call_post_tool(monkeypatch, server, env: dict, *, tool: str, command: str, output: str, session_id: str) -> None:
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    port = server.server_address[1]
    monkeypatch.setenv("ACP_API_BASE", f"http://127.0.0.1:{port}")
    acp_hermes._post_tool_call(
        tool,
        {"command": command},
        result=output,
        task_id=session_id,  # see module docstring: lands in body["session_id"]
        duration_ms=5,
    )


# --- notice capability --------------------------------------------------------


@pytest.mark.parametrize("case_id", ["notice-shown", "notice-shadow-off"])
def test_notice_channel_stdout_stderr(case_id, capsys, monkeypatch) -> None:
    """Channels asserted: process stdout and stderr — acp_hermes has no
    toast/logger/UI-notify surface, so these are the only person-visible
    channels its process can use."""
    case = CASES[case_id]
    with fake_gateway(case["gatewayReply"]) as (server, _requests):
        _call_post_tool(
            monkeypatch,
            server,
            case["env"],
            tool="shell",
            command=f"echo {MARKER}",
            output=f"{MARKER}\n",
            session_id="acpconf-session-0001",
        )
    out, err = capsys.readouterr()
    seen = (MARKER in out) or (MARKER in err)
    expected = case["expect"]["personSees"]

    if case_id in _DIVERGENT_CASE_IDS:
        assert seen != expected, (
            f"{case_id}: now matches the corpus contract (seen={seen}); "
            "fixed, remove the EXPECTED_DIVERGENCES entry"
        )
    else:
        assert seen == expected, f"stdout={out!r} stderr={err!r}"


def test_expected_divergences_matches_actual_failures(monkeypatch, capsys) -> None:
    """Recompute pass/fail for every notice case independently of the test
    above and assert EXPECTED_DIVERGENCES is exactly the set that actually
    fails — so an undeclared NEW failure (or a silently-fixed one) also
    turns this file red."""
    actual_failures = []
    for case_id in ("notice-shown", "notice-shadow-off"):
        case = CASES[case_id]
        with fake_gateway(case["gatewayReply"]) as (server, _requests):
            _call_post_tool(
                monkeypatch,
                server,
                case["env"],
                tool="shell",
                command=f"echo {MARKER}",
                output=f"{MARKER}\n",
                session_id="acpconf-session-0001",
            )
        out, err = capsys.readouterr()
        seen = (MARKER in out) or (MARKER in err)
        if seen != case["expect"]["personSees"]:
            actual_failures.append(case_id)
        monkeypatch.delenv("ACP_SHADOW", raising=False)

    assert sorted(actual_failures) == sorted(_DIVERGENT_CASE_IDS)


# --- post-tool capability -----------------------------------------------------

# Declared canonical mapping: hermes performs no tool-name canonicalisation,
# so the native tool name is expected back on the wire unchanged.
NATIVE_TOOL_NAME = "shell"


def test_post_tool_fields_reach_gateway(monkeypatch) -> None:
    case = CASES["post-tool-fields"]
    call = case["call"]
    with fake_gateway(case["gatewayReply"]) as (server, requests):
        _call_post_tool(
            monkeypatch,
            server,
            case["env"],
            tool=call["tool"],
            command=call["command"],
            output=call["output"],
            session_id=call["sessionId"],
        )

    posts = [r for r in requests if r["path"] == "/govern/tool-output" and r["method"] == "POST"]
    assert len(posts) == 1, f"expected exactly one POST /govern/tool-output, got {requests!r}"
    body = posts[0]["body"]

    assert body["hook_event_name"] == "PostToolUse"
    assert body["tool_name"] == NATIVE_TOOL_NAME
    assert isinstance(body["tool_input"], dict)
    assert MARKER in json.dumps(body["tool_input"])
    assert MARKER in json.dumps(body["tool_output"])
    assert isinstance(body["session_id"], str) and body["session_id"]
