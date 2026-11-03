"""Config generator (Phase 7): service config and rollback per vendor dialect."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import models as m
from app.database import get_db
from app.main import app
from app.services.configgen import AddressError, Addressing, generate, parse_vlans
from app.services.mop import MopMeta, build_mop
from app.services.running_config import RunningConfigError
from tests.test_validation import net  # noqa: F401  (shared A — P — B network fixture)

VENDORS = {"vrp": ("Huawei", "VRP"), "iosxr": ("Cisco", "IOS-XR"), "zte": ("ZTE", "ZXROS"), "fiberhome": ("FiberHome", None)}


def _vendor(db, dialect: str) -> m.Vendor:
    name, os_family = VENDORS[dialect]
    vendor = db.scalar(select(m.Vendor).where(m.Vendor.name == name))
    if vendor is None:
        vendor = m.Vendor(name=name, os_family=os_family)
        db.add(vendor)
        db.flush()
    return vendor


@pytest.fixture
def make_service(db, net):  # noqa: F811
    db.add(m.Customer(id=1, name="PT Contoh", account_no="A1"))
    db.flush()

    def make(dialect: str = "vrp", service_type: str = "l2vpn", **kw) -> m.Service:
        vendor = _vendor(db, dialect)
        for name in ("A-PE1", "B-PE1"):
            net["dev"][name].vendor_id = vendor.id
        fields = dict(customer_id=1, circuit_id=f"C-{service_type}", service_type=service_type, bandwidth_mbps=1000,
                      vlan_id=300, a_device_id=net["dev"]["A-PE1"].id, a_interface="ge-0/0/1",
                      b_device_id=net["dev"]["B-PE1"].id, b_interface="ge-0/0/1", status="reserved")
        if service_type == "l3vpn":
            fields.update(vrf_name="CUST", route_distinguisher="65000:42", route_target="65000:42")
        service = m.Service(**{**fields, **kw})
        db.add(service)
        db.commit()
        return service

    return make


def test_should_build_ldp_vsi_bound_to_sub_interface_when_service_is_l2vpn_on_vrp(db, make_service):
    service = make_service("vrp", "l2vpn")
    config = generate(db, service)["endpoints"]["A"]["config"]
    assert ["  vsi-id 300", "  control-word enable", "  peer 10.0.0.1"] == config[3:6]
    assert " l2 binding vsi PT-CONTOH_300" in config


def test_should_put_vlan_under_vlan_configuration_when_device_is_zte(db, make_service):
    config = generate(db, make_service("zte", "l2vpn"))["endpoints"]["A"]["config"]
    start = config.index("vlan-configuration")
    assert config[start + 1:start + 3] == ["  interface ge-0/0/1.300", "    encapsulation-dot1q 300"]


def test_should_write_interface_name_with_space_when_device_is_fiberhome(db, make_service, net):  # noqa: F811
    service = make_service("fiberhome", "l2vpn", a_interface="lag11")
    assert "interface lag 11.300" in generate(db, service)["endpoints"]["A"]["config"]


def test_should_skip_l3_service_when_device_is_fiberhome(db, make_service):
    a = generate(db, make_service("fiberhome", "l3vpn"))["endpoints"]["A"]
    assert a["config"] == [] and "hanya membawa L2" in a["warnings"][0]


def test_should_use_given_address_and_asn_from_rd_when_service_is_l3vpn_on_iosxr(db, make_service):
    service = make_service("iosxr", "l3vpn")
    config = generate(db, service, Addressing(a_ip="10.10.10.1/30"))["endpoints"]["A"]["config"]
    assert " ipv4 address 10.10.10.1 255.255.255.252" in config and "router bgp 65000" in config


def test_should_prefer_given_asn_over_rd_when_asn_is_supplied(db, make_service):
    config = generate(db, make_service("vrp", "l3vpn"), Addressing(bgp_asn=46038))["endpoints"]["A"]["config"]
    assert "bgp 46038" in config


def test_should_leave_vrf_alone_when_another_service_already_uses_it_on_the_device(db, make_service):
    make_service("vrp", "l3vpn", circuit_id="OLD-1", status="active")
    service = make_service("vrp", "l3vpn", circuit_id="NEW-1", vlan_id=301)
    a = generate(db, service)["endpoints"]["A"]
    assert not any(line.startswith("ip vpn-instance") for line in a["config"])
    assert not any("undo ip vpn-instance" in line for line in a["rollback"])


SUPPORTED = [(d, t) for d in VENDORS for t in ("l2vpn", "iplc", "l3vpn", "internet")
             if d != "fiberhome" or t in ("l2vpn", "iplc")]


@pytest.mark.parametrize("dialect, service_type", SUPPORTED)
def test_should_configure_and_roll_back_sub_interface_for_every_supported_vendor_and_service(db, make_service, dialect,
                                                                                           service_type):
    a = generate(db, make_service(dialect, service_type))["endpoints"]["A"]
    assert any("ge-0/0/1.300" in line for line in a["config"])
    assert any("ge-0/0/1.300" in line for line in a["rollback"])


def test_should_skip_generation_when_vendor_is_unsupported(db, make_service, net):  # noqa: F811
    service = make_service("vrp")
    other = m.Vendor(name="Juniper", os_family="Junos")
    db.add(other)
    db.flush()
    net["dev"]["B-PE1"].vendor_id = other.id
    db.commit()
    b = generate(db, service)["endpoints"]["B"]
    assert b["config"] == [] and b["warnings"]


def test_should_put_generated_config_into_mop_execution(db, make_service):
    service = make_service("vrp", "l2vpn")
    execution = next(s for s in build_mop(db, service, MopMeta())["sections"] if s["title"].startswith("8."))
    assert "interface ge-0/0/1.300" in execution["steps"][0]["commands"]


def test_should_not_reuse_service_commit_label_when_rolling_back_on_iosxr(db, make_service):
    service = make_service("iosxr", "l2vpn")
    rollback = next(s for s in build_mop(db, service, MopMeta())["sections"] if s["title"].startswith("10."))
    step = rollback["steps"][0]
    assert not any(c.startswith("commit label") for c in step["commands"])
    assert "rollback configuration to C-l2vpn" not in step["note"]


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_should_reject_address_when_it_is_not_cidr(client, make_service):
    service = make_service("vrp", "internet")
    assert client.get(f"/api/configs/services/{service.id}", params={"a_ip": "10.0.0.300/30"}).status_code == 422


def test_should_download_one_site_named_after_circuit_and_device(client, make_service):
    service = make_service("vrp", "l2vpn", circuit_id="L2/JKT-1")
    res = client.get(f"/api/configs/services/{service.id}/text", params={"side": "B", "part": "rollback"})
    assert 'filename="rollback-L2_JKT-1-B-PE1.txt"' in res.headers["content-disposition"]
    assert res.text.splitlines()[-2:] == ["commit", "return"]


@pytest.mark.parametrize("dialect, service_type", SUPPORTED)
def test_should_emit_no_comment_or_write_lines_in_generated_script(db, make_service, dialect, service_type):
    a = generate(db, make_service(dialect, service_type), Addressing(policer=True))["endpoints"]["A"]
    lines = [line.strip() for line in a["config"] + a["rollback"]]
    assert not [line for line in lines if line.startswith(("#", "!")) or line.split()[0] in ("save", "write")]


def test_should_leave_out_policer_unless_requested(db, make_service):
    service = make_service("vrp", "l2vpn")
    assert not any("qos car" in line for line in generate(db, service)["endpoints"]["A"]["config"])
    assert any("qos car" in line for line in generate(db, service, Addressing(policer=True))["endpoints"]["A"]["config"])


def test_should_warn_when_vlan_vc_id_is_already_used_between_same_pes(db, make_service):
    make_service("vrp", "l2vpn", circuit_id="OLD-1", status="active")
    service = make_service("vrp", "l2vpn", circuit_id="NEW-1", a_interface="ge-0/0/2", b_interface="ge-0/0/2")
    assert any("OLD-1" in w for w in generate(db, service)["endpoints"]["A"]["warnings"])


def test_should_expand_ranges_when_parsing_vlan_list():
    assert parse_vlans("1480-1482, 1489") == [1480, 1481, 1482, 1489]


@pytest.mark.parametrize("text", ["1480-", "0", "4095", "1-100"])
def test_should_reject_vlan_list_when_out_of_range_or_too_long(text):
    with pytest.raises(AddressError):
        parse_vlans(text)


def test_should_build_one_instance_per_vlan_when_vpls_batch_is_requested(db, make_service):
    config = generate(db, make_service("vrp", "l2vpn"), Addressing(vlans=[1480, 1481, 1482]))["endpoints"]["A"]["config"]
    assert config.count("system-view") == 1
    assert [line for line in config if line.startswith("vsi ")] == [f"vsi PT-CONTOH_{v}" for v in (1480, 1481, 1482)]


def test_should_define_policy_map_once_when_iosxr_batch_is_policed(db, make_service):
    a = generate(db, make_service("iosxr", "l2vpn"), Addressing(vlans=[10, 11], policer=True))["endpoints"]["A"]
    assert sum(line.startswith("policy-map") for line in a["config"]) == 1


ZTE_RUNNING = """pw pw1
pw pw2
vpls OXY_1480
  pseudo-wire pw4 split-horizon scope B
    neighbour 10.0.0.1 vcid 1481 cw-preferred
    $
  $
$
"""


def test_should_allocate_lowest_free_pw_per_vlan_when_device_is_zte(db, make_service):
    a = generate(db, make_service("zte", "l2vpn"), Addressing(vlans=[1490, 1491]),
                 config_source=lambda device_id: ZTE_RUNNING)["endpoints"]["A"]
    assert [line for line in a["config"] if line.startswith("pw ")] == ["pw pw3", "pw pw5"]
    assert "VLAN 1490 → pw3, VLAN 1491 → pw5" in a["notes"][0]


def test_should_warn_when_vcid_to_same_neighbour_already_exists_on_zte(db, make_service):
    a = generate(db, make_service("zte", "l2vpn"), Addressing(vlans=[1481]),
                 config_source=lambda device_id: ZTE_RUNNING)["endpoints"]["A"]
    assert any("vcid 1481" in w for w in a["warnings"])


def test_should_fall_back_to_vc_named_pw_when_running_config_is_unavailable(db, make_service):
    def unavailable(device_id: int) -> str:
        raise RunningConfigError("Config perangkat belum ada di backup RANCID.")

    a = generate(db, make_service("zte", "l2vpn"), config_source=unavailable)["endpoints"]["A"]
    assert "pw pw300" in a["config"] and any("belum ada di backup RANCID" in w for w in a["warnings"])


def test_should_reject_bad_vlan_list_via_api(client, make_service):
    service = make_service("vrp", "l2vpn")
    assert client.get(f"/api/configs/services/{service.id}", params={"vlans": "10-5000"}).status_code == 422
