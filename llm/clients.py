"""Клиент Chat Completions для обращения к модели через DigitalOcean или другой совместимый API."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from time import perf_counter
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DIGITALOCEAN_ENDPOINT = "https://inference.do-ai.run/v1/chat/completions"


class ModelTimeoutError(RuntimeError):
    """Истекло время ожидания ответа от API модели."""


class ModelClient(Protocol):
    def complete(self, *, model: str, messages: list[dict[str, Any]],
                 tools: list[dict[str, Any]], temperature: float) -> dict[str, Any]: ...


def _positive_int_from_env(name: str, default: int) -> int:
    value = int(os.environ.get(name, str(default)))
    if value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _parse_sse(response: Any, *, started: float | None = None) -> dict[str, Any]:
    """Собирает ответ модели из потоковых событий, включая вызовы инструментов.

    TTFT измеряется до первого фрагмента содержимого или вызова инструмента.
    Если API не передало usage, расход токенов остаётся неизвестным.
    """
    started = perf_counter() if started is None else started
    first_token = None
    chunks = []
    assistant_content = []
    tool_calls: dict[int, dict[str, Any]] = {}
    usage = None
    finish_reason = None
    saw_done = False
    for raw in response:
        line = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            saw_done = True
            break
        if not data:
            continue
        chunk = json.loads(data)
        if not isinstance(chunk, dict):
            raise ValueError("Malformed streaming chunk")
        if isinstance(chunk.get("usage"), dict):
            usage = chunk["usage"]
        choices = chunk.get("choices") or []
        if not choices:
            continue
        if len(choices) > 1:
            raise ValueError("Only one streaming choice is supported")
        choice = choices[0]
        if choice.get("finish_reason"):
            finish_reason = choice["finish_reason"]
        delta = choice.get("delta") or {}
        content = delta.get("content")
        if isinstance(content, str) and content:
            if first_token is None:
                first_token = perf_counter() - started
            assistant_content.append(content)
        for tool in delta.get("tool_calls") or []:
            if first_token is None:
                first_token = perf_counter() - started
            index = tool.get("index")
            if not isinstance(index, int) or index < 0:
                raise ValueError("Streaming tool delta has invalid index")
            current = tool_calls.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
            if tool.get("id"):
                # ID обычно приходит в первом фрагменте вызова инструмента.
                current["id"] += tool["id"]
            if tool.get("type"):
                current["type"] = tool["type"]
            function = tool.get("function") or {}
            current["function"]["name"] += function.get("name") or ""
            current["function"]["arguments"] += function.get("arguments") or ""
    if not saw_done:
        raise ValueError("Incomplete model stream: missing [DONE]")
    message: dict[str, Any] = {"role": "assistant", "content": "".join(assistant_content) or None}
    if tool_calls:
        message["tool_calls"] = [tool_calls[k] for k in sorted(tool_calls)]
    reply: dict[str, Any] = {"choices": [{"message": message, "finish_reason": finish_reason}]}
    if usage is not None:
        reply["usage"] = usage
    reply["_restoka_metrics"] = {"ttft_seconds": first_token, "streamed": True}
    return reply


@dataclass(frozen=True)
class ChatCompletionsClient:
    """Настройки подключения к совместимому с Chat Completions API.

    Адрес берётся только из конфигурации, не из сообщений модели или поставщика.
    Лимит ответа не связан с плановой оценкой количества токенов.
    """
    api_key: str
    endpoint: str = DIGITALOCEAN_ENDPOINT
    timeout_seconds: float = 30.0
    max_completion_tokens: int = 1024
    stream: bool = False

    def __post_init__(self):
        if not self.api_key or not self.api_key.strip():
            raise ValueError("Model API key is required")
        if not self.endpoint.startswith("https://") and not self.endpoint.startswith("http://127.0.0.1:"):
            raise ValueError("Model endpoint must use HTTPS (or loopback HTTP for testing)")
        if self.timeout_seconds <= 0 or self.max_completion_tokens < 1:
            raise ValueError("Model timeout and max_completion_tokens must be positive")

    @classmethod
    def from_env(cls) -> "ChatCompletionsClient":
        key = os.environ.get("RESTOKA_LLM_API_KEY") or os.environ.get("DIGITALOCEAN_TOKEN")
        if not key:
            raise RuntimeError("Set RESTOKA_LLM_API_KEY or DIGITALOCEAN_TOKEN in your local environment")
        return cls(
            api_key=key,
            endpoint=os.environ.get("RESTOKA_LLM_ENDPOINT", DIGITALOCEAN_ENDPOINT),
            timeout_seconds=float(os.environ.get("RESTOKA_LLM_TIMEOUT_SECONDS", "30")),
            max_completion_tokens=_positive_int_from_env("RESTOKA_MAX_COMPLETION_TOKENS", 1024),
            stream=os.environ.get("RESTOKA_LLM_STREAM", "0").lower() in ("1", "true", "yes"),
        )

    def complete(self, *, model: str, messages: list[dict[str, Any]],
                 tools: list[dict[str, Any]], temperature: float) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model, "messages": messages, "temperature": temperature,
            "max_completion_tokens": self.max_completion_tokens,
        }
        # При повторной проверке ответа инструменты недоступны. Некоторые
        # совместимые API отклоняют tool_choice при пустом списке tools.
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if self.stream:
            payload.update(stream=True, stream_options={"include_usage": True})
        request = Request(
            self.endpoint, data=json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        started = perf_counter()
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                answer = _parse_sse(response, started=started) if self.stream else json.load(response)
        except HTTPError as exc:
            # Показываем только HTTP-статус, не тело ответа с возможными секретами.
            raise RuntimeError(f"Model provider HTTP status {exc.code}") from None
        except (TimeoutError,) as exc:
            raise ModelTimeoutError("Model provider network timeout") from None
        except URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise ModelTimeoutError("Model provider network timeout") from None
            raise RuntimeError("Model provider network error") from None
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"Model provider request failed ({type(exc).__name__})") from None
        if not isinstance(answer, dict) or not answer.get("choices"):
            raise ValueError("Model provider returned no choices")
        return answer
