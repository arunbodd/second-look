"""Token and cost accounting for every model call.

Providers report usage in different shapes (OpenAI Chat Completions, the Anthropic Messages API,
and the Responses-style Perplexity Agent API). ``normalize`` turns each into one record:

    {"calls", "input", "cached", "output", "reasoning", "cost_usd", "cost_source"}

``cost_source`` is "provider" when the API reported the charge (Perplexity does) and "estimate"
when it is computed from the catalogue price. Cached input is billed at the provider's cache
rate, so the estimate prices it at a quarter of the input price, a middle value across providers
(OpenAI 10% to 50%, Anthropic 10% for reads); the provider-reported cost is exact.
"""

from __future__ import annotations

from typing import Any

CACHED_PRICE_FACTOR = 0.25
FIELDS = ("calls", "input", "cached", "output", "reasoning", "cost_usd")


def empty() -> dict:
    return {"calls": 0, "input": 0, "cached": 0, "output": 0, "reasoning": 0, "cost_usd": 0.0, "cost_source": "none"}


def _int(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def price_for(model: str) -> tuple[float, float] | None:
    """USD per million tokens (input, output) for a model string, from the catalogue."""
    from .catalog import CATALOG  # local import: catalog imports providers, which import this module

    for e in CATALOG:
        if e["model"] == model and e.get("in") is not None:
            return e["in"], e["out"]
    bare = model.split("/", 1)[-1]
    for e in CATALOG:
        if e["model"].split("/", 1)[-1] == bare and e.get("in") is not None:
            return e["in"], e["out"]
    return None


def normalize(raw: dict | None, model: str) -> dict:
    raw = raw or {}
    inp = _int(raw.get("input_tokens", raw.get("prompt_tokens")))
    out = _int(raw.get("output_tokens", raw.get("completion_tokens")))
    in_det = raw.get("input_tokens_details") or raw.get("prompt_tokens_details") or {}
    out_det = raw.get("output_tokens_details") or raw.get("completion_tokens_details") or {}
    cached = _int(in_det.get("cached_tokens") or in_det.get("cache_read_input_tokens") or raw.get("cache_read_input_tokens"))
    # Anthropic reports cache reads separately from input_tokens; the others include them.
    if "cache_read_input_tokens" in raw and "prompt_tokens" not in raw and "input_tokens_details" not in raw:
        inp += cached + _int(raw.get("cache_creation_input_tokens"))
    rec = {"calls": 1, "input": inp, "cached": min(cached, inp) if inp else cached, "output": out,
           "reasoning": _int(out_det.get("reasoning_tokens")), "cost_usd": 0.0, "cost_source": "none"}
    cost = raw.get("cost")
    total = cost.get("total_cost") if isinstance(cost, dict) else cost if isinstance(cost, (int, float)) else None
    if isinstance(total, (int, float)):
        rec["cost_usd"], rec["cost_source"] = float(total), "provider"
    else:
        price = price_for(model)
        if price:
            fresh = rec["input"] - rec["cached"]
            rec["cost_usd"] = (fresh * price[0] + rec["cached"] * price[0] * CACHED_PRICE_FACTOR + out * price[1]) / 1e6
            rec["cost_source"] = "estimate"
    return rec


def add(a: dict | None, b: dict | None) -> dict:
    out = empty()
    for rec in (a, b):
        if not rec:
            continue
        for f in FIELDS:
            out[f] += rec.get(f, 0) or 0
        src = rec.get("cost_source", "none")
        if src != "none":
            out["cost_source"] = src if out["cost_source"] in ("none", src) else "mixed"
    out["cost_usd"] = round(out["cost_usd"], 6)
    return out


def total(records) -> dict:
    out = empty()
    for r in records:
        out = add(out, r)
    return out


def estimate_tokens(text: str) -> int:
    """Rough token count for text before it is sent (about four characters per token for English
    and JSON). Used only for the pre-run estimate; the counter shows the provider's own figures."""
    return max(1, len(text) // 4)
