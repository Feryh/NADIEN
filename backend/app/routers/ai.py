from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import models as m
from ..config import settings
from ..database import get_db
from ..services.ai import context as ai_context
from ..services.ai.providers import KINDS, AiError, chat
from ..services.ai.registry import PRESETS, as_dict, default_provider, seed_local, to_config
from ..services.collector_settings import encrypt_secret

router = APIRouter(prefix="/ai", tags=["AI Assistant"])

SYSTEM_PROMPT = """Anda adalah asisten NADIEN untuk engineer jaringan ISP (IP/MPLS Metro, IPLC, L2VPN, L3VPN, DWDM).
Jawab dalam Bahasa Indonesia kecuali pengguna memakai bahasa lain.

Untuk fakta tentang jaringan ini (perangkat, link, POP, utilisasi, layanan, validasi), pakai hanya data di bagian
"Data jaringan NADIEN" pada pesan pengguna. Bila datanya tidak ada di sana, katakan begitu dan sebutkan menu NADIEN
yang bisa dipakai (Inventory, Topology, Capacity Planning, Customer Path, Service Validation, MoP Generator,
Config Generator, Dashboard). Jangan mengarang hostname, angka, atau status.

Untuk pertanyaan umum jaringan, jawab dari pengetahuan Anda dan sebutkan bila jawabannya bergantung pada vendor
atau versi software. Contoh konfigurasi ditulis per vendor (Huawei VRP, ZTE, Cisco IOS-XR, FiberHome) tanpa baris
komentar di dalam script, dan disimpan dengan commit."""

MAX_HISTORY = 12
MAX_MESSAGE_CHARS = 6000


class ProviderIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    kind: Literal["ollama", "openai", "azure_openai", "anthropic"]
    base_url: str | None = Field(default=None, max_length=255)
    model: str = Field(default="", max_length=128)
    api_key: str | None = Field(default=None, max_length=512)  # write-only; "" clears it
    api_version: str | None = Field(default=None, max_length=32)
    extra_headers: dict[str, str] | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=16, le=128000)
    timeout_s: int = Field(default=120, ge=5, le=900)
    enabled: bool = True
    is_default: bool = False


class ProviderPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    kind: Literal["ollama", "openai", "azure_openai", "anthropic"] | None = None
    base_url: str | None = Field(default=None, max_length=255)
    model: str | None = Field(default=None, max_length=128)
    api_key: str | None = Field(default=None, max_length=512)
    api_version: str | None = Field(default=None, max_length=32)
    extra_headers: dict[str, str] | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=16, le=128000)
    timeout_s: int | None = Field(default=None, ge=5, le=900)
    enabled: bool | None = None
    is_default: bool | None = None


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=20000)


class ChatIn(BaseModel):
    provider_id: int | None = None
    messages: list[ChatMessage] = Field(min_length=1, max_length=50)
    include_network_context: bool = True


def _editable() -> None:
    if not settings.settings_ui_enabled:
        raise HTTPException(403, "Pengaturan lewat UI dimatikan (NADIEN_SETTINGS_UI_ENABLED=false)")


def _provider(db: Session, provider_id: int) -> m.AiProvider:
    provider = db.get(m.AiProvider, provider_id)
    if provider is None:
        raise HTTPException(404, "Provider not found")
    return provider


def _apply(db: Session, provider: m.AiProvider, values: dict) -> None:
    api_key = values.pop("api_key", None)
    if api_key is not None:  # None = keep, "" = clear
        provider.api_key_encrypted = encrypt_secret(db, api_key) if api_key.strip() else None
    for key, value in values.items():
        setattr(provider, key, value)
    if provider.kind not in KINDS:
        raise HTTPException(422, f"kind harus salah satu dari {', '.join(KINDS)}")
    if not provider.model and provider.kind != "anthropic":
        raise HTTPException(422, "Model wajib diisi")
    if provider.is_default:
        db.execute(update(m.AiProvider).where(m.AiProvider.id != provider.id).values(is_default=False))


def _save(db: Session, provider: m.AiProvider) -> None:
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, f"Nama provider '{provider.name}' sudah dipakai") from exc


@router.get("/providers")
def list_providers(db: Session = Depends(get_db)):
    seed_local(db)
    rows = db.scalars(select(m.AiProvider).order_by(m.AiProvider.is_default.desc(), m.AiProvider.name)).all()
    return {"editable": settings.settings_ui_enabled, "providers": [as_dict(p) for p in rows], "presets": PRESETS}


@router.post("/providers", status_code=201)
def create_provider(payload: ProviderIn, db: Session = Depends(get_db)):
    _editable()
    provider = m.AiProvider(name=payload.name, kind=payload.kind, model=payload.model)
    db.add(provider)
    try:
        db.flush()  # an id first: making it the default clears the flag on every *other* provider
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, f"Nama provider '{payload.name}' sudah dipakai") from exc
    _apply(db, provider, payload.model_dump(exclude={"name", "kind", "model"}))
    _save(db, provider)
    return as_dict(provider)


@router.patch("/providers/{provider_id}")
def update_provider(provider_id: int, payload: ProviderPatch, db: Session = Depends(get_db)):
    _editable()
    provider = _provider(db, provider_id)
    _apply(db, provider, payload.model_dump(exclude_unset=True))
    _save(db, provider)
    return as_dict(provider)


@router.delete("/providers/{provider_id}", status_code=204)
def delete_provider(provider_id: int, db: Session = Depends(get_db)):
    _editable()
    db.delete(_provider(db, provider_id))
    db.commit()
    return Response(status_code=204)


@router.post("/providers/{provider_id}/test")
def test_provider(provider_id: int, db: Session = Depends(get_db)):
    """A one-line prompt, without network data, to check the endpoint, model and key."""
    provider = _provider(db, provider_id)
    try:
        result = chat(to_config(db, provider), "Jawab singkat.", [{"role": "user", "content": "Balas dengan kata: siap"}])
    except AiError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"ok": True, "reply": result.text[:200], "model": result.model, "duration_ms": result.duration_ms}


@router.post("/chat")
def chat_with_assistant(payload: ChatIn, db: Session = Depends(get_db)):
    """Answer the last user message with the provider's model; the network data relevant to that question is
    attached to it (not to earlier turns, which are sent as they were)."""
    seed_local(db)
    provider = _provider(db, payload.provider_id) if payload.provider_id else default_provider(db)
    if provider is None:
        raise HTTPException(404, "Belum ada provider AI yang aktif. Tambahkan di halaman AI Assistant.")
    if not provider.enabled:
        raise HTTPException(409, f"Provider {provider.name} sedang nonaktif")
    if payload.messages[-1].role != "user":
        raise HTTPException(422, "Pesan terakhir harus dari user")
    history = [{"role": msg.role, "content": msg.content[:MAX_MESSAGE_CHARS]} for msg in payload.messages[-MAX_HISTORY:]]
    question = payload.messages[-1].content
    network = ai_context.build(db, question) if payload.include_network_context else None
    if network:
        history[-1]["content"] = f"# Data jaringan NADIEN\n{network.text}\n# Pertanyaan\n{question}"
    try:
        result = chat(to_config(db, provider), SYSTEM_PROMPT, history)
    except AiError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"reply": result.text, "provider": as_dict(provider), "model": result.model,
            "duration_ms": result.duration_ms, "usage": result.usage,
            "context": {"sources": network.sources if network else [], "chars": len(network.text) if network else 0}}
