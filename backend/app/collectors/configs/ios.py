"""Cisco IOS / IOS-XE / NX-OS running-config parser (Catalyst, Nexus, ASR920 classic).

`channel-group N mode ...` under a physical port makes it a member of Port-channelN
(NX-OS writes the bundle as `port-channelN`; both forms are matched case-insensitively later).

RANCID adds the module list of a Nexus as comments (`!Slot 1: type 48x10/25G/32G + 6x40/100G ..., 54 ports`).
Observium leaves out ports without a transceiver, so the `EthernetSLOT/PORT` ports of such a module are card ports:
they exist on the box and count in its port capacity, at the fastest Ethernet speed of their group.
"""

import re

from .base import ParsedConfig, find_blocks, indent_tree, ip_only, to_cidr, to_int

LOOPBACKS = ("loopback0",)

_NXOS = re.compile(r"^(!Image:.*NXOS Image|boot nxos\b)", re.MULTILINE)
_SLOT_LINE = re.compile(r"^!Slot (\d+): type (.+?), (\d+) ports\s*$", re.MULTILINE)
_PORT_GROUP = re.compile(r"(\d+)x([\d/.]+)G\b")
_ETHERNET_SPEEDS = {1.0, 2.5, 5.0, 10.0, 25.0, 40.0, 50.0, 100.0, 200.0, 400.0, 800.0}
_NXOS_ETHERNET = re.compile(r"^Ethernet(\d+)/(\d+)$", re.IGNORECASE)


def _group_speed(speeds: str) -> float | None:
    # "10/25G/32G": the 32G is Fibre Channel; the port's Ethernet capability is 25G.
    values = {float(v) for v in re.findall(r"[\d.]+", speeds)} & _ETHERNET_SPEEDS
    return max(values) if values else None


def module_port_speeds(text: str) -> dict[int, list[float | None]]:
    """Per slot, the speed of port 1, 2, ... from RANCID's `!Slot N: type 48x25G + 6x100G ..., 54 ports` line."""
    slots: dict[int, list[float | None]] = {}
    for slot, spec, total in _SLOT_LINE.findall(text):
        speeds: list[float | None] = []
        for count, group in _PORT_GROUP.findall(spec):
            speeds += [_group_speed(group)] * int(count)
        if speeds and len(speeds) == int(total):
            slots[int(slot)] = speeds
    return slots


def _mark_card_ports(cfg: ParsedConfig, slots: dict[int, list[float | None]]) -> None:
    for itf in cfg.interfaces.values():
        match = _NXOS_ETHERNET.match(itf.name)
        speeds = slots.get(int(match.group(1))) if match else None
        port = int(match.group(2)) if match else 0
        if speeds and 1 <= port <= len(speeds):
            itf.card_port = True
            itf.speed_gbps = speeds[port - 1]


def parse(text: str) -> ParsedConfig:
    cfg = ParsedConfig(vendor="ios")
    # NX-OS writes `no shutdown` for enabled ports; one without it is shut down (the Nexus 9000 default).
    nxos = bool(_NXOS.search(text))
    blocks = indent_tree(text)
    for block in blocks:
        words = block.words
        if words[0] == "hostname" and len(words) > 1:
            cfg.hostname = words[1]
        elif words[0] == "interface" and len(words) > 1:
            name = words[1]
            itf = cfg.iface(name)
            itf.is_bundle = name.lower().startswith("port-channel") and "." not in name
            if nxos and _NXOS_ETHERNET.match(name):
                itf.shutdown = True
            for c in block.children:
                cw = c.words
                if cw[0] == "description" and len(cw) > 1:
                    itf.description = c.text.split(None, 1)[1]
                elif cw[0] == "mtu" and len(cw) > 1:
                    itf.mtu = to_int(cw[1])
                elif cw[:2] == ["ip", "address"] and len(cw) >= 3 and "secondary" not in cw and cw[2] != "dhcp":
                    itf.ipv4 = to_cidr(cw[2], cw[3] if len(cw) > 3 else None)
                elif cw[0] == "channel-group" and len(cw) > 1:
                    itf.bundle = f"Port-channel{cw[1]}"
                elif cw[0] == "shutdown":
                    itf.shutdown = True
                elif cw[:2] == ["no", "shutdown"]:
                    itf.shutdown = False
            if name.lower() in LOOPBACKS:
                cfg.loopback_ipv4 = ip_only(itf.ipv4)
    _mark_card_ports(cfg, module_port_speeds(text))
    net = next(find_blocks(blocks, lambda b: b.words[0] == "net" and len(b.words) > 1), None)
    if net:
        cfg.isis_net = net.words[1]
    return cfg
