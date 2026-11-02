"""Nokia SR OS parser — classic CLI (`admin display-config`) and MD-CLI (brace) formats."""

from .base import Block, ParsedConfig, brace_tree, find_blocks, indent_tree, ip_only, looks_braced, to_cidr, to_int, unquote


def _lag_name(token: str) -> str:
    token = unquote(token)
    return f"lag-{token}" if token.isdigit() else token


def _shutdown(block: Block) -> bool:
    for c in block.children:
        if c.text == "shutdown" or c.text == "admin-state disable":
            return True
    return False


def parse(text: str) -> ParsedConfig:
    cfg = ParsedConfig(vendor="sros")
    tree = brace_tree(text) if looks_braced(text) else indent_tree(text, comment_prefixes=("#",))

    for system in find_blocks(tree, lambda b: b.text == "system"):
        name = system.child("name")
        if name and len(name.words) > 1:
            cfg.hostname = unquote(name.text.split(None, 1)[1])
            break

    # Physical ports: `port 1/1/c1/1` (top level of configure, not the `port x` lines inside lag/interface).
    for port in find_blocks(tree, lambda b: b.words[0] == "port" and len(b.words) == 2 and b.children):
        itf = cfg.iface(unquote(port.words[1]))
        desc = port.child("description")
        if desc and len(desc.words) > 1:
            itf.description = unquote(desc.text.split(None, 1)[1])
        eth = port.child("ethernet")
        mtu = eth.child("mtu") if eth else None
        if mtu and len(mtu.words) > 1:
            itf.mtu = to_int(mtu.words[1])
        itf.shutdown = _shutdown(port)

    for lag in find_blocks(tree, lambda b: b.words[0] == "lag" and len(b.words) == 2 and b.children):
        name = _lag_name(lag.words[1])
        itf = cfg.iface(name)
        itf.is_bundle = True
        desc = lag.child("description")
        if desc and len(desc.words) > 1:
            itf.description = unquote(desc.text.split(None, 1)[1])
        itf.shutdown = _shutdown(lag)
        for member in lag.children:
            if member.words[0] == "port" and len(member.words) >= 2:
                cfg.iface(unquote(member.words[1])).bundle = name

    # IP addressing lives on router interfaces that point at a port or LAG.
    for router in find_blocks(tree, lambda b: b.words[0] == "router" and len(b.words) <= 2):
        for intf in router.children:
            if intf.words[0] != "interface" or len(intf.words) < 2:
                continue
            ifname = unquote(intf.words[1])
            cidr = _address(intf)
            if ifname == "system":
                cfg.loopback_ipv4 = ip_only(cidr)
                continue
            port = intf.child("port")
            if port and len(port.words) > 1 and cidr:
                target = unquote(port.words[1]).split(":")[0]
                if not cfg.iface(target).ipv4:
                    cfg.iface(target).ipv4 = cidr
        isis = next(find_blocks(router.children, lambda b: b.words[0] == "isis"), None)
        area = next(find_blocks([isis], lambda b: b.words[0] == "area-id" or b.words[0] == "area-address"), None) if isis else None
        if area and len(area.words) > 1:
            cfg.isis_net = unquote(area.words[1]).strip("[]")  # MD-CLI lists: area-address [49.0001]

    for loc in find_blocks(tree, lambda b: b.words[0] == "locator" and len(b.words) > 1):
        prefix = next(find_blocks(loc.children, lambda b: b.words[0] in ("ip-prefix", "prefix") and len(b.words) > 1), None)
        if prefix:
            cfg.srv6_locator = unquote(prefix.words[-1])
            break
    return cfg


def _address(intf: Block) -> str | None:
    # classic: `address 10.1.1.1/30`; MD-CLI: ipv4 { primary { address 10.1.1.1 prefix-length 30 } }
    for b in find_blocks(intf.children, lambda b: b.words[0] == "address" and len(b.words) > 1):
        if "/" in b.words[1]:
            return to_cidr(b.words[1], None)
        siblings = b.words
        if "prefix-length" in siblings:
            return to_cidr(b.words[1], siblings[siblings.index("prefix-length") + 1])
    primary = next(find_blocks(intf.children, lambda b: b.text == "primary"), None)
    if primary:
        addr = primary.child("address")
        plen = primary.child("prefix-length")
        if addr and plen:
            return to_cidr(addr.words[1], plen.words[1])
    return None
