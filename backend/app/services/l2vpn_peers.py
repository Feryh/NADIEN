"""L2VPN neighbours already configured on a PE, read from its RANCID running config.

The config generator offers these as the destination of a new pseudowire (the far PE's loopback is usually
already a neighbour of the source) and checks the PW / VC id against them. One reader per dialect, matching the
forms the generator itself writes:
- Huawei VRP: `vsi NAME` blocks with `vsi-id N` and `peer IP`, plus VPWS `mpls l2vc IP N` on interfaces.
- ZTE ZXROS: `neighbour IP vcid N` under a pseudo-wire.
- Cisco IOS-XR: `neighbor ipv4 IP pw-id N` (also the older `neighbor IP pw-id N`).
- FiberHome: `vfi NAME` blocks with `vsi-id N` and `peer IP …`.
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
                pairs += [(peer, vsi_id) for peer in peers]
            in_block, vsi_id, peers = bool(line and start.match(line)), None, []
            continue
        if in_block:
            if match := _VSI_ID.match(line):
                vsi_id = int(match.group(1))
            elif match := _PEER.match(line):
                peers.append(match.group(1))
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
