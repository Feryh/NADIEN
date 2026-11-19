"""Merge a ParsedConfig into the inventory.

Precedence: Observium (live SNMP) owns operational state — speed, oper/admin status, ifAlias.
The config owns what SNMP cannot see reliably — bundle membership, interface IPv4/MTU when
missing, loopback, IS-IS NET and SRv6 locator. Interfaces only present in the config are
created when they carry something useful (description, IP, bundle role).
"""

import re
from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import models as m
from ...services.topology import norm_host, norm_if
from ..observium.parsers import SKIP_PORT, VIRTUAL_NAME
from ...services.areas import PON_GBPS
from .base import ParsedConfig


# Logical interfaces in vendor configs that are never ports: VLAN/bridge interfaces, tunnels, virtual
# templates, DCN channels and GPON ONUs (one per customer). Creating them would inflate port counts.
LOGICAL_PORT = re.compile(
    r"^(vlan|bvi|bdi|vbdif|vbui|supervlan|virtual-|ve-group|te_tunnel|tunnel-te|pw-ether|dcn-|gpon[-_]onu|nve|"
    r"vsi|vxlan|bridge|dialer|mgmt_eth|qx)", re.IGNORECASE)


def _classify(name: str, is_bundle: bool) -> str:
    if is_bundle:
        return "bundle"
    if "." in name:
        return "subinterface"
    return "virtual" if VIRTUAL_NAME.match(name) else "physical"


def _apply_pon_ports(db: Session, device: m.Device, cfg: ParsedConfig, index: dict[str, m.Interface],
                     counts: Counter) -> None:
    """OLT PON ports as Observium names them (`Slot3/Port5`), with their technology in optic_type. The config lists
    every PON port of the cards; Observium reports only those in service, so the others (enabled without ONTs, or
    shut down) are added here as idle or disabled ports and still count in the OLT's port capacity."""
    for p in cfg.interfaces.values():
        if not p.pon or p.pon_slot_port is None:
            continue
        slot, port = p.pon_slot_port
        name = f"Slot{slot}/Port{port}"
        iface = index.get(norm_if(name))
        if iface is None:
            iface = m.Interface(device_id=device.id, name=name, if_type="physical", mib_type="PonStatus",
                                speed_gbps=PON_GBPS.get(p.pon), admin_status="down" if p.shutdown else "up",
                                oper_status="down", config_port=True)
            db.add(iface)
            db.flush()
            index[norm_if(name)] = iface
            counts["pon_ports_created"] += 1
        if iface.optic_type != p.pon:
            iface.optic_type = p.pon
            counts["pon_types_set"] += 1


_SLOT_PORT_TAIL = re.compile(r"(\d+/\d+)/(\d+)$")


def _apply_card_ports(db: Session, device: m.Device, cfg: ParsedConfig, index: dict[str, m.Interface],
                      counts: Counter) -> None:
    """Card ports from the config (OLT uplink boards) that the NMS does not report: added as idle or disabled ports
    that count in the device's port capacity. A port the NMS reports under another name style
    (`Huawei-MAN-VNR0-Et 0/8/1`) is matched on its frame/slot/port."""
    by_tail: dict[str, m.Interface] = {}
    slot_speed: dict[str, float] = {}
    for iface in index.values():
        if iface.if_type == "physical" and (match := _SLOT_PORT_TAIL.search(iface.name)):
            by_tail[match.group(0)] = iface
            if iface.speed_gbps:
                slot_speed[match.group(1)] = max(slot_speed.get(match.group(1), 0.0), iface.speed_gbps)
    for p in cfg.interfaces.values():
        match = _SLOT_PORT_TAIL.search(p.name) if p.card_port else None
        if match is None or norm_if(p.name) in index or match.group(0) in by_tail:
            continue
        iface = m.Interface(device_id=device.id, name=p.name, if_type="physical", mib_type="ethernetCsmacd",
                            speed_gbps=p.speed_gbps or slot_speed.get(match.group(1)),
                            admin_status="down" if p.shutdown else "up", oper_status="down", config_port=True)
        db.add(iface)
        db.flush()
        index[norm_if(p.name)] = iface
        counts["card_ports_created"] += 1


def apply_config(db: Session, device: m.Device, cfg: ParsedConfig, create_ports: bool = True) -> dict:
    """`create_ports=False` (device monitored by an NMS): only ports the NMS already reported are
    filled in, plus LAG parents; the NMS stays the source of truth for which physical ports exist."""
    counts: Counter = Counter()
    warnings: list[str] = []
    if cfg.hostname and norm_host(cfg.hostname) != norm_host(device.hostname):
        warnings.append(f"config hostname '{cfg.hostname}' differs from inventory '{device.hostname}'")

    for attr in ("loopback_ipv4", "isis_net", "srv6_locator"):
        value = getattr(cfg, attr)
        if value and getattr(device, attr) != value:
            setattr(device, attr, value)
            counts["device_fields"] += 1

    index = {norm_if(i.name): i for i in db.scalars(select(m.Interface).where(m.Interface.device_id == device.id))}
    created_here: set[int] = set()

    def get_or_create(name: str, is_bundle: bool) -> m.Interface:
        iface = index.get(norm_if(name))
        if iface is None:
            iface = m.Interface(device_id=device.id, name=name, if_type=_classify(name, is_bundle))
            db.add(iface)
            db.flush()
            index[norm_if(name)] = iface
            created_here.add(iface.id)
            counts["interfaces_created"] += 1
        return iface

    _apply_pon_ports(db, device, cfg, index, counts)
    _apply_card_ports(db, device, cfg, index, counts)
    for p in cfg.interfaces.values():
        # loopback/mgmt: on the device record; PON and card ports: handled above
        if SKIP_PORT.match(p.name) or p.pon or p.card_port:
            continue
        known = norm_if(p.name) in index
        useful = p.description or p.ipv4 or p.bundle or p.is_bundle
        if not known and (not useful or LOGICAL_PORT.match(p.name) or not (create_ports or p.is_bundle)):
            continue
        iface = get_or_create(p.name, p.is_bundle)
        if p.is_bundle and iface.if_type != "bundle":
            iface.if_type = "bundle"
            counts["bundles_marked"] += 1
        if p.description and not iface.description:
            iface.description = p.description
        if p.mtu and not iface.mtu:
            iface.mtu = p.mtu
        if p.ipv4 and iface.ipv4_address != p.ipv4:
            iface.ipv4_address = p.ipv4
            counts["ipv4_set"] += 1
        if iface.id in created_here:
            iface.admin_status = "down" if p.shutdown else "up"

    # Bundle membership second, so parents created above are available.
    for bundle_name, members in cfg.bundles().items():
        parent = get_or_create(bundle_name, True)
        parent.if_type = "bundle"
        for member_name in members:
            member = index.get(norm_if(member_name))
            if member is not None and member.parent_id != parent.id:
                member.parent_id = parent.id
                counts["bundle_members_linked"] += 1
        # A bundle's speed is the sum of its members when SNMP did not report one.
        if not parent.speed_gbps:
            speeds = [index[norm_if(n)].speed_gbps for n in members if norm_if(n) in index]
            if speeds and all(s for s in speeds):
                parent.speed_gbps = sum(speeds)

    db.commit()
    return {"device": device.hostname, "vendor": cfg.vendor, "counts": dict(counts), "warnings": warnings}
