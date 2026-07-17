"""Unit tests for the local metering plane (store, hooks, pricing, report)."""

from __future__ import annotations

import json
import sys
import types
from unittest.mock import patch

import pytest

import acp_hermes
from acp_hermes import local_store, pricing, report
from acp_hermes.cli import main as cli_main


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ACP_LOCAL_DB", str(tmp_path / "hermes-local.db"))
    monkeypatch.delenv("ACP_LOCAL_METERING", raising=False)
    monkeypatch.delenv("ACP_BEARER_TOKEN", raising=False)
    return tmp_path


USAGE = {
    "input_tokens": 1000,
    "output_tokens": 200,
    "cache_read_tokens": 4000,
    "cache_write_tokens": 500,
    "reasoning_tokens": 0,
    "request_count": 1,
    "prompt_tokens": 5500,
    "total_tokens": 5700,
}


def _rows(table: str) -> list[tuple]:
    conn = local_store.connect()
    try:
        return conn.execute(f"SELECT * FROM {table}").fetchall()
    finally:
        conn.close()


def test_post_api_request_records_model_call():
    acp_hermes._post_api_request(
        model="claude-sonnet-5",
        provider="anthropic",
        api_mode="anthropic_messages",
        usage=dict(USAGE),
        session_id="s1",
        task_id="t1",
        turn_id="turn1",
        api_duration=1.25,
        finish_reason="stop",
        message_count=12,
    )
    (row,) = _rows("model_calls")
    data = dict(zip([d[0] for d in _describe("model_calls")], row))
    assert data["model"] == "claude-sonnet-5"
    assert data["input_tokens"] == 1000
    assert data["cache_read_tokens"] == 4000
    assert data["api_duration_ms"] == 1250
    # Outside Hermes, pricing is honestly unknown — never a stale guess.
    assert data["cost_usd"] is None
    assert data["cost_status"] == "unknown"


def _describe(table: str) -> list[tuple]:
    conn = local_store.connect()
    try:
        return [(r[1],) for r in conn.execute(f"PRAGMA table_info({table})")]
    finally:
        conn.close()


def test_post_tool_call_records_locally_without_token():
    # No credentials anywhere — the cloud plane is off, local metering still works.
    with patch("urllib.request.urlopen", side_effect=AssertionError("no network expected")):
        acp_hermes._post_tool_call(
            tool_name="bash",
            args={"command": "ls"},
            result="x" * 100,
            task_id="t1",
            session_id="s1",
            duration_ms=42,
            status="ok",
        )
    (row,) = _rows("tool_calls")
    data = dict(zip([d[0] for d in _describe("tool_calls")], row))
    assert data["tool_name"] == "bash"
    assert data["result_bytes"] == 100
    assert data["status"] == "ok"


def test_metering_kill_switch(monkeypatch):
    monkeypatch.setenv("ACP_LOCAL_METERING", "off")
    acp_hermes._post_api_request(model="m", usage=dict(USAGE))
    acp_hermes._post_tool_call(tool_name="bash", args={}, result="hi")
    conn = local_store.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0] == 0
    finally:
        conn.close()


def test_post_llm_call_composition_buckets():
    history = [
        {"role": "system", "content": "S" * 50},
        {"role": "user", "content": "U" * 30},
        {
            "role": "user",  # Anthropic-style tool results ride user messages.
            "content": [{"type": "tool_result", "content": "T" * 400}],
        },
        {"role": "assistant", "content": [{"type": "text", "text": "A" * 20}]},
        {"role": "tool", "content": "T" * 100},  # OpenAI-style tool role.
    ]
    acp_hermes._post_llm_call(
        conversation_history=history, session_id="s1", task_id="t1", model="m"
    )
    (row,) = _rows("turns")
    data = dict(zip([d[0] for d in _describe("turns")], row))
    assert data["system_chars"] == 50
    assert data["user_chars"] == 30
    assert data["tool_chars"] == 500
    assert data["assistant_chars"] == 20
    assert data["message_count"] == 5


def test_pricing_uses_hermes_engine_when_importable(monkeypatch):
    calls = {}

    class FakeUsage:
        def __init__(self, **kw):
            calls["usage_kwargs"] = kw

    class FakeResult:
        amount_usd = 0.1234
        status = "estimated"

    def fake_estimate(model, cu, provider=None, base_url=None):
        calls["model"] = model
        calls["provider"] = provider
        return FakeResult()

    fake = types.ModuleType("agent.usage_pricing")
    fake.CanonicalUsage = FakeUsage
    fake.estimate_usage_cost = fake_estimate
    pkg = types.ModuleType("agent")
    pkg.usage_pricing = fake
    monkeypatch.setitem(sys.modules, "agent", pkg)
    monkeypatch.setitem(sys.modules, "agent.usage_pricing", fake)

    cost, status = pricing.estimate_cost_usd(
        "claude-sonnet-5", dict(USAGE), provider="anthropic"
    )
    assert cost == pytest.approx(0.1234)
    assert status == "estimated"
    assert calls["model"] == "claude-sonnet-5"
    assert calls["usage_kwargs"]["cache_read_tokens"] == 4000


def test_report_renders_and_json_parses(capsys):
    acp_hermes._post_api_request(
        model="claude-sonnet-5", provider="anthropic", usage=dict(USAGE), task_id="t1"
    )
    acp_hermes._post_tool_call(
        tool_name="bash", args={}, result="out", status="error", error_type="Timeout"
    )
    text = report.build_report(days=7)
    assert "claude-sonnet-5" in text
    assert "hermes-acp login" in text  # connect footer always present
    assert "bash" in text

    assert cli_main(["report", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["totals"]["calls"] == 1
    assert data["tools"][0]["errors"] == 1


def test_report_empty_db_message():
    text = report.build_report(days=7)
    assert "No metered activity yet" in text
