"""Collector connection settings editable from the UI (Pengaturan Collector).

Precedence: value saved in the UI (table `collector_settings`) > NADIEN_* environment / backend/.env >
default. Clearing a UI value falls back to the environment again.

Secrets (tokens, passwords) are encrypted with Fernet and never returned by the API — only whether
one is set. Because NADIEN has no login yet, changing a URL requires entering that system's
credentials again in the same request: otherwise anyone who can open the page could point the URL at
their own server and receive the stored token.

Every API process applies the saved values to the shared `settings` object: at startup, and before
each collector request / scheduled run (`refresh()`; a handful of rows, cheap).
"""

import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .. import models as m
from ..config import settings

log = logging.getLogger("nadien.settings")

Kind = Literal["text", "secret", "bool", "int", "select"]


@dataclass(frozen=True)
class FieldSpec:
    key: str  # attribute on Settings
    group: str
    label: str
    kind: Kind = "text"
    options: tuple[str, ...] = ()
    help: str = ""


FIELDS: list[FieldSpec] = [
    FieldSpec("observium_url", "observium", "URL Observium", help="mis. http://172.17.42.136"),
    FieldSpec("observium_token", "observium", "API token", "secret", help="Disarankan: token read-only, dibatasi IP server NADIEN"),
    FieldSpec("observium_user", "observium", "Username", help="Hanya dipakai kalau token kosong"),
    FieldSpec("observium_password", "observium", "Password", "secret"),
    FieldSpec("observium_verify_tls", "observium", "Verifikasi sertifikat TLS", "bool"),
    FieldSpec("observium_pop_regex", "observium", "Regex kode POP dari hostname", help="Grup pertama = kode POP"),
    FieldSpec("observium_include_cdp", "observium", "Pakai CDP untuk perangkat Cisco tanpa LLDP", "bool"),
    FieldSpec("observium_rrd_source", "observium", "Sumber histori RRD", "select", ("api", "ssh", "local"),
              help="api = graph.php dengan login di atas (tanpa SSH)"),
    FieldSpec("observium_graph_workers", "observium", "Request graph paralel", "int"),
    FieldSpec("gitweb_url", "gitweb", "URL gitweb RANCID", help="mis. http://172.17.12.232/gitweb/"),
    FieldSpec("gitweb_user", "gitweb", "Username (opsional)"),
    FieldSpec("gitweb_password", "gitweb", "Password (opsional)", "secret"),
    FieldSpec("gitweb_daily_at", "gitweb", "Jadwal update harian (HH:MM)", help="Kosong = tidak terjadwal"),
]
FIELD_BY_KEY = {f.key: f for f in FIELDS}
# Changing one of these URLs needs that system's credentials in the same request.
CREDENTIALS_FOR_URL = {
    "observium_url": ("observium_token", "observium_user", "observium_password"),
    "gitweb_url": ("gitweb_user", "gitweb_password"),
}

# Environment/.env values as loaded at start, so a cleared UI value can fall back to them.
_ENV = {f.key: getattr(settings, f.key) for f in FIELDS}


class SettingsError(ValueError):
    pass


# --------------------------------------------------------------------------- encryption


def _key_file(db: Session) -> Path:
    from ..backup import backup_dir  # late: backup imports database/engine

    return backup_dir(db.get_bind()) / "nadien-secret.key"


def _fernet(db: Session, create: bool = False) -> Fernet | None:
    if settings.secret_key:
        return Fernet(settings.secret_key.encode())
    path = _key_file(db)
    if path.exists():
        return Fernet(path.read_bytes().strip())
    if not create:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(Fernet.generate_key())
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return Fernet(path.read_bytes().strip())


# --------------------------------------------------------------------------- conversion


def _parse(spec: FieldSpec, raw: Any) -> Any:
    """UI input -> typed value (None = clear back to the environment)."""
    if raw is None or (isinstance(raw, str) and not raw.strip() and spec.kind not in ("bool",)):
        return None
    if spec.kind == "bool":
        return raw if isinstance(raw, bool) else str(raw).lower() in ("1", "true", "yes", "on")
    if spec.kind == "int":
        try:
            value = int(raw)
        except (TypeError, ValueError) as exc:
            raise SettingsError(f"{spec.label}: harus angka") from exc
        if not 1 <= value <= 64:
            raise SettingsError(f"{spec.label}: 1–64")
        return value
    text = str(raw).strip()
    if spec.kind == "select" and text not in spec.options:
        raise SettingsError(f"{spec.label}: pilih salah satu dari {', '.join(spec.options)}")
    if spec.key.endswith("_url") and not text.startswith(("http://", "https://")):
        raise SettingsError(f"{spec.label}: harus diawali http:// atau https://")
    if spec.key == "gitweb_daily_at":
        from ..collectors.scheduler import parse_hhmm

        try:
            parse_hhmm(text)
        except ValueError as exc:
            raise SettingsError("Jadwal harus berformat HH:MM, mis. 02:30") from exc
    return text


def _decode(spec: FieldSpec, stored: str) -> Any:
    if spec.kind == "bool":
        return stored == "true"
    if spec.kind == "int":
        return int(stored)
    return stored


def _encode(spec: FieldSpec, value: Any) -> str:
    if spec.kind == "bool":
        return "true" if value else "false"
    return str(value)


# --------------------------------------------------------------------------- load / apply


def _saved(db: Session) -> dict[str, Any]:
    rows = {r.key: r for r in db.scalars(select(m.CollectorSetting))}
    out: dict[str, Any] = {}
    fernet = _fernet(db) if any(r.secret for r in rows.values()) else None
    for key, row in rows.items():
        spec = FIELD_BY_KEY.get(key)
        if spec is None:
            continue
        if row.secret:
            if fernet is None:
                log.warning("secret %s saved but no key available (NADIEN_SECRET_KEY / key file)", key)
                continue
            try:
                out[key] = fernet.decrypt(row.value.encode()).decode()
            except InvalidToken:
                log.warning("secret %s cannot be decrypted with the current key", key)
        else:
            out[key] = _decode(spec, row.value)
    return out


def apply(db: Session) -> None:
    """Saved values over the environment, onto the shared settings object."""
    saved = _saved(db)
    for spec in FIELDS:
        setattr(settings, spec.key, saved.get(spec.key, _ENV[spec.key]))


def refresh() -> None:
    """Re-read saved settings in this process (startup, before collector work). Never raises."""
    from ..database import SessionLocal

    try:
        with SessionLocal() as db:
            apply(db)
    except SQLAlchemyError as exc:
        log.warning("collector settings not loaded: %s", exc)


def describe(db: Session) -> dict[str, Any]:
    """Every field with its effective value and where it comes from. Secrets: only `is_set`."""
    saved = _saved(db)
    fields = []
    for spec in FIELDS:
        source = "ui" if spec.key in saved else ("env" if _ENV[spec.key] not in (None, "") else "default")
        value = saved.get(spec.key, _ENV[spec.key])
        item: dict[str, Any] = {"key": spec.key, "group": spec.group, "label": spec.label, "kind": spec.kind,
                                "options": list(spec.options), "help": spec.help, "source": source}
        if spec.kind == "secret":
            item["is_set"] = bool(value)
        else:
            item["value"] = value
        fields.append(item)
    return {"editable": settings.settings_ui_enabled, "fields": fields}


def update(db: Session, changes: dict[str, Any], clear: list[str]) -> None:
    """Save UI changes. `changes`: key -> new value ('' on a secret = keep it). `clear`: keys to drop
    (back to the environment)."""
    if not settings.settings_ui_enabled:
        raise SettingsError("Pengaturan lewat UI dimatikan (NADIEN_SETTINGS_UI_ENABLED=false); pakai backend/.env")
    unknown = (set(changes) | set(clear)) - set(FIELD_BY_KEY)
    if unknown:
        raise SettingsError(f"Field tidak dikenal: {', '.join(sorted(unknown))}")
    changes = {k: v for k, v in changes.items() if not (FIELD_BY_KEY[k].kind == "secret" and v in ("", None))}
    _require_credentials_with_url(db, changes, clear)
    fernet = None
    for key, raw in changes.items():
        spec = FIELD_BY_KEY[key]
        value = _parse(spec, raw)
        row = db.get(m.CollectorSetting, key)
        if value is None:
            if row is not None:
                db.delete(row)
            continue
        if spec.kind == "secret":
            fernet = fernet or _fernet(db, create=True)
            stored = fernet.encrypt(str(value).encode()).decode()
        else:
            stored = _encode(spec, value)
        if row is None:
            db.add(m.CollectorSetting(key=key, value=stored, secret=spec.kind == "secret"))
        else:
            row.value, row.secret = stored, spec.kind == "secret"
    for key in clear:
        row = db.get(m.CollectorSetting, key)
        if row is not None:
            db.delete(row)
    db.commit()
    apply(db)


def _require_credentials_with_url(db: Session, changes: dict[str, Any], clear: list[str]) -> None:
    current = {spec.key: getattr(settings, spec.key) for spec in FIELDS}
    for url_key, credential_keys in CREDENTIALS_FOR_URL.items():
        new_url = changes.get(url_key)
        if new_url is None or str(new_url).strip().rstrip("/") == str(current[url_key] or "").rstrip("/"):
            continue
        has_stored_secret = any(current[k] for k in credential_keys if FIELD_BY_KEY[k].kind == "secret")
        resent = any(changes.get(k) for k in credential_keys if FIELD_BY_KEY[k].kind == "secret")
        cleared = all(k in clear for k in credential_keys if FIELD_BY_KEY[k].kind == "secret")
        if has_stored_secret and not resent and not cleared:
            raise SettingsError(
                f"{FIELD_BY_KEY[url_key].label} berubah: isi ulang token/password-nya juga, supaya kredensial "
                "yang tersimpan tidak terkirim ke server lain")
