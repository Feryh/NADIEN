from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

import httpx
import pytest
from sqlalchemy import select

from app import models as m
from app.collectors.observium.client import ObserviumClient
from app.collectors.observium.graph_api import GraphApiSource, daily_peaks, parse_graph_json
from app.collectors.observium.rrd import RrdError, open_source, sync_rrd
from app.collectors.observium.sync import sync_observium
from app.config import settings

from .conftest import observium_transport

DAY = 86400
NOW = int(datetime.now(timezone.utc).timestamp())
TODAY = NOW // DAY
Series = Callable[[int, int], tuple[float, float] | None]  # (ts, step) -> (in, out) bits/s


def graph_json(start: int, step: int, rows: list[tuple[float, float] | None]) -> dict:
    """The shape of rrdtool's graph JSON as Observium returns it: In positive, Out mirrored negative."""
    data = [[None] * 6 if r is None else [r[0], r[0], -r[1], -r[1], None, None] for r in rows]
    return {"about": "RRDtool graph JSON output",
            "meta": {"start": start, "end": start + step * (len(rows) - 1), "step": step,
                     "legend": ["", "In ", "", "Out", "", ""]},
            "data": data}


def graph_transport(series: dict[str, Series], calls: list[dict]) -> httpx.MockTransport:
    """graph.php: 5-minute points for ranges up to 7 days, 2-hour points beyond (like Observium's RRAs)."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/graph.php"
        query = parse_qs(request.url.query.decode())
        calls.append(query)
        fn = series.get(query["id"][0])
        if fn is None:  # unknown port: Observium answers with an empty body
            return httpx.Response(200, headers={"content-type": "application/json"}, content=b"")
        days = int(query["from"][0].strip("-d"))
        step = 300 if days <= 7 else 7200
        end = NOW // step * step
        start = end - days * DAY
        return httpx.Response(200, json=graph_json(start, step, [fn(t, step) for t in range(start, end + 1, step)]))

    return httpx.MockTransport(handler)


def growing_with_daily_spike(t: int, step: int) -> tuple[float, float]:
    """Average 100 G + 1 G/day; at 5-minute resolution a +5 G spike at noon shows up."""
    day = (t - 1) // DAY  # a value covers the step that ends at t
    gbps = 100 + (day - (TODAY - 90))
    if step == 300 and (t - 1) % DAY // step == 12 * 3600 // step:
        gbps += 5
    return gbps * 1e9, gbps * 0.5e9


@pytest.fixture
def synced(db):
    sync_observium(db, ObserviumClient(base_url="http://o.test", token="t", transport=observium_transport()))
    return db


def api_source(series: dict[str, Series], calls: list[dict]) -> GraphApiSource:
    client = ObserviumClient(base_url="http://o.test/api/v0", token="t", transport=graph_transport(series, calls))
    return GraphApiSource(client, workers=2)


def core_samples(db) -> tuple[m.Link, dict[int, m.UtilizationSample]]:
    core = next(link for link in db.scalars(select(m.Link)) if link.capacity_gbps == 400)
    rows = db.scalars(select(m.UtilizationSample).where(m.UtilizationSample.link_id == core.id))
    return core, {int(s.ts.replace(tzinfo=timezone.utc).timestamp()) // DAY: s for s in rows}


def test_should_read_in_and_mirrored_out_when_parsing_graph_json():
    points = parse_graph_json(graph_json(1_000, 300, [(2e9, 1e9), None, (4e9, 3e9)]))
    assert points == [(1_000, 2e9, 1e9), (1_300, None, None), (1_600, 4e9, 3e9)]


def test_should_drop_partial_first_day_and_count_empty_days_when_building_daily_peaks():
    step = 7200
    start = 10 * DAY + 20 * 3600  # only the last 4 hours of day 10
    points = [(t, None if (t - 1) // DAY == 12 else 8e9, 2e9) for t in range(start + step, 14 * DAY + 1, step)]
    points = [(t, i, None if i is None else o) for t, i, o in points]
    peaks, empty = daily_peaks(points)
    assert sorted(peaks) == [11, 13] and empty == 1
    assert peaks[11][1:] == (8.0, 2.0)


def test_should_use_5_minute_peaks_for_last_7_days_and_2_hour_peaks_before(synced):
    calls: list[dict] = []
    report = sync_rrd(synced, api_source({"1001": growing_with_daily_spike}, calls), days=90)
    core, by_day = core_samples(synced)

    assert by_day[TODAY - 10].in_gbps == 180  # 2-hour averages: no spike visible
    assert by_day[TODAY - 3].in_gbps == 192  # 187 average + 5 G spike from the 5-minute pass
    assert by_day[TODAY - 3].out_gbps == 96  # Out came mirrored negative; half of In
    assert core.current_gbps == 194  # yesterday's peak, not today's partial day
    assert report["source"] == "api" and report["links_with_history"] == 1
    assert sorted(q["from"][0] for q in calls if q["id"] == ["1001"]) == ["-7d", "-90d"]


def test_should_report_error_and_keep_going_when_port_has_no_rrd(synced):
    report = sync_rrd(synced, api_source({}, []), days=30)
    assert report["links_with_history"] == 0
    assert report["error_count"] >= 1 and "no data" in report["errors"][0]


def test_should_use_graph_api_by_default_and_fail_early_without_credentials(monkeypatch):
    monkeypatch.setattr(settings, "observium_rrd_source", "api")
    monkeypatch.setattr(settings, "observium_token", "t")
    source = open_source()
    assert isinstance(source, GraphApiSource)
    source.close()

    monkeypatch.setattr(settings, "observium_token", None)
    monkeypatch.setattr(settings, "observium_user", None)
    with pytest.raises(RrdError, match="not configured"):
        open_source()


def test_should_keep_samples_outside_the_window(synced):
    core = next(link for link in synced.scalars(select(m.Link)) if link.capacity_gbps == 400)
    old = datetime.now(timezone.utc) - timedelta(days=200)
    synced.add(m.UtilizationSample(link_id=core.id, ts=old, in_gbps=1, out_gbps=1))
    synced.commit()
    sync_rrd(synced, api_source({"1001": growing_with_daily_spike}, []), days=30)
    _, by_day = core_samples(synced)
    assert int(old.timestamp()) // DAY in by_day
