export class ApiError extends Error {}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(`/api${path}`, {
    method,
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!res.ok) {
    let detail = res.statusText
    try {
      const data = await res.json()
      detail = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail)
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(`${res.status} — ${detail}`)
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
