"""Cisco IOS-XR running-config parser."""

from .base import ParsedConfig, find_blocks, indent_tree, ip_only, to_cidr, to_int

LOOPBACKS = ("loopback0",)


def parse(text: str) -> ParsedConfig:
    cfg = ParsedConfig(vendor="iosxr")
    tree = indent_tree(text)
    for block in tree:
        words = block.words
        if words[0] == "hostname" and len(words) > 1:
            cfg.hostname = words[1]
        elif words[0] == "interface" and len(words) > 1:
            name = words[2] if words[1] == "preconfigure" and len(words) > 2 else words[1]
            itf = cfg.iface(name)
            itf.is_bundle = name.lower().startswith("bundle-ether") and "." not in name
            for c in block.children:
                cw = c.words
                if cw[0] == "description":
                    itf.description = c.text.split(None, 1)[1] if len(cw) > 1 else None
                elif cw[0] == "mtu" and len(cw) > 1:
                    itf.mtu = to_int(cw[1])
                elif cw[:2] == ["ipv4", "address"] and len(cw) >= 4 and "secondary" not in cw:
                    itf.ipv4 = to_cidr(cw[2], cw[3])
                elif cw[:2] == ["bundle", "id"] and len(cw) > 2:
                    itf.bundle = f"Bundle-Ether{cw[2]}"
                elif cw[0] == "shutdown":
                    itf.shutdown = True
            if name.lower() in LOOPBACKS:
                cfg.loopback_ipv4 = ip_only(itf.ipv4)
        elif words[:2] == ["router", "isis"]:
            net = block.child("net")
            if net and len(net.words) > 1:
                cfg.isis_net = net.words[1]
        elif words[0] == "segment-routing":
            for loc in find_blocks(block.children, lambda b: b.words[0] == "locator" and len(b.words) > 1):
                prefix = loc.child("prefix")
                if prefix and len(prefix.words) > 1:
                    cfg.srv6_locator = prefix.words[1]
                    break
    return cfg
