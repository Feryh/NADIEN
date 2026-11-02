"""Thin read-only client for the Observium REST API (/api/v0).

Observium returns collections keyed by entity id, e.g. {"status": "ok", "devices": {"277": {...}}};
`fetch()` normalises that to a plain list of rows and follows pagination where requested.
"""

from typing import Any

import httpx

from ...config import settings


class ObserviumError(RuntimeError):
    pass


class ObserviumClient:
    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        user: str | None = None,
        password: str | None = None,
        verify: bool | None = None,
        timeout: float | None = None,
        transport: httpx.BaseTransport | None = None,  # injected by tests
    ):
        base_url = (base_url or settings.observium_url).rstrip("/")
        token = token or settings.observium_token
        user = user or settings.observium_user
        password = password or settings.observium_password
        if not token and not (user and password):
            raise ObserviumError(
                "Observium credentials missing: set NADIEN_OBSERVIUM_TOKEN "
                "or NADIEN_OBSERVIUM_USER + NADIEN_OBSERVIUM_PASSWORD"
            )
        self._http = httpx.Client(
            base_url=f"{base_url}/api/v0",
            headers={"Authorization": f"Bearer {token}"} if token else None,
            auth=None if token else (user, password),
            verify=settings.observium_verify_tls if verify is None else verify,
            timeout=timeout or settings.observium_timeout_s,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "ObserviumClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = self._http.get(path, params=params)
        except httpx.HTTPError as exc:
            raise ObserviumError(f"Observium unreachable: {exc}") from exc
        if resp.status_code == 401:
            raise ObserviumError("Observium rejected the credentials (401)")
        if resp.status_code == 429:
            raise ObserviumError("Observium throttled this client after failed logins (429); wait and retry")
        if resp.status_code >= 400:
            raise ObserviumError(f"Observium {path} returned HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        if data.get("status") not in (None, "ok"):
            raise ObserviumError(f"Observium {path}: {data.get('message') or data.get('status')}")
        return data

    def fetch(self, entity: str, page_size: int | None = None, **params: Any) -> list[dict[str, Any]]:
        """All rows of an entity collection (`devices`, `ports`, `neighbours`, `address`, ...)."""
        path = f"/{entity}/"
        key = "addresses" if entity == "address" else entity
        if not page_size:
            return _rows(self._get(path, params), key)
        rows: list[dict[str, Any]] = []
        page = 1
        while True:
            data = self._get(path, {**params, "pagesize": page_size, "pageno": page})
            batch = _rows(data, key)
            rows.extend(batch)
            total = int(data.get("count") or 0)
            if not batch or len(rows) >= total:
                return rows
            page += 1


def _rows(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    payload = data.get(key) or []
    if isinstance(payload, dict):
        return list(payload.values())
    return list(payload)
