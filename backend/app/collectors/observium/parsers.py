"""Pure parsers: raw Observium rows -> typed records for the inventory.

No database access here, so every mapping rule can be unit-tested with plain dicts.
Observium returns most numeric columns as strings and sometimes "" for unknown;
`_num`/`_int` treat both as missing.
"""

import re
from dataclasses import dataclass
from typing import Any

from ...services.areas import FUNCTION_TOKENS, csr_parts, first_city_code, is_city_code
from ...services.topology import strip_domain

# Observium `os` -> (vendor, OS family). Unknown OSes fall back to the row's own `vendor` field.
OS_VENDOR: dict[str, tuple[str, str]] = {
    "iosxr": ("Cisco", "IOS-XR"),
    "iosxe": ("Cisco", "IOS-XE"),
    "ios": ("Cisco", "IOS"),
    "nxos": ("Cisco", "NX-OS"),
    "vrp": ("Huawei", "VRP"),
    "junos": ("Juniper", "Junos"),
    "timos": ("Nokia", "SR OS"),
    "sros": ("Nokia", "SR OS"),
}

# Hostname role tokens (trailing digits ignored: OLT2, PE1, ME1) -> device role.
# Hostnames follow either POP-SITE-ROLE#-MODEL (JKT-ABW-OLT1-HW) or ROLE#-MODEL-POP-SITE
# (OLT2-MA5800-X15-JKTS-TND); the first role token from the left wins.
ROLE_TOKENS: dict[str, str] = {
    "OLT": "olt",
    "BNG": "bng", "BRAS": "bng",
    "DCI": "dci",
    "P": "p", "CORE": "p", "CR": "p",
    "PE": "pe", "PEIN": "pe", "PESH": "pe", "CGW": "pe", "NNI": "pe", "BR": "pe",
    "ME": "agg", "AGG": "agg", "DS": "agg", "CS": "agg",
    "SW": "access", "ACC": "access", "AC": "access", "CSG": "access",
}
_TOKEN_SPLIT = re.compile(r"[-_.\s]+")

# Column sizes in models.Interface; PostgreSQL rejects longer values instead of truncating.
MAX_IFNAME = 128
MAX_DESCRIPTION = 512

BUNDLE_NAME = re.compile(r"^(bundle-ether|be|eth-trunk|ae|lag-?|port-channel|po)\d+$", re.IGNORECASE)
LAG_IFTYPES = {"ieee8023adlag", "propmultiplexor"}
# Logical/internal ports that never carry a Metro link.
SKIP_PORT = re.compile(r"^(null|nu|mgmt|management|fxp|em\d|me\d|lo|loopback|inloopback|tunnel|tu|vlanif|irb|dummy|sit|gre)", re.IGNORECASE)


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    n = _num(value)
    return int(n) if n is not None else None


def _is_ip(name: str) -> bool:
    return bool(re.fullmatch(r"[\d.]+|[0-9a-fA-F:]*:[0-9a-fA-F:]*", name.strip()))


def short_hostname(name: str) -> str:
    """'jkt-p-01.metro.net' -> 'JKT-P-01'; dotted names that are not FQDNs stay whole; IPs unchanged."""
    name = name.strip()
    if _is_ip(name):
        return name
    return strip_domain(name).upper()


# --------------------------------------------------------------------------- devices


@dataclass
class DeviceRecord:
    observium_id: str
    hostname: str
    pop_code: str | None
    pop_name: str | None
    latitude: float | None
    longitude: float | None
    city: str | None
    vendor: str | None
    os_family: str | None
    model: str | None
    software: str | None
    role: str
    mgmt_ip: str | None
    status: str
    observium_hostname: str = ""  # Observium's own hostname: the RRD directory name


def _tokens(hostname: str) -> list[str]:
    return [t for t in _TOKEN_SPLIT.split(hostname.upper()) if t]


def _role_word(token: str) -> str | None:
    return ROLE_TOKENS.get(token.rstrip("0123456789"))


def is_role_word(code: str) -> bool:
    return _role_word(code.upper()) is not None


def role_from_hostname(hostname: str) -> str | None:
    """Role written in the hostname, or None when no token names one."""
    if csr_parts(hostname):  # BAL-BA-BLI-0004-ME1-ZTE: a cell-site router despite its ME1 token
        return "csr"
    return next((role for token in _tokens(hostname) if (role := _role_word(token))), None)


def guess_role(hostname: str) -> str:
    return role_from_hostname(hostname) or "pe"


def pop_from_tokens(hostname: str, regex_code: str | None) -> str | None:
    """A registered city code anywhere in the hostname wins over a first token that is not one
    (VRR-MPLS-JKT-BSD -> JKT, not VRR). Without any registered code the first token stays (BKT-GGP-BNG1-M4
    -> BKT: a real city the dictionary does not list yet), unless it is a function word (NAP, CGN, ...)."""
    if regex_code and is_city_code(regex_code):
        return regex_code
    found = first_city_code(hostname)
    if found:
        return found
    return None if regex_code in FUNCTION_TOKENS else regex_code


def infer_pop(hostname: str, known_codes: set[str]) -> str | None:
    """POP for hostnames the regex missed (role-first convention, e.g. OLT2-MA5800-X15-JKTS-TND).

    A token that is already a known POP code wins (left to right: the POP precedes the site);
    otherwise the first purely alphabetic 3–4 letter token that is not a role word.
    """
    found = first_city_code(hostname)  # CGW-NAP-BDG-BBU -> BDG, never the function word NAP
    if found:
        return found
    tokens = [t for t in _tokens(hostname) if not _role_word(t) and t not in FUNCTION_TOKENS]
    for token in tokens:  # role words are never a POP, even if an older sync created a POP named "OLT"
        if token in known_codes:
            return token
        base = token.rstrip("0123456789")  # JKT2 -> JKT
        if base != token and base in known_codes:
            return base
    for token in tokens:
        if token.isalpha() and 3 <= len(token) <= 4:
            return token
    return None


def parse_device(row: dict[str, Any], pop_regex: str) -> DeviceRecord | None:
    # sysName is what LLDP neighbours advertise, so it is the matching key; it can be unset or an
    # IP on some boxes, in which case the Observium hostname is used instead.
    sysname = (row.get("sysName") or "").strip()
    raw_name = sysname if sysname and not _is_ip(sysname) else (row.get("hostname") or sysname)
    if not raw_name:
        return None
    hostname = short_hostname(raw_name)

    match = re.search(pop_regex, hostname)
    location = (row.get("location") or "").strip() or None
    pop_code = match.group(1).upper() if match else None
    if pop_code and _role_word(pop_code):  # "OLT-..." / "CGW-...": a role, not a POP
        pop_code = None
    cell_site = csr_parts(hostname)
    if cell_site:  # the first token is the island group (BAL, SUM); the city is the POP
        pop_code = cell_site[2]
    else:
        pop_code = pop_from_tokens(hostname, pop_code)

    os_name = (row.get("os") or "").lower()
    vendor, os_family = OS_VENDOR.get(os_name, (row.get("vendor") or None, row.get("os") or None))

    disabled = str(row.get("disabled") or "0") == "1"
    up = str(row.get("status") or "1") == "1"
    return DeviceRecord(
        observium_id=str(row.get("device_id")),
        hostname=hostname,
        pop_code=pop_code,
        pop_name=location,
        latitude=_num(row.get("location_lat")),
        longitude=_num(row.get("location_lon")),
        city=row.get("location_city") or None,
        vendor=vendor,
        os_family=os_family,
        model=(row.get("hardware") or "").strip() or None,
        software=(row.get("version") or "").strip() or None,
        role=guess_role(hostname),
        mgmt_ip=row.get("ip") or None,
        status="decommissioned" if disabled else ("active" if up else "maintenance"),
        observium_hostname=(row.get("hostname") or "").strip(),
    )


# --------------------------------------------------------------------------- ports


@dataclass
class PortRecord:
    observium_id: str
    observium_device_id: str
    name: str
    if_type: str
    speed_gbps: float | None
    mtu: int | None
    admin_status: str
    oper_status: str
    description: str | None
    in_gbps: float | None
    out_gbps: float | None
    if_index: int | None = None  # RRD file is port-<ifIndex>.rrd


def classify_port(name: str, if_type: str) -> str:
    if "." in name or ":" in name.split("/")[-1]:
        return "subinterface"
    if if_type.lower() in LAG_IFTYPES or BUNDLE_NAME.match(name):
        return "bundle"
    return "physical"


def _status(value: Any) -> str:
    return "up" if str(value).lower() in ("up", "1") else "down"


IF_SPEED_MAX = 4_294_967_295  # ifSpeed (Gauge32) ceiling; faster ports report it unless ifHighSpeed is set


def _rate_gbps(row: dict[str, Any], key: str) -> float | None:
    rate = _num(row.get(key))  # bytes per second
    return round(rate * 8 / 1e9, 4) if rate is not None else None


def parse_port(row: dict[str, Any]) -> PortRecord | None:
    if str(row.get("deleted") or "0") == "1":
        return None
    name = (row.get("ifName") or row.get("port_label_short") or row.get("ifDescr") or "").strip()
    if not name or SKIP_PORT.match(name):
        return None
    high_speed = _num(row.get("ifHighSpeed"))  # Mbps
    speed = _num(row.get("ifSpeed"))  # bps
    if speed is not None and speed >= IF_SPEED_MAX:
        speed = None  # 32-bit ifSpeed is pinned at 4.29G for anything faster: unknown, not 4.29G
    speed_gbps = high_speed / 1000 if high_speed else (speed / 1e9 if speed else None)
    if_type = str(row.get("ifType") or "")
    mtu = _int(row.get("ifMtu"))
    return PortRecord(
        observium_id=str(row.get("port_id")),
        observium_device_id=str(row.get("device_id")),
        name=name[:MAX_IFNAME],
        if_type=classify_port(name, if_type),
        speed_gbps=round(speed_gbps, 3) if speed_gbps else None,
        mtu=mtu if mtu and 64 <= mtu <= 16000 else None,  # SNMP reports 0 on many logical ports
        admin_status=_status(row.get("ifAdminStatus")),
        oper_status=_status(row.get("ifOperStatus")),
        description=(row.get("ifAlias") or "").strip()[:MAX_DESCRIPTION] or None,
        in_gbps=_rate_gbps(row, "ifInOctets_rate"),
        out_gbps=_rate_gbps(row, "ifOutOctets_rate"),
        if_index=_int(row.get("ifIndex")),
    )


# --------------------------------------------------------------------------- neighbours


@dataclass
class NeighbourRecord:
    observium_device_id: str
    local_port_id: str
    remote_system_name: str
    remote_port_id: str
    protocol: str
    # Observium's own resolution of the far end, when it found it among its ports/devices.
    remote_observium_port_id: str | None = None
    remote_address: str | None = None


def neighbour_skip_reason(row: dict[str, Any], include_cdp: bool = False) -> str | None:
    protocol = str(row.get("protocol") or "").lower()
    if protocol != "lldp" and not (include_cdp and protocol == "cdp"):
        return f"protocol:{protocol or 'none'}"
    if str(row.get("active", "1")) == "0":
        return "inactive"
    if not (row.get("remote_hostname") or "").strip():
        return "no_remote_hostname"
    if not (row.get("remote_port") or "").strip():
        return "no_remote_port"
    if not row.get("port_id"):
        return "no_port_id"
    return None


def parse_neighbour(row: dict[str, Any], include_cdp: bool = False) -> NeighbourRecord | None:
    if neighbour_skip_reason(row, include_cdp):
        return None
    remote_host = row["remote_hostname"].strip()[:255]
    remote_port = row["remote_port"].strip()[:MAX_IFNAME]
    protocol = str(row.get("protocol")).lower()
    return NeighbourRecord(
        observium_device_id=str(row.get("device_id")),
        local_port_id=str(row.get("port_id")),
        remote_system_name=remote_host,
        remote_port_id=remote_port,
        protocol=protocol,
        remote_observium_port_id=str(row["remote_port_id"]) if row.get("remote_port_id") not in (None, "", "0", 0) else None,
        remote_address=(row.get("remote_address") or "").strip() or None,
    )


# --------------------------------------------------------------------------- addresses


@dataclass
class AddressRecord:
    observium_port_id: str
    cidr: str


def parse_address(row: dict[str, Any]) -> AddressRecord | None:
    address = row.get("ipv4_address")
    if not address or not row.get("port_id"):
        return None
    prefix = row.get("ipv4_prefixlen")
    if prefix in (None, "") and row.get("ipv4_network"):
        prefix = str(row["ipv4_network"]).split("/")[-1]
    return AddressRecord(observium_port_id=str(row["port_id"]), cidr=f"{address}/{prefix}" if prefix else address)
