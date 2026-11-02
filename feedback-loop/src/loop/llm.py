"""One structured Claude call, with refusal handling, usage logging and cost."""

import json
import time
from dataclasses import dataclass, field

import anthropic

MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# $/MTok: input, output, cache read, cache write (5-minute TTL). Fallback-served calls are priced
# at these rates too, so cost is approximate when served_model differs.
PRICES = {"claude-opus-5-5": (4.0, 20.0, 0.20, 5.0)}
# Our own request is wrong or credentials are bad: stop the run instead of failing every item.
FATAL = (anthropic.BadRequestError, anthropic.AuthenticationError, anthropic.PermissionDeniedError,
         anthropic.NotFoundError)


@dataclass
class LLMResult:
    data: dict | None = None
    stop_reason: str | None = None
    refusal_category: str | None = None
    served_model: str | None = None
    request_id: str | None = None
    usage: dict = field(default_factory=dict)
    latency_ms: int = 0
    cost_usd: float = 0.0
    error: str | None = None


def make_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(max_retries=4)


def cost_usd(usage: dict, model: str = MODEL) -> float:
    p_in, p_out, p_read, p_write = PRICES[model]
    return (usage.get("input_tokens", 0) * p_in + usage.get("output_tokens", 0) * p_out
            + usage.get("cache_read_tokens", 0) * p_read + usage.get("cache_write_tokens", 0) * p_write) / 1e6


def structured_call(client, *, system: str, user: str, schema: dict, effort: str,
                    max_tokens: int = 8000) -> LLMResult:
    """Ask for JSON matching `schema`. Checks stop_reason before reading content.

    The system prompt carries a cache breakpoint: it is identical across calls, so every call after
    the first reads it from cache (verify with usage.cache_read_tokens > 0).
    """
    t0 = time.monotonic()
    try:
        resp = client.beta.messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            betas=[FALLBACK_BETA],
            fallbacks="default",
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
        )
    except FATAL:
        raise
    except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:  # retries already exhausted
        return LLMResult(error=f"{type(e).__name__}: {e}", latency_ms=int((time.monotonic() - t0) * 1000))

    u = resp.usage
    usage = {
        "input_tokens": u.input_tokens or 0,
        "output_tokens": u.output_tokens or 0,
        "cache_read_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
        "cache_write_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
    }
    result = LLMResult(
        stop_reason=resp.stop_reason,
        served_model=resp.model,
        request_id=getattr(resp, "_request_id", None),
        usage=usage,
        latency_ms=int((time.monotonic() - t0) * 1000),
        cost_usd=cost_usd(usage),
    )
    if resp.stop_reason == "refusal":
        result.refusal_category = getattr(getattr(resp, "stop_details", None), "category", None)
        return result
    if resp.stop_reason != "end_turn":
        return result  # max_tokens and friends: no trustworthy output
    text = next((b.text for b in resp.content if b.type == "text"), None)
    try:
        result.data = json.loads(text) if text else None
    except json.JSONDecodeError as e:
        result.error = f"invalid JSON: {e}"
    return result


def llm_call_row(run_id: str, purpose: str, subject: str, r: LLMResult, called_at) -> dict:
    return {
        "run_id": run_id, "purpose": purpose, "subject": subject, "model": r.served_model or MODEL,
        "request_id": r.request_id, "input_tokens": r.usage.get("input_tokens"),
        "output_tokens": r.usage.get("output_tokens"), "cache_read_tokens": r.usage.get("cache_read_tokens"),
        "cache_write_tokens": r.usage.get("cache_write_tokens"), "latency_ms": r.latency_ms,
        "cost_usd": r.cost_usd, "stop_reason": r.stop_reason, "error": r.error, "called_at": called_at,
    }
