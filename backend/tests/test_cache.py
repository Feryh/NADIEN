"""Read-model cache: single flight, and invalidation that reaches every API process."""

import pytest
from sqlalchemy import update
from sqlalchemy.orm import sessionmaker

from app import database
from app import models as m
from app.config import settings
from app.services import cache


@pytest.fixture
def shared_db(db, monkeypatch):
    factory = sessionmaker(bind=db.get_bind(), autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(settings, "cache_ttl_s", 60)
    monkeypatch.setattr(cache, "_seen_version", None)
    monkeypatch.setattr(cache, "_checked_at", 0.0)
    cache._drop_local()
    return db


def test_should_serve_cached_value_when_nothing_changed(shared_db):
    calls = []
    assert cache.cached("k", lambda: calls.append(1) or "v1") == "v1"
    assert cache.cached("k", lambda: calls.append(1) or "v2") == "v1"
    assert calls == [1]


def test_should_recompute_when_another_process_wrote(shared_db, monkeypatch):
    cache.clear()  # this process wrote once: version 1 seen
    assert cache.cached("k", lambda: "old") == "old"
    # Another worker/replica handles a write: it bumps the shared version in the database.
    shared_db.execute(update(m.AppState).where(m.AppState.key == cache.VERSION_KEY).values(value=m.AppState.value + 1))
    shared_db.commit()
    monkeypatch.setattr(cache, "_checked_at", 0.0)  # next request is past the 1 s check interval
    assert cache.cached("k", lambda: "new") == "new"


def test_should_drop_own_cache_immediately_when_this_process_writes(shared_db):
    assert cache.cached("k", lambda: "old") == "old"
    cache.clear()
    assert cache.cached("k", lambda: "new") == "new"
