import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models  # noqa: F401  (registers tables)
from app.database import Base

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def config_text(name: str) -> str:
    return (FIXTURES / "configs" / name).read_text(encoding="utf-8")


def observium_transport(port_rates: dict[str, tuple[int, int]] | None = None, status: int = 200) -> httpx.MockTransport:
    """Serve tests/fixtures/observium/*.json the way /api/v0 does (keyed by id, paginated)."""
    data = json.loads((FIXTURES / "observium" / "api.json").read_text(encoding="utf-8"))
    if port_rates:
        for port_id, (rx, tx) in port_rates.items():
            data["ports"][port_id]["ifInOctets_rate"] = str(rx)
            data["ports"][port_id]["ifOutOctets_rate"] = str(tx)

    def handler(request: httpx.Request) -> httpx.Response:
        if status != 200:
            return httpx.Response(status, json={"status": "failed", "message": "denied"})
        entity = request.url.path.removeprefix("/api/v0/").strip("/")
        key = "addresses" if entity == "address" else entity
        rows = data.get(key, {})
        query = parse_qs(request.url.query.decode())
        items = list(rows.items())
        if "pagesize" in query:
            size, page = int(query["pagesize"][0]), int(query.get("pageno", ["1"])[0])
            items = items[(page - 1) * size : page * size]
        return httpx.Response(200, json={"status": "ok", "count": len(rows), key: dict(items)})

    return httpx.MockTransport(handler)
