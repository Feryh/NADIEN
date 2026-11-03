"""Pick the right parser from an explicit hint (Observium OS / vendor) or from the text itself."""

import re
from collections.abc import Callable

from . import fiberhome, ios, iosxr, junos, sros, vrp, zte
from .base import ParsedConfig

PARSERS: dict[str, Callable[[str], ParsedConfig]] = {
    "iosxr": iosxr.parse,
    "vrp": vrp.parse,
    "junos": junos.parse,
    "sros": sros.parse,
    "zte": zte.parse,
    "fiberhome": fiberhome.parse,
    "ios": ios.parse,
}

# Accepted hints: parser keys, Observium `os` values and inventory OS family names.
HINTS = {
    "iosxr": "iosxr", "ios-xr": "iosxr",
    "vrp": "vrp", "huawei": "vrp",
    "junos": "junos", "juniper": "junos",
    "sros": "sros", "sr os": "sros", "timos": "sros", "nokia": "sros",
    "zte": "zte", "zxros": "zte", "zxr10": "zte",
    "fiberhome": "fiberhome", "fos": "fiberhome",
    "ios": "ios", "iosxe": "ios", "ios-xe": "ios", "nxos": "ios", "nx-os": "ios", "cisco": "ios",
}


def detect_vendor(text: str, hint: str | None = None) -> str | None:
    if hint and hint.strip().lower() in HINTS:
        vendor = HINTS[hint.strip().lower()]
        # RANCID's generic "ios"/"cisco" type is also used for IOS-XR boxes; the text decides.
        return "iosxr" if vendor == "ios" and _looks_iosxr(text) else vendor
    if re.search(r"^\s*sysname\s", text, re.M) or "#\ninterface" in text and "undo " in text:
        return "vrp"
    if re.search(r"^set system host-name", text, re.M) or re.search(r"^\s*host-name\s+\S+;", text, re.M):
        return "junos"
    if "TiMOS" in text or re.search(r"^\s+port \d+/\d+/c?\d+", text, re.M) and "configure" in text:
        return "sros"
    if _looks_iosxr(text):
        return "iosxr"
    if "ZXCTN" in text or "ZXR10" in text or re.search(r"^interface (x{0,2}v?gei|cgei|smartgroup)", text, re.M):
        return "zte"
    if re.search(r"^interface eth-\d*gi ", text, re.M) or "Fiberhome" in text:
        return "fiberhome"
    if re.search(r"^interface (GigabitEthernet|TenGigabitEthernet|Ethernet|Port-channel|port-channel)", text, re.M):
        return "ios"
    return None


def _looks_iosxr(text: str) -> bool:
    return bool(re.search(r"^interface (Bundle-Ether|HundredGigE|TenGigE|FourHundredGigE)", text, re.M)) or "IOS XR" in text


def parse_config(text: str, hint: str | None = None) -> ParsedConfig:
    vendor = detect_vendor(text, hint)
    if vendor is None:
        raise ValueError("Cannot detect config vendor; pass vendor=" + "|".join(PARSERS))
    return PARSERS[vendor](text)
