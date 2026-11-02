"""Engineering database — the Single Source of Truth.

Tables marked "external" are containers for data owned by other platforms
(fiber inventory, DWDM EMS, ODF/patch records). They are filled through
/api/integrations/ingest/{entity} today and by API connectors later; the
(source_system, external_id) pair keeps each record traceable to its origin.
"""

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base

JSONType = JSON().with_variant(JSONB(), "postgresql")


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ExternalRefMixin:
    source_system: Mapped[str | None] = mapped_column(String(64))
    external_id: Mapped[str | None] = mapped_column(String(128))
    attributes: Mapped[dict | None] = mapped_column(JSONType)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# --------------------------------------------------------------------------- Node inventory


class Pop(TimestampMixin, Base):
    __tablename__ = "pops"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    tier: Mapped[str] = mapped_column(String(32), default="aggregation")  # core | aggregation | access
    region: Mapped[str | None] = mapped_column(String(64))
    city: Mapped[str | None] = mapped_column(String(64))
    address: Mapped[str | None] = mapped_column(Text)
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(32), default="active")

    devices: Mapped[list["Device"]] = relationship(back_populates="pop")


class Vendor(TimestampMixin, Base):
    __tablename__ = "vendors"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    os_family: Mapped[str | None] = mapped_column(String(64))  # IOS-XR, VRP, Junos, SR OS


class DeviceModel(TimestampMixin, Base):
    __tablename__ = "device_models"
    __table_args__ = (UniqueConstraint("vendor_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    vendor_id: Mapped[int] = mapped_column(ForeignKey("vendors.id"))
    name: Mapped[str] = mapped_column(String(64))
    device_type: Mapped[str] = mapped_column(String(32), default="router")
    max_throughput_gbps: Mapped[float | None] = mapped_column(Float)


class SoftwareVersion(TimestampMixin, Base):
    __tablename__ = "software_versions"
    __table_args__ = (UniqueConstraint("vendor_id", "version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    vendor_id: Mapped[int] = mapped_column(ForeignKey("vendors.id"))
    version: Mapped[str] = mapped_column(String(64))
    is_recommended: Mapped[bool] = mapped_column(Boolean, default=False)
    supports_srv6: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text)


class Device(TimestampMixin, Base):
    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(primary_key=True)
    hostname: Mapped[str] = mapped_column(String(128), unique=True)
    pop_id: Mapped[int] = mapped_column(ForeignKey("pops.id"))
    vendor_id: Mapped[int | None] = mapped_column(ForeignKey("vendors.id"))
    model_id: Mapped[int | None] = mapped_column(ForeignKey("device_models.id"))
    software_id: Mapped[int | None] = mapped_column(ForeignKey("software_versions.id"))
    role: Mapped[str] = mapped_column(String(32), default="pe")  # p | pe | agg | access | dci
    mgmt_ip: Mapped[str | None] = mapped_column(String(64))
    loopback_ipv4: Mapped[str | None] = mapped_column(String(64))
    isis_net: Mapped[str | None] = mapped_column(String(64))
    srv6_locator: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="active")
    # Saved manual position on the topology map (drag & drop).
    map_x: Mapped[float | None] = mapped_column(Float)
    map_y: Mapped[float | None] = mapped_column(Float)

    pop: Mapped[Pop] = relationship(back_populates="devices")
    interfaces: Mapped[list["Interface"]] = relationship(
        back_populates="device", cascade="all, delete-orphan"
    )


class Interface(TimestampMixin, Base):
    """Physical port, bundle (LAG) or sub-interface. Bundle members point to their bundle via parent_id."""

    __tablename__ = "interfaces"
    __table_args__ = (UniqueConstraint("device_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(64))
    if_type: Mapped[str] = mapped_column(String(32), default="physical")  # physical | bundle | subinterface
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("interfaces.id"))
    speed_gbps: Mapped[float | None] = mapped_column(Float)
    mtu: Mapped[int | None] = mapped_column(Integer)
    admin_status: Mapped[str] = mapped_column(String(16), default="up")
    oper_status: Mapped[str] = mapped_column(String(16), default="down")
    description: Mapped[str | None] = mapped_column(String(255))
    ipv4_address: Mapped[str | None] = mapped_column(String(64))
    optic_type: Mapped[str | None] = mapped_column(String(64))

    device: Mapped[Device] = relationship(back_populates="interfaces")


class LldpNeighbor(TimestampMixin, Base):
    """Raw LLDP neighbor table per device — the mandatory source for topology discovery."""

    __tablename__ = "lldp_neighbors"
    __table_args__ = (UniqueConstraint("device_id", "local_interface"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"))
    local_interface: Mapped[str] = mapped_column(String(64))
    remote_system_name: Mapped[str] = mapped_column(String(128))
    remote_port_id: Mapped[str] = mapped_column(String(64))
    remote_chassis_id: Mapped[str | None] = mapped_column(String(64))
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# --------------------------------------------------------------------------- External (via API later)


class DwdmNode(TimestampMixin, ExternalRefMixin, Base):
    __tablename__ = "dwdm_nodes"
    __table_args__ = (UniqueConstraint("source_system", "external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    pop_id: Mapped[int | None] = mapped_column(ForeignKey("pops.id"))
    vendor: Mapped[str | None] = mapped_column(String(64))
    model: Mapped[str | None] = mapped_column(String(64))
    node_type: Mapped[str | None] = mapped_column(String(32))  # roadm | ila | terminal
    status: Mapped[str] = mapped_column(String(32), default="active")


class Odf(TimestampMixin, ExternalRefMixin, Base):
    __tablename__ = "odfs"
    __table_args__ = (UniqueConstraint("source_system", "external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    pop_id: Mapped[int | None] = mapped_column(ForeignKey("pops.id"))
    rack: Mapped[str | None] = mapped_column(String(64))
    port_count: Mapped[int | None] = mapped_column(Integer)
    ports_used: Mapped[int | None] = mapped_column(Integer)


class PatchPanel(TimestampMixin, ExternalRefMixin, Base):
    __tablename__ = "patch_panels"
    __table_args__ = (UniqueConstraint("source_system", "external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    pop_id: Mapped[int | None] = mapped_column(ForeignKey("pops.id"))
    odf_id: Mapped[int | None] = mapped_column(ForeignKey("odfs.id"))
    rack: Mapped[str | None] = mapped_column(String(64))
    connector_type: Mapped[str | None] = mapped_column(String(32))  # LC | SC | MPO
    port_count: Mapped[int | None] = mapped_column(Integer)
    ports_used: Mapped[int | None] = mapped_column(Integer)


class Fiber(TimestampMixin, ExternalRefMixin, Base):
    __tablename__ = "fibers"
    __table_args__ = (UniqueConstraint("source_system", "external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    a_pop_id: Mapped[int | None] = mapped_column(ForeignKey("pops.id"))
    b_pop_id: Mapped[int | None] = mapped_column(ForeignKey("pops.id"))
    length_km: Mapped[float | None] = mapped_column(Float)
    core_count: Mapped[int | None] = mapped_column(Integer)
    cores_used: Mapped[int | None] = mapped_column(Integer)
    cable_type: Mapped[str | None] = mapped_column(String(32))  # aerial | underground | submarine
    attenuation_db: Mapped[float | None] = mapped_column(Float)


class DwdmWavelength(TimestampMixin, ExternalRefMixin, Base):
    __tablename__ = "dwdm_wavelengths"
    __table_args__ = (UniqueConstraint("source_system", "external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    a_dwdm_node_id: Mapped[int | None] = mapped_column(ForeignKey("dwdm_nodes.id"))
    b_dwdm_node_id: Mapped[int | None] = mapped_column(ForeignKey("dwdm_nodes.id"))
    fiber_id: Mapped[int | None] = mapped_column(ForeignKey("fibers.id"))
    channel: Mapped[str | None] = mapped_column(String(16))
    frequency_thz: Mapped[float | None] = mapped_column(Float)
    rate_gbps: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(32), default="active")


class IntegrationSource(TimestampMixin, Base):
    """Registry of external platforms (NMS, inventory, DWDM EMS) that will feed the external tables."""

    __tablename__ = "integration_sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    system_type: Mapped[str] = mapped_column(String(32))  # nms | inventory | dwdm_ems | oss
    target_entity: Mapped[str] = mapped_column(String(32))  # dwdm_nodes | odfs | patch_panels | fibers | dwdm_wavelengths
    base_url: Mapped[str | None] = mapped_column(String(255))
    auth_type: Mapped[str | None] = mapped_column(String(32))  # none | token | basic | oauth2
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[str | None] = mapped_column(String(255))
    notes: Mapped[str | None] = mapped_column(Text)


# --------------------------------------------------------------------------- Link inventory


class Link(TimestampMixin, Base):
    """Logical IP/Metro-E link between two router interfaces (a physical port or a bundle)."""

    __tablename__ = "links"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    link_type: Mapped[str] = mapped_column(String(32), default="metro_e")  # metro_e | bundle
    a_device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"))
    a_interface_id: Mapped[int | None] = mapped_column(ForeignKey("interfaces.id", ondelete="SET NULL"))
    b_device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"))
    b_interface_id: Mapped[int | None] = mapped_column(ForeignKey("interfaces.id", ondelete="SET NULL"))
    capacity_gbps: Mapped[float] = mapped_column(Float, default=0)
    current_gbps: Mapped[float] = mapped_column(Float, default=0)
    reserved_gbps: Mapped[float] = mapped_column(Float, default=0)
    # Manual growth override; when NULL the engine derives growth from utilization samples.
    growth_gbps_per_month: Mapped[float | None] = mapped_column(Float)
    latency_ms: Mapped[float | None] = mapped_column(Float)
    distance_km: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(16), default="up")  # up | down | planned
    discovery_source: Mapped[str] = mapped_column(String(16), default="manual")  # lldp | config | manual
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fiber_id: Mapped[int | None] = mapped_column(ForeignKey("fibers.id"))
    wavelength_id: Mapped[int | None] = mapped_column(ForeignKey("dwdm_wavelengths.id"))


class UtilizationSample(Base):
    __tablename__ = "utilization_samples"
    __table_args__ = (Index("ix_util_link_ts", "link_id", "ts"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    link_id: Mapped[int] = mapped_column(ForeignKey("links.id", ondelete="CASCADE"))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    in_gbps: Mapped[float] = mapped_column(Float, default=0)
    out_gbps: Mapped[float] = mapped_column(Float, default=0)


# --------------------------------------------------------------------------- Customer database


class Customer(TimestampMixin, Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    account_no: Mapped[str] = mapped_column(String(64), unique=True)
    segment: Mapped[str | None] = mapped_column(String(32))  # enterprise | wholesale | carrier | government
    contact_email: Mapped[str | None] = mapped_column(String(128))


class Service(TimestampMixin, Base):
    __tablename__ = "services"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"))
    circuit_id: Mapped[str] = mapped_column(String(64), unique=True)
    service_type: Mapped[str] = mapped_column(String(16))  # internet | iplc | l2vpn | l3vpn
    bandwidth_mbps: Mapped[int] = mapped_column(Integer)
    a_pop_id: Mapped[int | None] = mapped_column(ForeignKey("pops.id"))
    a_device_id: Mapped[int | None] = mapped_column(ForeignKey("devices.id"))
    a_interface: Mapped[str | None] = mapped_column(String(64))
    b_pop_id: Mapped[int | None] = mapped_column(ForeignKey("pops.id"))
    b_device_id: Mapped[int | None] = mapped_column(ForeignKey("devices.id"))
    b_interface: Mapped[str | None] = mapped_column(String(64))
    vlan_id: Mapped[int | None] = mapped_column(Integer)
    vrf_name: Mapped[str | None] = mapped_column(String(64))
    route_distinguisher: Mapped[str | None] = mapped_column(String(64))
    route_target: Mapped[str | None] = mapped_column(String(64))
    protection: Mapped[str] = mapped_column(String(16), default="none")  # none | 1+1 | 1:1
    status: Mapped[str] = mapped_column(String(16), default="active")  # planned | reserved | active | terminated
    notes: Mapped[str | None] = mapped_column(Text)
