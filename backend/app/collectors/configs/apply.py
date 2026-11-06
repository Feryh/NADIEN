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

    for p in cfg.interfaces.values():
        if SKIP_PORT.match(p.name):  # loopback/mgmt: kept on the device record, not as Metro ports
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
