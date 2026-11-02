import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import models as m
from app.backup import BackupError, create_backup, list_backups, restore_backup
from app.collectors import jobs
from app.collectors.observium.client import ObserviumClient
from app.config import settings
from app.database import Base, get_db
from app.main import app

from .conftest import observium_transport


@pytest.fixture
def file_db(tmp_path, monkeypatch):
    """A real SQLite file (backups need one) with backups going to tmp_path/backups."""
    monkeypatch.setattr(settings, "backup_dir", str(tmp_path / "backups"))
    monkeypatch.setattr(settings, "backup_keep", 15)
    monkeypatch.setattr(settings, "backup_before_sync", True)
    engine = create_engine(f"sqlite:///{tmp_path / 'nadien.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    yield session
    session.close()
    engine.dispose()


def client() -> ObserviumClient:
    return ObserviumClient(base_url="http://observium.test", token="t", transport=observium_transport())


def test_retention_keeps_newest_15(file_db):
    engine = file_db.get_bind()
    created = [create_backup(engine, "observium").name for _ in range(17)]
    remaining = [b.name for b in list_backups(engine)]
    assert len(remaining) == 15
    assert remaining[0] == created[-1]  # newest first
    assert created[0] not in remaining and created[1] not in remaining
    assert set(created[2:]) == set(remaining)


def test_backup_and_restore_roundtrip(file_db):
    engine = file_db.get_bind()
    file_db.add(m.Pop(code="JKT", name="Jakarta"))
    file_db.commit()
    snap = create_backup(engine, "before-change")

    file_db.add(m.Pop(code="SBY", name="Surabaya"))
    file_db.commit()
    restore_backup(snap.name, engine)

    file_db.expire_all()
    assert [p.code for p in file_db.scalars(select(m.Pop))] == ["JKT"]
    reasons = [b.reason for b in list_backups(engine)]
    assert "pre-restore" in reasons  # state just before the restore is kept


def test_restore_unknown_backup(file_db):
    with pytest.raises(BackupError, match="not found"):
        restore_backup("nadien-20000101-000000-x.db", file_db.get_bind())


def test_observium_job_backs_up_before_sync(file_db):
    report = jobs.run_observium(file_db, {"inventory", "neighbours", "utilization"}, client=client())
    assert report["backup"]["status"] == "ok" and report["backup"]["reason"] == "observium"
    assert report["counts"]["devices_created"] == 4
    backups = list_backups(file_db.get_bind())
    assert len(backups) == 1 and backups[0].size_bytes > 0


def test_utilization_only_skips_backup(file_db):
    jobs.run_observium(file_db, {"inventory"}, client=client())
    report = jobs.run_observium(file_db, {"utilization"}, client=client())
    assert report["backup"]["status"] == "skipped"
    assert len(list_backups(file_db.get_bind())) == 1


def test_failed_backup_aborts_sync(file_db, tmp_path, monkeypatch):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    monkeypatch.setattr(settings, "backup_dir", str(blocker / "backups"))  # mkdir under a file fails
    with pytest.raises(jobs.JobError, match="sync not started"):
        jobs.run_observium(file_db, {"inventory"}, client=client())
    assert file_db.scalars(select(m.Device)).all() == []


def test_backup_can_be_disabled(file_db, monkeypatch):
    monkeypatch.setattr(settings, "backup_before_sync", False)
    assert jobs.run_observium(file_db, {"inventory"}, client=client())["backup"]["status"] == "disabled"


def test_in_memory_db_is_skipped(db):
    assert jobs.backup_before(db, "observium")["status"] == "skipped"


def test_backups_api(file_db):
    app.dependency_overrides[get_db] = lambda: file_db
    try:
        api = TestClient(app)
        created = api.post("/api/backups")
        listing = api.get("/api/backups").json()
    finally:
        app.dependency_overrides.clear()
    assert created.status_code == 201 and created.json()["reason"] == "manual"
    assert listing["keep"] == 15 and listing["supported"] is True
    assert [b["name"] for b in listing["backups"]] == [created.json()["name"]]
