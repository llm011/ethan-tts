"""OpenAI-compatible streaming chat client.

Any endpoint that speaks ``POST /chat/completions`` with SSE ``data:`` frames
works: DeepSeek, OpenAI, 智谱, 通义, Kimi, Ollama and LM Studio's local server.
The connection is a plain ``httpx`` stream rather than a vendor SDK, so a new
provider only needs a base URL, a key and a model name from the settings page.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable, Iterator
from urllib.parse import urlparse

import httpx

REQUEST_TIMEOUT = httpx.Timeout(connect=10.0, read=90.0, write=30.0, pool=10.0)


class LLMError(RuntimeError):
    """A provider error with a message already suitable for the UI."""


@dataclass
class ChatConfig:
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    temperature: float = .7
    max_tokens: int = 320
    extra_headers: dict[str, str] = field(default_factory=dict)
    # 默认直连：本机开着系统代理时，httpx 会把请求也塞进代理，直连 API 的
    # 国内服务（DeepSeek、智谱……）常被重置（Errno 54）。要让模型走代理
    # 时再显式打开，并把代理地址填在下面。
    use_proxy: bool = False
    proxy_url: str = ""

    def generate(self, messages: list[dict],
                 cancel: Callable[[], bool] | None = None) -> Iterator[str]:
        """Instance shortcut; ``configured`` is validated inside ``stream_chat``."""
        return stream_chat(self, messages, cancel=cancel)

    @property
    def configured(self) -> bool:
        return bool(self.base_url.strip() and self.model.strip())

    def endpoint(self) -> str:
        base = self.base_url.strip().rstrip("/")
        if not base:
            raise LLMError("还没有配置模型地址")
        # Accept both "https://host/v1" and "https://host" (typical for Ollama).
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"

    def masked_key(self) -> str:
        if not self.api_key:
            return ""
        key = self.api_key.strip()
        return f"{key[:4]}…{key[-4:]}" if len(key) > 10 else "已设置"


def normalize_base_url(value: str) -> str:
    value = (value or "").strip().rstrip("/")
    if not value:
        return ""
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("模型地址需要以 http:// 或 https:// 开头")
    return value


def parse_sse_line(line: str) -> str | None:
    """Extract the text delta from one SSE line, or None if it carries none."""
    line = line.strip()
    if not line.startswith("data:"):
        return None
    payload = line[5:].strip()
    if payload in {"", "[DONE]"}:
        return None
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return None
    # Some gateways wrap errors inside a 200 SSE stream.
    if isinstance(data, dict) and data.get("error"):
        error = data["error"]
        message = error.get("message") if isinstance(error, dict) else str(error)
        raise LLMError(str(message or "模型返回了错误"))
    choices = data.get("choices") or []
    if not choices:
        return None
    delta = choices[0].get("delta") or choices[0].get("message") or {}
    content = delta.get("content")
    if isinstance(content, list):  # content parts style
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return content or None


def stream_chat(config: ChatConfig, messages: list[dict],
                cancel: Callable[[], bool] | None = None) -> Iterator[str]:
    """Yield text deltas as the provider produces them.

    ``cancel`` is polled between chunks so a user pressing stop ends the HTTP
    request promptly instead of waiting for the full completion.
    """
    if not config.configured:
        raise LLMError("还没有配置模型，请先到设置页填写模型地址和名称")
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    if config.api_key.strip():
        headers["Authorization"] = f"Bearer {config.api_key.strip()}"
    headers.update(config.extra_headers or {})
    body = {
        "model": config.model.strip(),
        "messages": messages,
        "stream": True,
        "temperature": config.temperature,
        "max_tokens": config.max_tokens,
    }
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT, follow_redirects=True,
                          trust_env=config.use_proxy,
                          proxy=config.proxy_url.strip() or None) as client:
            with client.stream("POST", config.endpoint(), headers=headers, json=body) as response:
                if response.status_code >= 400:
                    detail = "".join(response.iter_text())[:400]
                    raise LLMError(_friendly_error(response.status_code, detail))
                for line in response.iter_lines():
                    if cancel and cancel():
                        return
                    delta = parse_sse_line(line)
                    if delta:
                        yield delta
    except httpx.TimeoutException as exc:
        raise LLMError("模型响应超时，请检查网络或模型服务是否在线") from exc
    except httpx.HTTPError as exc:
        raise LLMError(f"无法连接模型服务：{exc}") from exc


def _friendly_error(status: int, detail: str) -> str:
    detail = detail.strip()
    if status in {401, 403}:
        return "API Key 无效或没有权限，请在设置页检查"
    if status == 404:
        return "模型地址不存在，请确认 Base URL 是否包含 /v1"
    if status == 429:
        return "请求太频繁或额度不足，请稍后再试"
    return f"模型服务返回 {status}：{detail[:200] or '无详细信息'}"


def list_models(config: ChatConfig) -> list[str]:
    """Best-effort model discovery for the settings page dropdown."""
    if not config.base_url.strip():
        return []
    base = config.base_url.strip().rstrip("/")
    if base.endswith("/chat/completions"):
        base = base[: -len("/chat/completions")]
    headers = {}
    if config.api_key.strip():
        headers["Authorization"] = f"Bearer {config.api_key.strip()}"
    with httpx.Client(timeout=httpx.Timeout(10.0), trust_env=config.use_proxy,
                      proxy=config.proxy_url.strip() or None) as client:
        response = client.get(f"{base}/models", headers=headers)
        response.raise_for_status()
        data = response.json()
    items = data.get("data") if isinstance(data, dict) else data
    return sorted({item.get("id") for item in items or [] if isinstance(item, dict) and item.get("id")})
