"""gitweb RANCID collector: all repos, CSR devices created from configs, incremental by blob hash."""

import httpx
import pytest
from sqlalchemy import select

from app import models as m
from app.collectors import jobs
from app.collectors.configs.gitweb import GitwebClient, parse_router_db, role_for_repo

from .conftest import config_text

PEER = """!RANCID-CONTENT-TYPE: zte
hostname BAL-BA-BLI-0005-ME1-ZTE
interface xgei-1/1/0/10
  description to-BAL-BA-BLI-0004-ME1-ZTE:xgei-1/1/0/9|ONNET
"""
OLT = "!RANCID-CONTENT-TYPE: huaweiolt\n sysname DPS-KTU-OLT1-HW\ninterface gpon 0/1\n"


class FakeGitweb:
    """Serves project_index, tree, router.db and blob_plain the way gitweb does."""

    def __init__(self):
        self.repos = {
            "CSR-METRO-ZTE/.git": {"bal-ba-bli-0004-me1-zte": (config_text("zte.cfg"), "a" * 40),
                                   "bal-ba-bli-0005-me1-zte": (PEER, "b" * 40)},
            "OLT-HUAWEI/.git": {"dps-ktu-olt1-hw": (OLT, "c" * 40)},
        }
        self.blob_requests: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        q = dict(request.url.params)
        if q.get("a") == "project_index":
            return httpx.Response(200, text="\n".join([*self.repos, ".git"]) + "\n")
        files = self.repos.get(q.get("p", ""), {})
        if q.get("a") == "tree":
            links = "".join(f'<a href="/gitweb?p={q["p"]};a=blob;f=configs/{n};h={sha};hb=HEAD">{n}</a>'
                            for n, (_, sha) in files.items())
            return httpx.Response(200, text=f"<html>{links}</html>")
        if q.get("a") == "blob_plain" and q.get("f") == "router.db":
            return httpx.Response(200, text="".join(f"{n.upper()};zte;up\n" for n in files))
        if q.get("a") == "blob_plain":
            name = q["f"].removeprefix("configs/")
            self.blob_requests.append(name)
            return httpx.Response(200, text=files[name][0])
        return httpx.Response(404)

    def client(self) -> GitwebClient:
        return GitwebClient(base_url="http://gitweb.test/gitweb/", transport=httpx.MockTransport(self.handler))


@pytest.fixture
def metro(db):
    """A metro box Observium knows; the CSR uplink names its port but it does not describe the CSR back."""
    pop = m.Pop(code="DPS", name="Denpasar")
    db.add(pop)
    db.flush()
    device = m.Device(hostname="DPS-KTU-ME1-FH820", pop_id=pop.id, role="agg")
    db.add(device)
    db.flush()
    db.add(m.Interface(device_id=device.id, name="eth-10gi 0/1/0/7", speed_gbps=10, oper_status="up"))
    db.commit()
    return device


def test_should_create_csr_devices_with_city_pop_when_only_gitweb_has_them(db, metro):
    report = jobs.run_gitweb(db, client=FakeGitweb().client())

    devices = {d.hostname: d for d in db.scalars(select(m.Device))}
    csr = devices["BAL-BA-BLI-0004-ME1-ZTE"]
    assert report["counts"]["devices_created"] == 2 and csr.role == "csr" and devices["BAL-BA-BLI-0005-ME1-ZTE"].role == "csr"
    assert db.get(m.Pop, csr.pop_id).code == "BLI"
    assert db.scalar(select(m.Vendor).where(m.Vendor.id == csr.vendor_id)).name == "ZTE"
    assert db.get(m.DeviceModel, csr.model_id).name == "ZXCTN 6120H-SC"
    assert report["unsupported_types"] == {"huaweiolt": 1}


def test_should_give_config_only_ports_speed_and_service_state_when_device_came_from_gitweb(db, metro):
    jobs.run_gitweb(db, client=FakeGitweb().client())
    device = db.scalar(select(m.Device).where(m.Device.hostname == "BAL-BA-BLI-0004-ME1-ZTE"))
    ports = {i.name: i for i in db.scalars(select(m.Interface).where(m.Interface.device_id == device.id))}
    assert ports["xgei-1/1/0/9"].speed_gbps == 10 and ports["xgei-1/1/0/9"].oper_status == "up"
    assert ports["smartgroup1"].if_type == "bundle"
    assert ports["xgei-1/1/0/11"].parent_id == ports["smartgroup1"].id


def test_should_link_csr_ring_and_one_sided_uplink_when_descriptions_name_the_port(db, metro):
    report = jobs.run_gitweb(db, client=FakeGitweb().client())
    names = {l.name for l in db.scalars(select(m.Link))}
    assert "BAL-BA-BLI-0004-ME1-ZTE:xgei-1/1/0/9 <> BAL-BA-BLI-0005-ME1-ZTE:xgei-1/1/0/10" in names
    assert "BAL-BA-BLI-0004-ME1-ZTE:xgei-1/1/0/10 <> DPS-KTU-ME1-FH820:eth-10gi 0/1/0/7" in names
    assert report["discovery"]["config_links"] >= 2


def test_should_link_csr_neighbour_when_description_omits_the_role_and_vendor_suffix(db, metro):
    fake = FakeGitweb()
    fake.repos["CSR-METRO-ZTE/.git"]["bal-ba-bli-0005-me1-zte"] = (PEER.replace(
        "to-BAL-BA-BLI-0004-ME1-ZTE:xgei-1/1/0/9", "to-EMR-00779|BAL-BA-BLI-0004:xgei-1/1/0/9"), "b" * 40)
    zte = config_text("zte.cfg").replace("to-BAL-BA-BLI-0005-ME1-ZTE:xgei-1/1/0/10", "BAL-BA-BLI-0005:xgei-1/1/0/10")
    fake.repos["CSR-METRO-ZTE/.git"]["bal-ba-bli-0004-me1-zte"] = (zte, "a" * 40)
    jobs.run_gitweb(db, client=fake.client())
    names = {l.name for l in db.scalars(select(m.Link))}
    assert "BAL-BA-BLI-0004-ME1-ZTE:xgei-1/1/0/9 <> BAL-BA-BLI-0005-ME1-ZTE:xgei-1/1/0/10" in names


def test_should_download_nothing_when_no_blob_changed_since_last_run(db, metro):
    fake = FakeGitweb()
    jobs.run_gitweb(db, client=fake.client())
    fake.blob_requests.clear()

    report = jobs.run_gitweb(db, client=fake.client())

    assert fake.blob_requests == []
    assert report["backup"]["detail"] == "no changes in gitweb" and report["counts"]["changed"] == 0


def test_should_fetch_only_the_changed_file_when_one_blob_changed(db, metro):
    fake = FakeGitweb()
    jobs.run_gitweb(db, client=fake.client())
    fake.blob_requests.clear()
    text, _ = fake.repos["CSR-METRO-ZTE/.git"]["bal-ba-bli-0005-me1-zte"]
    fake.repos["CSR-METRO-ZTE/.git"]["bal-ba-bli-0005-me1-zte"] = (text, "d" * 40)

    report = jobs.run_gitweb(db, client=fake.client())

    assert fake.blob_requests == ["bal-ba-bli-0005-me1-zte"] and report["counts"]["applied"] == 1


def test_should_promote_existing_device_to_csr_when_it_sits_in_a_csr_repo(db, metro):
    pop = db.scalar(select(m.Pop))
    db.add(m.Device(hostname="BAL-BA-BLI-0005-ME1-ZTE", pop_id=pop.id, role="agg"))
    db.commit()
    report = jobs.run_gitweb(db, client=FakeGitweb().client())
    assert report["counts"]["roles_set_csr"] == 1
    assert db.scalar(select(m.Device).where(m.Device.hostname == "BAL-BA-BLI-0005-ME1-ZTE")).role == "csr"


def test_should_only_fill_known_ports_when_device_is_monitored_by_observium(db, metro):
    fake = FakeGitweb()
    fake.repos["METRO-FIBERHOME/.git"] = {"dps-ktu-me1-fh820": ("""!RANCID-CONTENT-TYPE: fiberhomemetro
hostname DPS-KTU-ME1-FH820
interface eth-10gi 0/1/0/7
 description Downlink to BAL-BA-BLI-0004-ME1-ZTE
interface eth-10gi 0/1/0/8
 description port Observium does not report
interface vlan 100
 description customer vlan
""", "e" * 40)}
    jobs.run_gitweb(db, client=fake.client())
    ports = {i.name: i for i in db.scalars(select(m.Interface).where(m.Interface.device_id == metro.id))}
    assert set(ports) == {"eth-10gi 0/1/0/7"}  # no new physical port, no VLAN interface
    assert ports["eth-10gi 0/1/0/7"].description == "Downlink to BAL-BA-BLI-0004-ME1-ZTE"


def test_should_read_both_router_db_formats_and_repo_roles():
    assert parse_router_db("A;zte;up\n# c\nB:cisco:down\n") == {"a": ("zte", "up"), "b": ("cisco", "down")}
    assert role_for_repo("CSR-METRO-ZTE/.git") == "csr"
    assert role_for_repo("xMTI-METRO-HUAWEI/.git") == "agg"
    assert role_for_repo("OLT-ZTE/.git") == "olt"
