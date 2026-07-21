"""Cost estimation that defers to Hermes's own pricing engine.

The plugin runs inside the Hermes process, so ``agent.usage_pricing`` —
the same module Hermes uses for its session cost display — is importable
at hook time. We reuse it rather than maintaining a second price table:
our numbers always match what Hermes shows the user, and routes Hermes
knows about (OpenRouter, Bedrock, subscription-included billing) come
along for free.

Outside Hermes (unit tests, standalone CLI use) the import fails and cost
is honestly ``(None, "unknown")`` — never a stale guess.
"""

from __future__ import annotations

from typing import Any


def estimate_cost_usd(
    model: str,
    usage: dict[str, Any],
    *,
    provider: str | None = None,
    base_url: str | None = None,
) -> tuple[float | None, str]:
    """Return (cost_usd, status) for one API request's usage summary.

    ``usage`` is the dict from Hermes's post_api_request hook (asdict of
    CanonicalUsage). status is Hermes's CostStatus ("estimated",
    "included", "unknown") or "unknown" when pricing is unavailable.
    """
    try:
        from agent.usage_pricing import CanonicalUsage, estimate_usage_cost
    except Exception:
        return _fallback_estimate(model, usage)

    try:
        cu = CanonicalUsage(
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            cache_read_tokens=int(usage.get("cache_read_tokens") or 0),
            cache_write_tokens=int(usage.get("cache_write_tokens") or 0),
            reasoning_tokens=int(usage.get("reasoning_tokens") or 0),
            request_count=int(usage.get("request_count") or 1),
        )
        result = estimate_usage_cost(
            model, cu, provider=provider or None, base_url=base_url or None
        )
    except Exception:
        return _fallback_estimate(model, usage)

    amount = getattr(result, "amount_usd", None)
    status = str(getattr(result, "status", "unknown") or "unknown")
    if amount is None or status == "unknown":
        fb_amount, fb_status = _fallback_estimate(model, usage)
        if fb_amount is not None:
            return fb_amount, fb_status
    return (float(amount) if amount is not None else None), status


# ── Fallback price table (0.2.3) ─────────────────────────────────────
# Used ONLY when Hermes's engine returns unknown — a cost X-ray whose
# money column says "n/a" isn't one (bit our own dogfood agent on the
# proxy's -latest routes). USD per 1M tokens; cache reads bill at
# read_mult × input (0.1 typical; Gemini implicit ~0.25), writes at
# 1.25 × input. Values mirror the gateway's MODEL_PRICING — update both
# together. Longest-prefix match so dated/aliased ids resolve.
_FALLBACK_UPDATED = "2026-07-21"  # bump when the table is refreshed
_FALLBACK_PER_1M: dict[str, tuple[float, float, float]] = {
    # model-prefix: (input, output, cache_read_mult)
    "gemini-flash-lite": (0.10, 0.40, 0.25),
    "gemini-flash": (0.30, 2.50, 0.25),
    "gemini-pro": (2.00, 12.00, 0.25),
    "gemini-2.5-flash": (0.15, 0.60, 0.25),
    "gemini-2.5-pro": (1.25, 10.00, 0.25),
    "claude-fable-5": (10.00, 50.00, 0.10),
    "claude-opus": (5.00, 25.00, 0.10),
    "claude-sonnet": (3.00, 15.00, 0.10),
    "claude-haiku": (1.00, 5.00, 0.10),
    "gpt-4o-mini": (0.15, 0.60, 0.50),
    "gpt-4o": (2.50, 10.00, 0.50),
    "gpt-5": (1.25, 10.00, 0.10),
    "llama-3.3-70b": (0.59, 0.79, 0.10),
    "llama-3.1-8b": (0.05, 0.08, 0.10),
}


def _fallback_estimate(model: str, usage: dict[str, Any]) -> tuple[float | None, str]:
    """Longest-prefix fallback pricing; (None, 'unknown') when unmatched."""
    m = (model or "").lower().split("/")[-1]
    best = None
    for prefix, rates in _FALLBACK_PER_1M.items():
        if m.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, rates)
    if not best:
        return None, "unknown"
    inp, out, read_mult = best[1]
    it = int(usage.get("input_tokens") or 0)
    ot = int(usage.get("output_tokens") or 0)
    cr = int(usage.get("cache_read_tokens") or 0)
    cw = int(usage.get("cache_write_tokens") or 0)
    cost = (it / 1e6) * inp + (ot / 1e6) * out + (cr / 1e6) * inp * read_mult + (cw / 1e6) * inp * 1.25
    return cost, "estimated-fallback"
