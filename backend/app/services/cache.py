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

import logging
import threading
import time
from collections.abc import Callable, Hashable
from enum import Enum
from typing import Any, TypeVar

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


# --------------------------------------------------------------------------- heavy derived views

# Kept across invalidations (unlike _entries): key -> (computed at, generation, value).
_lasting: dict[Hashable, tuple[float, int, object]] = {}
_background_locks: dict[Hashable, threading.Lock] = {}
# An outdated value is served while its refresh runs, but never older than this.
MAX_OUTDATED_S = 900.0


def cached_in_background(key: Hashable, compute: Callable[[Any], T], db: Any) -> T:
    """For heavy views derived from synced data (capacity lists): after a write or past the TTL, keep serving the
    previous value while one background thread computes the new one with its own session. Only a process's first
    request for a key, or one whose value is older than MAX_OUTDATED_S, waits.

    Plain `cached` drops entries on every write so an edit shows at once; these views change with each 10-minute
    utilization sync, so waiting ~10 s for each recomputation would hit whoever opens the page next."""
    ttl = settings.cache_ttl_s
    if ttl <= 0:
        return compute(db)
    _follow_other_processes()
    now = time.monotonic()
    with _lock:
        hit = _lasting.get(key)
        generation = _generation
    if hit is not None and now - hit[0] < MAX_OUTDATED_S:
        computed_at, seen_generation, value = hit
        if seen_generation != generation or now >= computed_at + ttl:
            _refresh_in_background(key, compute)
        return value  # type: ignore[return-value]
    key_lock = _background_lock(key)
    with key_lock:  # first request (or a value too old): compute here, once
        with _lock:
            hit = _lasting.get(key)
        if hit is not None and time.monotonic() - hit[0] < MAX_OUTDATED_S:
            return hit[2]  # type: ignore[return-value]
        return _compute_and_keep(key, compute, db)


def _compute_and_keep(key: Hashable, compute: Callable[[Any], T], db: Any) -> T:
    with _lock:
        generation = _generation
    value = compute(db)
    with _lock:
        # Stamped with the generation it started from: a write during the computation triggers another refresh.
        _lasting[key] = (time.monotonic(), generation, value)
        while len(_lasting) > MAX_ENTRIES:
            del _lasting[min(_lasting, key=lambda k: _lasting[k][0])]
    return value


def _refresh_in_background(key: Hashable, compute: Callable[[Any], T]) -> None:
    key_lock = _background_lock(key)
    if not key_lock.acquire(blocking=False):
        return  # already being refreshed

    def run() -> None:
        from ..database import SessionLocal  # late: the database layer imports settings

        try:
            with SessionLocal() as session:
                _compute_and_keep(key, compute, session)
        except Exception:  # noqa: BLE001 - a failed refresh keeps the old value; the next request retries
            logging.getLogger("nadien.cache").exception("background refresh of %s failed", key)
        finally:
            key_lock.release()

    threading.Thread(target=run, name=f"cache-refresh-{key}", daemon=True).start()


def _background_lock(key: Hashable) -> threading.Lock:
    with _lock:  # separate from _key_locks, which _store prunes by _entries
        return _background_locks.setdefault(key, threading.Lock())


def warm_in_background(key: Hashable, compute: Callable[[Any], T]) -> None:
    """Start computing a cached_in_background key now (process start), so the first visitor finds it ready; a request
    arriving meanwhile waits for this computation instead of starting its own."""
    if settings.cache_ttl_s > 0:
        _refresh_in_background(key, compute)
