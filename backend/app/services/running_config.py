"""Latest running-config of a device from the RANCID gitweb backup, for generators that must avoid ids in use."""

import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as m
from ..collectors.configs.gitweb import GitwebClient, GitwebError

CACHE_SECONDS = 300  # the generator page refetches on every input change; RANCID itself updates daily
_cache: dict[tuple[str, str], tuple[float, str]] = {}


class RunningConfigError(RuntimeError):
    pass


def fetch(db: Session, device_id: int) -> str:
    record = db.scalar(select(m.GitwebFile).where(m.GitwebFile.device_id == device_id, m.GitwebFile.status == "applied")
                       .order_by(m.GitwebFile.synced_at.desc()).limit(1))
    if record is None:
        raise RunningConfigError("Config perangkat belum ada di backup RANCID (jalankan sync gitweb dulu).")
    key = (record.repo, record.path)
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    try:
        client = GitwebClient()
        try:
            text = client.blob(f"{record.repo}/.git", record.path)
        finally:
            client.close()
    except GitwebError as exc:
        raise RunningConfigError(f"Config RANCID tidak bisa diambil: {exc}") from exc
    if len(_cache) > 256:
        _cache.clear()
    _cache[key] = (time.monotonic(), text)
    return text
