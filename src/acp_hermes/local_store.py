"""Local metering store for Hermes runs.

SQLite at ~/.acp/hermes-local.db (override: ACP_LOCAL_DB). Written by the
post_api_request / post_llm_call / post_tool_call hooks; read by
`hermes-acp report`. Works with zero cloud credentials — nothing here
leaves the machine.

Three tables:
  model_calls — one row per LLM API request (tokens, cache buckets, cost)
  tool_calls  — one row per tool execution (duration, status, result size)
  turns       — one row per agent turn (context composition by role)

Writes are fail-open: any sqlite/OS error is swallowed by the hook layer so
metering can never break a Hermes run. Set ACP_LOCAL_METERING=off to disable.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS model_calls (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    session_id TEXT DEFAULT '',
    task_id TEXT DEFAULT '',
    turn_id TEXT DEFAULT '',
    api_request_id TEXT DEFAULT '',
    model TEXT DEFAULT '',
    provider TEXT DEFAULT '',
    api_mode TEXT DEFAULT '',
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    reasoning_tokens INTEGER DEFAULT 0,
    request_count INTEGER DEFAULT 1,
    api_duration_ms INTEGER DEFAULT 0,
    finish_reason TEXT DEFAULT '',
    message_count INTEGER DEFAULT 0,
    cost_usd REAL,
    cost_status TEXT DEFAULT 'unknown'
);
CREATE TABLE IF NOT EXISTS tool_calls (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    session_id TEXT DEFAULT '',
    task_id TEXT DEFAULT '',
    turn_id TEXT DEFAULT '',
    tool_name TEXT DEFAULT '',
    duration_ms INTEGER DEFAULT 0,
    status TEXT DEFAULT '',
    error_type TEXT DEFAULT '',
    result_bytes INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    session_id TEXT DEFAULT '',
    task_id TEXT DEFAULT '',
    turn_id TEXT DEFAULT '',
    model TEXT DEFAULT '',
    message_count INTEGER DEFAULT 0,
    system_chars INTEGER DEFAULT 0,
    user_chars INTEGER DEFAULT 0,
    assistant_chars INTEGER DEFAULT 0,
    tool_chars INTEGER DEFAULT 0,
    other_chars INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_model_calls_ts ON model_calls (ts);
CREATE INDEX IF NOT EXISTS idx_tool_calls_ts ON tool_calls (ts);
CREATE INDEX IF NOT EXISTS idx_turns_ts ON turns (ts);
"""


def metering_enabled() -> bool:
    return os.environ.get("ACP_LOCAL_METERING", "").strip().lower() not in (
        "off",
        "0",
        "false",
        "disabled",
    )


def db_path() -> Path:
    override = os.environ.get("ACP_LOCAL_DB")
    if override:
        return Path(override)
    return Path.home() / ".acp" / "hermes-local.db"


def connect(path: Path | None = None) -> sqlite3.Connection:
    target = path or db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), timeout=2.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def _insert(table: str, row: dict[str, Any]) -> None:
    row = dict(row)
    row.setdefault("ts", time.time())
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    conn = connect()
    try:
        conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", list(row.values()))
        conn.commit()
    finally:
        conn.close()


def record_model_call(row: dict[str, Any]) -> None:
    _insert("model_calls", row)


def record_tool_call(row: dict[str, Any]) -> None:
    _insert("tool_calls", row)


def record_turn(row: dict[str, Any]) -> None:
    _insert("turns", row)
