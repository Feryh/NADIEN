"""Huawei VRP (NE40E / NE8000) current-configuration parser, plus the uplink boards of Huawei OLTs (MA5600T/MA5800)."""

import re

from .base import ParsedConfig, find_blocks, indent_tree, ip_only, to_cidr, to_int

LOOPBACKS = ("loopback0",)

# OLT uplink boards: `interface mpu 0/8` holds per-port lines (`speed 1 10000`, `shutdown 2` …) for ports
# 0/8/0-3. Observium reports only the ports in use, as `ethernet0/8/1`; the others still exist on the board.
_OLT_BOARD = re.compile(r"^\s*interface (mpu|giu|eth|ethb|scu) (\d+)/(\d+)\s*$")
_OLT_BOARD_TYPES = {"mpu", "giu", "eth", "ethb", "scu"}
_OLT_PORT_LINE = re.compile(r"^\s*(undo\s+)?(speed|auto-neg|shutdown|mdi|duplex|flow-control|native-vlan)\s+(\d+)\b\s*(\S*)")
_OLT_SECTION_END = re.compile(r"^\s*(interface\s|<|#|quit\b|return\b)")


def _olt_uplink_ports(text: str, cfg: ParsedConfig) -> None:
    """The OLT config is not indented consistently (sections end where the next `<section>` marker starts), so the
    board blocks are read line by line."""
    board: tuple[str, str] | None = None
    for line in text.splitlines():
        if match := _OLT_BOARD.match(line):
            board = (match.group(2), match.group(3))
            continue
        if _OLT_SECTION_END.match(line):
            board = None
            continue
        match = _OLT_PORT_LINE.match(line) if board else None
        if not match:
            continue
        undo, command, port, value = match.groups()
        itf = cfg.iface(f"ethernet{board[0]}/{board[1]}/{port}")
        itf.card_port = True
        if command == "shutdown":
            itf.shutdown = not undo
        elif command == "speed" and not undo and value.isdigit():
            itf.speed_gbps = int(value) / 1000


def parse(text: str) -> ParsedConfig:
    cfg = ParsedConfig(vendor="vrp")
    for block in indent_tree(text):
        words = block.words
        if words[0] == "sysname" and len(words) > 1:
            cfg.hostname = words[1]
        elif words[0] == "interface" and len(words) > 1 and not (len(words) == 3 and words[1] in _OLT_BOARD_TYPES):
            name = words[1]
            itf = cfg.iface(name)
            itf.is_bundle = name.lower().startswith("eth-trunk") and "." not in name
            for c in block.children:
                cw = c.words
                if cw[0] == "description":
                    itf.description = c.text.split(None, 1)[1] if len(cw) > 1 else None
                elif cw[0] == "mtu" and len(cw) > 1:
                    itf.mtu = to_int(cw[1])
                elif cw[:2] == ["jumboframe", "enable"] and len(cw) > 2:
                    itf.mtu = itf.mtu or to_int(cw[2])
                elif cw[:2] == ["ip", "address"] and len(cw) >= 4 and "sub" not in cw:
                    itf.ipv4 = to_cidr(cw[2], cw[3])
                elif cw[0] == "eth-trunk" and len(cw) > 1:
                    itf.bundle = f"Eth-Trunk{cw[1]}"
                elif cw[0] == "shutdown":
                    itf.shutdown = True
                elif cw[:2] == ["undo", "shutdown"]:
                    itf.shutdown = False
            if name.lower() in LOOPBACKS:
                cfg.loopback_ipv4 = ip_only(itf.ipv4)
        elif words[0] == "isis":
            net = block.child("network-entity")
            if net and len(net.words) > 1:
                cfg.isis_net = net.words[1]
        elif words[:2] == ["segment-routing", "ipv6"]:
            # locator MAIN ipv6-prefix fc00:0:1:: 48 [static 32]
            for loc in find_blocks([block], lambda b: b.words[0] == "locator" and "ipv6-prefix" in b.words):
                w = loc.words
                i = w.index("ipv6-prefix")
                if len(w) > i + 2:
                    cfg.srv6_locator = f"{w[i + 1]}/{w[i + 2]}"
                    break
    _olt_uplink_ports(text, cfg)
    return cfg
