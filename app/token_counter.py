from __future__ import annotations

import logging
from typing import Any

from litellm import encode, token_counter

logger = logging.getLogger(__name__)


def estimate_message_tokens(payload: list[dict[str, Any]], model: str | None = None) -> int:
    """
    Estimate prompt tokens using LiteLLM's cross-provider tokenizer abstraction.

    This keeps pre-send counts aligned with the same model naming layer the app
    already uses for routing, while still giving us a fallback if a provider
    tokenizer is unavailable.
    """
    try:
        count = token_counter(model=model or "", messages=_normalize_messages(payload))
        return max(int(count), 1)
    except Exception as exc:
        logger.warning("LiteLLM token count failed for model %s: %s", model, exc)
        return max(_fallback_estimate(payload), 1)


def estimate_text_tokens(text: str, model: str | None = None) -> int:
    try:
        return max(len(encode(model=model or "", text=text or "")), 1)
    except Exception as exc:
        logger.warning("LiteLLM text token count failed for model %s: %s", model, exc)
        return max((len(text or "") // 4) + 1, 1)


def _normalize_messages(payload: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for msg in payload:
        normalized.append(
            {
                "role": str(msg.get("role", "user")),
                "content": _normalize_content(msg.get("content")),
            }
        )
    return normalized


def _normalize_content(content: Any) -> Any:
    if isinstance(content, list):
        parts: list[dict[str, Any]] = []
        for item in content:
            if not isinstance(item, dict):
                parts.append({"type": "text", "text": str(item)})
                continue

            part_type = item.get("type")
            if part_type == "text":
                parts.append({"type": "text", "text": str(item.get("text", ""))})
            elif part_type == "image_url":
                image = item.get("image_url") or {}
                url = image.get("url", "") if isinstance(image, dict) else str(image)
                parts.append({"type": "text", "text": f"[image:{url[:64]}]"})
            else:
                parts.append({"type": "text", "text": str(item)})
        return parts
    return "" if content is None else str(content)


def _fallback_estimate(payload: list[dict[str, Any]]) -> int:
    total_chars = 0
    for msg in payload:
        total_chars += len(str(msg.get("role", "")))
        content = msg.get("content")
        if isinstance(content, list):
            for item in content:
                total_chars += len(str(item))
        else:
            total_chars += len("" if content is None else str(content))
    return (total_chars // 4) + 8
