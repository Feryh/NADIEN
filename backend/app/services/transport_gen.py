"""End-to-end transport configuration between two PEs: SR-MPLS, SRv6 and VXLAN (Phase 7).

Each request names PE A, PE B and optionally the transit P nodes of the path. Every device gets its own config and
rollback in its dialect; values already on a device (IS-IS process, BGP ASN, prefix-SID, SRv6 locator, IPv6
loopback) are read from its RANCID running config so the generator does not fight what is configured.

- sr-mpls: IS-IS SR-MPLS (SRGB, prefix-SID on the loopback, optional TI-LFA) on every node of the path, and an
  SR-TE policy A -> B and B -> A on a colour: dynamic IGP path, plus an explicit node-SID list through the transit
  nodes when they are given. Nodes that already carry a prefix-SID keep it and their underlay is not regenerated.
- srv6: locator per node (block + node id), IS-IS SRv6, and BGP VPNv4 over SRv6 between A and B; a VRF on top
  when one is given (per-VRF SID).
- vxlan: L2 VNI between A and B on an access sub-interface, with BGP EVPN (or static head-end replication on VRP).
- evpn: EVPN over the existing MPLS underlay (LDP or SR): E-Line (VPWS, local/remote service id 1/2) or E-LAN
  (bridge domain bound to the EVI), with the BGP l2vpn evpn family between A and B.

Templates are vendor-standard forms for Huawei VRP (NE/ATN/CE) and Cisco IOS-XR; ZTE and FiberHome get no config
(only a warning) until their syntax is confirmed against this network's backups. No comment lines in the output.
"""

import ipaddress
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import models as m
from . import running_config
from .configgen import COMMIT, _dialect
from .vendors import DIALECT_LABELS

Technology = Literal["sr-mpls", "srv6", "vxlan", "evpn"]
SUPPORTED = {"sr-mpls": {"vrp", "iosxr"}, "srv6": {"vrp", "iosxr"}, "vxlan": {"vrp", "iosxr"},
             "evpn": {"vrp", "iosxr"}}
TECH_LABELS = {"sr-mpls": "SR-MPLS", "srv6": "SRv6", "vxlan": "VXLAN", "evpn": "EVPN"}
SYNTAX_WARNING = ("Template transport mengikuti sintaks standar vendor; cocokkan dengan versi software dan desain "
                  "jaringan (SRGB, locator, RT) sebelum eksekusi.")
NAME_PATTERN = r"^[A-Za-z0-9_-]{1,32}$"

ConfigSource = Callable[[int], str]


class TransportIn(BaseModel):
    technology: Technology
    a_device_id: int
    b_device_id: int
    transit_device_ids: list[int] = Field(default_factory=list, max_length=16)
    isis_process: str | None = Field(default=None, max_length=32, pattern=r"^[\w.-]+$")
    bgp_asn: int | None = Field(default=None, ge=1, le=4294967295)
    # SR-MPLS
    srgb_start: int = Field(default=16000, ge=16, le=1048575)
    srgb_end: int = Field(default=23999, ge=16, le=1048575)
    sid_indexes: dict[int, int] = Field(default_factory=dict)  # device id -> prefix-SID index
    ti_lfa: bool = True
    sr_policy: bool = True
    color: int = Field(default=100, ge=1, le=4294967295)
    # SRv6
    locator_name: str = Field(default="NADIEN", pattern=NAME_PATTERN)
    srv6_block: str = Field(default="fcbb:bb00::/32", max_length=43)
    ipv6_loopbacks: dict[int, str] = Field(default_factory=dict)  # device id -> IPv6 loopback (BGP over SRv6)
    # SRv6 L3VPN / VXLAN EVPN service
    vrf: str | None = Field(default=None, pattern=NAME_PATTERN)
    route_distinguisher: str | None = Field(default=None, max_length=32)
    route_target: str | None = Field(default=None, max_length=32)
    # VXLAN
    vlan: int | None = Field(default=None, ge=1, le=4094)
    vni: int | None = Field(default=None, ge=1, le=16777215)
    a_port: str | None = Field(default=None, max_length=64)
    b_port: str | None = Field(default=None, max_length=64)
    control_plane: Literal["evpn", "static"] = "evpn"
    # EVPN over MPLS: E-Line (VPWS) or E-LAN (bridge domain); the EVI defaults to the VLAN
    evpn_type: Literal["vpws", "elan"] = "vpws"
    evi: int | None = Field(default=None, ge=1, le=65535)


class TransportError(ValueError):
    pass


@dataclass
class _Node:
    role: Literal["A", "B", "P"]
    device: m.Device
    dialect: str | None
    running: str
    loopback: str  # loopback interface name in the device's own spelling
    isis: str
    asn: str
    sid_index: int
    sid_existing: bool
    sid_default: bool  # index = NADIEN device id: neither on the device nor given in the request
    ipv6: str | None
    level: str
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return self.device.hostname


# --------------------------------------------------------------------------- running-config readers


def _first(pattern: str, text: str) -> str | None:
    match = re.search(pattern, text, re.M)
    return match.group(1) if match else None


def _stanza(text: str, header: str) -> list[str]:
    """Indented lines under a top-level `header` line (IOS-XR / VRP both indent sub-commands)."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.rstrip() == header:
            body = []
            for sub in lines[index + 1:]:
                if not sub.startswith(" "):
                    break
                body.append(sub)
            return body
    return []


def _loopback_name(text: str, dialect: str | None) -> str:
    found = _first(r"^interface ((?:Loopback|LoopBack)0)\s*$", text)
    return found or ("LoopBack0" if dialect == "vrp" else "Loopback0")


def _ipv6_on(text: str, interface: str) -> str | None:
    for line in _stanza(text, f"interface {interface}"):
        match = re.match(r"\s*ipv6 address ([0-9A-Fa-f:]+)(?:/| )128\b", line)
        if match:
            return match.group(1)
    return None


def _isis_interfaces(text: str, process: str) -> list[str]:
    """Core interfaces of the IS-IS process on IOS-XR (TI-LFA is enabled per interface there)."""
    return [match.group(1) for line in _stanza(text, f"router isis {process}")
            if (match := re.match(r"^ interface (\S+)", line)) and not match.group(1).lower().startswith("loopback")]


# --------------------------------------------------------------------------- nodes


def _fetch(source: ConfigSource, device_id: int) -> tuple[str, str | None]:
    try:
        return source(device_id), None
    except running_config.RunningConfigError as exc:
        return "", f"Config RANCID tidak terbaca ({exc}); nilai default/placeholder dipakai."


def _node(db: Session, payload: TransportIn, role: Literal["A", "B", "P"], device_id: int,
          source: ConfigSource) -> _Node:
    device = db.get(m.Device, device_id)
    if device is None:
        raise TransportError(f"Perangkat {device_id} tidak ditemukan.")
    dialect = _dialect(db, device.id)
    running, problem = _fetch(source, device.id)
    isis = payload.isis_process or _first(r"^(?:router )?isis (\S+)\s*$", running) or "1"
    asn = str(payload.bgp_asn or _first(r"^(?:router )?bgp (\d+)\s*$", running) or "<asn>")
    existing_sid = _first(r"prefix-sid index (\d+)", running)
    loopback = _loopback_name(running, dialect)
    node = _Node(role=role, device=device, dialect=dialect, running=running, loopback=loopback, isis=isis, asn=asn,
                 sid_index=int(existing_sid) if existing_sid else payload.sid_indexes.get(device.id, device.id),
                 sid_existing=existing_sid is not None,
                 sid_default=not existing_sid and device.id not in payload.sid_indexes,
                 ipv6=payload.ipv6_loopbacks.get(device.id) or _ipv6_on(running, loopback),
                 level=_first(r"^ is-level (level-\S+)", running) or "level-2")
    if problem:
        node.warnings.append(problem)
    elif not re.search(r"^(?:router )?isis \S+", running, re.M) and payload.technology != "vxlan":
        node.warnings.append("IS-IS belum ada di config RANCID: migrasikan IGP ke IS-IS dulu (menu Migrasi IGP).")
    return node


def _nodes(db: Session, payload: TransportIn, source: ConfigSource) -> list[_Node]:
    ids = [payload.a_device_id, *payload.transit_device_ids, payload.b_device_id]
    if len(set(ids)) != len(ids):
        raise TransportError("PE A, PE B dan node transit harus perangkat yang berbeda.")
    roles: list[Literal["A", "B", "P"]] = ["A", *(["P"] * len(payload.transit_device_ids)), "B"]
    return [_node(db, payload, role, device_id, source) for role, device_id in zip(roles, ids)]


def _check_sr_mpls(payload: TransportIn, nodes: list[_Node]) -> None:
    size = payload.srgb_end - payload.srgb_start + 1
    if size < 1:
        raise TransportError("SRGB akhir harus lebih besar dari awal.")
    for node in nodes:
        if node.sid_index >= size:
            raise TransportError(f"Index prefix-SID {node.sid_index} ({node.label}) di luar SRGB ({size} label); "
                                 "isi index atau perbesar SRGB.")
    indexes = [node.sid_index for node in nodes]
    duplicates = sorted({i for i in indexes if indexes.count(i) > 1})
    if duplicates:
        raise TransportError(f"Index prefix-SID dipakai lebih dari satu perangkat: {', '.join(map(str, duplicates))}")


def _label(payload: TransportIn, node: _Node) -> int:
    return payload.srgb_start + node.sid_index


def _policy_name(prefix: str, peer: _Node, color: int) -> str:
    host = re.sub(r"[^A-Za-z0-9]+", "_", peer.label).strip("_")[:40]
    return f"{prefix}_C{color}_{host}"


# --------------------------------------------------------------------------- SR-MPLS


def _sr_underlay_xr(payload: TransportIn, node: _Node) -> tuple[list[str], list[str]]:
    config = ["segment-routing", f" global-block {payload.srgb_start} {payload.srgb_end}", "root",
              f"router isis {node.isis}", " address-family ipv4 unicast", "  metric-style wide",
              "  segment-routing mpls", "root",
              f"router isis {node.isis}", f" interface {node.loopback}", "  address-family ipv4 unicast",
              f"   prefix-sid index {node.sid_index}", "root"]
    rollback = [f"router isis {node.isis}", f" interface {node.loopback}", "  address-family ipv4 unicast",
                f"   no prefix-sid index {node.sid_index}", "root",
                f"router isis {node.isis}", " address-family ipv4 unicast", "  no segment-routing mpls", "root",
                "segment-routing", " no global-block", "root"]
    if payload.ti_lfa:
        interfaces = _isis_interfaces(node.running, node.isis)
        if not interfaces:
            node.warnings.append("Interface core IS-IS tidak terbaca dari RANCID: TI-LFA tidak dibuat.")
        for interface in interfaces:
            config += [f"router isis {node.isis}", f" interface {interface}", "  address-family ipv4 unicast",
                       "   fast-reroute per-prefix", "   fast-reroute per-prefix ti-lfa", "root"]
            rollback[:0] = [f"router isis {node.isis}", f" interface {interface}", "  address-family ipv4 unicast",
                            "   no fast-reroute per-prefix", "root"]
    return config, rollback


def _sr_underlay_vrp(payload: TransportIn, node: _Node) -> tuple[list[str], list[str]]:
    config = ["segment-routing", "quit", f"isis {node.isis}", " cost-style wide", " segment-routing mpls",
              f" segment-routing global-block {payload.srgb_start} {payload.srgb_end}"]
    if payload.ti_lfa:
        config += [" frr", f"  loop-free-alternate {node.level}", f"  ti-lfa {node.level}", "  quit"]
    config += ["quit", f"interface {node.loopback}", f" isis prefix-sid index {node.sid_index}", "quit"]
    rollback = [f"interface {node.loopback}", " undo isis prefix-sid", "quit", f"isis {node.isis}"]
    if payload.ti_lfa:
        rollback += [" frr", f"  undo ti-lfa {node.level}", f"  undo loop-free-alternate {node.level}", "  quit"]
    rollback += [" undo segment-routing global-block", " undo segment-routing mpls", "quit"]
    return config, rollback


def _sr_policy_xr(payload: TransportIn, peer: _Node, path: list[_Node]) -> tuple[list[str], list[str]]:
    name = _policy_name("NADIEN", peer, payload.color)
    config = ["segment-routing", " traffic-eng"]
    if path:
        config += [f"  segment-list {name}",
                   *[f"   index {10 * (i + 1)} mpls label {_label(payload, hop)}" for i, hop in enumerate([*path, peer])],
                   "  exit"]
    config += [f"  policy {name}", f"   color {payload.color} end-point ipv4 {peer.device.loopback_ipv4 or '<loopback-peer>'}",
               "   candidate-paths"]
    if path:
        config += ["    preference 200", f"     explicit segment-list {name}", "     exit", "    exit"]
    config += ["    preference 100", "     dynamic", "      metric", "       type igp", "root"]
    rollback = ["segment-routing", " traffic-eng", f"  no policy {name}"]
    rollback += [f"  no segment-list {name}"] if path else []
    return config, rollback + ["root"]


def _sr_policy_vrp(payload: TransportIn, peer: _Node, path: list[_Node]) -> tuple[list[str], list[str]]:
    name = _policy_name("NADIEN", peer, payload.color)
    hops = [*path, peer]
    config = ["segment-routing", f" segment-list {name}",
              *[f"  index {10 * (i + 1)} sid label {_label(payload, hop)}" for i, hop in enumerate(hops)], "  quit",
              f" sr-te policy {name} endpoint {peer.device.loopback_ipv4 or '<loopback-peer>'} color {payload.color}",
              "  candidate-path preference 100", f"   segment-list {name}", "   quit", "  quit", "quit"]
    rollback = ["segment-routing", f" undo sr-te policy {name}", f" undo segment-list {name}", "quit"]
    return config, rollback


def _sr_mpls(payload: TransportIn, nodes: list[_Node], node: _Node) -> tuple[list[str], list[str]]:
    config: list[str] = []
    rollback: list[str] = []
    if node.sid_existing:
        node.notes.append(f"SR-MPLS sudah aktif (prefix-SID index {node.sid_index}, label "
                          f"{_label(payload, node)}): underlay tidak dibuat ulang.")
    else:
        underlay = _sr_underlay_xr if node.dialect == "iosxr" else _sr_underlay_vrp
        config, rollback = underlay(payload, node)
        default = " (default = ID perangkat NADIEN; ganti bila sudah ada rencana SID)" if node.sid_default else ""
        node.notes.append(f"Prefix-SID index {node.sid_index} → label {_label(payload, node)}{default}.")
    if payload.sr_policy and node.role != "P":
        peer = nodes[-1] if node.role == "A" else nodes[0]
        transit = nodes[1:-1] if node.role == "A" else list(reversed(nodes[1:-1]))
        policy = _sr_policy_xr if node.dialect == "iosxr" else _sr_policy_vrp
        policy_config, policy_rollback = policy(payload, peer, transit)
        config += policy_config
        rollback = policy_rollback + rollback
        node.notes.append(f"SR-TE policy color {payload.color} ke {peer.label}: arahkan layanan dengan color/"
                          "tunnel-policy yang sama.")
    return config, rollback


# --------------------------------------------------------------------------- SRv6


def _locator(payload: TransportIn, node: _Node) -> ipaddress.IPv6Network:
    try:
        block = ipaddress.IPv6Network(payload.srv6_block.strip())
    except ValueError as exc:
        raise TransportError(f"Block SRv6 tidak valid: {payload.srv6_block} (contoh fcbb:bb00::/32)") from exc
    if not 16 <= block.prefixlen <= 48:
        raise TransportError("Block SRv6 harus /16–/48 (locator = block + 16 bit node id).")
    if node.device.id >= 1 << 16:
        raise TransportError(f"ID perangkat {node.label} terlalu besar untuk node id 16 bit.")
    length = block.prefixlen + 16
    return ipaddress.IPv6Network((int(block.network_address) + (node.device.id << (128 - length)), length))


def _has_bgp_peer(node: _Node, ip: str) -> bool:
    return bool(re.search(rf"^\s*(?:neighbor|peer) {re.escape(ip)}(?:\s|$)", node.running, re.M))


def _xr_neighbor(node: _Node, ip: str, family: str) -> tuple[list[str], list[str]]:
    """Neighbour lines inside router bgp; an existing session only gains (and on rollback loses) the family."""
    if _has_bgp_peer(node, ip):
        node.notes.append(f"Neighbor BGP {ip} sudah ada: hanya address-family {family} yang ditambahkan.")
        config = [f" neighbor {ip}", f"  address-family {family}", "  exit"]
        return config, [f" neighbor {ip}", f"  no address-family {family}", "  exit"]
    return [f" neighbor {ip}", f"  remote-as {node.asn}", f"  update-source {node.loopback}",
            f"  address-family {family}", "  exit"], [f" no neighbor {ip}"]


def _vrp_peer(node: _Node, ip: str, family: str) -> tuple[list[str], list[str]]:
    """Peer creation lines in the bgp view and the rollback; an existing peer is only disabled in the family."""
    if _has_bgp_peer(node, ip):
        node.notes.append(f"Peer BGP {ip} sudah ada: hanya {family} yang diaktifkan.")
        return [], [f" {family}", f"  undo peer {ip} enable", "  quit"]
    return [f" peer {ip} as-number {node.asn}", f" peer {ip} connect-interface {node.loopback}"], [f" undo peer {ip}"]


def _srv6_peer(nodes: list[_Node], node: _Node) -> _Node | None:
    return {"A": nodes[-1], "B": nodes[0]}.get(node.role)


def _vrf_values(payload: TransportIn, node: _Node) -> tuple[str, str]:
    rd = payload.route_distinguisher or (f"{node.asn}:{node.device.id}" if node.asn != "<asn>" else "<rd>")
    rt = payload.route_target or (f"{node.asn}:{payload.vni or 100}" if node.asn != "<asn>" else "<rt>")
    return rd, rt


def _srv6_xr(payload: TransportIn, nodes: list[_Node], node: _Node) -> tuple[list[str], list[str]]:
    locator = _locator(payload, node)
    name = payload.locator_name
    config = ["segment-routing", " srv6", "  encapsulation", f"   source-address {node.ipv6 or '<ipv6-loopback>'}",
              "  exit", "  locators", f"   locator {name}", f"    prefix {locator}", "root",
              f"router isis {node.isis}", " address-family ipv6 unicast", "  metric-style wide",
              "  segment-routing srv6", f"   locator {name}", "root"]
    rollback = [f"router isis {node.isis}", " address-family ipv6 unicast", "  no segment-routing srv6", "root",
                "no segment-routing srv6"]
    peer = _srv6_peer(nodes, node)
    if peer is None:
        return config, rollback
    peer_ip = peer.ipv6 or "<ipv6-loopback-peer>"
    if payload.vrf:
        rd, rt = _vrf_values(payload, node)
        config += [f"vrf {payload.vrf}", " address-family ipv4 unicast", f"  import route-target {rt}",
                   f"  export route-target {rt}", "root"]
    neighbor, neighbor_rollback = _xr_neighbor(node, peer_ip, "vpnv4 unicast")
    config += [f"router bgp {node.asn}", " address-family vpnv4 unicast", " exit", *neighbor, "root"]
    bgp_rollback = [f"router bgp {node.asn}", *neighbor_rollback]
    if payload.vrf:
        config += [f"router bgp {node.asn}", f" vrf {payload.vrf}", f"  rd {rd}", "  address-family ipv4 unicast",
                   "   segment-routing srv6", f"    locator {name}", "    alloc mode per-vrf", "   exit",
                   "   redistribute connected", "root"]
        bgp_rollback += [f" no vrf {payload.vrf}"]
    rollback = bgp_rollback + ["root"] + ([f"no vrf {payload.vrf}"] if payload.vrf else []) + rollback
    return config, rollback


def _srv6_vrp(payload: TransportIn, nodes: list[_Node], node: _Node) -> tuple[list[str], list[str]]:
    locator = _locator(payload, node)
    name = payload.locator_name
    config = ["segment-routing ipv6", f" encapsulation source-address {node.ipv6 or '<ipv6-loopback>'}",
              f" locator {name} ipv6-prefix {locator.network_address} {locator.prefixlen} static 32", "quit",
              f"isis {node.isis}", " ipv6 enable topology ipv6", f" segment-routing ipv6 locator {name}", "quit"]
    rollback = [f"isis {node.isis}", f" undo segment-routing ipv6 locator {name}", "quit", "undo segment-routing ipv6"]
    peer = _srv6_peer(nodes, node)
    if peer is None:
        return config, rollback
    peer_ip = peer.ipv6 or "<ipv6-loopback-peer>"
    if payload.vrf:
        rd, rt = _vrf_values(payload, node)
        config += [f"ip vpn-instance {payload.vrf}", " ipv4-family", f"  route-distinguisher {rd}",
                   f"  vpn-target {rt} export-extcommunity", f"  vpn-target {rt} import-extcommunity", "  quit",
                   "quit"]
    peer_lines, peer_rollback = _vrp_peer(node, peer_ip, "ipv4-family vpnv4")
    config += [f"bgp {node.asn}", *peer_lines, " ipv4-family vpnv4", f"  peer {peer_ip} enable",
               f"  peer {peer_ip} prefix-sid", "  quit"]
    bgp_rollback = [f"bgp {node.asn}", *peer_rollback]
    if payload.vrf:
        config += [f" ipv4-family vpn-instance {payload.vrf}", f"  segment-routing ipv6 locator {name}",
                   "  segment-routing ipv6 best-effort", "  import-route direct", "  quit"]
        bgp_rollback += [f" undo ipv4-family vpn-instance {payload.vrf}"]
    config += ["quit"]
    rollback = bgp_rollback + ["quit"] + ([f"undo ip vpn-instance {payload.vrf}"] if payload.vrf else []) + rollback
    return config, rollback


def _srv6(payload: TransportIn, nodes: list[_Node], node: _Node) -> tuple[list[str], list[str]]:
    existing = _first(r"^\s+locator (\S+)", node.running) if "srv6" in node.running or "segment-routing ipv6" in node.running else None
    if existing and existing != payload.locator_name:
        node.warnings.append(f"Locator SRv6 {existing} sudah ada di perangkat: pakai nama itu bila tidak ingin locator kedua.")
    if node.role != "P" and not node.ipv6:
        node.warnings.append("Loopback IPv6 belum diketahui (tidak di RANCID): isi agar BGP over SRv6 lengkap.")
    if node.asn == "<asn>" and node.role != "P":
        node.warnings.append("ASN BGP tidak terbaca dari RANCID: isi ASN.")
    node.notes.append(f"Locator {payload.locator_name} = {_locator(payload, node)} (node id = ID perangkat NADIEN).")
    builder = _srv6_xr if node.dialect == "iosxr" else _srv6_vrp
    return builder(payload, nodes, node)


# --------------------------------------------------------------------------- VXLAN


def _vxlan_values(payload: TransportIn) -> tuple[int, int]:
    if not payload.vlan:
        raise TransportError("Isi VLAN akses untuk VXLAN.")
    return payload.vlan, payload.vni or 10000 + payload.vlan


def _vxlan_vrp(payload: TransportIn, node: _Node, peer: _Node, port: str) -> tuple[list[str], list[str]]:
    vlan, vni = _vxlan_values(payload)
    rd, rt = _vrf_values(payload.model_copy(update={"vni": vni}), node)
    peer_ip = peer.device.loopback_ipv4 or "<loopback-peer>"
    config = [f"bridge-domain {vlan}", f" vxlan vni {vni}"]
    if payload.control_plane == "evpn":
        config += [" evpn", f"  route-distinguisher {rd}", f"  vpn-target {rt} export-extcommunity",
                   f"  vpn-target {rt} import-extcommunity", "  quit"]
    config += ["quit", f"interface {port}.{vlan} mode l2", f" encapsulation dot1q vid {vlan}", f" bridge-domain {vlan}",
               "quit", "interface Nve1", f" source {node.device.loopback_ipv4 or '<loopback>'}"]
    config += [f" vni {vni} head-end peer-list protocol bgp" if payload.control_plane == "evpn"
               else f" vni {vni} head-end peer-list {peer_ip}", "quit"]
    rollback = ["interface Nve1", f" undo vni {vni}", "quit", f"undo interface {port}.{vlan}",
                f"undo bridge-domain {vlan}"]
    if payload.control_plane == "evpn":
        peer_lines, peer_rollback = _vrp_peer(node, peer_ip, "l2vpn-family evpn")
        config += [f"bgp {node.asn}", *peer_lines, " l2vpn-family evpn", "  policy vpn-target",
                   f"  peer {peer_ip} enable", f"  peer {peer_ip} advertise encap-type vxlan", "  quit", "quit"]
        rollback = [f"bgp {node.asn}", *peer_rollback, "quit"] + rollback
        node.notes.append("Switch CE: aktifkan `evpn-overlay enable` sekali per perangkat bila belum.")
    return config, rollback


def _vxlan_xr(payload: TransportIn, node: _Node, peer: _Node, port: str) -> tuple[list[str], list[str]]:
    vlan, vni = _vxlan_values(payload)
    rd, rt = _vrf_values(payload.model_copy(update={"vni": vni}), node)
    peer_ip = peer.device.loopback_ipv4 or "<loopback-peer>"
    group = "NADIEN-VXLAN"
    config = ["interface nve1", f" source-interface {node.loopback}", f" member vni {vni}",
              "  host-reachability protocol bgp", "root",
              f"interface {port}.{vlan} l2transport", f" encapsulation dot1q {vlan}",
              " rewrite ingress tag pop 1 symmetric", "root",
              "evpn", f" evi {vni}", f"  bgp", f"   rd {rd}", f"   route-target import {rt}",
              f"   route-target export {rt}", "  exit", "  advertise-mac", "root",
              "l2vpn", f" bridge group {group}", f"  bridge-domain BD-{vni}", f"   interface {port}.{vlan}", "   exit",
              f"   member vni {vni}", "root",
              f"router bgp {node.asn}", " address-family l2vpn evpn", " exit"]
    neighbor, neighbor_rollback = _xr_neighbor(node, peer_ip, "l2vpn evpn")
    config += [*neighbor, "root"]
    rollback = [f"router bgp {node.asn}", *neighbor_rollback, "root",
                f"no l2vpn bridge group {group} bridge-domain BD-{vni}", f"no evpn evi {vni}",
                f"no interface {port}.{vlan}", "interface nve1", f" no member vni {vni}", "root"]
    if payload.control_plane == "static":
        node.warnings.append("IOS-XR: VXLAN dibuat dengan BGP EVPN (replikasi statis tidak dibuat untuk IOS-XR).")
    return config, rollback


def _vxlan(payload: TransportIn, nodes: list[_Node], node: _Node) -> tuple[list[str], list[str]]:
    if node.role == "P":
        node.notes.append("Node transit cukup underlay IP (loopback A/B saling terjangkau, MTU ≥ 1550): tidak ada config VXLAN.")
        return [], []
    peer = nodes[-1] if node.role == "A" else nodes[0]
    port = (payload.a_port if node.role == "A" else payload.b_port) or "<port-akses>"
    if port == "<port-akses>":
        node.warnings.append("Port akses belum dipilih: ganti <port-akses>.")
    if payload.control_plane == "evpn" and node.asn == "<asn>":
        node.warnings.append("ASN BGP tidak terbaca dari RANCID: isi ASN.")
    builder = _vxlan_xr if node.dialect == "iosxr" else _vxlan_vrp
    return builder(payload, node, peer, port)


# --------------------------------------------------------------------------- EVPN over MPLS (VPWS / ELAN)

EVPN_GROUP = "NADIEN-EVPN"


def _evpn_values(payload: TransportIn) -> tuple[int, int]:
    if not payload.vlan:
        raise TransportError("Isi VLAN akses untuk EVPN.")
    return payload.vlan, payload.evi or payload.vlan


def _evpn_rd_rt(payload: TransportIn, node: _Node, evi: int) -> tuple[str, str]:
    return _vrf_values(payload.model_copy(update={"vni": evi}), node)


def _service_ids(node: _Node) -> tuple[int, int]:
    """VPWS attachment-circuit ids as (local, remote): PE A is 1, PE B is 2."""
    return (1, 2) if node.role == "A" else (2, 1)


def _evpn_xr(payload: TransportIn, node: _Node, peer: _Node, port: str) -> tuple[list[str], list[str]]:
    vlan, evi = _evpn_values(payload)
    rd, rt = _evpn_rd_rt(payload, node, evi)
    sub = f"{port}.{vlan}"
    config = [f"interface {sub} l2transport", f" encapsulation dot1q {vlan}", " rewrite ingress tag pop 1 symmetric",
              "root", "evpn", f" evi {evi}", "  bgp", f"   rd {rd}", f"   route-target import {rt}",
              f"   route-target export {rt}", "  exit"]
    if payload.evpn_type == "vpws":
        local, remote = _service_ids(node)
        config += ["root", "l2vpn", f" xconnect group {EVPN_GROUP}", f"  p2p EVPN-{evi}", f"   interface {sub}",
                   f"   neighbor evpn evi {evi} target {remote} source {local}", "root"]
        service_rollback = [f"no l2vpn xconnect group {EVPN_GROUP} p2p EVPN-{evi}"]
    else:
        config += ["  advertise-mac", "root", "l2vpn", f" bridge group {EVPN_GROUP}", f"  bridge-domain EVPN-{evi}",
                   f"   interface {sub}", "   exit", f"   evi {evi}", "root"]
        service_rollback = [f"no l2vpn bridge group {EVPN_GROUP} bridge-domain EVPN-{evi}"]
    neighbor, neighbor_rollback = _xr_neighbor(node, peer.device.loopback_ipv4 or "<loopback-peer>", "l2vpn evpn")
    config += [f"router bgp {node.asn}", " address-family l2vpn evpn", " exit", *neighbor, "root"]
    rollback = [f"router bgp {node.asn}", *neighbor_rollback, "root", *service_rollback, f"no evpn evi {evi}",
                f"no interface {sub}"]
    return config, rollback


def _evpn_vrp(payload: TransportIn, node: _Node, peer: _Node, port: str) -> tuple[list[str], list[str]]:
    vlan, evi = _evpn_values(payload)
    rd, rt = _evpn_rd_rt(payload, node, evi)
    sub, instance = f"{port}.{vlan}", f"EVPN-{evi}"
    config: list[str] = []
    if not re.search(r"^evpn source-address ", node.running, re.M):
        config.append(f"evpn source-address {node.device.loopback_ipv4 or '<loopback>'}")
        node.notes.append("evpn source-address ditambahkan (global, tidak dihapus saat rollback).")
    mode = "vpws" if payload.evpn_type == "vpws" else "bd-mode"
    config += [f"evpn vpn-instance {instance} {mode}", f" route-distinguisher {rd}",
               f" vpn-target {rt} export-extcommunity", f" vpn-target {rt} import-extcommunity", "quit"]
    access = [f"interface {sub} mode l2", f" encapsulation dot1q vid {vlan}", " rewrite pop single"]
    if payload.evpn_type == "vpws":
        local, remote = _service_ids(node)
        config += [f"evpl instance {evi} mpls-mode", f" evpn binding vpn-instance {instance}",
                   f" local-service-id {local} remote-service-id {remote}", "quit", *access, f" evpl instance {evi}",
                   "quit"]
        service_rollback = [f"undo interface {sub}", f"undo evpl instance {evi}"]
    else:
        config += [f"bridge-domain {vlan}", f" evpn binding vpn-instance {instance}", "quit", *access,
                   f" bridge-domain {vlan}", "quit"]
        service_rollback = [f"undo interface {sub}", f"undo bridge-domain {vlan}"]
    peer_ip = peer.device.loopback_ipv4 or "<loopback-peer>"
    peer_lines, peer_rollback = _vrp_peer(node, peer_ip, "l2vpn-family evpn")
    config += [f"bgp {node.asn}", *peer_lines, " l2vpn-family evpn", "  policy vpn-target", f"  peer {peer_ip} enable",
               "  quit", "quit"]
    rollback = [f"bgp {node.asn}", *peer_rollback, "quit", *service_rollback, f"undo evpn vpn-instance {instance}"]
    return config, rollback


def _evpn(payload: TransportIn, nodes: list[_Node], node: _Node) -> tuple[list[str], list[str]]:
    if node.role == "P":
        node.notes.append("Node transit cukup underlay MPLS (LDP/SR) yang sudah ada: tidak ada config EVPN.")
        return [], []
    peer = nodes[-1] if node.role == "A" else nodes[0]
    port = (payload.a_port if node.role == "A" else payload.b_port) or "<port-akses>"
    if port == "<port-akses>":
        node.warnings.append("Port akses belum dipilih: ganti <port-akses>.")
    if node.asn == "<asn>":
        node.warnings.append("ASN BGP tidak terbaca dari RANCID: isi ASN.")
    builder = _evpn_xr if node.dialect == "iosxr" else _evpn_vrp
    return builder(payload, node, peer, port)


# --------------------------------------------------------------------------- entry point

BUILDERS = {"sr-mpls": _sr_mpls, "srv6": _srv6, "vxlan": _vxlan, "evpn": _evpn}
ENTRY = {"vrp": ["system-view"], "iosxr": ["configure"]}


def _device_result(payload: TransportIn, nodes: list[_Node], node: _Node) -> dict[str, Any]:
    config: list[str] = []
    rollback: list[str] = []
    if node.dialect in SUPPORTED[payload.technology]:
        config, rollback = BUILDERS[payload.technology](payload, nodes, node)
    else:
        node.warnings.append(f"Template {TECH_LABELS[payload.technology]} belum tersedia untuk "
                             f"{DIALECT_LABELS.get(node.dialect or '', 'vendor ini')}: konfigurasi manual.")
    entry = ENTRY.get(node.dialect or "", [])
    return {"role": node.role, "device": node.label, "device_id": node.device.id, "dialect": node.dialect,
            "dialect_label": DIALECT_LABELS.get(node.dialect or "", "Tidak didukung"),
            "config": entry + config if config else [], "rollback": entry + rollback if rollback else [],
            "commit": COMMIT.get(node.dialect or "", []) if config else [], "warnings": node.warnings, "notes": node.notes}


def generate_transport(db: Session, payload: TransportIn, config_source: ConfigSource | None = None) -> dict[str, Any]:
    """{"technology", "devices": [A, P…, B]} with config, rollback, commit, warnings and notes per device."""
    source = config_source or (lambda device_id: running_config.fetch(db, device_id))
    nodes = _nodes(db, payload, source)
    if payload.technology == "sr-mpls":
        _check_sr_mpls(payload, nodes)
    asns = {node.asn for node in nodes if node.role != "P" and node.asn != "<asn>"}
    warnings = [SYNTAX_WARNING]
    if len(asns) > 1 and payload.technology != "sr-mpls":
        warnings.append(f"ASN PE A dan B berbeda ({', '.join(sorted(asns))}): template memakai iBGP; sesuaikan untuk eBGP.")
    devices = [_device_result(payload, nodes, node) for node in nodes]
    return {"technology": payload.technology, "label": TECH_LABELS[payload.technology], "devices": devices,
            "warnings": warnings}
