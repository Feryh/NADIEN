export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message)
  }
}

/** Fired when the session is gone, so the app can show the login screen again. */
export const UNAUTHORIZED_EVENT = 'nadien:unauthorized'

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(`/api${path}`, {
    method,
    credentials: 'same-origin',
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (res.status === 401 && !path.startsWith('/auth/')) window.dispatchEvent(new Event(UNAUTHORIZED_EVENT))
  if (!res.ok) {
    let detail = res.statusText
    try {
      const data = await res.json()
      detail = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail)
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(`${res.status} — ${detail}`, res.status)
  }
  return res.status === 204 ? (undefined as T) : res.json()
}

export const api = {
  get: <T>(path: string) => request<T>('GET', path),
  post: <T>(path: string, body?: unknown) => request<T>('POST', path, body ?? {}),
  patch: <T>(path: string, body: unknown) => request<T>('PATCH', path, body),
  put: <T>(path: string, body: unknown) => request<T>('PUT', path, body),
  del: (path: string) => request<void>('DELETE', path),
}

export type CapacityColor = 'green' | 'yellow' | 'red'

export interface CapacityResult {
  capacity_gbps: number
  current_gbps: number
  reserved_gbps: number
  available_gbps: number
  utilization_pct: number
  committed_pct: number
  growth_gbps_per_month: number | null
  growth_pct_per_month: number | null
  growth_source: 'manual' | 'trend' | 'input' | 'none'
  months_to_threshold: number | null
  months_to_full: number | null
  threshold_date: string | null
  exhaustion_date: string | null
  color: CapacityColor
  /** Level the forecast starts from: trend value today + typical peak above it, or the current peak. */
  forecast_basis_gbps: number | null
  forecast_method: 'trend' | 'current'
  trend_r2: number | null
  trend_days: number
}

/** suspect: utilization far above 100 % — the link capacity data itself is wrong. */
export type HotspotLevel = 'critical' | 'warning' | 'normal' | 'suspect'

export interface NodeForecast extends CapacityResult {
  key: string
  label: string
  pop: string | null
  links: number
  history_days: number
}

export interface Hotspots {
  thresholds: { warning_pct: number; critical_pct: number; suspect_pct: number }
  links: {
    counts: Record<HotspotLevel, number>
    rows: {
      link_id: number
      a_device_id: number | null
      b_device_id: number | null
      a_device: string
      b_device: string
      /** Port at each end of the link; bundle: either end is a LAG (Bundle-Ether, Eth-Trunk, ae, …). */
      a_port: string | null
      b_port: string | null
      bundle: boolean
      a_pop: string | null
      b_pop: string | null
      status: string
      capacity_gbps: number
      current_gbps: number
      utilization_pct: number
      level: HotspotLevel
      months_to_threshold: number | null
    }[]
  }
  devices: {
    counts: Record<HotspotLevel, number>
    rows: {
      device_id: number
      hostname: string
      pop: string
      area: string
      category: string
      vendor: string | null
      total: number
      used: number
      idle: number
      disabled: number
      used_pct: number
      level: HotspotLevel
    }[]
  }
}

export interface DeviceSummary {
  total: number
  categories: {
    category: string
    label: string
    devices: number
    roles: Record<string, number>
    vendors: Record<string, number>
    status: Record<string, number>
    areas: Record<string, number>
  }[]
}

export interface TopologySearch {
  pops: { pop_id: number; code: string; name: string; area: string; devices: number }[]
  devices: { device_id: number; hostname: string; pop: string; role: string; category: string; area: string; mgmt_ip: string | null }[]
}

export interface OltCapacity {
  thresholds: { warning_pct: number; critical_pct: number; pon_max_onts: number }
  total: number
  rows: {
    device_id: number
    hostname: string
    pop: string | null
    vendor: string | null
    pon_total: number
    pon_used: number
    onts_online: number
    /** Online ONTs of the fullest PON port, and that against pon_max_onts (split ratio). */
    busiest_pon_onts: number
    /** null: the OLT reports no ONT counters (Dasan), occupancy unknown. */
    busiest_pon_pct: number | null
    pon_hot_ports: number
    pon_level: HotspotLevel | null
    uplinks: number
    uplink_capacity_gbps: number
    uplink_current_gbps: number
    uplink_utilization_pct: number
    uplink_level: HotspotLevel
    uplinks_down: number
  }[]
}

export interface LinkCapacity extends CapacityResult {
  link_id: number
  name: string
  link_type: string
  status: string
  a_device: string
  b_device: string
  a_pop: string | null
  b_pop: string | null
  /** Growth G/bulan per trend window ("7", "30", "90" hari), null when the window has too few samples. */
  growth_trends: Record<string, number | null>
  samples_days: number
}

export interface LinkCapacityDetail extends LinkCapacity {
  history: { ts: string; in_gbps: number; out_gbps: number }[]
  forecast: { month: number; date: string; projected_gbps: number; capacity_gbps: number; threshold_gbps: number }[]
}

export interface PopUtilization {
  pop_id: number
  code: string
  name: string
  links: number
  devices: number
  capacity_gbps: number
  current_gbps: number
  reserved_gbps: number
  available_gbps: number
  utilization_pct: number
  committed_pct: number
  color: CapacityColor
}

export interface TopologyData {
  pops: { id: number; code: string; name: string; tier: string; latitude: number | null; longitude: number | null }[]
  devices: {
    id: number
    hostname: string
    pop_id: number
    role: string
    vendor: string | null
    status: string
    loopback_ipv4: string | null
    x: number | null
    y: number | null
  }[]
  links: {
    id: number
    source: number
    target: number
    name: string
    a_interface: string | null
    b_interface: string | null
    link_type: string
    status: string
    discovery_source: string
    capacity_gbps: number
    current_gbps: number
    reserved_gbps: number
    utilization_pct: number
    committed_pct: number
    color: CapacityColor
    latency_ms: number | null
  }[]
}

export interface DiscoveryResult {
  lldp_links: number
  config_links: number
  created: number
  updated: number
  marked_down: number
  interfaces_created: number
  unresolved_neighbors: string[]
}

export interface DashboardSummary {
  counts: Record<string, number>
  available_ports: number
  capacity: { total_gbps: number; current_gbps: number; reserved_gbps: number; available_gbps: number; utilization_pct: number }
  link_colors: Partial<Record<CapacityColor, number>>
  links_down: number
  services_by_type: Record<string, number>
  bandwidth_by_type_mbps: Record<string, number>
  exhausting_soon: LinkCapacity[]
}
