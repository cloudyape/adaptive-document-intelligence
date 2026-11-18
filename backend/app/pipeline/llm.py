"""Thin wrapper around the Anthropic SDK: structured outputs + cost accounting."""
from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TypeVar

import anthropic
from pydantic import BaseModel

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

# USD per 1M tokens (input, output). Keep in sync with Anthropic pricing.
PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-fable-5-2": (8.00, 40.00),
}
# Models that support the server-side refusal fallback parameter.
SERVER_FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-2"}


class LLMRefusal(RuntimeError):
    pass


@dataclass
class Usage:
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


@lru_cache
def client() -> anthropic.Anthropic:
    return anthropic.Anthropic(max_retries=3)


def available() -> bool:
    """True when some Anthropic credential source is configured."""
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    return (Path.home() / ".config" / "anthropic").exists()


def cost_of(model: str, input_tokens: int, output_tokens: int) -> float:
    pin, pout = PRICES.get(model, (4.0, 20.0))
    return round(input_tokens / 1e6 * pin + output_tokens / 1e6 * pout, 6)


def image_block(png: bytes) -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": base64.standard_b64encode(png).decode()}}


def pdf_block(pdf: bytes) -> dict:
    return {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": base64.standard_b64encode(pdf).decode()}}


def parse(
    *,
    model: str,
    system: str,
    content: list[dict],
    output_model: type[T],
    max_tokens: int = 16000,
    effort: str | None = None,
) -> tuple[T, Usage]:
    """One structured-output call. Raises on refusal / truncation / API errors."""
    kwargs: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": content}],
        "output_format": output_model,
    }
    if effort:
        kwargs["output_config"] = {"effort": effort}

    if model in SERVER_FALLBACK_MODELS:
        # Server-side refusal fallback (routes by refusal category; no model list to maintain).
        response = client().beta.messages.parse(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
    else:
        response = client().messages.parse(**kwargs)

    u = response.usage
    usage = Usage(
        model=response.model,
        input_tokens=(u.input_tokens or 0) + (getattr(u, "cache_read_input_tokens", 0) or 0),
        output_tokens=u.output_tokens or 0,
    )
    usage.cost_usd = cost_of(model, usage.input_tokens, usage.output_tokens)

    if response.stop_reason == "refusal":
        category = getattr(response.stop_details, "category", None) if response.stop_details else None
        raise LLMRefusal(f"model declined (category={category})")
    if response.stop_reason == "max_tokens":
        raise RuntimeError(f"output truncated at max_tokens={max_tokens}")
    if response.parsed_output is None:
        raise RuntimeError("model returned no parseable output")
    log.info("llm %s in=%d out=%d $%.5f req=%s", model, usage.input_tokens, usage.output_tokens, usage.cost_usd, response._request_id)
    return response.parsed_output, usage
