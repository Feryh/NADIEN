"""Thin read-only client for the Observium REST API (/api/v0).

Observium returns collections keyed by entity id, e.g. {"status": "ok", "devices": {"277": {...}}};
`fetch()` normalises that to a plain list of rows and follows pagination where requested.
"""

import logging
from typing import Any

import httpx

from ...config import settings

log = logging.getLogger("nadien.observium")

PROBE_TIMEOUT_S = 10.0  # a standby that is down refuses at once; one that hangs must not stall every run


class ObserviumError(RuntimeError):
    pass


def _root(url: str) -> str:
    return url.rstrip("/").removesuffix("/api/v0")


class ObserviumClient:
    """`base_url` None = the main Observium: `observium_url`, or `observium_standby_url` when that one does not
    answer (an active/standby pair, only the active one serves). Which one answered is in `base_url`."""

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        user: str | None = None,
        password: str | None = None,
        verify: bool | None = None,
        timeout: float | None = None,
        transport: httpx.BaseTransport | None = None,  # injected by tests
        standby_url: str | None = None,
    ):
        if not base_url:
            base_url, standby_url = settings.observium_url, settings.observium_standby_url
        token = settings.observium_token if token is None else token
        user = settings.observium_user if user is None else user
        password = settings.observium_password if password is None else password
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
        self._open(_root(base_url), common)
        self.failed_over = False
        if standby_url and _root(standby_url) != self.base_url and not self._answers():
            log.warning("Observium %s does not answer: using the standby %s", self.base_url, _root(standby_url))
            self.close()
            self._open(_root(standby_url), common)
            self.failed_over = True
        # Response shape per entity (top-level keys, reported count, rows returned) — kept for
        # diagnostics, because Observium versions differ in what some endpoints return.
        self.meta: dict[str, dict[str, Any]] = {}

    def _open(self, base_url: str, common: dict[str, Any]) -> None:
        self.base_url = base_url
        self._http = httpx.Client(base_url=f"{base_url}/api/v0", **common)
        # graph.php sits beside the API, not under /api/v0.
        self._web = httpx.Client(base_url=base_url, **common)

    def _answers(self) -> bool:
        """False only when the server is not serving (refused, unreachable, timed out, 5xx): a standby. A wrong
        password still answers, and is reported by the real call with its own message."""
        try:
            resp = self._http.get("/devices/", params={"pagesize": 1, "pageno": 1}, timeout=PROBE_TIMEOUT_S)
        except httpx.TransportError:
            return False
        return resp.status_code < 500

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
        data = _api_json(resp, path)
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


def _api_json(resp: httpx.Response, path: str) -> dict[str, Any]:
    """The API answer, or a readable error when something else answered: an SSO/login portal in front of Observium
    (F5 APM redirects every call to its login page and the API never sees NADIEN's credentials), or a URL that is
    the web UI rather than the server root."""
    location = resp.headers.get("location", "")
    if resp.is_redirect:
        raise ObserviumError(f"Observium {path} dialihkan (HTTP {resp.status_code}) ke {location[:120]}: URL ini di "
                             "belakang portal login/SSO; pakai alamat Observium langsung (tanpa portal) untuk API")
    try:
        data = resp.json()
    except ValueError:
        kind = resp.headers.get("content-type", "?").split(";")[0]
        raise ObserviumError(f"Observium {path} membalas {kind}, bukan JSON API: URL bukan Observium API atau "
                             "tertutup portal login/SSO; pakai alamat Observium langsung") from None
    if not isinstance(data, dict):
        raise ObserviumError(f"Observium {path} membalas format yang tidak dikenal")
    return data


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
