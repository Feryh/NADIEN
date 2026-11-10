"""Capacity planning engine.

  available        = capacity - current - reserved
  committed %      = (current + reserved) / capacity
  months_to_full   = available / growth            (linear growth, Gbps/month)
                   = ln(target / current) / ln(1+r) (compound growth, r = %/month)

Example: 400G link, 250G current, 20G/month growth -> 150 / 20 = 7.5 months.
Growth comes from the link's manual override, else from a least-squares trend over
utilization samples (peak of in/out per sample).
"""

import math
from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from statistics import median

from ..config import settings
from ..schemas import CapacityResult, ForecastPoint

DAYS_PER_MONTH = 30.4375
MAX_FORECAST_MONTHS = 600  # beyond 50 years the date is meaningless


def capacity_color(committed_pct: float) -> str:
    if committed_pct >= settings.util_red_pct:
        return "red"
    if committed_pct >= settings.util_yellow_pct:
        return "yellow"
    return "green"


def committed_pct(current_gbps: float, reserved_gbps: float, capacity_gbps: float | None) -> float:
    """Share of capacity that is used or promised; 0.0 when the capacity is unknown so it never reads as congested."""
    return (current_gbps + reserved_gbps) / capacity_gbps * 100 if capacity_gbps else 0.0


def utilization_pct(current_gbps: float, capacity_gbps: float | None) -> float:
    return current_gbps / capacity_gbps * 100 if capacity_gbps else 0.0


def hotspot_level(pct: float) -> str:
    """Busiest links/ports: critical ≥ 90%, warning ≥ 70% (NADIEN_HOTSPOT_*_PCT), else normal.
    Far above 100% the capacity itself is wrong (e.g. a 10G uplink whose SNMP speed says 10M):
    reported as `suspect` so bad data does not crowd out the real hotspots."""
    if pct > settings.hotspot_suspect_pct:
        return "suspect"
    if pct >= settings.hotspot_critical_pct:
        return "critical"
    if pct >= settings.hotspot_warning_pct:
        return "warning"
    return "normal"


def occupancy_pct(used: int, total: int) -> float:
    return round(used / total * 100, 1) if total else 0.0


def port_utilization_pct(in_gbps: float | None, out_gbps: float | None, speed_gbps: float | None) -> float | None:
    """Busier direction against the port speed; None when the rate or the speed is unknown."""
    if not speed_gbps or (in_gbps is None and out_gbps is None):
        return None
    return round(max(in_gbps or 0.0, out_gbps or 0.0) / speed_gbps * 100, 1)


MIN_TREND_DAYS = 14  # a shorter history is mostly weekly seasonality, not growth
PEAK_PERCENTILE = 0.95


@dataclass
class TrendFit:
    """Least-squares line through daily peaks: y = intercept + slope * day."""

    slope_per_day: float
    level_now: float  # the line's value at the newest sample
    peak_margin: float  # 95th percentile of how far peaks sit above the line
    r2: float
    days: int

    @property
    def growth_per_month(self) -> float:
        return self.slope_per_day * DAYS_PER_MONTH

    @property
    def basis(self) -> float:
        """Peak level the forecast starts from: today's trend value plus the usual peak above it."""
        return max(self.level_now + self.peak_margin, 0.0)


def peak_series(samples: Sequence) -> list[tuple[float, float]]:
    """(day, peak of in/out in Gbps) per sample, oldest first."""
    return sorted((s.ts.timestamp() / 86400, max(s.in_gbps or 0, s.out_gbps or 0)) for s in samples)


SPIKE_WINDOW_DAYS = 7  # days on each side a daily peak is compared with
SPIKE_MIN_NEIGHBOURS = 4  # with fewer there is no baseline to judge by


def spike_indexes(series: Sequence[tuple[float, float]], capacity_gbps: float | None) -> set[int]:
    """Positions in a (day, peak) series, oldest first, holding a one-off spike: above NADIEN_SPIKE_RATIO × the
    median peak within ±SPIKE_WINDOW_DAYS, higher than that median by NADIEN_SPIKE_MIN_RISE_PCT of the capacity,
    and with neither adjacent sample as high. A counter glitch (a 10G link "peaking" at 15G one day between 4G
    days) stands alone; a real step up keeps the next day high too, so it stays — on the newest day it reads as a
    spike until that next day arrives."""
    min_rise = (capacity_gbps or 0) * settings.spike_min_rise_pct / 100
    if min_rise <= 0:  # without a capacity there is no scale to tell a burst from a glitch
        return set()
    days = [day for day, _ in series]
    spikes = set()
    for k, (day, peak) in enumerate(series):
        lo, hi = bisect_left(days, day - SPIKE_WINDOW_DAYS), bisect_right(days, day + SPIKE_WINDOW_DAYS)
        neighbours = [series[j][1] for j in range(lo, hi) if j != k]
        if len(neighbours) < SPIKE_MIN_NEIGHBOURS:
            continue
        baseline = median(neighbours)
        ceiling = settings.spike_ratio * baseline
        if peak <= ceiling or peak - baseline < min_rise:
            continue
        if all(series[j][1] <= ceiling for j in (k - 1, k + 1) if 0 <= j < len(series)):
            spikes.add(k)
    return spikes


def without_spikes(series: Sequence[tuple[float, float]],
                   capacity_gbps: float | None) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """(kept, spikes) of a (day, peak) series."""
    spikes = spike_indexes(series, capacity_gbps)
    return [p for k, p in enumerate(series) if k not in spikes], [p for k, p in enumerate(series) if k in spikes]


def planning_current(series: Sequence[tuple[float, float]], capacity_gbps: float | None,
                     today: float) -> float | None:
    """Peak of the newest complete day that is not a spike (a day stamped within the last 24 h is still filling;
    it counts only when nothing else is there). None for an empty series."""
    kept, _ = without_spikes(series, capacity_gbps)
    full = [peak for day, peak in kept if day <= today - 1] or [peak for _, peak in kept]
    return full[-1] if full else None


def fit_trend(points: Sequence[tuple[float, float]]) -> TrendFit | None:
    """Fit over (day, peak Gbps) points. None if fewer than 2 points or under a day of history."""
    if len(points) < 2:
        return None
    xs = [p[0] for p in points]
    span = max(xs) - min(xs)
    if span < 1:
        return None
    mean_x = sum(xs) / len(xs)
    mean_y = sum(p[1] for p in points) / len(points)
    var = sum((x - mean_x) ** 2 for x in xs)
    slope = sum((x - mean_x) * (y - mean_y) for x, y in points) / var
    intercept = mean_y - slope * mean_x
    residuals = sorted(y - (intercept + slope * x) for x, y in points)
    total = sum((y - mean_y) ** 2 for _, y in points)
    r2 = 1 - sum(r * r for r in residuals) / total if total else 0.0
    margin = residuals[min(len(residuals) - 1, int(len(residuals) * PEAK_PERCENTILE))]
    return TrendFit(slope, intercept + slope * max(xs), max(margin, 0.0), max(r2, 0.0), round(span))


def _months_to(target: float, current: float, growth_abs: float | None, growth_pct: float | None) -> float | None:
    headroom = target - current
    if headroom <= 0:
        return 0.0
    if growth_pct is not None:
        if growth_pct <= 0 or current <= 0:
            return None
        return math.log(target / current) / math.log(1 + growth_pct / 100)
    if growth_abs is None or growth_abs <= 0:
        return None
    return headroom / growth_abs


def _months_to_date(months: float | None) -> str | None:
    if months is None or months > MAX_FORECAST_MONTHS:
        return None
    return (date.today() + timedelta(days=months * DAYS_PER_MONTH)).isoformat()


def calculate(
    capacity: float,
    current: float,
    reserved: float = 0,
    growth_abs: float | None = None,
    growth_pct: float | None = None,
    growth_source: str = "manual",
    threshold_pct: float | None = None,
    trend: "TrendFit | None" = None,
) -> CapacityResult:
    """`trend` (from the last N days of daily peaks) makes the forecast start from the trend's peak
    level today instead of the single newest sample, which can be an unusually quiet or busy day."""
    threshold_pct = threshold_pct or settings.planning_threshold_pct
    capacity = capacity or 0
    utilization = current / capacity * 100 if capacity else 0.0
    committed = (current + reserved) / capacity * 100 if capacity else 0.0
    if growth_abs is None and growth_pct is None:
        growth_source = "none"
    basis = trend.basis if trend is not None else current

    # Reserved bandwidth is already promised, so growth only eats into what's left after it.
    months_full = _months_to(capacity - reserved, basis, growth_abs, growth_pct) if capacity else None
    months_thr = (
        _months_to(capacity * threshold_pct / 100 - reserved, basis, growth_abs, growth_pct) if capacity else None
    )

    return CapacityResult(
        capacity_gbps=round(capacity, 3),
        current_gbps=round(current, 3),
        reserved_gbps=round(reserved, 3),
        available_gbps=round(max(capacity - current - reserved, 0), 3),
        utilization_pct=round(utilization, 2),
        committed_pct=round(committed, 2),
        growth_gbps_per_month=round(growth_abs, 3) if growth_abs is not None else None,
        growth_pct_per_month=round(growth_pct, 3) if growth_pct is not None else None,
        growth_source=growth_source,  # type: ignore[arg-type]
        months_to_threshold=round(months_thr, 2) if months_thr is not None else None,
        months_to_full=round(months_full, 2) if months_full is not None else None,
        threshold_date=_months_to_date(months_thr),
        exhaustion_date=_months_to_date(months_full),
        color=capacity_color(committed),  # type: ignore[arg-type]
        forecast_basis_gbps=round(basis, 3),
        forecast_method="trend" if trend is not None else "current",
        trend_r2=round(trend.r2, 3) if trend is not None else None,
        trend_days=trend.days if trend is not None else 0,
    )


def forecast_series(result: CapacityResult, months: int = 24, threshold_pct: float | None = None) -> list[ForecastPoint]:
    threshold_pct = threshold_pct or settings.planning_threshold_pct
    points = []
    start = result.forecast_basis_gbps if result.forecast_basis_gbps is not None else result.current_gbps
    for month in range(months + 1):
        if result.growth_pct_per_month is not None:
            projected = start * (1 + result.growth_pct_per_month / 100) ** month
        else:
            projected = start + (result.growth_gbps_per_month or 0) * month
        points.append(
            ForecastPoint(
                month=month,
                date=(date.today() + timedelta(days=month * DAYS_PER_MONTH)).isoformat(),
                projected_gbps=round(projected + result.reserved_gbps, 3),
                capacity_gbps=result.capacity_gbps,
                threshold_gbps=round(result.capacity_gbps * threshold_pct / 100, 3),
            )
        )
    return points
