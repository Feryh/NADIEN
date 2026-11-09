// Mirrors the /api/topology/view responses for level=city and level=neighbors
// (field names stay snake_case exactly as the API sends them).

export interface CityLink {
  id: number
  source: string  // drawn node id: dev:<id>, pop:<code> or summary:<pop>
  target: string
  a_device: string
  a_interface: string | null  // null when the link has no interface record
  b_device: string
  b_interface: string | null
  capacity_gbps: number
  current_gbps: number
  utilization_pct: number
  committed_pct: number
  color: string  // computed by backend capacity.py; the frontend never recomputes it
  status: 'up' | 'down' | 'planned'
  segment: string
  spof: boolean
}

export interface CityCluster {
  id: string  // "c:<smallest>-<second smallest member device id>": unique, two rings share at most one device
  kind: 'ring' | 'mesh' | 'pair'
  pop: string | null  // null: members span several POPs
  device_ids: number[]
}

export interface CityPop {
  code: string
  name: string
  city: string
  device_count: number
}

export interface CityDeviceNode {
  id: string
  kind: 'device'
  device_id: number
  label: string
  pop: string  // POP code; the backend sends "?" for a device without a POP
  site: string
  role: string
  category: string
  tier: number
  vendor: string | null
  status: string
  x: number | null
  y: number | null
  center: boolean
  mgmt_ip: string | null
  city: string | null
  reachability: 'up' | 'down' | 'unknown'
  uptime_s: number | null
  last_polled_at: string | null
  cluster_id: string | null
  spof: boolean
  located?: boolean
  estimated?: boolean
  lat?: number | null
  lon?: number | null
}

export interface CityExternalPopNode {
  id: string
  kind: 'external_pop'
  code: string
  devices: number
  label: string
  area?: string
  province?: string
  regency?: string
  lat?: number | null
  lon?: number | null
}

// Present in the city view only, when the scale guard collapses access devices.
export interface CitySummaryNode {
  id: string
  kind: 'summary'
  pop: string
  label: string
  devices: number
  tier: number
}

export type CityNode = CityDeviceNode | CityExternalPopNode | CitySummaryNode

export interface CitySummarized {
  node_id: string
  pop: string
  count: number
  device_ids: number[]
}

export interface CityAggregateEdge {
  id: string
  source: string
  target: string
  segment: string
  links: number
  down: number
  capacity_gbps: number
  current_gbps: number
  utilization_pct: number
  committed_pct: number
  color: string
  capacity_unknown: number  // count of links with unknown capacity
}

export interface CityView {
  level: 'city' | 'neighbors'
  area?: string
  province?: string
  regency?: string
  nodes: CityNode[]
  pops: CityPop[]
  clusters: CityCluster[]
  links: CityLink[]
  edges: CityAggregateEdge[]
  segments: Record<string, unknown>
  hidden_links: number  // cross-city config links without traffic left out of this view (counted even when shown)
  layout_key: string
  layout?: Record<string, { x: number; y: number }>
  summarized?: CitySummarized[]
  // neighbors view only
  device?: string
  device_id?: number
  depth?: number
  truncated?: boolean
  pop?: string | null
}
