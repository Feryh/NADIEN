"""ZTE ZXCTN / FiberHome R8xx / Cisco IOS parsers and the cell-site (CSR) naming rules."""

import pytest

from app.collectors.configs import parse_config
from app.collectors.configs.base import speed_from_name
from app.collectors.configs.rancid import parse_rancid
from app.collectors.observium.parsers import guess_role, parse_device
from app.services.areas import area_for_hostname, category_for, csr_parts, pop_code_for_hostname, site_for
from app.services.topology import _names_port

from .conftest import config_text


def test_should_read_interfaces_lag_loopback_and_isis_when_config_is_zte():
    cfg = parse_rancid(config_text("zte.cfg"))
    assert cfg.vendor == "zte" and cfg.hostname == "BAL-BA-BLI-0004-ME1-ZTE"
    assert cfg.interfaces["xgei-1/1/0/9"].description.startswith("to-BAL-BA-BLI-0005-ME1-ZTE")
    assert cfg.interfaces["gei-1/1/0/1"].mtu == 9000 and cfg.interfaces["gei-1/1/0/2"].shutdown
    assert cfg.bundles() == {"smartgroup1": ["xgei-1/1/0/11", "xgei-1/1/0/12"]}
    assert cfg.interfaces["smartgroup1"].is_bundle
    assert cfg.loopback_ipv4 == "10.20.30.4"
    assert cfg.interfaces["vlan100"].ipv4 == "10.99.0.1/30"  # the secondary address is ignored
    assert cfg.isis_net == "49.0001.0100.2003.0004.00"


def test_should_keep_spaced_names_and_lag_members_when_config_is_fiberhome():
    cfg = parse_rancid(config_text("fiberhome.cfg"))
    assert cfg.vendor == "fiberhome" and cfg.hostname == "SUM-AC-BNA-0595-ME1-FH820"
    assert cfg.interfaces["eth-10gi 0/1/0/1"].mtu == 9216
    assert cfg.bundles() == {"lag 1": ["eth-10gi 0/1/0/3", "eth-10gi 0/1/0/4"]}
    assert cfg.interfaces["lag 1"].is_bundle
    assert cfg.loopback_ipv4 == "172.28.10.35"
    assert cfg.interfaces["eth-10gi 0/1/0/1.100"].ipv4 is None  # unnumbered
    assert cfg.isis_net == "49.0001.1720.2801.0035.00"


def test_should_map_channel_group_to_port_channel_when_config_is_cisco_ios():
    cfg = parse_rancid(config_text("ios.cfg"))
    assert cfg.vendor == "ios" and cfg.hostname == "JKT-CSF-SW1-CAT95"
    assert cfg.bundles() == {"Port-channel10": ["TenGigabitEthernet1/0/1", "TenGigabitEthernet1/0/2"]}
    assert cfg.loopback_ipv4 == "10.1.1.1" and cfg.interfaces["Vlan99"].ipv4 is None


def test_should_use_iosxr_parser_when_rancid_says_cisco_but_text_is_xr():
    text = "!RANCID-CONTENT-TYPE: ios\ninterface Bundle-Ether10\n description core\n"
    assert parse_config(text, "ios").vendor == "iosxr"


def test_should_refuse_olt_configs_when_rancid_type_is_an_olt():
    with pytest.raises(ValueError):
        parse_rancid("!RANCID-CONTENT-TYPE: huaweiolt\n sysname X\n")


@pytest.mark.parametrize("name,speed", [
    ("xgei-1/1/0/9", 10), ("xxvgei-1/1/0/1", 25), ("cgei-0/2/0/1", 100), ("gei-1/1/0/1", 1),
    ("eth-10gi 0/1/0/2", 10), ("eth-25gi 0/1/0/9", 25), ("eth-gi 0/1/0/1", 1),
    ("TenGigabitEthernet1/0/1", 10), ("HundredGigE0/0/0/1", 100), ("100GE1/0/1", 100), ("GigabitEthernet0/0/1", 1),
    ("smartgroup1", None), ("lag 1", None),
])
def test_should_derive_speed_when_only_the_port_name_is_known(name, speed):
    assert speed_from_name(name) == speed


def test_should_split_region_province_city_site_when_hostname_is_cell_site():
    assert csr_parts("BAL-BA-BLI-0004-ME1-ZTE") == ("BAL", "BA", "BLI", "0004")
    assert csr_parts("JKT-ABW-OLT1-HW") is None
    assert pop_code_for_hostname("SUL-SN-SGM-0078-ME1-ZTE") == "SGM"
    assert site_for("SUL-SN-SGM-0078-ME1-ZTE", "SGM") == "0078"
    assert area_for_hostname("SUM-AC-BNA-0595-ME1-FH820") == "Sumatera"
    assert area_for_hostname("NUS-NB-PYA-0001-ME1-ZTE") == "Bali & Nusa Tenggara"


def test_should_classify_as_csr_when_hostname_is_cell_site_despite_me_token():
    assert guess_role("BAL-BA-BLI-0004-ME1-ZTE") == "csr"
    assert category_for("csr") == "csr"
    rec = parse_device({"device_id": 1, "sysName": "SUL-SN-SGM-0078-ME1-ZTE", "os": "zxr10"}, r"^([A-Za-z]{3,4})[-_.]")
    assert rec.role == "csr" and rec.pop_code == "SGM"  # not the island group "SUL"


@pytest.mark.parametrize("text,name,expected", [
    ("to-x:xgei-1/1/0/10|onnet", "xgei-1/1/0/10", True),
    ("to-x:xgei-1/1/0/10|onnet", "xgei-1/1/0/1", False),  # prefix of a longer port number
    ("to-x:xgei-1/1/0/10", "gei-1/1/0/10", False),  # inside a longer port name
    ("uplinktobna-kta-me1-fh820_eth-10gi0/1/0/7", "eth-10gi0/1/0/7", True),
])
def test_should_match_whole_port_names_only_when_reading_descriptions(text, name, expected):
    assert _names_port(text, name) is expected
