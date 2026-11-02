import json
import os
from pathlib import Path
from urllib.parse import parse_qs

# Tests must never touch the developer's backend/.env targets: environment variables take
# precedence over .env, so pin an in-memory DB and blank every external credential before the
# app (and its settings) is imported.
os.environ.update({
    "NADIEN_DATABASE_URL": "sqlite://",
    "NADIEN_OBSERVIUM_URL": "http://observium.invalid",
    "NADIEN_OBSERVIUM_TOKEN": "",
    "NADIEN_OBSERVIUM_USER": "",
    "NADIEN_OBSERVIUM_PASSWORD": "",
    "NADIEN_RANCID_PATH": "",
    "NADIEN_RANCID_SSH_HOST": "",
    "NADIEN_RANCID_SSH_USER": "",
    "NADIEN_RANCID_SSH_PASSWORD": "",
    "NADIEN_OXIDIZED_URL": "",
})

import httpx  # noqa: E402
import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app import models  # noqa: E402,F401  (registers tables)
from app.database import Base  # noqa: E402

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


@pytest.fixture
def job_runner(db, tmp_path, monkeypatch):
    """Background jobs run against the test session, with their state file in tmp_path."""
    from app.collectors.runner import JobRunner
    from app.routers import collectors

    factory = sessionmaker(bind=db.get_bind(), autoflush=False, expire_on_commit=False)
    runner = JobRunner(session_factory=factory, state_file=tmp_path / "last-sync-job.json")
    monkeypatch.setattr(collectors, "runner", runner)
    return runner


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
