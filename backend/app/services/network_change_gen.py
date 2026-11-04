"""Vendor-aware standalone network change templates seeded from the latest RANCID config."""

import ipaddress
import re
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import models as m
from .configgen import _dialect
from .running_config import RunningConfigError, fetch
from .vendors import DIALECT_LABELS


class NetworkChangeIn(BaseModel):
    operation: Literal["igp", "bgp", "parameters"]
    device_id: int
    # IGP migration
    ospf_process: str = Field(default="1", max_length=32)
    isis_process: str = Field(default="1", max_length=32)
    net: str = Field(default="", max_length=32)
    interfaces: list[str] = Field(default_factory=list, max_length=64)
    metric: int = Field(default=10, ge=1, le=16777214)
    # BGP
    local_asn: int | None = Field(default=None, ge=1, le=4294967295)
    neighbor: str = Field(default="", max_length=64)
    remote_asn: int | None = Field(default=None, ge=1, le=4294967295)
    address_family: Literal["ipv4", "ipv6"] = "ipv4"
    # Interface parameter changes
    interface: str = Field(default="", max_length=128)
    mtu: int | None = Field(default=None, ge=64, le=16000)
    description: str | None = Field(default=None, max_length=255)
    bandwidth_mbps: int | None = Field(default=None, ge=1, le=10000000)


class NetworkChangeError(ValueError):
    pass


def _validate_device(db: Session, payload: NetworkChangeIn) -> tuple[m.Device, str, str]:
    device = db.get(m.Device, payload.device_id)
    if device is None:
        raise NetworkChangeError("Perangkat tidak ditemukan.")
    dialect = _dialect(db, device.id)
    if not dialect:
        raise NetworkChangeError("Vendor/OS perangkat tidak dikenali oleh Config Generator.")
    try:
        running = fetch(db, device.id)
    except RunningConfigError as exc:
        raise NetworkChangeError(str(exc)) from exc
    return device, dialect, running


def _save_config_lines(config: str, interface: str) -> list[str]:
    """Extract one interface stanza for parameter-change rollback."""
    lines = config.splitlines()
    found: list[str] = []
    active = False
    for line in lines:
        stripped = line.strip()
        if re.match(r"^(interface|int)\s+", stripped, re.I):
            if active:
                break
            active = stripped.split(None, 1)[-1].strip() == interface
        if active:
            found.append(stripped)
            if stripped.lower() in {"!", "#", "quit", "exit", "end"} and len(found) > 1:
                break
    return found


def _current_value(lines: list[str], regex: str) -> str | None:
    return next((m.group(1) for line in lines if (m := re.search(regex, line, re.I))), None)


def _generate_igp(dialect: str, payload: NetworkChangeIn, running: str) -> tuple[list[str], list[str], list[str]]:
    if not re.fullmatch(r"(?:[0-9A-Fa-f]{2}\.){2,9}[0-9A-Fa-f]{2}", payload.net):
        raise NetworkChangeError("NET IS-IS harus berupa Network Entity Title (NET), bukan prefix IP.")
    if not re.search(rf"\brouter\s+ospf\s+{re.escape(payload.ospf_process)}\b|\bospf\s+{re.escape(payload.ospf_process)}\b", running, re.I):
        raise NetworkChangeError(f"Process OSPF {payload.ospf_process} tidak ditemukan di RANCID; migrasi dibatalkan.")
    if re.search(rf"\bisis\s+{re.escape(payload.isis_process)}\b|\brouter isis {re.escape(payload.isis_process)}\b", running, re.I):
        raise NetworkChangeError(f"Process IS-IS {payload.isis_process} sudah ada di RANCID; pilih tag lain atau review manual.")
    if not payload.interfaces:
        raise NetworkChangeError("Tambahkan minimal satu interface untuk IS-IS.")
    missing = [i for i in payload.interfaces if not re.search(rf"^\s*(?:interface|int)\s+{re.escape(i)}\s*$", running, re.I | re.M)]
    if missing:
        raise NetworkChangeError(f"Interface tidak ditemukan di RANCID: {', '.join(missing)}")
    if dialect == "vrp":
        config = [f"isis {payload.isis_process}", " is-level level-2", f" network-entity {payload.net}", "quit"]
        for interface in payload.interfaces:
            config.extend([f"interface {interface}", f" isis enable {payload.isis_process}", f" isis circuit-level level-2", f" isis cost {payload.metric}", "quit"])
        rollback = []
        for interface in payload.interfaces:
            rollback.extend([f"interface {interface}", f" undo isis enable {payload.isis_process}", "quit"])
        rollback.append(f"undo isis {payload.isis_process}")
        # Restore OSPF is intentionally left to the preserved RANCID stanza; do not remove it in generated config.
        return config, rollback, ["OSPF dipertahankan selama tahap migrasi agar rollback aman. Hapus OSPF setelah adjacency IS-IS terverifikasi."]
    if dialect == "iosxr":
        config = [f"router isis {payload.isis_process}", f" net {payload.net}", " address-family ipv4 unicast", "  metric-style wide", " !"]
        for interface in payload.interfaces:
            config.extend([f" interface {interface}", f"  address-family ipv4 unicast", f"   metric {payload.metric}", "  !"])
        config.append("!")
        rollback = [f"no router isis {payload.isis_process}"]
        return config, rollback, ["OSPF dipertahankan selama tahap migrasi agar rollback aman. Hapus OSPF setelah adjacency IS-IS terverifikasi."]
    raise NetworkChangeError("Migrasi OSPF ke IS-IS tersedia untuk Huawei VRP dan Cisco IOS-XR.")


def _generate_bgp(dialect: str, payload: NetworkChangeIn, running: str) -> tuple[list[str], list[str], list[str]]:
    try:
        ipaddress.ip_address(payload.neighbor)
    except ValueError as exc:
        raise NetworkChangeError("Alamat neighbor BGP tidak valid.") from exc
    if payload.local_asn is None or payload.remote_asn is None:
        raise NetworkChangeError("ASN lokal dan remote wajib diisi.")
    if dialect == "vrp":
        family = "ipv6-family unicast" if payload.address_family == "ipv6" else "ipv4-family unicast"
        config = [f"bgp {payload.local_asn}", f" peer {payload.neighbor} as-number {payload.remote_asn}",
                  f" {family}",
                  f"  peer {payload.neighbor} enable", "quit", "quit"]
        rollback = [f"bgp {payload.local_asn}", f" undo peer {payload.neighbor}", "quit"]
    elif dialect == "iosxr":
        af = "ipv6 unicast" if payload.address_family == "ipv6" else "ipv4 unicast"
        config = [f"router bgp {payload.local_asn}", f" neighbor {payload.neighbor}", f"  remote-as {payload.remote_asn}",
                  f"  address-family {af}", "  !", " !"]
        rollback = [f"router bgp {payload.local_asn}", f" no neighbor {payload.neighbor}", "!"]
    else:
        raise NetworkChangeError("Pembuatan peer BGP tersedia untuk Huawei VRP dan Cisco IOS-XR.")
    if re.search(rf"\bneighbor\s+{re.escape(payload.neighbor)}\b|\bpeer\s+{re.escape(payload.neighbor)}\b", running, re.I):
        raise NetworkChangeError("Neighbor sudah ada di RANCID; generator menolak menimpa config existing. Gunakan perubahan parameter atau edit manual.")
    notes = ["Pasang route-policy import/export yang disetujui sebelum mengaktifkan pertukaran route."] if dialect == "iosxr" else []
    return config, rollback, notes


def _generate_parameters(dialect: str, payload: NetworkChangeIn, running: str) -> tuple[list[str], list[str], list[str]]:
    if not payload.interface:
        raise NetworkChangeError("Nama interface wajib diisi.")
    old = _save_config_lines(running, payload.interface)
    if not old:
        raise NetworkChangeError(f"Interface {payload.interface} tidak ditemukan di RANCID.")
    config: list[str] = []
    rollback: list[str] = []
    if dialect == "vrp":
        config.append(f"interface {payload.interface}")
        rollback.append(f"interface {payload.interface}")
        for key, value, command, restore, rx in [
            ("mtu", payload.mtu, lambda v: f" mtu {v}", lambda v: f" mtu {v}", r"mtu\s+(\d+)"),
            ("description", payload.description, lambda v: f" description {v}", lambda v: f" description {v}", r"description\s+(.+)"),
            ("bandwidth", payload.bandwidth_mbps, lambda v: f" bandwidth {int(v) * 1000}", lambda v: f" bandwidth {v}", r"bandwidth\s+(\d+)"),
        ]:
            if value is None: continue
            config.append(command(value))
            previous = _current_value(old, rx)
            rollback.append(restore(previous) if previous is not None else f" undo {key}")
        config.append("quit"); rollback.append("quit")
    elif dialect == "iosxr":
        config.append(f"interface {payload.interface}")
        rollback.append(f"interface {payload.interface}")
        for value, command, restore, rx, undo in [
            (payload.mtu, lambda v: f" mtu {v}", lambda v: f" mtu {v}", r"mtu\s+(\d+)", "no mtu"),
            (payload.description, lambda v: f" description {v}", lambda v: f" description {v}", r"description\s+(.+)", "no description"),
            (payload.bandwidth_mbps, lambda v: f" bandwidth {int(v) * 1000}", lambda v: f" bandwidth {v}", r"bandwidth\s+(\d+)", "no bandwidth"),
        ]:
            if value is None: continue
            config.append(command(value))
            previous = _current_value(old, rx)
            rollback.append(restore(previous) if previous is not None else f" {undo}")
        config.append("!"); rollback.append("!")
    else:
        raise NetworkChangeError("Perubahan parameter interface tersedia untuk Huawei VRP dan Cisco IOS-XR.")
    if not config or len(config) <= 2:
        raise NetworkChangeError("Isi minimal satu parameter yang akan diubah.")
    return config, rollback, []


def generate_network_change(db: Session, payload: NetworkChangeIn) -> dict[str, Any]:
    device, dialect, running = _validate_device(db, payload)
    generators = {"igp": _generate_igp, "bgp": _generate_bgp, "parameters": _generate_parameters}
    config, rollback, warnings = generators[payload.operation](dialect, payload, running)
    return {
        "operation": payload.operation,
        "device": device.hostname,
        "device_id": device.id,
        "dialect": dialect,
        "dialect_label": DIALECT_LABELS[dialect],
        "config": config,
        "rollback": rollback,
        "commit": ["commit"] if dialect == "iosxr" else ["commit"],
        "warnings": warnings + ["Config dibuat dari template vendor dengan RANCID sebagai referensi. Verifikasi syntax dan policy sesuai versi software sebelum eksekusi."],
    }


def network_change_hints(db: Session, device_id: int, operation: str) -> dict[str, Any]:
    payload = NetworkChangeIn(operation=operation, device_id=device_id)  # type: ignore[arg-type]
    device, dialect, running = _validate_device(db, payload)
    interfaces = re.findall(r"^\s*(?:interface|int)\s+(\S+)\s*$", running, re.I | re.M)
    ospf = re.search(r"^\s*(?:router\s+ospf|ospf)\s+([\w.-]+)", running, re.I | re.M)
    bgp = re.search(r"^\s*(?:router\s+)?bgp\s+(\d+)", running, re.I | re.M)
    net = re.search(r"^\s*(?:network-entity|net)\s+(\S+)", running, re.I | re.M)
    return {
        "device": device.hostname,
        "dialect": dialect,
        "interfaces": list(dict.fromkeys(interfaces)),
        "ospf_process": ospf.group(1) if ospf else "1",
        "local_asn": bgp.group(1) if bgp else None,
        "isis_net": net.group(1) if net else None,
    }
