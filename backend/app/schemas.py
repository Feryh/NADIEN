from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, create_model, model_validator


def partial(model: type[BaseModel], name: str) -> type[BaseModel]:
    """Same fields as `model`, all optional — used for PATCH payloads."""
    fields = {k: (Optional[f.annotation], None) for k, f in model.model_fields.items()}
    return create_model(name, **fields)  # type: ignore[call-overload]


class ORMRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ExternalFields(BaseModel):
    source_system: str | None = None
    external_id: str | None = None
    attributes: dict | None = None
    synced_at: datetime | None = None


# --------------------------------------------------------------------------- Node inventory


class PopCreate(BaseModel):
    code: str = Field(max_length=32)
    name: str
    tier: Literal["core", "aggregation", "access"] = "aggregation"
    region: str | None = None
    city: str | None = None
    address: str | None = None
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    status: Literal["active", "planned", "decommissioned"] = "active"


class PopRead(PopCreate, ORMRead):
    pass


class VendorCreate(BaseModel):
    name: str
    os_family: str | None = None


class VendorRead(VendorCreate, ORMRead):
    pass


class DeviceModelCreate(BaseModel):
    vendor_id: int
    name: str
    device_type: Literal["router", "switch", "dwdm", "other"] = "router"
    max_throughput_gbps: float | None = None


class DeviceModelRead(DeviceModelCreate, ORMRead):
    pass


class SoftwareVersionCreate(BaseModel):
    vendor_id: int
    version: str
    is_recommended: bool = False
    supports_srv6: bool = False
    notes: str | None = None


class SoftwareVersionRead(SoftwareVersionCreate, ORMRead):
    pass


class DeviceCreate(BaseModel):
    hostname: str
    pop_id: int
    vendor_id: int | None = None
    model_id: int | None = None
    software_id: int | None = None
    role: Literal["p", "pe", "agg", "switch", "csr", "access", "dci", "olt", "bng"] = "pe"
    mgmt_ip: str | None = None
    loopback_ipv4: str | None = None
    isis_net: str | None = None
    srv6_locator: str | None = None
    status: Literal["active", "planned", "maintenance", "decommissioned"] = "active"
    map_x: float | None = None
    map_y: float | None = None
    pop_locked: bool = False  # POP chosen by hand: never moved by sync or the hostname rules


class DeviceRead(DeviceCreate, ORMRead):
    reachability: Literal["up", "down", "unknown"] = "unknown"
    uptime_s: int | None = None
    last_polled_at: datetime | None = None


class InterfaceCreate(BaseModel):
    device_id: int
    name: str
    if_type: Literal["physical", "bundle", "subinterface", "virtual"] = "physical"
    mib_type: str | None = None  # Observium ifType
    parent_id: int | None = None
    speed_gbps: float | None = Field(default=None, ge=0)
    mtu: int | None = Field(default=None, ge=64, le=16000)
    admin_status: Literal["up", "down"] = "up"
    oper_status: Literal["up", "down"] = "down"
    description: str | None = None
    ipv4_address: str | None = None
    optic_type: str | None = None


class InterfaceRead(InterfaceCreate, ORMRead):
    pass


# --------------------------------------------------------------------------- External containers


class DwdmNodeCreate(ExternalFields):
    name: str
    pop_id: int | None = None
    vendor: str | None = None
    model: str | None = None
    node_type: str | None = None
    status: str = "active"


class DwdmNodeRead(DwdmNodeCreate, ORMRead):
    pass


class OdfCreate(ExternalFields):
    name: str
    pop_id: int | None = None
    rack: str | None = None
    port_count: int | None = None
    ports_used: int | None = None


class OdfRead(OdfCreate, ORMRead):
    pass


class PatchPanelCreate(ExternalFields):
    name: str
    pop_id: int | None = None
    odf_id: int | None = None
    rack: str | None = None
    connector_type: str | None = None
    port_count: int | None = None
    ports_used: int | None = None


class PatchPanelRead(PatchPanelCreate, ORMRead):
    pass


class FiberCreate(ExternalFields):
    name: str
    a_pop_id: int | None = None
    b_pop_id: int | None = None
    length_km: float | None = None
    core_count: int | None = None
    cores_used: int | None = None
    cable_type: str | None = None
    attenuation_db: float | None = None


class FiberRead(FiberCreate, ORMRead):
    pass


class DwdmWavelengthCreate(ExternalFields):
    name: str
    a_dwdm_node_id: int | None = None
    b_dwdm_node_id: int | None = None
    fiber_id: int | None = None
    channel: str | None = None
    frequency_thz: float | None = None
    rate_gbps: float | None = None
    status: str = "active"


class DwdmWavelengthRead(DwdmWavelengthCreate, ORMRead):
    pass


class IntegrationSourceCreate(BaseModel):
    name: str
    system_type: Literal["nms", "inventory", "dwdm_ems", "oss"]
    target_entity: Literal["devices", "dwdm_nodes", "odfs", "patch_panels", "fibers", "dwdm_wavelengths"]
    base_url: str | None = None
    auth_type: Literal["none", "token", "basic", "oauth2"] | None = "none"
    enabled: bool = False
    notes: str | None = None


class IntegrationSourceRead(IntegrationSourceCreate, ORMRead):
    last_sync_at: datetime | None = None
    last_status: str | None = None


class IngestResult(BaseModel):
    entity: str
    created: int
    updated: int


# --------------------------------------------------------------------------- Link inventory


class LinkCreate(BaseModel):
    name: str
    link_type: Literal["metro_e", "bundle"] = "metro_e"
    a_device_id: int
    a_interface_id: int | None = None
    b_device_id: int
    b_interface_id: int | None = None
    capacity_gbps: float = Field(default=0, ge=0)
    current_gbps: float = Field(default=0, ge=0)
    reserved_gbps: float = Field(default=0, ge=0)
    growth_gbps_per_month: float | None = None
    latency_ms: float | None = Field(default=None, ge=0)
    distance_km: float | None = Field(default=None, ge=0)
    status: Literal["up", "down", "planned"] = "up"
    discovery_source: Literal["lldp", "config", "manual"] = "manual"
    fiber_id: int | None = None
    wavelength_id: int | None = None


class LinkRead(LinkCreate, ORMRead):
    last_seen: datetime | None = None


# --------------------------------------------------------------------------- Customer database


class CustomerCreate(BaseModel):
    name: str
    account_no: str
    segment: Literal["enterprise", "wholesale", "carrier", "government", "retail"] | None = None
    contact_email: str | None = None


class CustomerRead(CustomerCreate, ORMRead):
    pass


class ServiceCreate(BaseModel):
    customer_id: int
    circuit_id: str
    service_type: Literal["internet", "iplc", "l2vpn", "l3vpn"]
    bandwidth_mbps: int = Field(gt=0)
    a_pop_id: int | None = None
    a_device_id: int | None = None
    a_interface: str | None = None
    b_pop_id: int | None = None
    b_device_id: int | None = None
    b_interface: str | None = None
    vlan_id: int | None = Field(default=None, ge=1, le=4094)
    vrf_name: str | None = None
    route_distinguisher: str | None = None
    route_target: str | None = None
    protection: Literal["none", "1+1", "1:1"] = "none"
    status: Literal["planned", "reserved", "active", "terminated"] = "active"
    notes: str | None = None


class ServiceRead(ServiceCreate, ORMRead):
    pass


# --------------------------------------------------------------------------- Topology


class LldpNeighborIn(BaseModel):
    local_interface: str
    remote_system_name: str
    remote_port_id: str
    remote_chassis_id: str | None = None


class LldpTableIn(BaseModel):
    """LLDP neighbor table of one device, as collected by Nornir/Scrapli or exported from NMS."""

    hostname: str
    neighbors: list[LldpNeighborIn]


class LldpNeighborRead(LldpNeighborIn, ORMRead):
    device_id: int
    collected_at: datetime | None = None


class NodePosition(BaseModel):
    device_id: int
    x: float
    y: float


class LayoutPoint(BaseModel):
    # Forbidding extras keeps x/y and lat/lon points apart: a mixed point is rejected, not silently trimmed.
    model_config = ConfigDict(extra="forbid")

    x: float = Field(ge=-1e7, le=1e7)
    y: float = Field(ge=-1e7, le=1e7)


class MapPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


MAP_VIEW_PREFIX = "map:"


class TopologyLayoutIn(BaseModel):
    """Positions of every node on one topology view, as `layout_key` of that view names it. A `map:<layout_key>`
    view holds where devices were dragged on the map as lat/lon, so it stays right whatever the map scale."""

    view_key: str = Field(min_length=1, max_length=255)
    positions: dict[str, LayoutPoint | MapPoint] = Field(max_length=5000)

    @model_validator(mode="after")
    def check_point_kind(self) -> "TopologyLayoutIn":
        on_map = self.view_key.startswith(MAP_VIEW_PREFIX)
        expected = MapPoint if on_map else LayoutPoint
        wrong = [node for node, point in self.positions.items() if not isinstance(point, expected)]
        if wrong:
            raise ValueError(f"positions of view {self.view_key} must be {'lat/lon' if on_map else 'x/y'}: "
                             + ", ".join(wrong[:5]))
        return self


class DiscoveryResult(BaseModel):
    lldp_links: int
    config_links: int
    created: int
    updated: int
    marked_down: int
    superseded: int = 0
    interfaces_created: int
    interfaces_removed: int = 0
    unresolved_neighbors: list[str]


# --------------------------------------------------------------------------- Capacity


class SampleIn(BaseModel):
    link_id: int
    ts: datetime
    in_gbps: float = Field(ge=0)
    out_gbps: float = Field(ge=0)


class CapacityCalcIn(BaseModel):
    capacity_gbps: float = Field(gt=0)
    current_gbps: float = Field(ge=0)
    reserved_gbps: float = Field(default=0, ge=0)
    growth_gbps_per_month: float = 0
    growth_pct_per_month: float | None = Field(
        default=None, description="Alternative to absolute growth: % of current per month (compound)."
    )
    threshold_pct: float | None = Field(default=None, gt=0, le=100)


class ForecastPoint(BaseModel):
    month: int
    date: str
    projected_gbps: float
    capacity_gbps: float
    threshold_gbps: float


class CapacityResult(BaseModel):
    capacity_gbps: float
    current_gbps: float
    reserved_gbps: float
    available_gbps: float
    utilization_pct: float
    committed_pct: float
    growth_gbps_per_month: float | None
    growth_pct_per_month: float | None
    growth_source: Literal["manual", "trend", "input", "none"]
    months_to_threshold: float | None
    months_to_full: float | None
    threshold_date: str | None
    exhaustion_date: str | None
    color: Literal["green", "yellow", "red"]
    # Level the forecast starts from: the trend line's value today plus the typical peak above it
    # (95th percentile of residuals), or the current peak when there is too little history.
    forecast_basis_gbps: float | None = None
    forecast_method: Literal["trend", "current"] = "current"
    trend_r2: float | None = None  # fit quality of the trend (0–1)
    trend_days: int = 0  # days of history the trend was fitted on


class LinkCapacity(CapacityResult):
    link_id: int
    name: str
    link_type: str
    status: str
    a_device: str
    b_device: str
    a_pop: str | None
    b_pop: str | None
    # Least-squares growth (Gbps/month) over the last 7 / 30 / 90 days of samples.
    growth_trends: dict[str, float | None] = {}
    samples_days: int = 0
    # Samples left out of current and trend as one-off spikes (services/capacity.spike_indexes).
    spike_days: list[datetime] = []


class LinkCapacityDetail(LinkCapacity):
    history: list[SampleIn]
    forecast: list[ForecastPoint]


class SyncAllIn(BaseModel):
    """Options of the combined collector job; every field has the daily run's default."""

    rrd_days: int = Field(2, ge=1, le=365, description="RRD window for links that already have history")
    rrd_backfill_days: int = Field(90, ge=1, le=365, description="RRD window for links without history yet")
    gitweb_full: bool = Field(False, description="Re-read every gitweb file, not only those changed")
