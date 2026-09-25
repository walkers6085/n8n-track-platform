"""Minimal client for OpenAI-compatible /chat/completions with tool calling (Mistral, OpenAI,
vLLM, Ollama...). Kept tiny on purpose so tests can swap `chat` for a fake."""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import httpx

from backend.models import AgentSettings


RETRIES = 3


class LLMError(Exception):
    pass


def chat(cfg: AgentSettings, messages: List[Dict[str, Any]],
         tools: Optional[List[Dict[str, Any]]] = None, model: str = "",
         json_mode: bool = False) -> Dict[str, Any]:
    """Return the assistant message dict: {"content": str|None, "tool_calls": [...]}."""
    if not cfg.api_key:
        raise LLMError("не задан API-ключ агента (Настройки → Агент)")
    body: Dict[str, Any] = {"model": model or cfg.model, "messages": messages,
                            "temperature": cfg.temperature, "max_tokens": cfg.max_tokens}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    body.update(cfg.extra_body)  # provider-specific knobs, e.g. {"thinking": {"type": "disabled"}}
    for attempt in range(RETRIES + 1):
        try:
            r = httpx.post(f"{cfg.base_url.rstrip('/')}/chat/completions", json=body,
                           headers={"Authorization": f"Bearer {cfg.api_key}"},
                           timeout=cfg.timeout_s)
        except httpx.TimeoutException as e:
            raise LLMError(f"модель не ответила за {cfg.timeout_s} с") from e
        except httpx.TransportError as e:  # dropped connections happen with some providers
            if attempt == RETRIES:
                raise LLMError(f"модель недоступна: {e}") from e
            time.sleep(2 ** attempt)
            continue
        if r.status_code not in (429, 500, 502, 503, 504) or attempt == RETRIES:
            break
        time.sleep(2 ** attempt * 1.5)
    if r.status_code >= 400:
        raise LLMError(f"модель вернула {r.status_code}: {r.text[:300]}")
    return r.json()["choices"][0]["message"]
