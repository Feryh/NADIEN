import csv

from app import models as m
from app.area_mapping import DEVICES_FILE, POPS_FILE, export, import_


def _seed(db):
    un = m.Pop(code="UNASSIGNED", name="UNASSIGNED")
    nap = m.Pop(code="NAP", name="NAP")
    jkt = m.Pop(code="JKT", name="Jakarta")
    db.add_all([un, nap, jkt])
    db.flush()
    db.add_all([
        m.Device(hostname="CGW-NAP-MDN-NDC", pop_id=nap.id),
        m.Device(hostname="CORE-NAP-JKT-BSD", pop_id=nap.id),
        m.Device(hostname="JKT-ABW-OLT1-HW", pop_id=jkt.id),
        m.Device(hostname="OLT-NGAMPILAN", pop_id=un.id),
        m.Device(hostname="PE1-ATN910-KEBUMEN", pop_id=un.id),
    ])
    db.commit()


def _read(path):
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.reader(fh, delimiter=";"))


def test_export_lists_only_unmapped_with_hints(db, tmp_path):
    _seed(db)
    pops_path, devices_path, n_pops, n_devs = export(db, tmp_path)
    rows = _read(pops_path)
    assert rows[0] == ["kode", "nama", "perangkat", "contoh_hostname", "petunjuk", "area"]
    assert n_pops == 1 and rows[1][0] == "NAP" and rows[1][2] == "2"
    assert "Sumatera 1" in rows[1][4] and "Jabodetabek 1" in rows[1][4]  # mixed: a hint, not a guess
    dev = _read(devices_path)
    assert n_devs == 2
    assert dev[1][0] == "OLT-NGAMPILAN" and dev[2][3] == "kota Kebumen (KBM)"


def test_import_applies_filled_rows_and_skips_blanks(db, tmp_path):
    _seed(db)
    export(db, tmp_path)
    # Fill as a user would in Excel: area for NAP, POP code for one device, leave the other blank.
    pops = _read(tmp_path / POPS_FILE)
    pops[1][5] = "Jabodetabek"
    with (tmp_path / POPS_FILE).open("w", encoding="utf-8-sig", newline="") as fh:
        csv.writer(fh, delimiter=";").writerows(pops)
    devs = _read(tmp_path / DEVICES_FILE)
    devs[1][4] = "YGY"  # OLT-NGAMPILAN is in Yogyakarta
    with (tmp_path / DEVICES_FILE).open("w", encoding="utf-8-sig", newline="") as fh:
        csv.writer(fh, delimiter=";").writerows(devs)

    report = import_(db, tmp_path)
    assert report["pops_updated"] == ["NAP -> Jabodetabek"]
    assert report["devices_moved"] == ["OLT-NGAMPILAN -> YGY"] and report["pops_created"] == ["YGY"]
    ygy = db.query(m.Pop).filter_by(code="YGY").one()
    assert ygy.name == "Yogyakarta" and ygy.region == "Jawa Tengah & DIY"
    kebumen = db.query(m.Device).filter_by(hostname="PE1-ATN910-KEBUMEN").one()
    assert db.get(m.Pop, kebumen.pop_id).code == "UNASSIGNED"  # left blank -> untouched


def test_import_warns_on_unknown_rows(db, tmp_path):
    _seed(db)
    (tmp_path / POPS_FILE).write_text("kode;area\nZZZ;Jabodetabek\nNAP;Priangan Timur\n", encoding="utf-8-sig")
    report = import_(db, tmp_path)
    assert "POP ZZZ tidak ditemukan — dilewati" in report["warnings"]
    assert any("area baru 'Priangan Timur'" in w for w in report["warnings"])
    assert db.query(m.Pop).filter_by(code="NAP").one().region == "Priangan Timur"
