"""Naming standards for devices and ports, checked against the inventory.

Hostname (NADIEN standard, what most of the network already uses):
    POP-first   <KOTA>-<SITE>-<FUNGSI><n>-<MODEL>              JKT-ABW-OLT1-HW, BKS-LBJ-ME1-NCS540
    Cell site   <REG>-<PROV>-<KOTA>-<NNNN>-<FUNGSI><n>-<VENDOR>  SUM-RI-PBR-0011-ME1-ZTE
    Legacy      <FUNGSI><n>-<MODEL>-<KOTA>-<SITE>              PE1-ATN910-PTT-WTR  (accepted, not for new devices)

Port description on a link (the operators' own `[BB]TO-…[100G]` habit, made uniform):
    [<BB|DS|LM>]TO-<PEER-HOSTNAME>-<PEER-PORT>[<KAPASITAS>]      [BB]TO-JKT-NDC-P1-8201-Hu0/0/0/8[100G]

Nothing here renames anything: suggestions and description scripts are for engineers to review and run.
"""

import re
from dataclasses import dataclass, field
from typing import Literal

from ..collectors.observium.parsers import ROLE_TOKENS
from .configgen import COMMIT, fiberhome_name
from .segments import SEGMENT_TAGS
from .topology import host_key

HostnameStatus = Literal["standard", "needs_fix", "legacy", "non_standard"]
DescriptionStatus = Literal["standard", "names_peer", "no_peer", "empty"]

HOSTNAME_STATUS_LABELS = {"standard": "Standar", "needs_fix": "Perlu perbaikan", "legacy": "Legacy (role di depan)",
                          "non_standard": "Non-standar"}
DESCRIPTION_STATUS_LABELS = {"standard": "Standar", "names_peer": "Menyebut lawan, format lain",
                             "no_peer": "Tidak menyebut perangkat lawan", "empty": "Kosong"}

# Function codes in hostnames: the inventory roles, plus service functions seen on the network.
EXTRA_FUNCTIONS = {"SPE", "IGW", "CGN", "AN", "RR", "ADC", "OLP", "DCGW", "BRAS", "CPE"}
FUNCTIONS = set(ROLE_TOKENS) | EXTRA_FUNCTIONS
STANDARD_TOKEN = {"p": "P", "pe": "PE", "bng": "BNG", "agg": "ME", "switch": "DS", "csr": "ME", "olt": "OLT", "access": "SW", "dci": "DCI"}
VENDOR_CODE = {"huawei": "HW", "zte": "ZTE", "fiberhome": "FH", "cisco": "CSCO", "juniper": "JNPR", "nokia": "NOK"}

_FUNC = "(" + "|".join(sorted(FUNCTIONS, key=len, reverse=True)) + r")(\d*)"
_CSR = re.compile(r"^(SUM|SUL|KAL|BAL|NUS|JAW|JAV|PAP|MAL|MLK)-([A-Z]{2})-([A-Z]{3,4})-(\d{3,6})-" + _FUNC + r"(?:-([A-Z0-9-]+))?$")
# The site may take two parts (JKT-CBR-MMR-SW1-NX93: data centre CBR, room MMR).
_POP_FIRST = re.compile(r"^([A-Z]{3,4}\d?)-([A-Z0-9]{2,8}(?:-[A-Z0-9]{2,8})?)-" + _FUNC + r"(?:-([A-Z0-9-]+))?$")
_ROLE_FIRST = re.compile(r"^" + _FUNC + r"-([A-Z0-9]+)-([A-Z0-9-]+)$")
_CLEAN = re.compile(r"^[A-Z0-9-]+$")
DESCRIPTION_MAX = 80
STANDARD_DESCRIPTION = re.compile(r"^\[(BB|DS|LM)\]TO-[A-Z0-9-]+-\S+?(\[\d+(?:\.\d+)?[GM]\])?$")


@dataclass
class HostnameCheck:
    convention: str | None  # pop_first | csr | role_first | None
    status: HostnameStatus
    issues: list[str] = field(default_factory=list)
    function: str | None = None
    index: str | None = None
    city: str | None = None
    site: str | None = None
    model: str | None = None
    suggestion: str | None = None


def _parse(name: str) -> HostnameCheck:
    if match := _CSR.match(name):
        return HostnameCheck("csr", "standard", function=match[5], index=match[6], city=match[3],
                             site=match[4], model=match[7])
    if match := _POP_FIRST.match(name):
        return HostnameCheck("pop_first", "standard", function=match[3], index=match[4], city=match[1],
                             site=match[2], model=match[5])
    if match := _ROLE_FIRST.match(name):
        rest = match[4].split("-")
        return HostnameCheck("role_first", "legacy", function=match[1], index=match[2], model=match[3],
                             city=rest[-2] if len(rest) >= 2 else rest[0], site=rest[-1] if len(rest) >= 2 else None)
    return HostnameCheck(None, "non_standard")


def check_hostname(hostname: str, role: str | None, pop_code: str | None, vendor: str | None,
                   site_hint: str | None = None) -> HostnameCheck:
    """Convention, problems against the inventory, and a suggested standard name when it is not standard."""
    raw = hostname.split(".")[0]
    name = raw.strip().strip('"').upper()
    check = _parse(name)
    if not _CLEAN.match(raw):
        check.issues.append("Huruf kecil, spasi atau karakter selain A-Z, 0-9, '-'")
    if check.convention and not check.index and check.model and check.model.isdigit():
        # JKT-JBG-OLT-02: the number sits in its own part instead of after the function code.
        check.index, check.model = check.model, None
        check.issues.append(f"Nomor urut terpisah dari kode fungsi ({check.function}-{check.index})")
    if check.convention:
        implied = ROLE_TOKENS.get(check.function or "")
        if implied and role and implied != role and not (implied == "agg" and role == "csr"):
            check.issues.append(f"Kode fungsi {check.function} berarti {implied.upper()}, role inventory {role.upper()}")
        if not check.index:
            check.issues.append(f"Tanpa nomor urut ({check.function}1, {check.function}2, …)")
        if check.convention == "pop_first" and not check.model and not check.issues:
            check.issues.append("Tanpa kode model/vendor di akhir")
        pop = (pop_code or "").upper()
        if (check.convention == "pop_first" and pop and pop != "UNASSIGNED" and check.city
                and check.city[:3] != pop[:3]):
            check.issues.append(f"Kode kota {check.city} berbeda dari POP {pop}")
    if check.status == "standard" and check.issues:
        check.status = "needs_fix"
    if check.status != "standard":
        check.suggestion = _suggest(check, name, role, pop_code, vendor, site_hint)
    return check


def _suggest(check: HostnameCheck, name: str, role: str | None, pop_code: str | None, vendor: str | None,
             site_hint: str | None) -> str | None:
    token = check.function if check.function and ROLE_TOKENS.get(check.function) in (role, None) else None
    token = token or STANDARD_TOKEN.get(role or "")
    if not token:
        return None
    index = check.index or "1"
    vendor_code = VENDOR_CODE.get((vendor or "").split()[0].lower()) if vendor else None
    model = re.sub(r"[^A-Z0-9-]", "", (check.model or "").upper()).strip("-") or vendor_code
    if check.convention == "csr":
        prefix = "-".join(name.split("-")[:4])
        return "-".join(p for p in (prefix, f"{token}{index}", model) if p)
    pop = (pop_code or "").upper()
    city = pop if pop and pop != "UNASSIGNED" else check.city
    site = re.sub(r"[^A-Z0-9-]", "", (check.site or site_hint or "").upper())[:17].strip("-")
    if not (city and site and model):
        return None
    suggestion = f"{city}-{site}-{token}{index}-{model}"
    return suggestion if suggestion != name else None


# --------------------------------------------------------------------------- ports


_PORT_ABBREVIATIONS = (
    ("FourHundredGigE", "FH"), ("HundredGigE", "Hu"), ("FiftyGigE", "Fi"), ("FortyGigE", "Fo"),
    ("TwentyFiveGigE", "TF"), ("TenGigE", "Te"), ("TenGigabitEthernet", "Te"), ("XGigabitEthernet", "XGE"),
    ("GigabitEthernet", "Gi"), ("Bundle-Ether", "BE"), ("Port-channel", "Po"),
)


def short_port(name: str | None) -> str:
    port = (name or "").strip().replace(" ", "")
    for long, short in _PORT_ABBREVIATIONS:
        if port.lower().startswith(long.lower()):
            return short + port[len(long):]
    return port


def _capacity(capacity_gbps: float | None) -> str:
    if not capacity_gbps:
        return ""
    value = f"{capacity_gbps:g}" if capacity_gbps >= 1 else f"{capacity_gbps * 1000:g}M"
    return f"[{value}G]" if capacity_gbps >= 1 else f"[{value}]"


def recommended_description(segment: str, peer_hostname: str, peer_port: str | None, capacity_gbps: float | None) -> str:
    peer = re.sub(r"[^A-Z0-9-]", "", peer_hostname.split(".")[0].upper())
    port = short_port(peer_port)
    base = f"[{SEGMENT_TAGS.get(segment, 'DS')}]TO-{peer}" + (f"-{port}" if port else "")
    text = base + _capacity(capacity_gbps)
    return text if len(text) <= DESCRIPTION_MAX else base[:DESCRIPTION_MAX]


def description_status(description: str | None, peer_hostname: str) -> DescriptionStatus:
    text = (description or "").strip()
    if not text:
        return "empty"
    names_peer = host_key(peer_hostname.split(".")[0]) in host_key(text)
    if names_peer and STANDARD_DESCRIPTION.match(text):
        return "standard"
    return "names_peer" if names_peer else "no_peer"


@dataclass
class PortFix:
    port: str
    description: str
    previous: str | None


_DESCRIPTION_SYNTAX = {
    # dialect: (enter config, open interface, set, clear, leave interface)
    "vrp": ("system-view", "interface {port}", " description {text}", " undo description", " quit"),
    "iosxr": ("configure", "interface {port}", " description {text}", " no description", " exit"),
    "zte": ("configure terminal", "interface {port}", " description {text}", " no description", "$"),
    "fiberhome": ("configure terminal", "interface {port}", " description {text}", " no description", " exit"),
}


def description_script(dialect: str | None, fixes: list[PortFix]) -> dict[str, list[str]] | None:
    """{"config": [...], "rollback": [...]} in the device's own syntax, saved with commit; None when unsupported."""
    syntax = _DESCRIPTION_SYNTAX.get(dialect or "")
    if syntax is None or not fixes:
        return None
    enter, open_if, set_text, clear, leave = syntax
    config, rollback = [enter], [enter]
    for fix in fixes:
        port = fiberhome_name(fix.port) if dialect == "fiberhome" else fix.port
        config += [open_if.format(port=port), set_text.format(text=fix.description), leave]
        restore = set_text.format(text=fix.previous) if fix.previous else clear
        rollback += [open_if.format(port=port), restore, leave]
    return {"config": config + COMMIT[dialect], "rollback": rollback + COMMIT[dialect]}
