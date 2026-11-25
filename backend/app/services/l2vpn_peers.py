"""L2VPN neighbours already configured on a PE, read from its RANCID running config.

The config generator offers these as the destination of a new pseudowire (the far PE's loopback is usually
already a neighbour of the source) and checks the PW / VC id against them. One reader per dialect, matching the
forms the generator itself writes:
- Huawei VRP: `vsi NAME` blocks with `vsi-id N` and `peer IP`, plus VPWS `mpls l2vc IP N` on interfaces.
- ZTE ZXROS: `neighbour IP vcid N` under a pseudo-wire.
- Cisco IOS-XR: `neighbor ipv4 IP pw-id N` (also the older `neighbor IP pw-id N`).
- FiberHome: `vfi NAME` blocks with `vsi-id N` and `peer IP …`.
`vpls_instances` lists the VPLS instances themselves (name, VC/VSI id, peers, attachment circuits) for the same
four dialects, so site B can add one peer to an existing instance instead of building a new one.
Junos and SR OS are not generator dialects (no L2 template), so they have no reader here.
Pure: no database, no network.
"""

import ipaddress
import re
from collections import defaultdict

_ZTE = re.compile(r"^\s*neighbour (\S+) vcid (\d+)", re.M)
_XR = re.compile(r"^\s*neighbor (?:ipv4 )?(\S+) pw-id (\d+)", re.M)
_VRP_L2VC = re.compile(r"^\s*mpls l2vc (\S+) (\d+)", re.M)
_BLOCK_START = {"vrp": re.compile(r"^vsi (\S+)"), "fiberhome": re.compile(r"^vfi (\S+)")}
_VSI_ID = re.compile(r"^\s+vsi-id (\d+)")
_PEER = re.compile(r"^\s+peer (\S+)")
_NEGOTIATION_VC_ID = re.compile(r"\bnegotiation-vc-id (\d+)")


def _is_ipv4(text: str) -> bool:
    try:
        ipaddress.IPv4Address(text)
    except ValueError:
        return False
    return True


def _vsi_blocks(text: str, start: re.Pattern[str]) -> list[tuple[str, int | None]]:
    """(peer, vsi-id) for every peer of every VSI/VFI block; a block ends at the next unindented line."""
    pairs: list[tuple[str, int | None]] = []
    in_block, vsi_id, peers = False, None, []
    for line in text.splitlines() + [""]:
        if line and not line[0].isspace() or not line:
            if in_block:
                pairs += [(peer, own if own is not None else vsi_id) for peer, own in peers]
            in_block, vsi_id, peers = bool(line and start.match(line)), None, []
            continue
        if in_block:
            if match := _VSI_ID.match(line):
                vsi_id = int(match.group(1))
            elif match := _PEER.match(line):
                own = _NEGOTIATION_VC_ID.search(line)
                peers.append((match.group(1), int(own.group(1)) if own else None))
    return pairs


def pw_usage(text: str, dialect: str | None) -> list[tuple[str, int | None]]:
    """(neighbour IP, PW/VC id) pairs configured on the device; the id is None when the block has none."""
    if dialect == "zte":
        pairs = [(ip, int(vc)) for ip, vc in _ZTE.findall(text)]
    elif dialect == "iosxr":
        pairs = [(ip, int(vc)) for ip, vc in _XR.findall(text)]
    elif dialect in _BLOCK_START:
        pairs = _vsi_blocks(text, _BLOCK_START[dialect])
        if dialect == "vrp":
            pairs += [(ip, int(vc)) for ip, vc in _VRP_L2VC.findall(text)]
    else:
        pairs = []
    return [(ip, vc) for ip, vc in pairs if _is_ipv4(ip)]


def neighbours(text: str, dialect: str | None) -> list[dict]:
    """[{"ip", "pw_ids", "count"}] per neighbour, busiest first: what a new pseudowire can point at."""
    ids: dict[str, set[int]] = defaultdict(set)
    count: dict[str, int] = defaultdict(int)
    for ip, vc in pw_usage(text, dialect):
        count[ip] += 1
        if vc is not None:
            ids[ip].add(vc)
    return [{"ip": ip, "pw_ids": sorted(ids[ip]), "count": count[ip]}
            for ip in sorted(count, key=lambda ip: (-count[ip], ipaddress.IPv4Address(ip)))]


# --------------------------------------------------------------------------- VPLS instances a pseudowire can join

VPLS_DIALECTS = ("vrp", "zte", "iosxr", "fiberhome")

_XR_GROUP = re.compile(r"^ bridge group (\S+)\s*$")
_XR_DOMAIN = re.compile(r"^  bridge-domain (\S+)\s*$")
_XR_DOMAIN_ITEM = re.compile(r"^   (interface|vfi) (\S+)\s*$")
_XR_NEIGHBOR = re.compile(r"^\s+neighbor (?:ipv4 )?(\S+) pw-id (\d+)\s*$")
_XR_PW_CLASS = re.compile(r"^\s+pw-class (\S+)\s*$")
_SUB_VLAN = re.compile(r"\.(\d{1,4})$")

_VRP_VSI = re.compile(r"^vsi (\S+)(\s+auto)?\s*$")
_VRP_PWSIGNAL = re.compile(r"^\s+pwsignal (\S+)")
_VRP_PEER = re.compile(r"^\s+peer (\S+)(?:.*?\bnegotiation-vc-id (\d+))?")
_VRP_BINDING = re.compile(r"^\s+l2 binding vsi (\S+)")
_VRP_DOT1Q = re.compile(r"^\s+vlan-type dot1q (\d+)")
_ZTE_VPLS = re.compile(r"^vpls (\S+)\s*$")
_ZTE_ACCESS = re.compile(r"^\s+access-point (\S+)")
_ZTE_PSEUDO_WIRE = re.compile(r"^\s+pseudo-wire (\S+)\s*(.*?)\s*$")
_ZTE_NEIGHBOUR_LINE = re.compile(r"^\s+neighbour (\S+) vcid (\d+)")
_FH_VFI = re.compile(r"^vfi (\S+)\s*$")
_FH_INTERFACE = re.compile(r"^ interface (.+?)\s*$")


def vpls_key(group: str, bridge_domain: str, vfi: str | None) -> str:
    """IOS-XR id of a VPLS instance in a request: "group|bridge-domain|vfi" (vfi empty when the PWs sit on the BD)."""
    return f"{group}|{bridge_domain}|{vfi or ''}"


def _blocks(text: str) -> list[tuple[str, list[str]]]:
    """Top-level blocks: (unindented header, the indented lines under it). VRP `#`, ZTE `$` and FiberHome `!`
    terminators are unindented themselves, so they only start a block nobody reads."""
    blocks: list[tuple[str, list[str]]] = []
    for line in text.splitlines():
        if line and not line[0].isspace():
            blocks.append((line.rstrip(), []))
        elif blocks and line.strip():
            blocks[-1][1].append(line.rstrip())
    return blocks


def vpls_instances(text: str, dialect: str | None) -> list[dict]:
    """VPLS instances already on a PE that a new LDP pseudowire can join, read from its running config.

    Every row: key, name, vsi_id (VRP / FiberHome), interfaces (attachment circuits), neighbours
    [{ip, pw_id, pw_class, pw, options}], vlans (from the attachment circuits) and pw_ids. Readers:
    - Huawei VRP: `vsi NAME` with `pwsignal ldp`, `vsi-id N` and `peer IP [negotiation-vc-id N]`; interfaces from
      `l2 binding vsi NAME`. BGP-signalled `vsi NAME auto` instances are left out (no LDP peer can be added).
    - ZTE ZXROS: `vpls NAME` with `access-point IF` and `pseudo-wire pwN` / `neighbour IP vcid N`.
    - FiberHome: `vfi NAME` with `vsi-id N`, `interface IF` and `peer IP …` (the PW id is the vsi-id).
    - Cisco IOS-XR: `l2vpn / bridge group / bridge-domain / vfi` with `neighbor IP pw-id N` (as on the ASR9K
      metro). One row per VFI, or per bridge-domain whose neighbours sit directly on it (H-VPLS spoke).
    """
    readers = {"vrp": _vrp_instances, "zte": _zte_instances, "fiberhome": _fh_instances, "iosxr": _xr_instances}
    reader = readers.get(dialect or "")
    if reader is None:
        return []
    rows = reader(text)
    for row in rows:
        if "vlans" not in row:
            row["vlans"] = sorted({int(m.group(1)) for name in row["interfaces"] if (m := _SUB_VLAN.search(name))})
        row["pw_ids"] = sorted({n["pw_id"] for n in row["neighbours"] if n["pw_id"] is not None})
    return rows


def _row(key: str, name: str, vsi_id: int | None = None, interfaces: list[str] | None = None) -> dict:
    return {"key": key, "name": name, "vsi_id": vsi_id, "interfaces": interfaces if interfaces is not None else [],
            "neighbours": []}


def _neighbour(ip: str, pw_id: int | None, pw: str | None = None, options: str = "") -> dict:
    return {"ip": ip, "pw_id": pw_id, "pw_class": None, "pw": pw, "options": options}


def _vrp_bindings(blocks: list[tuple[str, list[str]]]) -> dict[str, list[tuple[str, int | None]]]:
    """vsi name -> [(interface, dot1q VLAN)] from `l2 binding vsi NAME` under interfaces."""
    bound: dict[str, list[tuple[str, int | None]]] = defaultdict(list)
    for header, body in blocks:
        if not header.startswith("interface "):
            continue
        binding = next((match.group(1) for line in body if (match := _VRP_BINDING.match(line))), None)
        if binding:
            dot1q = next((int(match.group(1)) for line in body if (match := _VRP_DOT1Q.match(line))), None)
            bound[binding].append((header.split(None, 1)[1], dot1q))
    return bound


def _vrp_instances(text: str) -> list[dict]:
    blocks = _blocks(text)
    bound = _vrp_bindings(blocks)
    rows: list[dict] = []
    for header, body in blocks:
        match = _VRP_VSI.match(header)
        if not match or match.group(2):
            continue
        signal = next((sig.group(1) for line in body if (sig := _VRP_PWSIGNAL.match(line))), None)
        if signal != "ldp":
            continue
        name = match.group(1)
        vsi_id = next((int(vid.group(1)) for line in body if (vid := _VSI_ID.match(line))), None)
        row = _row(f"vsi|{name}", name, vsi_id, [iface for iface, _ in bound[name]])
        row["vlans"] = sorted({dot1q for _, dot1q in bound[name] if dot1q})
        for line in body:
            if (peer := _VRP_PEER.match(line)) and _is_ipv4(peer.group(1)):
                row["neighbours"].append(_neighbour(peer.group(1), int(peer.group(2)) if peer.group(2) else vsi_id))
        rows.append(row)
    return rows


def _zte_instances(text: str) -> list[dict]:
    rows: list[dict] = []
    for header, body in _blocks(text):
        match = _ZTE_VPLS.match(header)
        if not match:
            continue
        row = _row(f"vpls|{match.group(1)}", match.group(1))
        pw, options = None, ""
        for line in body:
            if access := _ZTE_ACCESS.match(line):
                row["interfaces"].append(access.group(1))
            elif wire := _ZTE_PSEUDO_WIRE.match(line):
                pw, options = wire.group(1), wire.group(2)
            elif (neighbour := _ZTE_NEIGHBOUR_LINE.match(line)) and _is_ipv4(neighbour.group(1)):
                row["neighbours"].append(_neighbour(neighbour.group(1), int(neighbour.group(2)), pw=pw, options=options))
        rows.append(row)
    return rows


def _fh_instances(text: str) -> list[dict]:
    rows: list[dict] = []
    for header, body in _blocks(text):
        match = _FH_VFI.match(header)
        if not match:
            continue
        vsi_id = next((int(vid.group(1)) for line in body if (vid := _VSI_ID.match(line))), None)
        row = _row(f"vfi|{match.group(1)}", match.group(1), vsi_id)
        for line in body:
            if iface := _FH_INTERFACE.match(line):
                row["interfaces"].append(iface.group(1))
            elif (peer := _PEER.match(line)) and _is_ipv4(peer.group(1)):
                row["neighbours"].append(_neighbour(peer.group(1), vsi_id))
        rows.append(row)
    return rows


def _xr_instances(text: str) -> list[dict]:
    rows: list[dict] = []
    group = domain = None
    current: dict | None = None  # the VFI or bridge-domain the next neighbour lines belong to
    interfaces: list[str] = []
    in_l2vpn = False
    for line in text.splitlines():
        if line and not line[0].isspace():
            in_l2vpn = line.startswith("l2vpn")
            group = domain = current = None
            continue
        if not in_l2vpn:
            continue
        if match := _XR_GROUP.match(line):
            group, domain, current = match.group(1), None, None
        elif group and (match := _XR_DOMAIN.match(line)):
            domain, current, interfaces = match.group(1), None, []
        elif domain and (match := _XR_DOMAIN_ITEM.match(line)):
            if match.group(1) == "interface":
                interfaces.append(match.group(2))
                current = None
            else:
                current = _xr_row(rows, group, domain, match.group(2), interfaces)
        elif domain and (match := _XR_NEIGHBOR.match(line)):
            if current is None or (len(line) - len(line.lstrip()) == 3 and current["vfi"]):
                current = _xr_row(rows, group, domain, None, interfaces)
            current["neighbours"].append(_neighbour(match.group(1), int(match.group(2))))
        elif current and current["neighbours"] and (match := _XR_PW_CLASS.match(line)):
            current["neighbours"][-1]["pw_class"] = match.group(1)
    return rows


def _xr_row(rows: list[dict], group: str, domain: str, vfi: str | None, interfaces: list[str]) -> dict:
    key = vpls_key(group, domain, vfi)
    for row in rows:
        if row["key"] == key:
            return row
    # Interfaces listed after the VFI still belong to the bridge-domain: share the list, not a copy.
    row = {**_row(key, vfi or domain, interfaces=interfaces), "bridge_group": group, "bridge_domain": domain,
           "vfi": vfi}
    rows.append(row)
    return row
