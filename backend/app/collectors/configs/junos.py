"""Juniper Junos parser — accepts both curly-brace config and `display set` output."""

import shlex

from .base import Block, ParsedConfig, brace_tree, ip_only, to_cidr, to_int, unquote

BUNDLE_OPTIONS = ("gigether-options", "ether-options", "aggregated-ether-options")


def _flatten(blocks: list[Block], prefix: list[str]) -> list[list[str]]:
    paths: list[list[str]] = []
    for b in blocks:
        try:
            words = shlex.split(b.text)
        except ValueError:
            words = b.text.split()
        if b.children:
            paths.extend(_flatten(b.children, prefix + words))
        else:
            paths.append(prefix + words)
    return paths


def to_set_paths(text: str) -> list[list[str]]:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    set_lines = [l for l in lines if l.startswith("set ")]
    if set_lines and len(set_lines) >= len(lines) // 2:
        paths = []
        for line in set_lines:
            try:
                paths.append(shlex.split(line)[1:])
            except ValueError:
                paths.append(line.split()[1:])
        return paths
    return _flatten(brace_tree(text), [])


def parse(text: str) -> ParsedConfig:
    cfg = ParsedConfig(vendor="junos")
    for p in to_set_paths(text):
        if p[:2] == ["system", "host-name"] and len(p) > 2:
            cfg.hostname = unquote(p[2])
        elif p[0] == "interfaces" and len(p) > 2:
            name = p[1]
            rest = p[2:]
            if name in ("interface-range",):
                continue
            itf = cfg.iface(name)
            itf.is_bundle = name.startswith("ae") and name[2:].isdigit()
            if rest[0] == "description" and len(rest) > 1:
                itf.description = unquote(" ".join(rest[1:]))
            elif rest[0] == "mtu" and len(rest) > 1:
                itf.mtu = to_int(rest[1])
            elif rest[0] == "disable":
                itf.shutdown = True
            elif rest[0] in BUNDLE_OPTIONS and "802.3ad" in rest:
                i = rest.index("802.3ad")
                if len(rest) > i + 1:
                    itf.bundle = rest[i + 1]
            elif rest[0] == "unit" and len(rest) >= 6 and rest[2:4] == ["family", "inet"] and rest[4] == "address":
                cidr = to_cidr(rest[5], None)
                if name == "lo0":
                    if cidr and not cidr.startswith("127.") and not cfg.loopback_ipv4:
                        cfg.loopback_ipv4 = ip_only(cidr)
                elif not itf.ipv4 and rest[1] == "0":
                    itf.ipv4 = cidr
            elif name == "lo0" and rest[0] == "unit" and len(rest) >= 6 and rest[2:4] == ["family", "iso"] and rest[4] == "address":
                cfg.isis_net = rest[5]
        elif p[:4] == ["routing-options", "source-packet-routing", "srv6", "locator"] and len(p) > 5:
            cfg.srv6_locator = cfg.srv6_locator or p[5]
    # lo0 is not a Metro interface; keep the inventory clean.
    cfg.interfaces.pop("lo0", None)
    return cfg
