"""ZTE ZXCTN (CSR / metro 6120H, 6180H, 9000) and ZXR10 M6000 (BNG/CGN) running-config parser.

Interfaces are top-level `interface <name>` blocks. LAG membership lives in the `lacp` section:
    lacp
      interface xgei-1/1/0/9
        smartgroup 1 mode active
IS-IS: `router isis <n>` with either `net <NET>` or `area <area>` + `system-id <id>`.
"""

from .base import Block, ParsedConfig, indent_tree, ip_only, to_cidr, to_int

LOOPBACKS = ("loopback0", "loopback1")


def _interface(cfg: ParsedConfig, block: Block) -> None:
    name = block.words[1]
    itf = cfg.iface(name)
    itf.is_bundle = name.lower().startswith("smartgroup") and "." not in name
    for c in block.children:
        cw = c.words
        if cw[0] == "description" and len(cw) > 1:
            itf.description = c.text.split(None, 1)[1]
        elif cw[0] == "mtu" and len(cw) > 1:
            itf.mtu = to_int(cw[1])
        elif cw[:2] == ["ip", "address"] and len(cw) >= 3 and "secondary" not in cw:
            itf.ipv4 = to_cidr(cw[2], cw[3] if len(cw) > 3 and "." in cw[3] else None)
        elif cw[0] == "shutdown":
            itf.shutdown = True
        elif cw[:2] == ["no", "shutdown"]:
            itf.shutdown = False
    if name.lower() in LOOPBACKS and itf.ipv4 and not cfg.loopback_ipv4:
        cfg.loopback_ipv4 = ip_only(itf.ipv4)


def _lacp(cfg: ParsedConfig, block: Block) -> None:
    for member in block.children:
        if member.words[0] != "interface" or len(member.words) < 2:
            continue
        group = member.child("smartgroup")
        if group and len(group.words) > 1:
            cfg.iface(member.words[1]).bundle = f"smartgroup{group.words[1]}"


def _isis(cfg: ParsedConfig, block: Block) -> None:
    net = block.child("net")
    if net and len(net.words) > 1:
        cfg.isis_net = net.words[1]
        return
    area, system_id = block.child("area"), block.child("system-id")
    if area and system_id and len(area.words) > 1 and len(system_id.words) > 1:
        cfg.isis_net = f"{area.words[1]}.{system_id.words[1]}.00"


def parse(text: str) -> ParsedConfig:
    cfg = ParsedConfig(vendor="zte")
    for block in indent_tree(text):
        words = block.words
        if words[0] == "hostname" and len(words) > 1:
            cfg.hostname = words[1]
        elif words[0] == "interface" and len(words) > 1:
            _interface(cfg, block)
        elif words[0] == "lacp":
            _lacp(cfg, block)
        elif words[:2] == ["router", "isis"] and not cfg.isis_net:
            _isis(cfg, block)
    # Smartgroup parents referenced only from the lacp section still count as bundles.
    for name in list(cfg.bundles()):
        cfg.iface(name).is_bundle = True
    return cfg
