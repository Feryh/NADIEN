"""LLM provider adapters: one chat call per provider kind, same input and output for all of them.

- ollama: local Ollama (`/api/chat`).
- openai: any OpenAI-compatible `/chat/completions` endpoint — OpenAI (ChatGPT), vLLM, LM Studio, OpenRouter,
  Groq, DeepSeek, Gemini's OpenAI endpoint, GitHub Models, ...
- azure_openai: Azure OpenAI deployments (the Microsoft / Copilot stack): `api-key` header, deployment in the URL.
- anthropic: Claude through the official `anthropic` SDK.
"""

import ipaddress
import time
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlparse

import httpx

ProviderKind = Literal["ollama", "openai", "azure_openai", "anthropic"]
KINDS: tuple[str, ...] = ("ollama", "openai", "azure_openai", "anthropic")
DEFAULT_BASE_URL = {"ollama": "http://localhost:11434", "openai": "https://api.openai.com/v1"}
AZURE_API_VERSION = "2024-10-21"
CLAUDE_DEFAULT_MODEL = "claude-opus-5-5"
CLAUDE_MAX_TOKENS = 16000
# Models that accept server-side refusal fallbacks (`fallbacks: "default"`) on the Claude API itself.
CLAUDE_FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
CLAUDE_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AiError(RuntimeError):
    pass


@dataclass
class ProviderConfig:
    kind: str
    model: str
    base_url: str | None = None
    api_key: str | None = None
    api_version: str | None = None
    extra_headers: dict[str, str] = field(default_factory=dict)
    temperature: float | None = None
    max_tokens: int | None = None
    timeout_s: int = 120


@dataclass
class ChatResult:
    text: str
    model: str
    duration_ms: int
    usage: dict[str, int] = field(default_factory=dict)


Message = dict[str, str]  # {"role": "user" | "assistant", "content": ...}


def is_external(kind: str, base_url: str | None) -> bool:
    """True when questions (and the network data in them) leave the internal network."""
    if kind == "anthropic" and not base_url:
        return True
    url = base_url or DEFAULT_BASE_URL.get(kind, "")
    host = (urlparse(url).hostname or "").lower()
    if host in ("localhost", "host.docker.internal") or host.endswith((".local", ".internal", ".lan")):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True  # a public DNS name
    return not (address.is_private or address.is_loopback)


def chat(config: ProviderConfig, system: str, messages: list[Message],
         transport: httpx.BaseTransport | None = None) -> ChatResult:
    started = time.monotonic()
    if config.kind == "anthropic":
        text, model, usage = _anthropic(config, system, messages)
    else:
        with httpx.Client(timeout=config.timeout_s, transport=transport) as client:
            text, model, usage = {"ollama": _ollama, "openai": _openai, "azure_openai": _azure}[config.kind](
                client, config, system, messages)
    if not text.strip():
        raise AiError("Model tidak mengembalikan jawaban.")
    return ChatResult(text=text, model=model, duration_ms=int((time.monotonic() - started) * 1000), usage=usage)


def _post(client: httpx.Client, url: str, body: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    try:
        resp = client.post(url, json=body, headers=headers)
    except httpx.TimeoutException as exc:
        raise AiError(f"Provider tidak menjawab dalam batas waktu ({url}).") from exc
    except httpx.HTTPError as exc:
        raise AiError(f"Provider tidak bisa dihubungi: {exc}") from exc
    if resp.status_code in (401, 403):
        raise AiError(f"Provider menolak kredensial (HTTP {resp.status_code}). Periksa API key.")
    if resp.status_code >= 400:
        raise AiError(f"Provider mengembalikan HTTP {resp.status_code}: {resp.text[:300]}")
    try:
        return resp.json()
    except ValueError as exc:
        raise AiError("Jawaban provider bukan JSON.") from exc


def _with_system(system: str, messages: list[Message]) -> list[Message]:
    return [{"role": "system", "content": system}, *messages]


def _ollama(client: httpx.Client, config: ProviderConfig, system: str, messages: list[Message]):
    options: dict[str, Any] = {}
    if config.temperature is not None:
        options["temperature"] = config.temperature
    if config.max_tokens:
        options["num_predict"] = config.max_tokens
    base = (config.base_url or DEFAULT_BASE_URL["ollama"]).rstrip("/")
    data = _post(client, f"{base}/api/chat", {"model": config.model, "messages": _with_system(system, messages),
                                              "stream": False, "options": options}, config.extra_headers)
    usage = {"input_tokens": data.get("prompt_eval_count") or 0, "output_tokens": data.get("eval_count") or 0}
    return (data.get("message") or {}).get("content") or "", data.get("model") or config.model, usage


def _openai_body(config: ProviderConfig, system: str, messages: list[Message]) -> dict[str, Any]:
    body: dict[str, Any] = {"model": config.model, "messages": _with_system(system, messages)}
    if config.temperature is not None:
        body["temperature"] = config.temperature
    if config.max_tokens:
        body["max_tokens"] = config.max_tokens
    return body


def _openai_result(data: dict[str, Any], model: str):
    choices = data.get("choices") or []
    text = ((choices[0].get("message") or {}).get("content") or "") if choices else ""
    usage = data.get("usage") or {}
    return text, data.get("model") or model, {"input_tokens": usage.get("prompt_tokens") or 0,
                                              "output_tokens": usage.get("completion_tokens") or 0}


def _openai(client: httpx.Client, config: ProviderConfig, system: str, messages: list[Message]):
    headers = dict(config.extra_headers)
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"
    base = (config.base_url or DEFAULT_BASE_URL["openai"]).rstrip("/")
    return _openai_result(_post(client, f"{base}/chat/completions", _openai_body(config, system, messages), headers),
                          config.model)


def _azure(client: httpx.Client, config: ProviderConfig, system: str, messages: list[Message]):
    if not config.base_url:
        raise AiError("Azure OpenAI butuh endpoint, mis. https://<resource>.openai.azure.com")
    headers = {**config.extra_headers, "api-key": config.api_key or ""}
    url = (f"{config.base_url.rstrip('/')}/openai/deployments/{config.model}/chat/completions"
           f"?api-version={config.api_version or AZURE_API_VERSION}")
    body = _openai_body(config, system, messages)
    body.pop("model")  # the deployment in the URL selects the model
    return _openai_result(_post(client, url, body, headers), config.model)


def _anthropic_client(config: ProviderConfig):
    import anthropic  # optional at import time: only needed when a Claude provider is used

    return anthropic.Anthropic(api_key=config.api_key, base_url=config.base_url or None,
                               timeout=float(config.timeout_s), max_retries=1,
                               default_headers=config.extra_headers or None)


def _anthropic(config: ProviderConfig, system: str, messages: list[Message]):
    import anthropic

    if not config.api_key:
        raise AiError("Provider Claude butuh API key Anthropic.")
    client = _anthropic_client(config)
    model = config.model or CLAUDE_DEFAULT_MODEL
    request: dict[str, Any] = {"model": model, "max_tokens": config.max_tokens or CLAUDE_MAX_TOKENS,
                               "system": system, "messages": messages}
    # A declined request is re-run server-side on the model Anthropic recommends for that refusal category;
    # only on the Claude API itself (not through a proxy base URL) and only for models that accept it.
    use_fallback = model in CLAUDE_FALLBACK_MODELS and not config.base_url
    try:
        if use_fallback:
            response = client.beta.messages.create(**request, betas=[CLAUDE_FALLBACK_BETA], fallbacks="default")
        else:
            response = client.messages.create(**request)
    except anthropic.AuthenticationError as exc:
        raise AiError("Anthropic menolak API key.") from exc
    except anthropic.PermissionDeniedError as exc:
        raise AiError("API key Anthropic tidak punya izin untuk model ini.") from exc
    except anthropic.NotFoundError as exc:
        raise AiError(f"Model {model} tidak dikenal oleh Anthropic.") from exc
    except anthropic.RateLimitError as exc:
        raise AiError("Batas pemakaian Anthropic tercapai; coba lagi nanti.") from exc
    except anthropic.APIStatusError as exc:
        raise AiError(f"Anthropic mengembalikan HTTP {exc.status_code}: {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise AiError(f"Anthropic tidak bisa dihubungi: {exc}") from exc
    if response.stop_reason == "refusal":
        raise AiError("Claude menolak menjawab pertanyaan ini.")
    text = "".join(block.text for block in response.content if block.type == "text")
    usage = {"input_tokens": response.usage.input_tokens or 0, "output_tokens": response.usage.output_tokens or 0}
    return text, response.model, usage
