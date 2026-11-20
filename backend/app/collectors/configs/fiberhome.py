"""FiberHome CiTRANS R8xx metro / CSR (FOS) running-config parser.

Interface names carry a space (`interface eth-10gi 0/1/0/2`, `interface lag 1`); they are kept as
written and matched space-insensitively elsewhere. LAG members are added inside the LAG block:
    interface lag 1
     lag mode lacp
     lag add interface eth-10gi 0/1/0/2
IS-IS NET: ` net 49.0001.0000.0000.0001.00` inside the isis block.
"""

import re

from .base import Block, ParsedConfig, find_blocks, indent_tree, ip_only, to_cidr, to_int

LOOPBACK_PREFIX = "loopback"
# RANCID sometimes captures the pager's echo in front of a line: `                      showrun showrun showrun
# interface lag 2`. Indented, that line would read as part of the interface before it, which would take lag 2's
# address and description (DPS-SGR eth-25gi 0/1/0/24.1155 got 172.15.3.169 instead of 172.16.39.2).
_PAGER_ECHO = re.compile(r"^[ \t]*(?:showrun[ \t]+)+(?=\S)", re.MULTILINE)


def _interface(cfg: ParsedConfig, block: Block) -> None:
    name = " ".join(block.words[1:])
    itf = cfg.iface(name)
    itf.is_bundle = block.words[1].lower() == "lag" and "." not in name
    for c in block.children:
        cw = c.words
        if cw[0] == "description" and len(cw) > 1:
            itf.description = c.text.split(None, 1)[1]
        elif cw[0] == "mtu" and len(cw) > 1:
            itf.mtu = to_int(cw[1])
        elif cw[:2] == ["ip", "address"] and len(cw) >= 3 and cw[2] != "unnumbered" and "secondary" not in cw:
            itf.ipv4 = to_cidr(cw[2], cw[3] if len(cw) > 3 and "." in cw[3] else None)
        elif cw[:3] == ["lag", "add", "interface"] and len(cw) > 3:
            cfg.iface(" ".join(cw[3:])).bundle = name
        elif cw[0] == "shutdown":
            itf.shutdown = True
        elif cw[:2] == ["no", "shutdown"]:
            itf.shutdown = False
    if name.lower().startswith(LOOPBACK_PREFIX) and itf.ipv4 and not cfg.loopback_ipv4:
        cfg.loopback_ipv4 = ip_only(itf.ipv4)


def parse(text: str) -> ParsedConfig:
    cfg = ParsedConfig(vendor="fiberhome")
    blocks = indent_tree(_PAGER_ECHO.sub("", text))
    for block in blocks:
        words = block.words
        if words[0] in ("hostname", "sysname") and len(words) > 1:
            cfg.hostname = words[1]
        elif words[0] == "interface" and len(words) > 1:
            _interface(cfg, block)
    net = next(find_blocks(blocks, lambda b: b.words[0] == "net" and len(b.words) > 1), None)
    if net:
        cfg.isis_net = net.words[1]
    return cfg
