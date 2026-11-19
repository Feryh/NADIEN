"""Port capacity groups ports by the port type in their name, not the speed the link runs at."""

from app import models as m
from app.services.areas import port_bucket
from tests.test_analysis import net  # noqa: F401  (shared inventory with Observium ports)


def test_should_count_a_gigabit_port_as_1g_when_it_links_at_100m():
    assert port_bucket("GigabitEthernet0/0/4", 0.1) == "1G"


def test_should_count_a_10g_port_as_10g_when_it_carries_a_1g_optic():
    assert port_bucket("XGigabitEthernet0/0/3", 1.0) == "10G"


def test_should_keep_the_reported_speed_when_the_name_has_no_port_type():
    assert port_bucket("Ethernet1/1", 25.0) == "25G"


def _add_port(db, device: m.Device, name: str, speed: float, admin: str, oper: str) -> None:
    port = m.Interface(device_id=device.id, name=name, speed_gbps=speed, admin_status=admin, oper_status=oper,
                       if_type="physical")
    db.add(port)
    db.flush()
    db.add(m.ObserviumPort(interface_id=port.id, observium_port_id=port.id, observium_hostname=device.hostname))
    db.commit()


def test_should_list_the_shutdown_10g_port_with_a_1g_optic_under_10g_idle(net, db):  # noqa: F811
    api, devices = net
    me = devices["JKT-CSF-ME1-HW"]
    _add_port(db, me, "XGigabitEthernet0/0/3", 1.0, "down", "down")
    row = next(d for d in api.get("/api/inventory/ports/devices").json()["devices"] if d["device_id"] == me.id)
    ports = api.get(f"/api/inventory/ports/devices/{me.id}/ports", params={"speed": "10G", "state": "idle"}).json()
    assert row["speeds"]["10G"]["available"] == 1 and [p["name"] for p in ports] == ["XGigabitEthernet0/0/3"]


def test_should_keep_the_reported_10g_when_a_huawei_ne_names_a_10g_port_gigabitethernet():
    assert port_bucket("GigabitEthernet0/1/0", 10.0) == "10G"


def test_should_use_the_port_type_when_the_reported_speed_is_an_snmp_overflow():
    assert port_bucket("GigabitEthernet0/0/1", 4294.967295) == "1G"
