import httpx

from app.collectors.observium.client import ObserviumClient
from app.config import settings

ACTIVE = "http://observium-a.test"
STANDBY = "http://observium-b.test"


def _pair(down: set[str], seen: list[str]) -> httpx.MockTransport:
    """An active/standby pair: a host in `down` refuses connections, like a standby Observium."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        if f"http://{request.url.host}" in down:
            raise httpx.ConnectError("Connection refused", request=request)
        return httpx.Response(200, json={"status": "ok", "count": 1, "devices": {"1": {"device_id": "1"}}})

    return httpx.MockTransport(handler)


def _configured(monkeypatch, standby: str | None) -> None:
    monkeypatch.setattr(settings, "observium_url", ACTIVE)
    monkeypatch.setattr(settings, "observium_standby_url", standby)


def test_should_use_the_standby_when_the_main_observium_refuses_connections(monkeypatch):
    _configured(monkeypatch, STANDBY)
    with ObserviumClient(token="t", transport=_pair({ACTIVE}, [])) as client:
        assert (client.base_url, client.failed_over) == (STANDBY, True)
        assert client.fetch("devices") == [{"device_id": "1"}]


def test_should_stay_on_the_main_observium_when_it_answers(monkeypatch):
    _configured(monkeypatch, STANDBY)
    seen: list[str] = []
    with ObserviumClient(token="t", transport=_pair({STANDBY}, seen)) as client:
        client.fetch("devices")
    assert seen == ["observium-a.test", "observium-a.test"]  # the probe, then the call; the standby never asked


def test_should_not_probe_when_no_standby_is_configured(monkeypatch):
    _configured(monkeypatch, None)
    seen: list[str] = []
    with ObserviumClient(token="t", transport=_pair(set(), seen)) as client:
        client.fetch("devices")
    assert seen == ["observium-a.test"]


def test_should_keep_an_explicit_url_when_no_standby_is_given(monkeypatch):
    _configured(monkeypatch, STANDBY)
    with ObserviumClient(base_url="http://csr.test", token="t", transport=_pair({"http://csr.test"}, [])) as client:
        assert client.base_url == "http://csr.test" and client.failed_over is False
