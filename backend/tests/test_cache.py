"""Read-model cache: single flight, and invalidation that reaches every API process."""

import threading
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.orm import sessionmaker

from app import database
from app import models as m
from app.config import settings
from app.database import get_db
from app.main import app
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


def _expire(key, past_stale: bool = False):
    """Age an entry instead of sleeping through its TTL."""
    _, generation, value = cache._entries[key]
    age = settings.cache_ttl_s * (cache.STALE_FOR_TTLS + 1) if past_stale else 1
    cache._entries[key] = (time.monotonic() - age, generation, value)


def test_should_recompute_when_entry_expired_and_nobody_else_is_refreshing(shared_db):
    cache.cached("k", lambda: "old")
    _expire("k")
    assert cache.cached("k", lambda: "new") == "new"


def test_should_serve_old_value_when_another_request_is_refreshing(shared_db):
    cache.cached("k", lambda: "old")
    _expire("k")
    refreshing, release = threading.Event(), threading.Event()

    def slow_refresh():
        refreshing.set()
        release.wait(5)
        return "new"

    refresher = threading.Thread(target=cache.cached, args=("k", slow_refresh))
    refresher.start()
    refreshing.wait(5)
    try:
        assert cache.cached("k", lambda: pytest.fail("a second request must not compute")) == "old"
    finally:
        release.set()
        refresher.join(5)
    assert cache.cached("k", lambda: pytest.fail("the refreshed value must be cached")) == "new"


def test_should_not_serve_old_value_when_entry_is_far_past_its_ttl(shared_db):
    cache.cached("k", lambda: "old")
    _expire("k", past_stale=True)
    assert cache.cached("k", lambda: "new") == "new"


def test_should_keep_at_most_max_entries_when_keys_keep_changing(shared_db, monkeypatch):
    monkeypatch.setattr(cache, "MAX_ENTRIES", 3)
    for key in range(10):
        cache.cached(key, lambda: "v")
    assert len(cache._entries) == 3
    assert 9 in cache._entries


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    yield TestClient(app)
    app.dependency_overrides.pop(get_db, None)


def test_should_keep_cache_when_a_post_changes_no_data(client, monkeypatch):
    cleared = []
    monkeypatch.setattr(cache, "clear", lambda: cleared.append(1))
    payload = {"capacity_gbps": 100, "current_gbps": 10}
    assert client.post("/api/capacity/calculate", json=payload).status_code == 200
    assert cleared == []


def test_should_drop_cache_when_a_write_succeeds(client, monkeypatch):
    cleared = []
    monkeypatch.setattr(cache, "clear", lambda: cleared.append(1))
    assert client.put("/api/topology/positions", json=[]).status_code == 204
    assert cleared == [1]
