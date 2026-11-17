"""Site B joins a VPLS already on its PE (IOS-XR bridge-domain / vfi) instead of getting an access port."""

from app.services import running_config
from app.services.configgen import Addressing, generate
from app.services.l2vpn_peers import vpls_instances, vpls_key
from tests.test_configgen import client, make_service  # noqa: F401  (fixtures)
from tests.test_validation import net  # noqa: F401

# Trimmed from the ASR9K metro config: a VFI, a bridge-domain whose PWs sit on it directly, and a p2p xconnect.
XR = """l2vpn
 pw-class MPLS-CONTROL-WORD
  encapsulation mpls
   control-word
  !
 !
 xconnect group xg_OXY
  p2p OXY_1480
   neighbor ipv4 10.3.3.3 pw-id 1480
  !
 !
 bridge group Corporate
  bridge-domain vpls-1919
   mtu 2000
   interface Bundle-Ether10.1919
   !
   vfi vpls-1919
    neighbor 10.0.1.18 pw-id 1919
     pw-class FAT-CW
    !
    neighbor 10.0.1.19 pw-id 1919
     pw-class FAT-CW
    !
   !
   interface Bundle-Ether35.1919
   !
  !
  bridge-domain VPLS-2600
   interface Bundle-Ether14.2600
   !
   neighbor 172.24.0.1 pw-id 2600
    pw-class MPLS-CONTROL-WORD
   !
  !
 !
!
router static
"""
VFI = vpls_key("Corporate", "vpls-1919", "vpls-1919")


def _join(db, make_service, vlan: int = 1919, **addressing):  # noqa: F811
    service = make_service("iosxr", "l2vpn", vlan_id=vlan, b_interface=None)
    result = generate(db, service, Addressing(vlans=[vlan], b_vpls=VFI, **addressing),
                      config_source=lambda device_id: XR)
    return result["endpoints"]["B"]


def test_should_list_vfi_and_bridge_domain_instances_with_their_interfaces_and_pw_ids():
    rows = {row["key"]: row for row in vpls_instances(XR, "iosxr")}
    assert set(rows) == {VFI, vpls_key("Corporate", "VPLS-2600", None)}
    assert rows[VFI]["interfaces"] == ["Bundle-Ether10.1919", "Bundle-Ether35.1919"]
    assert (rows[VFI]["pw_ids"], rows[VFI]["vlans"]) == ([1919], [1919])


def test_should_add_only_a_neighbour_to_the_vfi_when_site_b_joins_an_existing_vpls(db, make_service):  # noqa: F811
    b = _join(db, make_service)
    assert b["config"] == ["configure", "l2vpn", " bridge group Corporate", "  bridge-domain vpls-1919",
                           "   vfi vpls-1919", "    neighbor 10.0.0.1 pw-id 1919", "     pw-class FAT-CW", "root"]
    assert "    no neighbor 10.0.0.1 pw-id 1919" in b["rollback"]


def test_should_write_the_neighbour_on_the_bridge_domain_when_the_instance_has_no_vfi(db, make_service):  # noqa: F811
    service = make_service("iosxr", "l2vpn", vlan_id=2600, b_interface=None)
    b = generate(db, service, Addressing(vlans=[2600], b_vpls=vpls_key("Corporate", "VPLS-2600", None)),
                 config_source=lambda device_id: XR)["endpoints"]["B"]
    assert "   neighbor 10.0.0.1 pw-id 2600" in b["config"] and "   vfi" not in " ".join(b["config"])


def test_should_flag_the_join_when_pe_a_is_already_a_neighbour_of_the_vpls(db, make_service):  # noqa: F811
    b = _join(db, make_service, b_peer="10.0.1.18")
    assert b["vc_ids"][0]["conflicts"]


def test_should_warn_when_the_vpls_uses_another_pw_id(db, make_service):  # noqa: F811
    b = _join(db, make_service, vlan=1920)
    assert any("pw-id 1919" in w for w in b["warnings"])


def test_should_keep_dialect_and_ask_for_a_port_or_vpls_when_site_b_has_neither(db, make_service):  # noqa: F811
    service = make_service("iosxr", "l2vpn", b_interface=None)
    b = generate(db, service, Addressing(vlans=[300]), config_source=lambda device_id: XR)["endpoints"]["B"]
    assert b["dialect"] == "iosxr" and not b["config"]
    assert any("VPLS yang sudah ada" in w for w in b["warnings"])


def test_should_build_site_b_join_from_hostnames_without_a_b_port(client, make_service, net, monkeypatch):  # noqa: F811
    make_service("iosxr", "l2vpn")  # sets the vendors
    monkeypatch.setattr(running_config, "fetch", lambda db, device_id: XR)
    response = client.get("/api/configs/l2vpn", params={
        "a_device_id": net["dev"]["A-PE1"].id, "a_port": "Bundle-Ether1", "b_device_id": net["dev"]["B-PE1"].id,
        "vlans": "1919", "b_vpls": VFI})
    assert response.status_code == 200, response.text
    assert "    neighbor 10.0.0.1 pw-id 1919" in response.json()["endpoints"]["B"]["config"]


def test_should_list_vpls_instances_of_an_iosxr_pe(client, make_service, net, monkeypatch):  # noqa: F811
    make_service("iosxr", "l2vpn")
    monkeypatch.setattr(running_config, "fetch", lambda db, device_id: XR)
    body = client.get("/api/configs/l2vpn/vpls", params={"device_id": net["dev"]["B-PE1"].id}).json()
    assert body["supported"] and [row["key"] for row in body["instances"]][0] == VFI
