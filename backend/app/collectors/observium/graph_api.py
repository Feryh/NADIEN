"""Utilization history through Observium's graph API — same login as the REST API, no SSH.

`graph.php?type=port_bits&id=<port_id>&format=json` renders the port's RRD with rrdtool and returns
the series instead of a PNG: bits/s, In positive and Out mirrored negative. rrdtool picks one step
for the whole range from the finest archive covering it (2 h for 90 days, 5 min for 7 days) and
each value is the average over that step, so long ranges under-read short peaks. The last
FINE_DAYS are therefore fetched a second time at 5-minute resolution and replace those days.
"""

from collections import defaultdict
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from ... import models as m
from ...config import settings
from .client import ObserviumClient, ObserviumError

if TYPE_CHECKING:
    from .rrd import Target

DAY = 86400
FINE_DAYS = 7
WIDTH = 2200  # more pixels than points in any archive, so rrdtool never consolidates further

Point = tuple[int, float | None, float | None]  # (unix ts, in bits/s, out bits/s)
DailyPeak = tuple[datetime, float, float]  # (UTC day, in Gbps, out Gbps)


def parse_graph_json(data: dict[str, Any]) -> list[Point]:
    """rrdtool graph JSON -> points. Rows carry no timestamp: row i is at start + i * step."""
    meta = data.get("meta") or {}
    start, step = int(meta["start"]), int(meta["step"])
    legend = [str(x).strip().lower() for x in meta.get("legend") or []]
    col_in = legend.index("in") if "in" in legend else 0
    col_out = legend.index("out") if "out" in legend else 2

    def value(row: list[Any], col: int) -> float | None:
        v = row[col] if col < len(row) else None
        return abs(float(v)) if isinstance(v, (int, float)) and v == v else None

    return [(start + i * step, value(row, col_in), value(row, col_out)) for i, row in enumerate(data.get("data") or [])]


def daily_peaks(points: list[Point]) -> tuple[dict[int, DailyPeak], int]:
    """Peak per UTC day and the number of days with no value at all (after the first data).

    The first day is dropped when it holds under half its points: a range that starts mid-day
    would otherwise report a peak for only part of that day.
    """
    if len(points) < 2:
        return {}, 0
    step = points[1][0] - points[0][0] or 1
    buckets: dict[int, list[Point]] = defaultdict(list)
    for p in points:
        buckets[(p[0] - step // 2) // DAY].append(p)  # a value covers the step that ends at its ts
    days = sorted(buckets)
    if len(buckets[days[0]]) < (DAY // step) / 2:
        days = days[1:]
    peaks: dict[int, DailyPeak] = {}
    empty, seen_data = 0, False
    for day in days:
        ins = [p[1] for p in buckets[day] if p[1] is not None]
        outs = [p[2] for p in buckets[day] if p[2] is not None]
        if not ins and not outs:
            empty += seen_data  # NaN: no data, or above the RRD's DS maximum
            continue
        seen_data = True
        peaks[day] = (datetime.fromtimestamp(day * DAY, tz=timezone.utc),
                      max(ins, default=0.0) / 1e9, max(outs, default=0.0) / 1e9)
    return peaks, empty


class GraphApiSource:
    name = "api"
    chunk_size = 200

    def __init__(self, client: ObserviumClient, workers: int | None = None):
        self.client = client
        self.workers = workers or settings.observium_graph_workers

    def usable(self, ref: m.ObserviumPort) -> bool:
        return ref.observium_port_id is not None

    def fetch(self, chunk: Iterable["Target"], days: int) -> dict[str, Any]:
        targets = list(chunk)
        results: dict[str, Any] = {}
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            for t, (got, empty) in zip(targets, pool.map(lambda t: self.history(t.ref.observium_port_id, days), targets)):
                results[str(t.link.id)] = got
                results[f"{t.link.id}#nan"] = empty
        return results

    def history(self, port_id: int, days: int) -> tuple[list[DailyPeak] | str, int]:
        try:
            peaks, empty = daily_peaks(self._series(port_id, days))
            if days > FINE_DAYS:
                fine, _ = daily_peaks(self._series(port_id, FINE_DAYS))
                peaks.update(fine)
        except ObserviumError as exc:
            return str(exc), 0
        except (KeyError, ValueError, TypeError) as exc:
            return f"unexpected graph.php output: {exc}", 0
        return [peaks[d] for d in sorted(peaks)], empty

    def _series(self, port_id: int, days: int) -> list[Point]:
        data = self.client.graph_json(type="port_bits", id=port_id, **{"from": f"-{days}d"}, to="now",
                                      width=WIDTH, height=100)
        return parse_graph_json(data)

    def close(self) -> None:
        self.client.close()
