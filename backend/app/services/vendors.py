"""Which CLI dialect a device speaks — shared by the MoP (Phase 6) and config generator (Phase 7)."""

import re
from typing import Literal

Dialect = Literal["vrp", "zte", "iosxr", "fiberhome"]

DIALECT_LABELS: dict[str, str] = {
    "vrp": "Huawei VRP", "zte": "ZTE ZXR10/ZXCTN", "iosxr": "Cisco IOS-XR", "fiberhome": "FiberHome FOS",
}

# Hostname hints when the inventory has no vendor/OS: model or vendor tokens seen in this network.
_HOSTNAME_HINTS: list[tuple[re.Pattern[str], Dialect]] = [
    (re.compile(r"(ASR9K|ASR9\d{3}|NCS\d|C8201|XRV)", re.I), "iosxr"),
    (re.compile(r"(-ZTE\b|M6K|6120H|ZXCTN|C300|C600)", re.I), "zte"),
    (re.compile(r"(-FH\d*\b|-FBH\b|FH820|R8\d0)", re.I), "fiberhome"),
    (re.compile(r"(-HW\b|NE40|NE8000|ATN\d|-M[48]\b|-X8\b|S67\d\d|S57\d\d)", re.I), "vrp"),
]


def dialect_for(vendor: str | None, os_family: str | None, hostname: str = "") -> Dialect | None:
    """Huawei -> vrp, ZTE -> zte, Cisco IOS-XR -> iosxr, FiberHome -> fiberhome; None when unknown
    (classic IOS/NX-OS and other vendors are outside the generators for now)."""
    v = (vendor or "").lower()
    os_ = (os_family or "").lower()
    if "huawei" in v or "vrp" in os_:
        return "vrp"
    if "zte" in v or "zx" in os_:
        return "zte"
    if "fiberhome" in v or "fos" == os_:
        return "fiberhome"
    if "cisco" in v and ("xr" in os_ or not os_):
        return "iosxr" if "xr" in os_ or _hint(hostname) == "iosxr" else None
    return _hint(hostname) if not v else None


def _hint(hostname: str) -> Dialect | None:
    return next((dialect for pattern, dialect in _HOSTNAME_HINTS if pattern.search(hostname)), None)
