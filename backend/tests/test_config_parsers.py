import pytest

from app.collectors.configs import detect_vendor, parse_config

from .conftest import config_text


def test_iosxr():
    cfg = parse_config(config_text("iosxr.cfg"), "iosxr")
    assert cfg.hostname == "JKT-P-01"
    assert cfg.loopback_ipv4 == "10.0.0.1"
    assert cfg.isis_net == "49.0001.0100.0000.0001.00"
    assert cfg.srv6_locator == "fc00:0:1::/48"
    be = cfg.interfaces["Bundle-Ether10"]
    assert be.is_bundle and be.mtu == 9216 and be.ipv4 == "10.1.1.1/30" and be.description == "TO-BDG-PE-01"
    assert cfg.bundles() == {"Bundle-Ether10": ["HundredGigE0/0/0/1", "HundredGigE0/0/0/2"]}
    assert cfg.interfaces["HundredGigE0/0/0/0"].ipv4 == "10.1.0.1/30"
    assert cfg.interfaces["HundredGigE0/0/0/3"].shutdown is True
    assert cfg.interfaces["HundredGigE0/0/0/8"].description == "SPARE-FUTURE-DWDM"  # preconfigure


def test_vrp():
    cfg = parse_config(config_text("vrp.cfg"), "vrp")
    assert cfg.hostname == "BDG-PE-01"
    assert cfg.loopback_ipv4 == "10.0.1.11"
    assert cfg.isis_net == "49.0001.0100.0000.0004.00"
    assert cfg.srv6_locator == "fc00:0:3::/48"
    assert cfg.bundles() == {"Eth-Trunk10": ["100GE1/0/1", "100GE1/0/2"]}
    trunk = cfg.interfaces["Eth-Trunk10"]
    assert trunk.is_bundle and trunk.ipv4 == "10.1.1.2/30" and trunk.mtu == 9216
    port3 = cfg.interfaces["100GE1/0/3"]
    assert port3.mtu == 9216 and port3.ipv4 == "10.1.3.1/30"  # jumboframe; secondary "sub" ignored
    assert cfg.interfaces["100GE1/0/1"].shutdown is False
    assert cfg.interfaces["100GE1/0/4"].shutdown is True


def test_junos_braces():
    cfg = parse_config(config_text("junos.cfg"), "junos")
    assert cfg.hostname == "SBY-P-01"
    assert cfg.loopback_ipv4 == "10.0.4.1"  # 127.0.0.1 skipped
    assert cfg.isis_net == "49.0001.0100.0000.0008.00"
    assert cfg.srv6_locator == "fc00:0:4::/48"
    assert cfg.interfaces["et-0/0/0"].description == "TO-JKT-P-01 400G"
    assert cfg.interfaces["et-0/0/0"].ipv4 == "10.1.0.2/30"
    assert sorted(cfg.bundles()["ae30"]) == ["et-0/0/2", "et-0/0/3"]
    assert cfg.interfaces["ae30"].is_bundle and cfg.interfaces["ae30"].ipv4 == "10.1.4.1/30"
    assert cfg.interfaces["et-0/0/4"].shutdown is True
    assert "lo0" not in cfg.interfaces


def test_junos_display_set():
    cfg = parse_config(config_text("junos_set.cfg"), "junos")
    assert cfg.hostname == "DPS-PE-01"
    assert cfg.loopback_ipv4 == "10.0.5.11"
    assert cfg.srv6_locator == "fc00:0:10::/48"
    assert cfg.bundles() == {"ae1": ["et-0/0/1", "et-0/0/2"]}
    assert cfg.interfaces["et-0/0/0"].description == "TO-SBY-P-01"


@pytest.mark.parametrize("fixture", ["sros_classic.cfg", "sros_md.cfg"])
def test_sros(fixture):
    cfg = parse_config(config_text(fixture), "timos")
    assert cfg.hostname == "SMG-PE-01"
    assert cfg.loopback_ipv4 == "10.0.2.11"
    assert cfg.isis_net == "49.0001"
    p1 = cfg.interfaces["1/1/c1/1"]
    assert p1.description == "TO-SBY-P-01" and p1.mtu == 9212 and p1.ipv4 == "10.1.2.1/30" and not p1.shutdown
    assert cfg.interfaces["1/1/c4/1"].shutdown is True
    assert cfg.interfaces["lag-20"].is_bundle and cfg.interfaces["lag-20"].description == "TO-YGY-AGG-01"
    assert cfg.bundles() == {"lag-20": ["1/1/c2/1", "1/1/c3/1"]}


def test_sros_md_srv6():
    assert parse_config(config_text("sros_md.cfg"), "sros").srv6_locator == "fc00:0:2::/48"


def test_sros_classic_lag_ip():
    assert parse_config(config_text("sros_classic.cfg"), "sros").interfaces["lag-20"].ipv4 == "10.1.6.1/30"


@pytest.mark.parametrize(
    "fixture,vendor",
    [("iosxr.cfg", "iosxr"), ("vrp.cfg", "vrp"), ("junos.cfg", "junos"), ("junos_set.cfg", "junos"),
     ("sros_classic.cfg", "sros"), ("sros_md.cfg", "sros")],
)
def test_detect_without_hint(fixture, vendor):
    assert detect_vendor(config_text(fixture)) == vendor


def test_hint_wins_and_unknown_raises():
    assert detect_vendor("anything", "Junos") == "junos"
    with pytest.raises(ValueError):
        parse_config("just some text")
