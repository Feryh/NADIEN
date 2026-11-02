"""Load a sample Metro network so every screen has something to show.

    python -m app.seed           # only if the database is empty
    python -m app.seed --reset   # drop everything and reseed

Topology is NOT inserted directly: the seed writes interfaces + LLDP neighbor tables
(and a few config-only descriptions), then runs the real discovery engine.
"""

import random
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from . import models as m
from .database import Base, SessionLocal, engine
from .routers.integrations import upsert_external
from .services.topology import discover_links

POPS = [
    # code, name, tier, region, city, lat, lon
    ("JKT", "Jakarta DC Cyber", "core", "Jabodetabek", "Jakarta", -6.2088, 106.8456),
    ("BDG", "Bandung Dago", "aggregation", "Jawa Barat", "Bandung", -6.9175, 107.6191),
    ("SMG", "Semarang Pandanaran", "aggregation", "Jawa Tengah", "Semarang", -6.9667, 110.4167),
    ("YGY", "Yogyakarta Kotabaru", "access", "DIY", "Yogyakarta", -7.7956, 110.3695),
    ("SBY", "Surabaya Kebalen", "core", "Jawa Timur", "Surabaya", -7.2575, 112.7521),
    ("DPS", "Denpasar Renon", "aggregation", "Bali", "Denpasar", -8.6705, 115.2126),
]

VENDORS = {
    # name: (os_family, models, software, port naming, bundle naming)
    "Cisco": ("IOS-XR", ["ASR9906", "NCS-5508"], [("7.9.2", True, True), ("7.5.2", False, True)],
              "HundredGigE0/0/0/{n}", "Bundle-Ether{n}"),
    "Huawei": ("VRP", ["NE8000-X8", "NE40E-X16A"], [("V800R022C10", True, True)],
               "100GE1/0/{n}", "Eth-Trunk{n}"),
    "Juniper": ("Junos", ["MX10008", "MX960"], [("23.4R1", True, True)],
                "et-0/0/{n}", "ae{n}"),
    "Nokia": ("SR OS", ["7750 SR-7s"], [("23.10.R1", True, True)],
              "1/1/c{n}/1", "lag-{n}"),
}

DEVICES = [
    # hostname, pop, vendor, model idx, role, loopback
    ("JKT-P-01", "JKT", "Cisco", 1, "p", "10.0.0.1"),
    ("JKT-P-02", "JKT", "Juniper", 0, "p", "10.0.0.2"),
    ("JKT-PE-01", "JKT", "Cisco", 0, "pe", "10.0.0.11"),
    ("BDG-PE-01", "BDG", "Huawei", 0, "pe", "10.0.1.11"),
    ("BDG-AGG-01", "BDG", "Huawei", 1, "agg", "10.0.1.21"),
    ("SMG-PE-01", "SMG", "Nokia", 0, "pe", "10.0.2.11"),
    ("YGY-AGG-01", "YGY", "Huawei", 1, "agg", "10.0.3.21"),
    ("SBY-P-01", "SBY", "Juniper", 0, "p", "10.0.4.1"),
    ("SBY-PE-01", "SBY", "Cisco", 0, "pe", "10.0.4.11"),
    ("DPS-PE-01", "DPS", "Juniper", 1, "pe", "10.0.5.11"),
]

# a, b, member speed (G), members, how it is discovered, latency ms, distance km
LINKS = [
    ("JKT-P-01", "JKT-P-02", 100, 4, "lldp", 0.1, 0.2),
    ("JKT-P-01", "SBY-P-01", 400, 1, "lldp", 9.8, 780),
    ("JKT-P-02", "SBY-P-01", 100, 2, "lldp", 10.2, 800),
    ("JKT-P-01", "JKT-PE-01", 100, 2, "lldp", 0.1, 0.1),
    ("JKT-P-02", "JKT-PE-01", 100, 1, "lldp", 0.1, 0.1),
    ("JKT-P-01", "BDG-PE-01", 100, 2, "lldp", 2.1, 150),
    ("JKT-P-02", "BDG-PE-01", 100, 1, "lldp", 2.3, 160),
    ("BDG-PE-01", "BDG-AGG-01", 100, 1, "lldp", 0.1, 3),
    ("JKT-P-01", "SMG-PE-01", 100, 1, "lldp", 5.4, 450),
    ("SMG-PE-01", "SBY-P-01", 100, 1, "lldp", 4.0, 350),
    ("SMG-PE-01", "YGY-AGG-01", 100, 1, "lldp", 1.6, 120),
    ("BDG-AGG-01", "YGY-AGG-01", 100, 1, "config", 4.7, 390),  # LLDP disabled -> found via descriptions
    ("SBY-P-01", "SBY-PE-01", 100, 2, "lldp", 0.1, 0.1),
    ("SBY-P-01", "DPS-PE-01", 100, 1, "lldp", 4.1, 420),
    ("SBY-PE-01", "DPS-PE-01", 100, 1, "config", 4.3, 430),
]

# Target state per link after discovery: (current Gbps, reserved Gbps, growth Gbps/month, manual growth?)
LINK_LOAD = {
    ("JKT-P-01", "SBY-P-01"): (250, 0, 20, True),  # the planning example: 400G, 250G, +20G/mo -> 7.5 months
    ("JKT-P-01", "JKT-P-02"): (180, 20, 6, False),
    ("JKT-P-02", "SBY-P-01"): (140, 10, 4, False),
    ("JKT-P-01", "BDG-PE-01"): (150, 10, 5, False),
    ("JKT-P-01", "SMG-PE-01"): (72, 5, 2.5, False),
    ("SBY-P-01", "DPS-PE-01"): (68, 0, 3, False),
    ("SBY-PE-01", "DPS-PE-01"): (55, 10, 1, False),
}

CUSTOMERS = [
    ("PT Bank Nusantara", "ACC-10001", "enterprise"),
    ("PT Telko Global Carrier", "ACC-10002", "carrier"),
    ("Kementerian Digital", "ACC-10003", "government"),
    ("PT Retail Maju Jaya", "ACC-10004", "enterprise"),
    ("Asia Pacific Transit Ltd", "ACC-10005", "wholesale"),
    ("PT Media Streaming Indo", "ACC-10006", "enterprise"),
]

SERVICES = [
    # account, circuit, type, Mbps, a_pop, a_dev, b_pop, b_dev, vlan, vrf, protection
    ("ACC-10001", "L3VPN-BNI-0001", "l3vpn", 1000, "JKT", "JKT-PE-01", None, None, 1101, "VRF-BANKNUS", "1+1"),
    ("ACC-10001", "L3VPN-BNI-0002", "l3vpn", 500, "SBY", "SBY-PE-01", None, None, 1102, "VRF-BANKNUS", "1+1"),
    ("ACC-10001", "L3VPN-BNI-0003", "l3vpn", 300, "BDG", "BDG-PE-01", None, None, 1103, "VRF-BANKNUS", "none"),
    ("ACC-10002", "L2VPN-TGC-0001", "l2vpn", 10000, "JKT", "JKT-PE-01", "SBY", "SBY-PE-01", 2001, None, "1+1"),
    ("ACC-10002", "IPLC-TGC-0001", "iplc", 100000, "JKT", "JKT-PE-01", "DPS", "DPS-PE-01", 2002, None, "1+1"),
    ("ACC-10003", "INET-KMD-0001", "internet", 5000, "JKT", "JKT-PE-01", None, None, 3001, None, "1:1"),
    ("ACC-10003", "L3VPN-KMD-0001", "l3vpn", 2000, "SMG", "SMG-PE-01", None, None, 3002, "VRF-KOMDIGI", "1+1"),
    ("ACC-10004", "INET-RMJ-0001", "internet", 1000, "BDG", "BDG-PE-01", None, None, 4001, None, "none"),
    ("ACC-10004", "L2VPN-RMJ-0001", "l2vpn", 200, "BDG", "BDG-AGG-01", "YGY", "YGY-AGG-01", 4002, None, "none"),
    ("ACC-10005", "IPLC-APT-0001", "iplc", 100000, "SBY", "SBY-PE-01", "JKT", "JKT-PE-01", 5001, None, "1+1"),
    ("ACC-10006", "INET-MSI-0001", "internet", 40000, "JKT", "JKT-PE-01", None, None, 6001, None, "1:1"),
    ("ACC-10006", "INET-MSI-0002", "internet", 20000, "SBY", "SBY-PE-01", None, None, 6002, None, "none"),
]


class _PortAllocator:
    def __init__(self):
        self.next_port: dict[str, int] = {}
        self.next_bundle: dict[str, int] = {}

    def port(self, hostname: str) -> int:
        n = self.next_port.get(hostname, 0)
        self.next_port[hostname] = n + 1
        return n

    def bundle(self, hostname: str) -> int:
        n = self.next_bundle.get(hostname, 10)
        self.next_bundle[hostname] = n + 10
        return n


def seed(db) -> None:
    rnd = random.Random(42)
    now = datetime.now(timezone.utc)

    pops = {}
    for code, name, tier, region, city, lat, lon in POPS:
        pops[code] = m.Pop(code=code, name=name, tier=tier, region=region, city=city, latitude=lat, longitude=lon)
    db.add_all(pops.values())

    vendors, models_, software = {}, {}, {}
    for vname, (os_family, model_names, versions, _, _) in VENDORS.items():
        vendor = vendors[vname] = m.Vendor(name=vname, os_family=os_family)
        db.add(vendor)
        db.flush()
        models_[vname] = [m.DeviceModel(vendor_id=vendor.id, name=n, max_throughput_gbps=48000) for n in model_names]
        software[vname] = [
            m.SoftwareVersion(vendor_id=vendor.id, version=v, is_recommended=rec, supports_srv6=srv6)
            for v, rec, srv6 in versions
        ]
        db.add_all(models_[vname] + software[vname])
    db.flush()

    devices = {}
    for i, (hostname, pop, vname, model_idx, role, loopback) in enumerate(DEVICES, start=1):
        devices[hostname] = m.Device(
            hostname=hostname, pop_id=pops[pop].id, vendor_id=vendors[vname].id,
            model_id=models_[vname][model_idx].id, software_id=software[vname][0].id, role=role,
            mgmt_ip=f"172.16.0.{i}", loopback_ipv4=loopback,
            isis_net=f"49.0001.0100.0000.{i:04d}.00", srv6_locator=f"fc00:0:{i}::/48",
        )
    db.add_all(devices.values())
    db.flush()
    vendor_of = {h: v for h, _, v, *_ in DEVICES}

    ports = _PortAllocator()

    def make_port(host: str, speed: float, desc: str | None, parent: m.Interface | None = None) -> m.Interface:
        name = VENDORS[vendor_of[host]][3].format(n=ports.port(host))
        iface = m.Interface(
            device_id=devices[host].id, name=name, if_type="physical", speed_gbps=speed, mtu=9216,
            oper_status="up", description=desc, parent_id=parent.id if parent else None,
            optic_type="QSFP-DD 400G-FR4" if speed == 400 else "QSFP28 100G-LR4",
        )
        db.add(iface)
        db.flush()
        return iface

    lldp: list[m.LldpNeighbor] = []
    for a, b, speed, members, how, _, _ in LINKS:
        a_parent = b_parent = None
        if members > 1:
            a_parent = m.Interface(device_id=devices[a].id, name=VENDORS[vendor_of[a]][4].format(n=ports.bundle(a)),
                                   if_type="bundle", mtu=9216, oper_status="up", description=f"TO-{b}")
            b_parent = m.Interface(device_id=devices[b].id, name=VENDORS[vendor_of[b]][4].format(n=ports.bundle(b)),
                                   if_type="bundle", mtu=9216, oper_status="up", description=f"TO-{a}")
            db.add_all([a_parent, b_parent])
            db.flush()
        for _ in range(members):
            a_if = make_port(a, speed, f"TO-{b}", a_parent)
            b_if = make_port(b, speed, f"TO-{a}", b_parent)
            a_if.description = f"TO-{b}-{b_if.name}"
            b_if.description = f"TO-{a}-{a_if.name}"
            if how == "lldp":
                lldp.append(m.LldpNeighbor(device_id=devices[a].id, local_interface=a_if.name,
                                           remote_system_name=f"{b}.metro.net", remote_port_id=b_if.name))
                lldp.append(m.LldpNeighbor(device_id=devices[b].id, local_interface=b_if.name,
                                           remote_system_name=f"{a}.metro.net", remote_port_id=a_if.name))

    # Spare ports per device -> "available ports" on the dashboard.
    for host in devices:
        for _ in range(rnd.randint(2, 6)):
            iface = make_port(host, 100, None)
            iface.oper_status = "down"
    db.add_all(lldp)
    db.commit()

    discover_links(db)

    # Latency/distance + load and 90 days of daily utilization history.
    meta = {frozenset((a, b)): (lat, km) for a, b, _, _, _, lat, km in LINKS}
    host_of = {d.id: h for h, d in devices.items()}
    for link in db.scalars(select(m.Link)):
        pair = (host_of[link.a_device_id], host_of[link.b_device_id])
        lat, km = meta[frozenset(pair)]
        link.latency_ms, link.distance_km = lat, km
        load = LINK_LOAD.get(pair) or LINK_LOAD.get(pair[::-1])
        if load is None:
            cur = round(link.capacity_gbps * rnd.uniform(0.15, 0.5), 1)
            load = (cur, 0, rnd.uniform(0.2, 1.5), False)
        current, reserved, growth, manual = load
        link.current_gbps, link.reserved_gbps = current, reserved
        link.growth_gbps_per_month = growth if manual else None
        for day in range(90, -1, -1):
            base = current - growth * day / 30.4375
            peak = max(base * rnd.uniform(0.96, 1.04), 0) if day else current
            db.add(m.UtilizationSample(link_id=link.id, ts=now - timedelta(days=day),
                                       in_gbps=round(peak, 2), out_gbps=round(peak * rnd.uniform(0.6, 0.9), 2)))
    db.commit()

    customers = {acc: m.Customer(name=n, account_no=acc, segment=seg) for n, acc, seg in CUSTOMERS}
    db.add_all(customers.values())
    db.flush()
    for acc, circuit, stype, mbps, a_pop, a_dev, b_pop, b_dev, vlan, vrf, prot in SERVICES:
        db.add(m.Service(
            customer_id=customers[acc].id, circuit_id=circuit, service_type=stype, bandwidth_mbps=mbps,
            a_pop_id=pops[a_pop].id, a_device_id=devices[a_dev].id,
            b_pop_id=pops[b_pop].id if b_pop else None, b_device_id=devices[b_dev].id if b_dev else None,
            vlan_id=vlan, vrf_name=vrf, protection=prot,
            route_distinguisher=f"65000:{vlan}" if vrf else None, route_target=f"65000:{vlan}" if vrf else None,
        ))

    db.add_all([
        m.IntegrationSource(name="Fiber Inventory (placeholder)", system_type="inventory", target_entity="fibers",
                            notes="Outside plant / fiber management system"),
        m.IntegrationSource(name="DWDM EMS (placeholder)", system_type="dwdm_ems", target_entity="dwdm_wavelengths",
                            notes="Optical EMS: nodes + wavelengths"),
        m.IntegrationSource(name="Physical Inventory (placeholder)", system_type="inventory", target_entity="odfs",
                            notes="ODF and patch panel records"),
    ])
    db.commit()

    # External containers, filled through the same upsert path the API connectors will use.
    pid = {c: p.id for c, p in pops.items()}
    upsert_external(db, "fibers", "seed", [
        {"external_id": "FO-JKT-BDG-01", "name": "FO JKT-BDG #1", "a_pop_id": pid["JKT"], "b_pop_id": pid["BDG"],
         "length_km": 152, "core_count": 96, "cores_used": 40, "cable_type": "underground"},
        {"external_id": "FO-JKT-SMG-01", "name": "FO JKT-SMG #1", "a_pop_id": pid["JKT"], "b_pop_id": pid["SMG"],
         "length_km": 455, "core_count": 48, "cores_used": 30, "cable_type": "underground"},
        {"external_id": "FO-SMG-SBY-01", "name": "FO SMG-SBY #1", "a_pop_id": pid["SMG"], "b_pop_id": pid["SBY"],
         "length_km": 352, "core_count": 48, "cores_used": 22, "cable_type": "aerial"},
        {"external_id": "FO-SBY-DPS-01", "name": "FO SBY-DPS #1", "a_pop_id": pid["SBY"], "b_pop_id": pid["DPS"],
         "length_km": 425, "core_count": 24, "cores_used": 18, "cable_type": "submarine"},
    ])
    upsert_external(db, "dwdm_nodes", "seed", [
        {"external_id": f"ROADM-{c}", "name": f"ROADM-{c}-01", "pop_id": pid[c], "vendor": "Ciena",
         "model": "6500 T-Series", "node_type": "roadm"}
        for c in ("JKT", "SMG", "SBY")
    ])
    upsert_external(db, "odfs", "seed", [
        {"external_id": f"ODF-{c}-01", "name": f"ODF-{c}-01", "pop_id": pid[c], "rack": "R01", "port_count": 144,
         "ports_used": rnd.randint(40, 120)}
        for c in pid
    ])
    upsert_external(db, "patch_panels", "seed", [
        {"external_id": f"PP-{c}-01", "name": f"PP-{c}-01", "pop_id": pid[c], "rack": "R02", "connector_type": "LC",
         "port_count": 48, "ports_used": rnd.randint(10, 40)}
        for c in pid
    ])
    roadm = {n.name: n.id for n in db.scalars(select(m.DwdmNode))}
    upsert_external(db, "dwdm_wavelengths", "seed", [
        {"external_id": "OCH-JKT-SBY-C21", "name": "JKT-SBY 400G C21", "a_dwdm_node_id": roadm["ROADM-JKT-01"],
         "b_dwdm_node_id": roadm["ROADM-SBY-01"], "channel": "C21", "frequency_thz": 192.1, "rate_gbps": 400},
        {"external_id": "OCH-JKT-SMG-C33", "name": "JKT-SMG 100G C33", "a_dwdm_node_id": roadm["ROADM-JKT-01"],
         "b_dwdm_node_id": roadm["ROADM-SMG-01"], "channel": "C33", "frequency_thz": 193.3, "rate_gbps": 100},
    ])


def main() -> None:
    reset = "--reset" in sys.argv
    if reset:
        Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        if not reset and db.scalar(select(func.count()).select_from(m.Pop)):
            print("Database already has data — skipping seed (use --reset to reseed).")
            return
        seed(db)
        print("Seed complete.")


if __name__ == "__main__":
    main()
