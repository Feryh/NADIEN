import re
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app import models as m
from app.collectors.observium.client import ObserviumClient
from app.collectors.observium.rrd import RrdError, ScriptSource, build_script, open_runner, parse_output, sync_rrd
from app.collectors.observium.sync import sync_observium
from app.config import settings

from .conftest import observium_transport

DAY = 86400


def xport(start: int, rows: list[tuple[float, float]], with_time: bool = False) -> str:
    body = "".join(
        f"<row>{f'<t>{start + i * DAY}</t>' if with_time else ''}<v>{a}</v><v>{b}</v></row>" for i, (a, b) in enumerate(rows)
    )
    return (f'<?xml version="1.0"?><xport><meta><start>{start}</start><step>{DAY}</step><end>0</end>'
            f"<rows>{len(rows)}</rows><columns>2</columns></meta><data>{body}</data></xport>")


class FakeRunner:
    """Answers the generated script from a {path: xport-xml | None (missing)} table."""

    def __init__(self, files: dict[str, str | None]):
        self.files = files
        self.scripts: list[str] = []

    def run(self, script: str) -> str:
        self.scripts.append(script)
        listing = script.split("<<'NADIEN_RRD_LIST'\n", 1)[1].split("\nNADIEN_RRD_LIST", 1)[0]
        out = []
        for line in listing.splitlines():
            key, path = line.split("|", 1)
            body = self.files.get(path)
            out.append(f"@@@ {key}\n{body if body is not None else 'MISSING'}")
        return "\n".join(out) + "\n"

    def close(self) -> None:
        pass


def test_parse_output_rows_nan_missing_and_errors():
    start = 1_790_000_000
    text = (
        f"@@@ 1\n{xport(start, [(1.25e9, 2.5e8), (float('nan'), float('nan')), (2.5e9, 5e8)])}\n"
        f"@@@ 2\nMISSING\n"
        f"@@@ 3\nERROR: opening '/x': No such file\n"
        f"@@@ 4\n{xport(start, [(1e9, 1e9)], with_time=True)}\n"
    )
    out = parse_output(text)
    assert [(round(a, 3), round(b, 3)) for _, a, b in out["1"]] == [(10.0, 2.0), (20.0, 4.0)]  # bytes/s -> Gbps, NaN day dropped
    assert out["1"][1][0] == datetime.fromtimestamp(start + 2 * DAY, tz=timezone.utc)
    assert out["2"] == "MISSING" and out["3"].startswith("ERROR") and len(out["4"]) == 1
    assert out["1#nan"] == 1


def test_clipped_400g_link_is_flagged(synced, monkeypatch):
    db = synced
    monkeypatch.setattr(settings, "observium_rrd_path", "/rrd")
    start = int(datetime.now(timezone.utc).timestamp()) // DAY * DAY - 4 * DAY
    # 400G link stuck at the 100G DS max, with a day lost above it.
    rows = [(1.249e10, 1e10), (float("nan"), 1e10), (1.249e10, 1e10), (1.2e10, 1e10), (1.2e10, 1e10)]
    report = sync_rrd(db, ScriptSource(FakeRunner({"/rrd/jkt-p-01.metro.net/port-21.rrd": xport(start, rows)})), days=5)
    assert report["days_without_data"] == 1
    assert len(report["possibly_clipped"]) == 1 and "400G link" in report["possibly_clipped"][0]


def test_script_escapes_colons_and_uses_daily_max():
    script = build_script({"7": "/opt/observium/rrd/r1/port-5.rrd"}, 30)
    assert "INOCTETS:MAX" in script and "--step 86400" in script and "end-30d" in script
    assert "7|/opt/observium/rrd/r1/port-5.rrd" in script
    assert "AVERAGE" in script  # fallback when an RRD has no MAX archive


@pytest.fixture
def synced(db):
    c = ObserviumClient(base_url="http://o.test", token="t", transport=observium_transport())
    sync_observium(db, c)
    return db


def test_port_map_written_by_sync(synced):
    refs = {(r.observium_hostname, r.if_index) for r in synced.scalars(select(m.ObserviumPort))}
    assert ("jkt-p-01.metro.net", 21) in refs and ("172.16.0.2", 540) in refs  # Observium hostname = RRD dir


def test_sync_rrd_writes_daily_peaks(synced, monkeypatch):
    db = synced
    monkeypatch.setattr(settings, "observium_rrd_path", "/rrd")
    now = int(datetime.now(timezone.utc).timestamp()) // DAY * DAY
    start = now - 89 * DAY
    # JKT-P-01 port 21 (A side of JKT–SBY): growth from 100G to 280G over 90 days.
    rows = [((100 + 2 * i) * 1.25e8, (50 + i) * 1.25e8) for i in range(90)]
    runner = FakeRunner({"/rrd/jkt-p-01.metro.net/port-21.rrd": xport(start, rows)})
    report = sync_rrd(db, ScriptSource(runner), days=90)

    core = next(l for l in db.scalars(select(m.Link)) if l.capacity_gbps == 400)
    samples = db.scalars(select(m.UtilizationSample).where(m.UtilizationSample.link_id == core.id)
                         .order_by(m.UtilizationSample.ts)).all()
    assert len(samples) == 90 and samples[0].in_gbps == 100 and samples[-1].in_gbps == 278
    assert core.current_gbps == 276  # last *complete* day, not today's partial bucket
    assert report["links_with_history"] == 1 and report["samples"] == 90
    assert report["links_without_port_map"] >= 1  # links whose ports have no ifIndex in the fixture
    assert len(runner.scripts) == 1


def test_sync_rrd_uses_b_side_swapped_and_counts_missing(synced, monkeypatch):
    db = synced
    monkeypatch.setattr(settings, "observium_rrd_path", "/rrd")
    # Drop the A-side mapping so the B side (SBY et-0/0/0, ifIndex 540) is used.
    core = next(l for l in db.scalars(select(m.Link)) if l.capacity_gbps == 400)
    a_ref = db.get(m.ObserviumPort, core.a_interface_id) or db.get(m.ObserviumPort, core.b_interface_id)
    jkt_ref = next(r for r in db.scalars(select(m.ObserviumPort)) if r.observium_hostname.startswith("jkt"))
    db.delete(jkt_ref)
    db.commit()
    start = int(datetime.now(timezone.utc).timestamp()) // DAY * DAY - 9 * DAY
    runner = FakeRunner({"/rrd/172.16.0.2/port-540.rrd": xport(start, [(1.25e9, 3.75e9)] * 10)})
    sync_rrd(db, ScriptSource(runner), days=10)
    s = db.scalars(select(m.UtilizationSample).where(m.UtilizationSample.link_id == core.id)).all()
    assert {(x.in_gbps, x.out_gbps) for x in s} == {(30.0, 10.0)}  # B's out is the link's in
    assert a_ref is not None

    missing = sync_rrd(db, ScriptSource(FakeRunner({})), days=10)
    assert missing["missing_files"] == 1


def test_rrd_requires_access_settings(monkeypatch):
    monkeypatch.setattr(settings, "observium_rrd_source", "ssh")
    monkeypatch.setattr(settings, "observium_ssh_user", None)
    with pytest.raises(RrdError, match="not configured"):
        open_runner()


def test_growth_trends_7_30_90(db):
    from app.routers.capacity import growth_trends

    class S:
        def __init__(self, ts, v):
            self.ts, self.in_gbps, self.out_gbps = ts, v, v

    now = datetime.now(timezone.utc)
    # flat for 60 days, then +1 G/day for the last 30 days
    samples = [S(now - timedelta(days=89 - i), 100 + max(0, i - 59)) for i in range(90)]
    t = growth_trends(samples)
    assert t["7"] == pytest.approx(30.44, rel=0.01) and t["30"] == pytest.approx(30.44, rel=0.02)
    assert 0 < t["90"] < t["30"]  # the long window dilutes the recent ramp
