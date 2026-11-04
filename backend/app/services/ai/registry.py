"""Stored AI providers: conversion to adapter config, API-safe view, presets and the local default."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import models as m
from ...config import settings
from ..collector_settings import decrypt_secret
from .providers import AZURE_API_VERSION, CLAUDE_DEFAULT_MODEL, DEFAULT_BASE_URL, ProviderConfig, is_external

# Starting points for the "add provider" form. Model names of hosted services change often: only the local
# ones and Claude carry a default, the rest show an example to replace.
PRESETS: list[dict[str, Any]] = [
    {"key": "ollama", "label": "Ollama (lokal)", "kind": "ollama", "base_url": DEFAULT_BASE_URL["ollama"],
     "model": settings.ai_default_model, "needs_key": False},
    {"key": "vllm", "label": "vLLM / LM Studio (lokal, OpenAI-compatible)", "kind": "openai",
     "base_url": "http://localhost:8001/v1", "model": "", "model_hint": "nama model yang di-serve, mis. Qwen/Qwen2.5-7B-Instruct",
     "needs_key": False},
    {"key": "claude", "label": "Claude (Anthropic)", "kind": "anthropic", "base_url": "", "model": CLAUDE_DEFAULT_MODEL,
     "needs_key": True},
    {"key": "openai", "label": "ChatGPT (OpenAI)", "kind": "openai", "base_url": DEFAULT_BASE_URL["openai"], "model": "",
     "model_hint": "model OpenAI yang aktif di akun Anda", "needs_key": True},
    {"key": "azure", "label": "Azure OpenAI (Microsoft / Copilot stack)", "kind": "azure_openai",
     "base_url": "https://<resource>.openai.azure.com", "model": "", "model_hint": "nama deployment di Azure",
     "api_version": AZURE_API_VERSION, "needs_key": True},
    {"key": "gemini", "label": "Gemini (endpoint OpenAI-compatible)", "kind": "openai",
     "base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "model": "",
     "model_hint": "model Gemini yang aktif di akun Anda", "needs_key": True},
    {"key": "custom", "label": "OpenAI-compatible lain (OpenRouter, Groq, DeepSeek, GitHub Models, …)",
     "kind": "openai", "base_url": "", "model": "", "model_hint": "lihat dokumentasi penyedia", "needs_key": True},
]


def to_config(db: Session, provider: m.AiProvider) -> ProviderConfig:
    api_key = decrypt_secret(db, provider.api_key_encrypted) if provider.api_key_encrypted else None
    return ProviderConfig(kind=provider.kind, model=provider.model, base_url=provider.base_url or None,
                          api_key=api_key, api_version=provider.api_version,
                          extra_headers=dict(provider.extra_headers or {}), temperature=provider.temperature,
                          max_tokens=provider.max_tokens, timeout_s=provider.timeout_s)


def as_dict(provider: m.AiProvider) -> dict[str, Any]:
    return {"id": provider.id, "name": provider.name, "kind": provider.kind, "base_url": provider.base_url,
            "model": provider.model, "api_version": provider.api_version, "has_api_key": bool(provider.api_key_encrypted),
            "extra_headers": provider.extra_headers or {}, "temperature": provider.temperature,
            "max_tokens": provider.max_tokens, "timeout_s": provider.timeout_s, "enabled": provider.enabled,
            "is_default": provider.is_default, "external": is_external(provider.kind, provider.base_url)}


def default_provider(db: Session) -> m.AiProvider | None:
    enabled = db.scalars(select(m.AiProvider).where(m.AiProvider.enabled.is_(True))
                         .order_by(m.AiProvider.is_default.desc(), m.AiProvider.id)).all()
    return enabled[0] if enabled else None


def seed_local(db: Session) -> None:
    """First start: a local Ollama provider as the default, so the assistant works without any hosted service."""
    if not settings.ai_seed_local or db.scalar(select(m.AiProvider.id).limit(1)) is not None:
        return
    db.add(m.AiProvider(name="Ollama lokal", kind="ollama", base_url=settings.ai_ollama_url,
                        model=settings.ai_default_model, is_default=True, enabled=True, timeout_s=180))
    db.commit()
