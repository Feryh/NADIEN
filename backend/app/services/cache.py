"""Short-lived cache for heavy read models (topology views, capacity lists, dashboard).

Those views aggregate thousands of devices and links per request; with tens of users looking at
the same pages, recomputing each one per request is wasted work. Entries live `NADIEN_CACHE_TTL_S`
seconds. Every successful write request and every finished sync bumps a version counter in the
database; each API process (uvicorn worker or replica) checks it at most once per second and drops
its cache when it moved, so an edit shows up everywhere within about a second.

Concurrent misses on the same key compute once (single flight). An entry that merely expired is
refreshed by one request while the others keep getting the old value, so nobody queues behind a
recomputation just because the TTL ran out. After a write there is no old value to serve: the entry
is gone and requests wait for the new one.
"""

import threading
import time
from collections.abc import Callable, Hashable
from enum import Enum
from typing import TypeVar

from sqlalchemy import update
from sqlalchemy.exc import SQLAlchemyError

from ..config import settings

T = TypeVar("T")

VERSION_KEY = "cache_version"
CHECK_EVERY_S = 1.0
# An old value is served for this many TTLs at most; past that, wait for a new one.
STALE_FOR_TTLS = 5
# Keys carry request parameters (area, POP, limit), so without a bound they grow with whatever is requested.
MAX_ENTRIES = 512

_lock = threading.Lock()
_entries: dict[Hashable, tuple[float, int, object]] = {}
_key_locks: dict[Hashable, threading.Lock] = {}
_generation = 0
_seen_version: int | None = None
_checked_at = 0.0


class _Lookup(Enum):
    FRESH = 1
    STALE = 2
    MISSING = 3


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


def _lookup(key: Hashable, ttl: float) -> tuple[_Lookup, object, int]:
    now = time.monotonic()
    with _lock:
        hit = _entries.get(key)
        if hit is None or hit[1] != _generation or now >= hit[0] + ttl * STALE_FOR_TTLS:
            return _Lookup.MISSING, None, _generation
        return (_Lookup.FRESH if now < hit[0] else _Lookup.STALE), hit[2], _generation


def _key_lock(key: Hashable) -> threading.Lock:
    with _lock:
        return _key_locks.setdefault(key, threading.Lock())


def _store(key: Hashable, value: object, generation: int, ttl: float) -> None:
    with _lock:
        if generation != _generation:  # invalidated while computing
            return
        while key not in _entries and len(_entries) >= MAX_ENTRIES:
            del _entries[min(_entries, key=lambda k: _entries[k][0])]
        _entries[key] = (time.monotonic() + ttl, generation, value)
        if len(_key_locks) > MAX_ENTRIES:
            # Worst case a lock in use loses its entry: two requests compute the same value once.
            for orphan in _key_locks.keys() - _entries.keys():
                del _key_locks[orphan]


def cached(key: Hashable, compute: Callable[[], T]) -> T:
    ttl = settings.cache_ttl_s
    if ttl <= 0:
        return compute()
    _follow_other_processes()
    state, value, _ = _lookup(key, ttl)
    if state is _Lookup.FRESH:
        return value  # type: ignore[return-value]
    key_lock = _key_lock(key)
    if not key_lock.acquire(blocking=state is _Lookup.MISSING):
        return value  # type: ignore[return-value]  # another request is already refreshing it
    try:
        state, value, generation = _lookup(key, ttl)  # another thread may have filled it
        if state is _Lookup.FRESH:
            return value  # type: ignore[return-value]
        result = compute()
        _store(key, result, generation, ttl)
        return result
    finally:
        key_lock.release()
