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
    false,
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
    pop_id: Mapped[int] = mapped_column(ForeignKey("pops.id"), index=True)
    vendor_id: Mapped[int | None] = mapped_column(ForeignKey("vendors.id"))
    model_id: Mapped[int | None] = mapped_column(ForeignKey("device_models.id"))
    software_id: Mapped[int | None] = mapped_column(ForeignKey("software_versions.id"))
    role: Mapped[str] = mapped_column(String(32), default="pe")  # p | pe | agg | access | dci | olt | bng
    mgmt_ip: Mapped[str | None] = mapped_column(String(64))
    loopback_ipv4: Mapped[str | None] = mapped_column(String(64))
    isis_net: Mapped[str | None] = mapped_column(String(64))
    srv6_locator: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="active")
    # Saved manual position on the topology map (drag & drop).
    map_x: Mapped[float | None] = mapped_column(Float)
    map_y: Mapped[float | None] = mapped_column(Float)
    # POP chosen by hand (inventory edit, mapping import, "ikuti manual"): sync and the hostname rules never
    # move such a device to another POP.
    pop_locked: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())

    pop: Mapped[Pop] = relationship(back_populates="devices")
    interfaces: Mapped[list["Interface"]] = relationship(
        back_populates="device", cascade="all, delete-orphan"
    )


class DeviceLocation(Base):
    """A device's own site coordinates (e.g. an OLT's site from a KMZ). Devices without one are
    shown at their POP. A separate table so existing databases need no column migration."""

    __tablename__ = "device_locations"

    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), primary_key=True)
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
    source: Mapped[str | None] = mapped_column(String(128))  # file the coordinates came from
    placemark: Mapped[str | None] = mapped_column(String(255))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Interface(TimestampMixin, Base):
    """Physical port, bundle (LAG) or sub-interface. Bundle members point to their bundle via parent_id."""

    __tablename__ = "interfaces"
    __table_args__ = (UniqueConstraint("device_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(128))
    if_type: Mapped[str] = mapped_column(String(32), default="physical")  # physical | bundle | subinterface
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("interfaces.id"))
    speed_gbps: Mapped[float | None] = mapped_column(Float)
    mtu: Mapped[int | None] = mapped_column(Integer)
    admin_status: Mapped[str] = mapped_column(String(16), default="up")
    oper_status: Mapped[str] = mapped_column(String(16), default="down")
    description: Mapped[str | None] = mapped_column(String(512))
    ipv4_address: Mapped[str | None] = mapped_column(String(64))
    optic_type: Mapped[str | None] = mapped_column(String(64))
    # Traffic rate at the last Observium utilization sync: ports without a link (PON ports) have no samples.
    in_gbps: Mapped[float | None] = mapped_column(Float)
    out_gbps: Mapped[float | None] = mapped_column(Float)

    device: Mapped[Device] = relationship(back_populates="interfaces")


class LldpNeighbor(TimestampMixin, Base):
    """Raw LLDP neighbor table per device — the mandatory source for topology discovery."""

    __tablename__ = "lldp_neighbors"
    __table_args__ = (UniqueConstraint("device_id", "local_interface"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"))
    local_interface: Mapped[str] = mapped_column(String(128))
    remote_system_name: Mapped[str] = mapped_column(String(255))
    remote_port_id: Mapped[str] = mapped_column(String(128))
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
    __table_args__ = (Index("ix_links_a_device", "a_device_id"), Index("ix_links_b_device", "b_device_id"))

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


class ObserviumPort(Base):
    """Where an interface's history lives in Observium: <rrd_dir>/<observium_hostname>/port-<if_index>.rrd."""

    __tablename__ = "observium_ports"

    interface_id: Mapped[int] = mapped_column(ForeignKey("interfaces.id", ondelete="CASCADE"), primary_key=True)
    observium_port_id: Mapped[int] = mapped_column(Integer)
    if_index: Mapped[int | None] = mapped_column(Integer)
    observium_hostname: Mapped[str] = mapped_column(String(255))


class GitwebFile(Base):
    """One RANCID config file seen in gitweb: its last blob hash, so daily runs fetch only changes."""

    __tablename__ = "gitweb_files"
    __table_args__ = (UniqueConstraint("repo", "path"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    repo: Mapped[str] = mapped_column(String(128))
    path: Mapped[str] = mapped_column(String(255))
    blob_sha: Mapped[str] = mapped_column(String(40))
    device_id: Mapped[int | None] = mapped_column(ForeignKey("devices.id", ondelete="SET NULL"))
    # True when the device exists only because of this file (not in Observium): its ports get
    # speed from the name and are treated as in service when described and not shut down.
    created_device: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16))  # applied | unmatched | unsupported | skipped | error
    detail: Mapped[str | None] = mapped_column(String(255))
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CollectorJob(Base):
    """Background sync jobs, shared by every API process/replica: one running job at a time."""

    __tablename__ = "collector_jobs"

    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), index=True)  # running | ok | failed
    phase: Mapped[str] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    report: Mapped[dict | None] = mapped_column(JSONType)
    error: Mapped[str | None] = mapped_column(Text)


class CollectorSetting(Base):
    """Collector connection settings entered in the UI; they override the NADIEN_* environment.
    Secret values (tokens, passwords) are stored encrypted (services/collector_settings.py)."""

    __tablename__ = "collector_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    secret: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class TopologyLayout(Base):
    """Node positions a user arranged on one topology view (area, a province, a POP's devices …), keyed by view.

    Covers every node kind the view draws, not only devices: areas, provinces, regencies, POPs and the dashed
    boxes of other groups. Positions are the schematic's model coordinates; map views follow coordinates instead.
    """

    __tablename__ = "topology_layouts"

    view_key: Mapped[str] = mapped_column(String(255), primary_key=True)  # e.g. device:JKT, regency:Jawa Timur|Jawa Timur
    positions: Mapped[dict] = mapped_column(JSONType)  # node id -> {"x": float, "y": float}
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AppState(Base):
    """Small shared counters, e.g. the read-model cache version every API process watches."""

    __tablename__ = "app_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[int] = mapped_column(Integer, default=0)


class ScheduledRun(Base):
    """Claims one scheduled run per job per day, so several API processes never start it twice."""

    __tablename__ = "scheduled_runs"
    __table_args__ = (UniqueConstraint("job", "run_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    job: Mapped[str] = mapped_column(String(32))
    run_date: Mapped[str] = mapped_column(String(10))  # YYYY-MM-DD in the schedule's time zone
    job_id: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UtilizationSample(Base):
    __tablename__ = "utilization_samples"
    # ix_util_ts serves the "last N days for every link" scans of the capacity views.
    __table_args__ = (Index("ix_util_link_ts", "link_id", "ts"), Index("ix_util_ts", "ts"))

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


class ServicePath(TimestampMixin, Base):
    """Engineered path of a service (Phase 4): the links it rides and the bandwidth reserved on them."""

    __tablename__ = "service_paths"

    id: Mapped[int] = mapped_column(primary_key=True)
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(16))  # primary | backup
    device_ids: Mapped[list] = mapped_column(JSONType)
    link_ids: Mapped[list] = mapped_column(JSONType)
    hops: Mapped[int] = mapped_column(Integer)
    latency_ms: Mapped[float | None] = mapped_column(Float)
    latency_estimated: Mapped[bool] = mapped_column(Boolean, default=True)
    reserved_gbps: Mapped[float] = mapped_column(Float, default=0)  # 0 when not reserved (1:1 backup)
    released: Mapped[bool] = mapped_column(Boolean, default=False)


# --------------------------------------------------------------------------- AI assistant (Phase 9)


class AiProvider(TimestampMixin, Base):
    """An LLM endpoint the AI assistant can use: a local model (Ollama, vLLM) or a hosted one added by hand.
    The API key is stored encrypted with the collector-settings key and never returned by the API."""

    __tablename__ = "ai_providers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    kind: Mapped[str] = mapped_column(String(32))  # ollama | openai | azure_openai | anthropic
    base_url: Mapped[str | None] = mapped_column(String(255))
    model: Mapped[str] = mapped_column(String(128))
    api_key_encrypted: Mapped[str | None] = mapped_column(Text)
    api_version: Mapped[str | None] = mapped_column(String(32))  # Azure OpenAI only
    extra_headers: Mapped[dict | None] = mapped_column(JSONType)  # e.g. OpenRouter's HTTP-Referer
    temperature: Mapped[float | None] = mapped_column(Float)
    max_tokens: Mapped[int | None] = mapped_column(Integer)
    timeout_s: Mapped[int] = mapped_column(Integer, default=120)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)


# --------------------------------------------------------------------------- Digital twin (Phase 10)


class TwinScenario(TimestampMixin, Base):
    """A saved what-if: a list of changes (failures, growth, upgrades, virtual links) and its last result.
    Running a scenario never touches the production tables."""

    __tablename__ = "twin_scenarios"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    changes: Mapped[list] = mapped_column(JSONType)
    result: Mapped[dict | None] = mapped_column(JSONType)
    run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# --------------------------------------------------------------------------- Users, audit, change execution


class User(TimestampMixin, Base):
    """NADIEN login. Staff roles rank viewer < engineer < approver < admin; a `customer` user sees only the
    services of its customer. Device credentials are never stored here.

    auth_source: local (password here) or oidc (password_hash unusable; identity = external_id, the OIDC sub)."""

    __tablename__ = "users"
    __table_args__ = (Index("ix_users_identity", "auth_source", "external_id", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    full_name: Mapped[str | None] = mapped_column(String(128))
    email: Mapped[str | None] = mapped_column(String(256))
    password_hash: Mapped[str] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(16), default="viewer")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    auth_source: Mapped[str] = mapped_column(String(16), default="local")
    external_id: Mapped[str | None] = mapped_column(String(256))
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id", ondelete="SET NULL"))
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SsoSettings(TimestampMixin, Base):
    """OpenID Connect provider (single row, id 1): Entra ID, Keycloak, Google Workspace, Okta, ...
    The client secret is encrypted with the same key as the collector secrets."""

    __tablename__ = "sso_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    display_name: Mapped[str] = mapped_column(String(64), default="SSO")
    issuer_url: Mapped[str | None] = mapped_column(String(512))
    client_id: Mapped[str | None] = mapped_column(String(256))
    client_secret_encrypted: Mapped[str | None] = mapped_column(Text)
    scopes: Mapped[str] = mapped_column(String(256), default="openid profile email")
    redirect_uri: Mapped[str | None] = mapped_column(String(512))
    role_claim: Mapped[str] = mapped_column(String(64), default="groups")
    role_mapping: Mapped[dict] = mapped_column(JSONType, default=dict)  # claim value -> role
    default_role: Mapped[str | None] = mapped_column(String(16))  # None: users matching no mapping are refused
    auto_create: Mapped[bool] = mapped_column(Boolean, default=True)
    allowed_domains: Mapped[str | None] = mapped_column(String(512))  # comma-separated email domains


class AuditLog(Base):
    """Every successful write through the API, plus change approvals and device runs."""

    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_logs_at", "at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    username: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(32))  # HTTP method or an event such as change.approve
    target: Mapped[str] = mapped_column(String(512))
    detail: Mapped[str | None] = mapped_column(Text)
    ip: Mapped[str | None] = mapped_column(String(64))


class ChangeRequest(TimestampMixin, Base):
    """Scripts frozen at submit time, so what the approver reviewed is exactly what is pushed.

    status: pending_approval -> approved | rejected; approved -> in_progress -> implemented | failed | rolled_back."""

    __tablename__ = "change_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    service_id: Mapped[int | None] = mapped_column(ForeignKey("services.id", ondelete="SET NULL"))
    params: Mapped[dict] = mapped_column(JSONType)  # generator inputs (vlans, vc_ids, ...) for traceability
    sites: Mapped[list] = mapped_column(JSONType)  # [{label, device_id, hostname, host, dialect, precheck, config, ...}]
    status: Mapped[str] = mapped_column(String(24), default="pending_approval")
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(64))
    approved_by: Mapped[str | None] = mapped_column(String(64))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_note: Mapped[str | None] = mapped_column(Text)


class ChangeRun(Base):
    """One CLI session against one site: precheck, push, postcheck or rollback, with its full transcript."""

    __tablename__ = "change_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    change_id: Mapped[int] = mapped_column(ForeignKey("change_requests.id", ondelete="CASCADE"), index=True)
    site: Mapped[str] = mapped_column(String(1))
    phase: Mapped[str] = mapped_column(String(16))
    protocol: Mapped[str] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(16), default="running")  # running | ok | failed
    started_by: Mapped[str] = mapped_column(String(64))
    device_user: Mapped[str] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    log: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str | None] = mapped_column(Text)


class TopologyEvent(Base):
    """What changed in the topology, for the live map and its change log: links that appeared, went down or
    came back. New devices are read from devices.created_at instead (they are created in several places)."""

    __tablename__ = "topology_events"
    __table_args__ = (Index("ix_topology_events_at", "at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    kind: Mapped[str] = mapped_column(String(24))  # link_added | link_down | link_up | discovery
    link_id: Mapped[int | None] = mapped_column(ForeignKey("links.id", ondelete="SET NULL"))
    device_id: Mapped[int | None] = mapped_column(ForeignKey("devices.id", ondelete="SET NULL"))
    title: Mapped[str] = mapped_column(String(255))
    detail: Mapped[str | None] = mapped_column(Text)
