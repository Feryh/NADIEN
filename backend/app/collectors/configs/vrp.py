"""Huawei VRP (NE40E / NE8000) current-configuration parser."""

from .base import ParsedConfig, find_blocks, indent_tree, ip_only, to_cidr, to_int

LOOPBACKS = ("loopback0",)


def parse(text: str) -> ParsedConfig:
    cfg = ParsedConfig(vendor="vrp")
    for block in indent_tree(text):
        words = block.words
        if words[0] == "sysname" and len(words) > 1:
            cfg.hostname = words[1]
        elif words[0] == "interface" and len(words) > 1:
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
    return cfg
