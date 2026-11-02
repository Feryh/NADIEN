from app.collectors.observium.parsers import (
    classify_port,
    guess_role,
    parse_address,
    parse_device,
    parse_neighbour,
    parse_port,
)

POP_RE = r"^([A-Za-z]{3,4})[-_.]"


def test_device_uses_sysname_and_maps_vendor():
    rec = parse_device({"device_id": "1", "hostname": "10.0.0.9", "sysName": "jkt-pe-02.metro.net", "os": "iosxr",
                        "hardware": "NCS-5508", "version": "7.9.2", "location": "Jakarta", "location_lat": "-6.2",
                        "location_lon": "106.8", "status": "1", "disabled": "0"}, POP_RE)
    assert rec.hostname == "JKT-PE-02"
    assert (rec.vendor, rec.os_family, rec.model, rec.software) == ("Cisco", "IOS-XR", "NCS-5508", "7.9.2")
    assert rec.pop_code == "JKT" and rec.latitude == -6.2 and rec.role == "pe" and rec.status == "active"


def test_device_falls_back_to_hostname_when_sysname_is_ip():
    rec = parse_device({"device_id": "2", "hostname": "sby-agg-01.metro.net", "sysName": "10.9.9.9", "os": "vrp"}, POP_RE)
    assert rec.hostname == "SBY-AGG-01" and rec.vendor == "Huawei" and rec.role == "agg"


def test_device_status_and_unknown_os():
    rec = parse_device({"device_id": "3", "sysName": "X1", "os": "edgeos", "vendor": "Ubiquiti", "status": "0"}, POP_RE)
    assert rec.vendor == "Ubiquiti" and rec.status == "maintenance" and rec.pop_code is None
    assert parse_device({"device_id": "4", "sysName": "Y", "disabled": "1"}, POP_RE).status == "decommissioned"


def test_roles():
    assert guess_role("JKT-P-01") == "p"
    assert guess_role("JKT-PE-01") == "pe"
    assert guess_role("BDG-AGG-01") == "agg"
    assert guess_role("JKT-DCI-01") == "dci"
    assert guess_role("JKT-CORE1") == "p"


def test_port_speed_rates_and_types():
    rec = parse_port({"port_id": "1", "device_id": "1", "ifName": "HundredGigE0/0/0/0", "ifType": "ethernetCsmacd",
                      "ifHighSpeed": "400000", "ifSpeed": "4294967295", "ifMtu": "9216", "ifAdminStatus": "up",
                      "ifOperStatus": "down", "ifAlias": " TO-X ", "ifInOctets_rate": "31250000000",
                      "ifOutOctets_rate": "", "deleted": "0"})
    assert rec.speed_gbps == 400 and rec.mtu == 9216 and rec.description == "TO-X"
    assert (rec.admin_status, rec.oper_status) == ("up", "down")
    assert rec.in_gbps == 250 and rec.out_gbps is None
    assert parse_port({"port_id": "2", "ifName": "Loopback0"}) is None
    assert parse_port({"port_id": "3", "ifName": "et-0/0/1", "deleted": "1"}) is None
    assert parse_port({"port_id": "4", "ifName": "ge-0/0/1", "ifSpeed": "1000000000"}).speed_gbps == 1


def test_classify_port():
    assert classify_port("Bundle-Ether10", "ieee8023adLag") == "bundle"
    assert classify_port("ae30", "propVirtual") == "bundle"
    assert classify_port("Eth-Trunk10", "") == "bundle"
    assert classify_port("lag-20", "") == "bundle"
    assert classify_port("et-0/0/0.0", "") == "subinterface"
    assert classify_port("1/1/c1/1:100", "") == "subinterface"
    assert classify_port("100GE1/0/1", "ethernetCsmacd") == "physical"


def test_neighbours_lldp_only_by_default():
    lldp = {"device_id": "1", "port_id": "9", "protocol": "lldp", "remote_hostname": "B", "remote_port": "x", "active": "1"}
    assert parse_neighbour(lldp).remote_system_name == "B"
    assert parse_neighbour({**lldp, "protocol": "cdp"}) is None
    assert parse_neighbour({**lldp, "protocol": "cdp"}, include_cdp=True) is not None
    assert parse_neighbour({**lldp, "active": "0"}) is None
    assert parse_neighbour({**lldp, "remote_port": ""}) is None


def test_address():
    assert parse_address({"port_id": "1", "ipv4_address": "10.0.0.1", "ipv4_prefixlen": "30"}).cidr == "10.0.0.1/30"
    assert parse_address({"port_id": "1", "ipv4_address": "10.0.0.1", "ipv4_network": "10.0.0.0/31"}).cidr == "10.0.0.1/31"
    assert parse_address({"port_id": None, "ipv4_address": "10.0.0.1"}) is None
