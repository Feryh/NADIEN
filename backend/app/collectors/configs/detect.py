"""Pick the right parser from an explicit hint (Observium OS / vendor) or from the text itself."""

import re
from collections.abc import Callable

from . import iosxr, junos, sros, vrp
from .base import ParsedConfig

PARSERS: dict[str, Callable[[str], ParsedConfig]] = {
    "iosxr": iosxr.parse,
    "vrp": vrp.parse,
    "junos": junos.parse,
    "sros": sros.parse,
}

# Accepted hints: parser keys, Observium `os` values and inventory OS family names.
HINTS = {
    "iosxr": "iosxr", "ios-xr": "iosxr",
    "vrp": "vrp", "huawei": "vrp",
    "junos": "junos", "juniper": "junos",
    "sros": "sros", "sr os": "sros", "timos": "sros", "nokia": "sros",
}


def detect_vendor(text: str, hint: str | None = None) -> str | None:
    if hint and hint.strip().lower() in HINTS:
        return HINTS[hint.strip().lower()]
    if re.search(r"^\s*sysname\s", text, re.M) or "#\ninterface" in text and "undo " in text:
        return "vrp"
    if re.search(r"^set system host-name", text, re.M) or re.search(r"^\s*host-name\s+\S+;", text, re.M):
        return "junos"
    if "TiMOS" in text or re.search(r"^\s+port \d+/\d+/c?\d+", text, re.M) and "configure" in text:
        return "sros"
    if re.search(r"^interface (Bundle-Ether|HundredGigE|TenGigE|FourHundredGigE)", text, re.M) or "IOS XR" in text:
        return "iosxr"
    return None


def parse_config(text: str, hint: str | None = None) -> ParsedConfig:
    vendor = detect_vendor(text, hint)
    if vendor is None:
        raise ValueError("Cannot detect config vendor; pass vendor=iosxr|vrp|junos|sros")
    return PARSERS[vendor](text)
