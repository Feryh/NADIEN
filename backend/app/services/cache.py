"""Short-lived cache for heavy read models (topology views, capacity lists, dashboard).

Those views aggregate thousands of devices and links per request; with tens of users looking at
the same pages, recomputing each one per request is wasted work. Entries live `NADIEN_CACHE_TTL_S`
seconds. Every successful write request and every finished sync bumps a version counter in the
database; each API process (uvicorn worker or replica) checks it at most once per second and drops
its cache when it moved, so an edit shows up everywhere within about a second. Concurrent misses on
the same key compute once (single flight) instead of stampeding the database.
"""

import threading
import time
from collections.abc import Callable, Hashable
from typing import TypeVar

from sqlalchemy import update
from sqlalchemy.exc import SQLAlchemyError

from ..config import settings

T = TypeVar("T")

VERSION_KEY = "cache_version"
CHECK_EVERY_S = 1.0

_lock = threading.Lock()
_entries: dict[Hashable, tuple[float, int, object]] = {}
_key_locks: dict[Hashable, threading.Lock] = {}
_generation = 0
_seen_version: int | None = None
_checked_at = 0.0


def _drop_local() -> None:
    global _generation
    with _lock:
        _generation += 1
        _entries.clear()


def _shared_version(bump: bool = False) -> int | None:
    """Read (or increment) the shared version; None when the database is unavailable."""
    from .. import models as m  # late: models import the database layer
    from ..database import SessionLocal

    try:
        with SessionLocal() as db:
            if bump and not db.execute(update(m.AppState).where(m.AppState.key == VERSION_KEY)
                                       .values(value=m.AppState.value + 1)).rowcount:
                db.add(m.AppState(key=VERSION_KEY, value=1))
            row = db.get(m.AppState, VERSION_KEY)
            value = row.value if row else 0
            db.commit()
            return value
    except SQLAlchemyError:
        return None


def clear() -> None:
    """Drop this process's cache and tell the other processes to drop theirs."""
    global _seen_version
    _drop_local()
    if settings.cache_ttl_s > 0:
        _seen_version = _shared_version(bump=True)


def _follow_other_processes() -> None:
    global _seen_version, _checked_at
    now = time.monotonic()
    if now - _checked_at < CHECK_EVERY_S:
        return
    _checked_at = now
    version = _shared_version()
    if version is not None and version != _seen_version:
        if _seen_version is not None:
            _drop_local()
        _seen_version = version


def _fresh(key: Hashable, now: float) -> tuple[bool, object, int]:
    with _lock:
        hit = _entries.get(key)
        if hit and hit[0] > now and hit[1] == _generation:
            return True, hit[2], _generation
        return False, None, _generation


def cached(key: Hashable, compute: Callable[[], T]) -> T:
    ttl = settings.cache_ttl_s
    if ttl <= 0:
        return compute()
    _follow_other_processes()
    found, value, _ = _fresh(key, time.monotonic())
    if found:
        return value  # type: ignore[return-value]
    with _lock:
        key_lock = _key_locks.setdefault(key, threading.Lock())
    with key_lock:
        found, value, generation = _fresh(key, time.monotonic())  # another thread may have filled it
        if found:
            return value  # type: ignore[return-value]
        result = compute()
        with _lock:
            if generation == _generation:  # not invalidated while computing
                _entries[key] = (time.monotonic() + ttl, generation, result)
        return result
