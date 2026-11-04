"""AI assistant (Phase 9): provider adapters, network context, provider registry and chat API."""

import json
from types import SimpleNamespace

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app import models as m
from app.config import settings
from app.database import get_db
from app.main import app
from app.routers import ai as ai_router
from app.services.ai import context, providers
from app.services.ai.providers import AiError, ChatResult, ProviderConfig, chat, is_external


def _transport(seen: list[httpx.Request], reply: dict, status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=reply)

    return httpx.MockTransport(handler)


MESSAGES = [{"role": "user", "content": "halo"}]


def test_should_send_system_first_and_read_message_when_provider_is_ollama():
    seen: list[httpx.Request] = []
    result = chat(ProviderConfig(kind="ollama", model="llama3.1:8b"), "SYS", MESSAGES,
                  _transport(seen, {"message": {"content": "siap"}, "model": "llama3.1:8b"}))
    body = json.loads(seen[0].content)
    assert str(seen[0].url) == "http://localhost:11434/api/chat"
    assert body["messages"][0] == {"role": "system", "content": "SYS"} and result.text == "siap"


def test_should_send_bearer_key_when_provider_is_openai_compatible():
    seen: list[httpx.Request] = []
    chat(ProviderConfig(kind="openai", model="m", base_url="http://vllm:8001/v1", api_key="k"), "S", MESSAGES,
         _transport(seen, {"choices": [{"message": {"content": "ok"}}]}))
    assert str(seen[0].url) == "http://vllm:8001/v1/chat/completions"
    assert seen[0].headers["authorization"] == "Bearer k"


def test_should_put_deployment_in_url_when_provider_is_azure():
    seen: list[httpx.Request] = []
    chat(ProviderConfig(kind="azure_openai", model="gpt-dep", base_url="https://x.openai.azure.com", api_key="k"), "S",
         MESSAGES, _transport(seen, {"choices": [{"message": {"content": "ok"}}]}))
    assert seen[0].url.path == "/openai/deployments/gpt-dep/chat/completions" and seen[0].headers["api-key"] == "k"
    assert "model" not in json.loads(seen[0].content)


def test_should_raise_readable_error_when_provider_rejects_key():
    with pytest.raises(AiError, match="kredensial"):
        chat(ProviderConfig(kind="openai", model="m", api_key="bad"), "S", MESSAGES, _transport([], {}, status=401))


class _FakeClaude:
    def __init__(self, stop_reason: str = "end_turn"):
        self.calls: list[dict] = []
        response = SimpleNamespace(stop_reason=stop_reason, model="claude-opus-5-5",
                                   content=[SimpleNamespace(type="text", text="jawaban")],
                                   usage=SimpleNamespace(input_tokens=10, output_tokens=3))

        def create(**kwargs):
            self.calls.append(kwargs)
            return response

        self.messages = SimpleNamespace(create=create)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=create))


def test_should_ask_for_server_side_fallback_when_claude_model_supports_it(monkeypatch):
    fake = _FakeClaude()
    monkeypatch.setattr(providers, "_anthropic_client", lambda config: fake)
    result = chat(ProviderConfig(kind="anthropic", model="claude-opus-5-5", api_key="k"), "SYS", MESSAGES)
    assert fake.calls[0]["fallbacks"] == "default" and fake.calls[0]["system"] == "SYS"
    assert result.text == "jawaban"


def test_should_report_refusal_when_claude_declines(monkeypatch):
    monkeypatch.setattr(providers, "_anthropic_client", lambda config: _FakeClaude(stop_reason="refusal"))
    with pytest.raises(AiError, match="menolak"):
        chat(ProviderConfig(kind="anthropic", model="claude-opus-5-5", api_key="k"), "SYS", MESSAGES)


@pytest.mark.parametrize("kind, base_url, expected", [
    ("ollama", "http://localhost:11434", False),
    ("openai", "http://10.20.0.5:8001/v1", False),
    ("openai", "https://api.openai.com/v1", True),
    ("anthropic", None, True),
])
def test_should_flag_providers_outside_the_network_as_external(kind, base_url, expected):
    assert is_external(kind, base_url) is expected


@pytest.fixture
def net(db):
    pop = m.Pop(code="JKT", name="Jakarta", region="Jabodetabek")
    db.add(pop)
    db.flush()
    a = m.Device(hostname="JKT-NDC-PE1", pop_id=pop.id, role="pe", loopback_ipv4="10.0.0.1")
    b = m.Device(hostname="JKT-NDC-PE2", pop_id=pop.id, role="pe")
    db.add_all([a, b])
    db.flush()
    db.add(m.Link(name="PE1-PE2", a_device_id=a.id, b_device_id=b.id, capacity_gbps=10, current_gbps=9.5))
    db.commit()


def test_should_attach_named_device_and_hotspots_when_question_mentions_them(db, net):
    ctx = context.build(db, "Kenapa link jkt-ndc-pe1 padat?")
    assert ctx.sources == ["Ringkasan jaringan", "Device JKT-NDC-PE1", "Hotspot link"]
    assert "ke JKT-NDC-PE2: 10G" in ctx.text


def test_should_stop_adding_sections_when_budget_is_used(db, net):
    ctx = context.build(db, "jkt-ndc-pe1 padat?", budget=400)
    assert len(ctx.text) <= 400 and ctx.sources[0] == "Ringkasan jaringan"


@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setattr(settings, "secret_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "settings_ui_enabled", True)
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_should_seed_local_ollama_as_default_when_no_provider_exists(client):
    providers_ = client.get("/api/ai/providers").json()["providers"]
    assert [(p["kind"], p["is_default"], p["external"]) for p in providers_] == [("ollama", True, False)]


def test_should_store_key_encrypted_and_move_default_when_provider_is_added(client, db):
    client.get("/api/ai/providers")
    res = client.post("/api/ai/providers", json={"name": "Claude", "kind": "anthropic", "model": "claude-opus-5-5",
                                                 "api_key": "sk-ant-secret", "is_default": True})
    assert res.status_code == 201 and res.json()["has_api_key"] is True and "sk-ant" not in res.text
    stored = db.query(m.AiProvider).filter_by(name="Claude").one()
    assert "sk-ant" not in stored.api_key_encrypted
    assert [p["name"] for p in client.get("/api/ai/providers").json()["providers"] if p["is_default"]] == ["Claude"]


def test_should_attach_network_data_to_last_question_when_chatting(client, db, net, monkeypatch):
    sent: list[list[dict]] = []

    def fake_chat(config, system, messages, transport=None):
        sent.append(messages)
        return ChatResult(text="Link penuh.", model=config.model, duration_ms=5)

    monkeypatch.setattr(ai_router, "chat", fake_chat)
    body = client.post("/api/ai/chat", json={"messages": [{"role": "user", "content": "status JKT-NDC-PE1?"}]}).json()
    assert body["reply"] == "Link penuh." and "Device JKT-NDC-PE1" in body["context"]["sources"]
    assert sent[0][-1]["content"].startswith("# Data jaringan NADIEN")


def test_should_return_502_when_provider_fails(client, monkeypatch):
    def failing(config, system, messages, transport=None):
        raise AiError("Provider tidak bisa dihubungi")

    monkeypatch.setattr(ai_router, "chat", failing)
    res = client.post("/api/ai/chat", json={"messages": [{"role": "user", "content": "halo"}]})
    assert res.status_code == 502 and "tidak bisa dihubungi" in res.json()["detail"]


def test_should_limit_hotspots_to_named_area_when_question_names_one(db, net):
    sby = m.Pop(code="SBY", name="Surabaya", region="Jawa Timur")
    db.add(sby)
    db.flush()
    c = m.Device(hostname="SBY-PE1", pop_id=sby.id, role="pe")
    d = m.Device(hostname="SBY-PE2", pop_id=sby.id, role="pe")
    db.add_all([c, d])
    db.flush()
    db.add(m.Link(name="SBY", a_device_id=c.id, b_device_id=d.id, capacity_gbps=10, current_gbps=9.8))
    db.commit()
    ctx = context.build(db, "link kritis di Jawa Timur?")
    assert "Hotspot link (Jawa Timur)" in ctx.sources and "SBY-PE1" in ctx.text and "JKT-NDC-PE1" not in ctx.text
