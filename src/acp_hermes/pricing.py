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
        return None, "unknown"

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
        return None, "unknown"

    amount = getattr(result, "amount_usd", None)
    status = str(getattr(result, "status", "unknown") or "unknown")
    return (float(amount) if amount is not None else None), status
