"""Cisco IOS / IOS-XE / NX-OS running-config parser (Catalyst, Nexus, ASR920 classic).

`channel-group N mode ...` under a physical port makes it a member of Port-channelN
(NX-OS writes the bundle as `port-channelN`; both forms are matched case-insensitively later).
"""

from .base import ParsedConfig, find_blocks, indent_tree, ip_only, to_cidr, to_int

LOOPBACKS = ("loopback0",)


def parse(text: str) -> ParsedConfig:
    cfg = ParsedConfig(vendor="ios")
    blocks = indent_tree(text)
    for block in blocks:
        words = block.words
        if words[0] == "hostname" and len(words) > 1:
            cfg.hostname = words[1]
        elif words[0] == "interface" and len(words) > 1:
            name = words[1]
            itf = cfg.iface(name)
            itf.is_bundle = name.lower().startswith("port-channel") and "." not in name
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
    net = next(find_blocks(blocks, lambda b: b.words[0] == "net" and len(b.words) > 1), None)
    if net:
        cfg.isis_net = net.words[1]
    return cfg
