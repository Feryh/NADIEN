import re
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as m
from ..database import get_db
from ..services import running_config
from ..services.configgen import (AddressError, Addressing, as_text, check_vc_ids, dialect_of, generate, parse_cidr,
                                  parse_peer, parse_vc_ids, parse_vlans)
from ..services.l2vpn_peers import neighbours
from ..services.network_change_gen import NetworkChangeError, NetworkChangeIn, generate_network_change, network_change_hints

router = APIRouter(prefix="/configs", tags=["Config Generator"])


@router.post("/network-change")
def network_change_config(payload: NetworkChangeIn, db: Session = Depends(get_db)):
    """Generate a device config change and rollback using its vendor and latest RANCID config."""
    try:
        return generate_network_change(db, payload)
    except NetworkChangeError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/network-change/hints")
def network_change_config_hints(device_id: int, operation: Literal["igp", "bgp", "parameters"], db: Session = Depends(get_db)):
    """Suggested values and interface choices parsed from the device's latest RANCID config."""
    try:
        return network_change_hints(db, device_id, operation)
    except NetworkChangeError as exc:
        raise HTTPException(422, str(exc)) from exc


def addressing_params(
    a_ip: str | None = Query(None, max_length=18, description="Alamat PE site A, CIDR (10.10.10.1/30)"),
    b_ip: str | None = Query(None, max_length=18, description="Alamat PE site B, CIDR"),
    vc_id: int | None = Query(None, ge=1, le=4294967295, description="Pseudowire/VC id; default = VLAN"),
    bgp_asn: int | None = Query(None, ge=1, le=4294967295, description="ASN BGP perangkat (L3VPN); default dari RD"),
    policer: bool = Query(False, description="Tambahkan policer sesuai bandwidth layanan"),
    vlans: str | None = Query(None, max_length=256, description="VPLS beberapa VLAN, mis. 1480-1485,1489"),
    vc_ids: str | None = Query(None, max_length=1200, description="VC id per VLAN bila bentrok, mis. 1480:9001,1481:9002"),
    a_peer: str | None = Query(None, max_length=15, description="L2VPN: neighbour di site A (loopback PE B), default dari inventory"),
    b_peer: str | None = Query(None, max_length=15, description="L2VPN: neighbour di site B (loopback PE A), default dari inventory"),
) -> Addressing:
    """Shared by the config generator and the MoP routes; bad addresses are a 422 before anything is built."""
    try:
        parse_cidr(a_ip)
        parse_cidr(b_ip)
        vlan_list = parse_vlans(vlans)
        vc_id_map = parse_vc_ids(vc_ids)
        check_vc_ids(vlan_list, vc_id_map)
        peers = [parse_peer(a_peer), parse_peer(b_peer)]
    except AddressError as exc:
        raise HTTPException(422, str(exc)) from exc
    return Addressing(a_ip or None, b_ip or None, vc_id, bgp_asn, policer, vlan_list, vc_id_map, *peers)


def _service(db: Session, service_id: int) -> m.Service:
    service = db.get(m.Service, service_id)
    if service is None:
        raise HTTPException(404, "Service not found")
    return service


@router.get("/services/{service_id}")
def service_config(service_id: int, addressing: Addressing = Depends(addressing_params), db: Session = Depends(get_db)):
    """Configuration and rollback per endpoint in each device's own dialect."""
    return generate(db, _service(db, service_id), addressing)


@router.get("/services/{service_id}/text", response_class=PlainTextResponse)
def service_config_text(service_id: int, side: Literal["A", "B"] = Query(...),
                        part: Literal["config", "rollback"] = Query("config"),
                        addressing: Addressing = Depends(addressing_params), db: Session = Depends(get_db)):
    """One site's config or rollback as a plain file to paste: commands and commit only, no comments."""
    service = _service(db, service_id)
    result = generate(db, service, addressing)
    device = result["endpoints"][side]["device"] or f"site{side}"
    filename = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{part}-{service.circuit_id}-{device}") + ".txt"
    return PlainTextResponse(as_text(result, side, part),
                             headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# --------------------------------------------------------------------------- L2VPN between two hostnames


@router.get("/l2vpn/neighbours")
def l2vpn_neighbours(device_id: int, db: Session = Depends(get_db)):
    """L2VPN neighbours already on the device (RANCID running config) with the PW ids used towards each, plus
    the device's own loopback: the choices for the destination of a new pseudowire."""
    device = db.get(m.Device, device_id)
    if device is None:
        raise HTTPException(404, "Device not found")
    dialect = dialect_of(db, device_id)
    by_loopback = {ip: hostname for ip, hostname in
                   db.execute(select(m.Device.loopback_ipv4, m.Device.hostname).where(m.Device.loopback_ipv4.is_not(None)))}
    out = {"device_id": device.id, "hostname": device.hostname, "dialect": dialect, "loopback": device.loopback_ipv4,
           "neighbours": [], "error": None}
    try:
        text = running_config.fetch(db, device_id)
    except running_config.RunningConfigError as exc:
        out["error"] = str(exc)
        return out
    out["neighbours"] = [{**row, "hostname": by_loopback.get(row["ip"])} for row in neighbours(text, dialect)]
    return out


def l2vpn_service_params(
    a_device_id: int = Query(..., description="PE site A (sumber)"),
    a_port: str = Query(..., min_length=1, max_length=64, description="Port akses di PE A, mis. GigabitEthernet0/3/1"),
    b_device_id: int | None = Query(None, description="PE site B (tujuan); kosong bila tujuan belum di inventory"),
    b_port: str | None = Query(None, max_length=64),
    name: str = Query("L2VPN", min_length=1, max_length=40, description="Nama instance / pelanggan (OXY -> OXY_1480)"),
    bandwidth_mbps: int = Query(100, ge=1, le=400_000),
    db: Session = Depends(get_db),
) -> m.Service:
    """An L2VPN that is not (yet) a service in the inventory: built from two hostnames, never saved."""
    for device_id in (a_device_id, b_device_id):
        if device_id is not None and db.get(m.Device, device_id) is None:
            raise HTTPException(404, f"Device {device_id} not found")
    if a_device_id == b_device_id:
        raise HTTPException(422, "PE site A dan site B harus perangkat yang berbeda")
    # Not added to the session: generate() only reads its fields.
    return m.Service(circuit_id=name.strip(), service_type="l2vpn", bandwidth_mbps=bandwidth_mbps,
                     a_device_id=a_device_id, a_interface=a_port.strip(), b_device_id=b_device_id,
                     b_interface=(b_port or "").strip() or None, status="planned")


def _l2vpn_addressing(addressing: Addressing = Depends(addressing_params)) -> Addressing:
    if not addressing.vlans:
        raise HTTPException(422, "Isi VLAN layanan L2VPN")
    return addressing


@router.get("/l2vpn")
def l2vpn_config(service: m.Service = Depends(l2vpn_service_params),
                 addressing: Addressing = Depends(_l2vpn_addressing), db: Session = Depends(get_db)):
    """L2VPN config and rollback for both PEs, picked by hostname, without an inventory service."""
    return generate(db, service, addressing)


@router.get("/l2vpn/text", response_class=PlainTextResponse)
def l2vpn_config_text(side: Literal["A", "B"] = Query(...), part: Literal["config", "rollback"] = Query("config"),
                      service: m.Service = Depends(l2vpn_service_params),
                      addressing: Addressing = Depends(_l2vpn_addressing), db: Session = Depends(get_db)):
    result = generate(db, service, addressing)
    device = result["endpoints"][side]["device"] or f"site{side}"
    filename = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{part}-{service.circuit_id}-{device}") + ".txt"
    return PlainTextResponse(as_text(result, side, part),
                             headers={"Content-Disposition": f'attachment; filename="{filename}"'})
