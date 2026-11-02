"""Shared structures for vendor config parsers.

Every parser turns raw configuration text into a ParsedConfig: the facts the inventory needs
that SNMP/Observium does not provide reliably — bundle (LAG) membership, interface IPs and MTU,
loopback, IS-IS NET and SRv6 locator.
"""

import ipaddress
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field


@dataclass
class ParsedInterface:
    name: str
    description: str | None = None
    mtu: int | None = None
    ipv4: str | None = None  # CIDR, e.g. 10.0.0.1/30
    bundle: str | None = None  # parent bundle name using the device's own naming (Bundle-Ether10, ae10, ...)
    is_bundle: bool = False
    shutdown: bool = False


@dataclass
class ParsedConfig:
    vendor: str
    hostname: str | None = None
    loopback_ipv4: str | None = None
    isis_net: str | None = None
    srv6_locator: str | None = None
    interfaces: dict[str, ParsedInterface] = field(default_factory=dict)

    def iface(self, name: str) -> ParsedInterface:
        if name not in self.interfaces:
            self.interfaces[name] = ParsedInterface(name=name)
        return self.interfaces[name]

    def bundles(self) -> dict[str, list[str]]:
        groups: dict[str, list[str]] = {}
        for itf in self.interfaces.values():
            if itf.bundle:
                groups.setdefault(itf.bundle, []).append(itf.name)
        return groups

    def as_dict(self) -> dict:
        data = asdict(self)
        data["interfaces"] = list(data["interfaces"].values())
        data["bundles"] = self.bundles()
        return data


# --------------------------------------------------------------------------- config trees


@dataclass
class Block:
    text: str
    children: list["Block"] = field(default_factory=list)
    indent: int = 0

    @property
    def words(self) -> list[str]:
        return self.text.split() or [""]

    def child(self, prefix: str) -> "Block | None":
        return next((c for c in self.children if c.text == prefix or c.text.startswith(prefix + " ")), None)

    def walk(self) -> Iterator["Block"]:
        for c in self.children:
            yield c
            yield from c.walk()


def find_blocks(blocks: list[Block], predicate: Callable[[Block], bool]) -> Iterator[Block]:
    for b in blocks:
        if predicate(b):
            yield b
        yield from find_blocks(b.children, predicate)


_SKIP_LINES = {"exit", "exit all", "quit", "return", "end", "configure", "}"}


def indent_tree(text: str, comment_prefixes: tuple[str, ...] = ("!", "#")) -> list[Block]:
    """Nest lines by indentation (IOS-XR, VRP, SR OS classic)."""
    root = Block("", indent=-1)
    stack = [root]
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith(comment_prefixes) or stripped in _SKIP_LINES or stripped.startswith("echo "):
            continue
        indent = len(line) - len(line.lstrip())
        while stack[-1].indent >= indent:
            stack.pop()
        block = Block(stripped, indent=indent)
        stack[-1].children.append(block)
        stack.append(block)
    return root.children


def brace_tree(text: str) -> list[Block]:
    """Nest `name { ... }` blocks (Junos, SR OS MD-CLI). Leaf statements may end with ';'."""
    root = Block("")
    stack = [root]
    for raw in text.splitlines():
        line = raw.split("##")[0].strip()
        if not line or line.startswith(("#", "/*", "*")):
            continue
        if line.startswith("inactive:"):
            line = line[len("inactive:"):].strip()
        if line.endswith("{"):
            block = Block(line[:-1].strip())
            stack[-1].children.append(block)
            stack.append(block)
        elif line.startswith("}"):
            if len(stack) > 1:
                stack.pop()
        else:
            stack[-1].children.append(Block(line.rstrip(";").strip()))
    return root.children


def looks_braced(text: str) -> bool:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return sum(1 for l in lines if l.endswith("{")) >= 3


# --------------------------------------------------------------------------- helpers


def unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def to_cidr(address: str, mask_or_prefix: str | None) -> str | None:
    """('10.0.0.1', '255.255.255.252') or ('10.0.0.1', '30') or ('10.0.0.1/30', None) -> '10.0.0.1/30'."""
    try:
        if mask_or_prefix is None:
            return str(ipaddress.ip_interface(address))
        return str(ipaddress.ip_interface(f"{address}/{mask_or_prefix}"))
    except ValueError:
        return None


def ip_only(cidr: str | None) -> str | None:
    return cidr.split("/")[0] if cidr else None


def to_int(value: str) -> int | None:
    try:
        return int(value)
    except ValueError:
        return None
