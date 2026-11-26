"""Resource registry: VLAN / VC id / IPv4 usage, next free values, reservations and IP pools."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import models as m
from app.database import get_db
from app.main import app
from app.services import resources as res
from app.services.configgen import Addressing, generate
from tests.conftest import config_text
from tests.test_configgen import make_service  # noqa: F401  (fixture)
from tests.test_validation import net  # noqa: F401  (shared A — P — B network fixture)

VRP_VSI = config_text("l2vpn/vrp_vsi.cfg")  # peers 10.20.0.52 (vsi-id 12490, 12491) and 10.0.0.1 (12491)


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def pes(db, net):  # noqa: F811
    a, b = net["dev"]["A-PE1"], net["dev"]["B-PE1"]
    a.loopback_ipv4, b.loopback_ipv4 = "10.20.0.1", "10.20.0.52"
    db.add_all([m.Interface(device_id=a.id, name="Eth-Trunk1.300", if_type="subinterface", ipv4_address="10.30.0.1/30"),
                m.Interface(device_id=a.id, name="GigabitEthernet0/1/1.2", if_type="subinterface")])
    db.commit()
    return a, b


def _reserve(db, *requests: res.Request, label: str = "PT CONTOH") -> list[m.ResourceReservation]:
    return res.reserve(db, list(requests), label, "tester")


# --------------------------------------------------------------------------- VLAN


def test_should_list_vlans_of_sub_interfaces_and_reservations_when_reading_a_device(db, pes):
    a, _ = pes
    _reserve(db, res.Request(kind="vlan", device_id=a.id, value=1480))
    assert sorted(res.vlans_in_use(db, a.id)) == [2, 300, 1480]


def test_should_skip_vlans_taken_on_either_pe_when_suggesting_the_next_free(db, pes):
    a, b = pes
    _reserve(db, res.Request(kind="vlan", device_id=b.id, value=3))
    assert res.next_free_vlans(db, [a.id, b.id], count=3) == [4, 5, 6]


def test_should_refuse_a_vlan_already_on_a_sub_interface(db, pes):
    a, _ = pes
    with pytest.raises(res.ResourceConflict, match="Eth-Trunk1.300"):
        _reserve(db, res.Request(kind="vlan", device_id=a.id, value=300))


def test_should_reserve_nothing_when_one_value_of_the_batch_is_taken(db, pes):
    a, _ = pes
    with pytest.raises(res.ResourceConflict):
        _reserve(db, res.Request(kind="vlan", device_id=a.id, value=301), res.Request(kind="vlan", device_id=a.id, value=300))
    assert db.query(m.ResourceReservation).count() == 0


# --------------------------------------------------------------------------- VC id


def test_should_read_vc_ids_from_the_configs_of_both_pes_towards_each_other(db, pes):
    a, b = pes
    res.record_pseudowires(db, b.id, VRP_VSI.replace("peer 10.0.0.1", "peer 10.20.0.1"), "vrp")
    db.commit()
    assert set(res.vc_ids_in_use(db, a.id, b.id)) == {12491}


def test_should_suggest_the_vlan_as_vc_id_unless_taken_between_the_pair(db, pes):
    a, b = pes
    _reserve(db, res.Request(kind="vc_id", device_id=b.id, peer_device_id=a.id, value=1480))
    assert res.next_free_vc_ids(db, a.id, b.id, [1480, 1481]) == {1480: 1482, 1481: 1481}


def test_should_store_a_vc_id_pair_the_same_way_whichever_end_reserved_it(db, pes):
    a, b = pes
    _reserve(db, res.Request(kind="vc_id", device_id=b.id, peer_device_id=a.id, value=77))
    with pytest.raises(res.ResourceConflict):
        _reserve(db, res.Request(kind="vc_id", device_id=a.id, peer_device_id=b.id, value=77))


def test_should_replace_the_pseudowires_of_a_device_when_its_config_is_read_again(db, pes):
    _, b = pes
    res.record_pseudowires(db, b.id, VRP_VSI, "vrp")
    padded = "#\n" * res.MIN_CONFIG_LINES  # long enough to count as a real config
    res.record_pseudowires(db, b.id, padded + "vsi X\n pwsignal ldp\n  vsi-id 5\n  peer 10.9.9.9\n#\n", "vrp")
    db.commit()
    assert [(p.peer_ip, p.vc_id) for p in db.query(m.ConfigPseudowire).filter_by(device_id=b.id)] == [("10.9.9.9", 5)]


# --------------------------------------------------------------------------- settling


def test_should_mark_a_reservation_fulfilled_when_the_vlan_shows_up_on_the_device(db, pes):
    a, _ = pes
    row = _reserve(db, res.Request(kind="vlan", device_id=a.id, value=1490))[0]
    db.add(m.Interface(device_id=a.id, name="Eth-Trunk1.1490", if_type="subinterface"))
    db.commit()
    res.settle(db)
    assert db.get(m.ResourceReservation, row.id).status == "fulfilled"


def test_should_stop_blocking_a_vlan_when_its_reservation_expired(db, pes):
    a, _ = pes
    row = _reserve(db, res.Request(kind="vlan", device_id=a.id, value=1490))[0]
    row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db.commit()
    res.settle(db)
    assert 1490 not in res.vlans_in_use(db, a.id)


# --------------------------------------------------------------------------- IPv4


def test_should_hand_out_the_first_prefix_not_used_by_an_interface_or_reservation(db, pes):
    pool = m.IpPool(name="P2P JKT", cidr="10.30.0.0/24", purpose="p2p", default_prefix_len=30)
    db.add(pool)
    db.commit()
    _reserve(db, res.Request(kind="ipv4", prefix="10.30.0.4/30", pool_id=pool.id))
    assert str(res.next_free_prefix(db, pool, 30)) == "10.30.0.8/30"


def test_should_refuse_a_prefix_overlapping_an_interface_address(db, pes):
    with pytest.raises(res.ResourceConflict, match="A-PE1 Eth-Trunk1.300"):
        _reserve(db, res.Request(kind="ipv4", prefix="10.30.0.0/29"))


def test_should_count_the_addresses_used_in_a_pool(db, pes):
    pool = m.IpPool(name="P2P", cidr="10.30.0.0/28", purpose="p2p")
    db.add(pool)
    db.commit()
    assert res.pool_view(db, pool)["used_addresses"] == 4  # the /30 of Eth-Trunk1.300


# --------------------------------------------------------------------------- API


def test_should_allocate_the_next_free_prefix_from_a_pool_through_the_api(client, pes):
    pool = client.post("/api/resources/ip-pools", json={"name": "P2P JKT", "cidr": "10.30.0.0/24", "purpose": "p2p",
                                                        "default_prefix_len": 31}).json()
    first = client.post(f"/api/resources/ip-pools/{pool['id']}/allocate", json={"label": "link A-B"}).json()
    second = client.post(f"/api/resources/ip-pools/{pool['id']}/allocate", json={"label": "link A-C"}).json()
    assert (first["prefix"], second["prefix"]) == ("10.30.0.4/31", "10.30.0.6/31")


def test_should_refuse_a_pool_overlapping_another_pool(client, pes):
    client.post("/api/resources/ip-pools", json={"name": "A", "cidr": "10.40.0.0/22"})
    assert client.post("/api/resources/ip-pools", json={"name": "B", "cidr": "10.40.2.0/24"}).status_code == 409


def test_should_answer_409_with_the_holder_when_reserving_a_taken_vlan(client, pes):
    a, _ = pes
    response = client.post("/api/resources/reservations", json={
        "label": "X", "items": [{"kind": "vlan", "device_id": a.id, "value": 300}]})
    assert response.status_code == 409 and "Eth-Trunk1.300" in response.json()["detail"]


def test_should_release_a_reservation_and_free_its_vlan(client, db, pes):
    a, _ = pes
    created = client.post("/api/resources/reservations", json={
        "label": "X", "items": [{"kind": "vlan", "device_id": a.id, "value": 1500}]}).json()["reservations"][0]
    client.post(f"/api/resources/reservations/{created['id']}/release")
    assert 1500 not in res.vlans_in_use(db, a.id)


def test_should_suggest_vc_ids_per_wanted_vlan_through_the_api(client, pes):
    a, b = pes
    body = client.get("/api/resources/vc-ids", params={"device_id": a.id, "peer_device_id": b.id,
                                                       "wanted": [1480, 1481]}).json()
    assert body["suggested"] == [{"wanted": 1480, "vc_id": 1480}, {"wanted": 1481, "vc_id": 1481}]


# --------------------------------------------------------------------------- config generator


def test_should_warn_in_the_generator_when_the_vlan_is_reserved_in_the_registry(db, make_service, net):  # noqa: F811
    service = make_service("vrp", "l2vpn", vlan_id=1490)
    res.reserve(db, [res.Request(kind="vlan", device_id=service.a_device_id, value=1490)], "PT LAIN", "tester")
    a = generate(db, service, Addressing(vlans=[1490]), config_source=lambda device_id: "")["endpoints"]["A"]
    assert any("VLAN 1490 direservasi untuk PT LAIN" in w for w in a["warnings"])


def test_should_keep_the_recorded_pseudowires_when_the_rancid_backup_is_empty(db, pes):
    _, b = pes
    res.record_pseudowires(db, b.id, VRP_VSI, "vrp")
    res.record_pseudowires(db, b.id, "#RANCID-CONTENT-TYPE: vrp\n#\n", "vrp")
    db.commit()
    assert db.query(m.ConfigPseudowire).filter_by(device_id=b.id).count() == 4


def test_should_count_only_the_address_of_an_interface_with_a_mask_wider_than_24(db, pes):
    a, _ = pes
    db.add(m.Interface(device_id=a.id, name="meth0", ipv4_address="10.40.7.9/8"))
    pool = m.IpPool(name="P2P 10.40", cidr="10.40.0.0/24", purpose="p2p")
    db.add(pool)
    db.commit()
    assert str(res.next_free_prefix(db, pool, 30)) == "10.40.0.0/30"
