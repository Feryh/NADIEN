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
        base_url = (base_url or settings.observium_url).rstrip("/").removesuffix("/api/v0")
        token = token or settings.observium_token
        user = user or settings.observium_user
        password = password or settings.observium_password
        if not token and not (user and password):
            raise ObserviumError(
                "Observium credentials missing: set NADIEN_OBSERVIUM_TOKEN "
                "or NADIEN_OBSERVIUM_USER + NADIEN_OBSERVIUM_PASSWORD"
            )
        common: dict[str, Any] = dict(
            headers={"Authorization": f"Bearer {token}"} if token else None,
            auth=None if token else (user, password),
            verify=settings.observium_verify_tls if verify is None else verify,
            timeout=timeout or settings.observium_timeout_s,
            transport=transport,
        )
        self._http = httpx.Client(base_url=f"{base_url}/api/v0", **common)
        # graph.php sits beside the API, not under /api/v0.
        self._web = httpx.Client(base_url=base_url, **common)
        # Response shape per entity (top-level keys, reported count, rows returned) — kept for
        # diagnostics, because Observium versions differ in what some endpoints return.
        self.meta: dict[str, dict[str, Any]] = {}

    def close(self) -> None:
        self._http.close()
        self._web.close()

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

    def graph_json(self, **params: Any) -> dict[str, Any]:
        """graph.php with format=json: the rrdtool series behind a graph instead of the PNG."""
        try:
            resp = self._web.get("/graph.php", params={**params, "format": "json"})
        except httpx.HTTPError as exc:
            raise ObserviumError(f"Observium unreachable: {exc}") from exc
        if resp.status_code == 401:
            raise ObserviumError("Observium rejected the credentials (401)")
        if resp.status_code >= 400:
            raise ObserviumError(f"graph.php returned HTTP {resp.status_code}")
        # An unknown port or missing RRD comes back as an empty body or Observium's error PNG.
        if "json" not in resp.headers.get("content-type", "") or not resp.content.strip():
            raise ObserviumError("graph.php returned no data (port without RRD?)")
        try:
            return resp.json()
        except ValueError as exc:
            raise ObserviumError(f"graph.php returned invalid JSON: {exc}") from exc

    def fetch(self, entity: str, page_size: int | None = None, **params: Any) -> list[dict[str, Any]]:
        """All rows of an entity collection (`devices`, `ports`, `neighbours`, `address`, ...)."""
        path = f"/{entity}/"
        key = "addresses" if entity == "address" else entity
        if not page_size:
            data = self._get(path, params)
            rows = _rows(data, key)
            self._remember(entity, data, rows)
            return rows
        rows = []
        page = 1
        while True:
            data = self._get(path, {**params, "pagesize": page_size, "pageno": page})
            batch = _rows(data, key)
            rows.extend(batch)
            total = int(data.get("count") or 0)
            if not batch or len(rows) >= total:
                self._remember(entity, data, rows)
                return rows
            page += 1

    def _remember(self, entity: str, data: dict[str, Any], rows: list[dict[str, Any]]) -> None:
        self.meta[entity] = {"response_keys": sorted(data.keys()), "count": data.get("count"), "rows": len(rows)}


_META_KEYS = {"status", "count", "countpage", "pagesize", "pageno", "message", "debug"}


def _rows(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    payload = data.get(key)
    if payload is None:
        # Some Observium versions name collections differently (e.g. "neighbors"); take the
        # single non-metadata collection in the response instead of silently returning nothing.
        candidates = [v for k, v in data.items() if k not in _META_KEYS and isinstance(v, (dict, list))]
        payload = candidates[0] if len(candidates) == 1 else []
    if isinstance(payload, dict):
        return list(payload.values())
    return list(payload)
