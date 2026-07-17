"""`hermes-acp report` — local cost & quality X-ray for Hermes runs.

Reads ~/.acp/hermes-local.db (written by the metering hooks) and renders
spend by model, cache hit rate, context composition, tool error rates, and
per-task cost spread. Pure stdlib, no network, no account.

`--json` emits the same aggregates as machine-readable JSON so agents can
read their own economics and self-optimize.
"""

from __future__ import annotations

import json
import time
from typing import Any

from . import local_store


def _fmt_tokens(n: float) -> str:
    n = int(n or 0)
    if n >= 10_000_000:
        return f"{n / 1_000_000:.0f}M"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 10_000:
        return f"{n / 1_000:.0f}K"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _fmt_usd(v: float | None) -> str:
    if v is None:
        return "n/a"
    if v >= 0.995:
        return f"${v:.2f}"
    return f"${v:.3f}"


def _percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, max(0, round(p * (len(sorted_vals) - 1))))
    return sorted_vals[idx]


def gather(days: int = 7) -> dict[str, Any]:
    """Collect report aggregates from the local DB."""
    since = time.time() - days * 86400
    conn = local_store.connect()
    try:
        models = [
            dict(
                model=r[0] or "(unknown)",
                calls=r[1],
                input_tokens=r[2],
                output_tokens=r[3],
                cache_read_tokens=r[4],
                cache_write_tokens=r[5],
                reasoning_tokens=r[6],
                cost_usd=r[7],
                unpriced_calls=r[8],
            )
            for r in conn.execute(
                """
                SELECT model, COUNT(*), SUM(input_tokens), SUM(output_tokens),
                       SUM(cache_read_tokens), SUM(cache_write_tokens),
                       SUM(reasoning_tokens), SUM(cost_usd),
                       SUM(CASE WHEN cost_usd IS NULL THEN 1 ELSE 0 END)
                FROM model_calls WHERE ts >= ? GROUP BY model
                ORDER BY SUM(cost_usd) DESC, COUNT(*) DESC
                """,
                (since,),
            )
        ]

        comp = conn.execute(
            """
            SELECT COUNT(*), SUM(system_chars), SUM(user_chars),
                   SUM(assistant_chars), SUM(tool_chars), SUM(other_chars)
            FROM turns WHERE ts >= ?
            """,
            (since,),
        ).fetchone()
        composition = {
            "turns": comp[0] or 0,
            "system_chars": comp[1] or 0,
            "user_chars": comp[2] or 0,
            "assistant_chars": comp[3] or 0,
            "tool_chars": comp[4] or 0,
            "other_chars": comp[5] or 0,
        }

        tools = [
            dict(
                tool=r[0],
                calls=r[1],
                errors=r[2],
                avg_ms=round(r[3] or 0),
                result_bytes=r[4] or 0,
            )
            for r in conn.execute(
                """
                SELECT tool_name, COUNT(*),
                       SUM(CASE WHEN status NOT IN ('', 'ok', 'success') THEN 1 ELSE 0 END),
                       AVG(duration_ms), SUM(result_bytes)
                FROM tool_calls WHERE ts >= ? GROUP BY tool_name
                ORDER BY COUNT(*) DESC LIMIT 10
                """,
                (since,),
            )
        ]

        task_costs = sorted(
            r[0]
            for r in conn.execute(
                """
                SELECT SUM(cost_usd) FROM model_calls
                WHERE ts >= ? AND task_id != '' AND cost_usd IS NOT NULL
                GROUP BY task_id
                """,
                (since,),
            )
            if r[0] is not None
        )
        task_tokens = sorted(
            float(r[0] or 0)
            for r in conn.execute(
                """
                SELECT SUM(input_tokens + cache_read_tokens + cache_write_tokens + output_tokens)
                FROM model_calls WHERE ts >= ? AND task_id != '' GROUP BY task_id
                """,
                (since,),
            )
        )
        sessions = conn.execute(
            "SELECT COUNT(DISTINCT session_id) FROM model_calls WHERE ts >= ?",
            (since,),
        ).fetchone()[0]
    finally:
        conn.close()

    prompt_total = sum(
        m["input_tokens"] + m["cache_read_tokens"] + m["cache_write_tokens"] for m in models
    )
    cache_read = sum(m["cache_read_tokens"] for m in models)
    return {
        "days": days,
        "db_path": str(local_store.db_path()),
        "models": models,
        "totals": {
            "calls": sum(m["calls"] for m in models),
            "input_tokens": sum(m["input_tokens"] for m in models),
            "output_tokens": sum(m["output_tokens"] for m in models),
            "prompt_tokens": prompt_total,
            "cache_read_tokens": cache_read,
            "cost_usd": sum(m["cost_usd"] or 0 for m in models),
            "unpriced_calls": sum(m["unpriced_calls"] for m in models),
            "cache_hit_rate": (cache_read / prompt_total) if prompt_total else None,
            "sessions": sessions,
        },
        "composition": composition,
        "tools": tools,
        "tasks": {
            "count": len(task_costs),
            "median_cost_usd": _percentile(task_costs, 0.5),
            "p90_cost_usd": _percentile(task_costs, 0.9),
            "max_cost_usd": task_costs[-1] if task_costs else 0.0,
            "token_count": len(task_tokens),
            "median_tokens": _percentile(task_tokens, 0.5),
            "p90_tokens": _percentile(task_tokens, 0.9),
            "max_tokens": task_tokens[-1] if task_tokens else 0.0,
        },
    }


def _cache_hit(m: dict[str, Any]) -> str:
    denom = m["input_tokens"] + m["cache_read_tokens"] + m["cache_write_tokens"]
    if not denom:
        return "—"
    return f"{100 * m['cache_read_tokens'] / denom:.0f}%"


def render(data: dict[str, Any]) -> str:
    lines: list[str] = []
    out = lines.append
    t = data["totals"]
    out(f"ACP local report — last {data['days']} days (this machine, {data['db_path']})")
    out("")

    if not data["models"] and not data["tools"]:
        out("No metered activity yet. Run Hermes with the acp plugin enabled and")
        out("come back — every model call and tool call lands here automatically.")
        return "\n".join(lines)

    if data["models"]:
        out("MODEL SPEND")
        out(f"  {'model':<34} {'calls':>6} {'in-tok':>8} {'out-tok':>8} {'cache':>6} {'est cost':>9}")
        for m in data["models"]:
            out(
                f"  {m['model'][:34]:<34} {m['calls']:>6} {_fmt_tokens(m['input_tokens']):>8}"
                f" {_fmt_tokens(m['output_tokens']):>8} {_cache_hit(m):>6}"
                f" {_fmt_usd(m['cost_usd']):>9}"
            )
        all_unpriced = t["unpriced_calls"] >= t["calls"]
        total_cost = "n/a" if all_unpriced else _fmt_usd(t["cost_usd"])
        out(
            f"  {'total':<34} {t['calls']:>6} {_fmt_tokens(t['input_tokens']):>8}"
            f" {_fmt_tokens(t['output_tokens']):>8} {'':>6} {total_cost:>9}"
        )
        if t["unpriced_calls"]:
            out(f"  ({t['unpriced_calls']} calls have no price — route unknown to Hermes pricing)")
        out("")

        hit = t["cache_hit_rate"]
        if hit is not None:
            out(
                f"CACHE  hit rate {100 * hit:.0f}% ({_fmt_tokens(t['cache_read_tokens'])}"
                f" of {_fmt_tokens(t['prompt_tokens'])} prompt tokens read from cache)"
            )
            if hit < 0.4 and t["calls"] >= 20:
                out("  Low. A stable system prompt and append-only context (no mid-loop edits)")
                out("  are the two biggest levers — cached reads bill at ~10% of full rate.")
            out("")

    comp = data["composition"]
    comp_total = sum(
        comp[k] for k in ("system_chars", "user_chars", "assistant_chars", "tool_chars", "other_chars")
    )
    if comp_total:
        pct = lambda k: 100 * comp[k] / comp_total  # noqa: E731
        out("CONTEXT COMPOSITION (share of prompt chars, all turns)")
        out(
            f"  tool results {pct('tool_chars'):.0f}% · system {pct('system_chars'):.0f}%"
            f" · assistant {pct('assistant_chars'):.0f}% · user {pct('user_chars'):.0f}%"
        )
        if pct("tool_chars") > 50:
            out("  Tool results dominate your context — truncating or summarizing large")
            out("  tool outputs is usually the cheapest big win.")
        out("")

    if data["tools"]:
        out("TOOLS (top by calls)")
        out(f"  {'tool':<28} {'calls':>6} {'errors':>7} {'avg ms':>7} {'output':>8}")
        for tool in data["tools"]:
            out(
                f"  {tool['tool'][:28]:<28} {tool['calls']:>6} {tool['errors']:>7}"
                f" {tool['avg_ms']:>7} {_fmt_tokens(tool['result_bytes']) + 'B':>8}"
            )
        noisy = [x for x in data["tools"] if x["calls"] >= 5 and x["errors"] / x["calls"] > 0.2]
        if noisy:
            names = ", ".join(x["tool"] for x in noisy[:3])
            out(f"  High error rate on: {names} — failed calls still bill the tokens that")
            out("  set them up and the retries that follow.")
        out("")

    tasks = data["tasks"]
    if tasks["count"]:
        out(
            f"TASKS  {tasks['count']} priced tasks · median {_fmt_usd(tasks['median_cost_usd'])}"
            f" · p90 {_fmt_usd(tasks['p90_cost_usd'])} · max {_fmt_usd(tasks['max_cost_usd'])}"
        )
        out("")
    elif tasks["token_count"]:
        out(
            f"TASKS  {tasks['token_count']} tasks · median {_fmt_tokens(tasks['median_tokens'])} tok"
            f" · p90 {_fmt_tokens(tasks['p90_tokens'])} tok · max {_fmt_tokens(tasks['max_tokens'])} tok"
        )
        out("")

    out("— This is one machine's local view. Policies, inline approvals, the team")
    out("  dashboard, and every machine in one place: `hermes-acp login` (free).")
    out("  https://cloud.agenticcontrolplane.com")
    return "\n".join(lines)


def build_report(days: int = 7, as_json: bool = False) -> str:
    data = gather(days)
    if as_json:
        return json.dumps(data, indent=2)
    return render(data)
