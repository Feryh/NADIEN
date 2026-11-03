import cytoscape, { Core, ElementDefinition } from 'cytoscape'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link as RouterLink, useSearchParams } from 'react-router-dom'
import { api, CapacityColor, DiscoveryResult } from '../api'
import { BasemapShape, Box, declutter, loadBasemap, project } from '../components/basemap'
import { PopAreaEditor, SearchHit, TopologySearchBox } from '../components/TopologyTools'
import { CAPACITY_HEX, fmtG } from '../components/ui'

// Drill-down topology:
//   area      — areas west→east as a schematic, trunks between them
//   pop       — POPs of one area in rows by their highest role, other areas as the top (uplink) row
//   device    — devices of one POP in role rows (Core → PE/BNG → Metro → OLT/Access), other POPs on top
//   neighbors — one device and everything within 1–2 hops, same role rows
type Level = 'area' | 'pop' | 'device' | 'neighbors'

interface EdgeData {
  id: string
  source: string
  target: string
  links: number
  down: number
  capacity_gbps: number
  current_gbps: number
  utilization_pct: number
  committed_pct: number
  color: CapacityColor
}

interface MonitorItem {
  id: string
  label: string
  links: number
  down: number
  capacity_gbps: number
  current_gbps: number
  reserved_gbps: number
  committed_pct: number
  color: CapacityColor
  edge?: EdgeData
  node?: NodeData
}

function monitorItems(view: View | null): MonitorItem[] {
  if (!view) return []
  const items: MonitorItem[] = view.edges.map((edge) => ({
    ...edge,
    label: `${edge.source} ↔ ${edge.target}`,
    reserved_gbps: Math.max(0, edge.capacity_gbps * edge.committed_pct / 100 - edge.current_gbps),
    edge,
  }))
  for (const node of view.nodes) {
    const internal = node.internal
    if (!internal || !internal.links) continue
    items.push({
      id: `internal:${node.id}`,
      label: `${node.label} · internal`,
      links: internal.links,
      down: internal.down,
      capacity_gbps: internal.capacity_gbps,
      current_gbps: internal.current_gbps,
      reserved_gbps: internal.reserved_gbps,
      committed_pct: internal.committed_pct,
      color: internal.color,
      node,
    })
  }
  return items.sort((a, b) => {
    const priority = (x: MonitorItem) => x.down > 0 ? 0 : x.color === 'red' ? 1 : x.color === 'yellow' ? 2 : 3
    return priority(a) - priority(b) || b.committed_pct - a.committed_pct
  })
}

interface NodeData {
  id: string
  kind: 'area' | 'pop' | 'device' | 'external_area' | 'external_pop'
  label: string
  name?: string
  area?: string
  pop?: string
  pops?: number
  devices?: number
  sites?: number
  links?: number
  categories?: Record<string, number>
  internal?: Omit<EdgeData, 'id' | 'source' | 'target'> & { reserved_gbps: number }
  lat?: number | null
  lon?: number | null
  site?: string
  role?: string
  category?: string
  tier?: number
  vendor?: string | null
  status?: string
  device_id?: number
  center?: boolean
  x?: number | null
  y?: number | null
  pop_id?: number
  region?: string | null
  estimated?: boolean  // POP without coordinates: position estimated for the map, never stored
  pops_located?: number
  located?: boolean  // device with its own site coordinates
}

interface View {
  level: Level
  area?: string
  pop?: string
  pop_name?: string
  pop_id?: number
  region?: string | null
  device?: string
  device_id?: number
  depth?: number
  truncated?: boolean
  nodes: NodeData[]
  edges: EdgeData[]
}

type Pos = { x: number; y: number }

const MAP_KEY = 'nadien.topology.map'

function storedMapOn(): boolean {
  try {
    return localStorage.getItem(MAP_KEY) !== '0'  // map on by default
  } catch {
    return true
  }
}

const ROLE_COLOR: Record<string, string> = {
  p: '#6366f1', dci: '#f97316', pe: '#0ea5e9', bng: '#e879f9', agg: '#14b8a6', csr: '#fb7185', access: '#64748b', olt: '#a3e635',
}
const TIER_LABEL = ['Core (P/DCI)', 'PE / BNG', 'Metro / Aggregation', 'CSR / OLT / Access', 'Lainnya']
const CATEGORY_LABEL: Record<string, string> = {
  router: 'Router', metro: 'Metro', csr: 'CSR', olt: 'OLT', access: 'Access', other: 'Lainnya',
}

const STYLE = [
  {
    selector: 'node',
    style: {
      label: 'data(display)',
      'text-wrap': 'wrap',
      color: '#e2e8f0',
      'font-size': 12,
      'text-valign': 'bottom',
      'text-margin-y': 6,
      'background-color': '#334155',
    },
  },
  {
    selector: 'node[kind="area"]',
    style: {
      shape: 'round-rectangle',
      width: 'data(w)',
      height: 'data(h)',
      'background-color': '#0c4a6e',
      'border-color': '#38bdf8',
      'border-width': 2,
      'text-valign': 'center',
      'text-margin-y': 0,
      'font-size': 15,
      'font-weight': 'bold',
      'text-max-width': 'data(w)',
    },
  },
  {
    selector: 'node[kind="pop"]',
    style: {
      shape: 'round-rectangle',
      width: 'data(w)',
      height: 34,
      'background-color': 'data(tierColor)',
      'background-opacity': 0.9,
      'border-color': '#0f172a',
      'border-width': 2,
      'text-valign': 'center',
      'text-margin-y': 0,
      'font-size': 13,
      'font-weight': 'bold',
      color: '#0b1120',
    },
  },
  {
    selector: 'node[kind="external_area"], node[kind="external_pop"]',
    style: {
      shape: 'round-rectangle',
      width: 'data(w)',
      height: 30,
      'background-color': '#1e293b',
      'border-color': '#64748b',
      'border-style': 'dashed',
      'border-width': 1.5,
      'text-valign': 'center',
      'text-margin-y': 0,
      color: '#cbd5e1',
      'font-size': 11,
    },
  },
  {
    selector: 'node[kind="device"]',
    style: {
      width: 26,
      height: 26,
      'background-color': 'data(roleColor)',
      'border-width': 2,
      'border-color': '#0f172a',
      'font-size': 11,
      'text-max-width': 140,
      'text-wrap': 'ellipsis',
    },
  },
  { selector: 'node[?estimated]', style: { 'border-style': 'dashed', 'border-color': '#e2e8f0', 'border-width': 2 } },
  { selector: 'node[?center]', style: { width: 40, height: 40, 'border-color': '#facc15', 'border-width': 4, 'font-weight': 'bold' } },
  { selector: 'node[status="maintenance"], node[status="decommissioned"]', style: { opacity: 0.5 } },
  {
    selector: 'edge',
    style: {
      width: 'data(width)',
      'line-color': 'data(colorHex)',
      'curve-style': 'bezier',
      label: '',
      'font-size': 10,
      color: '#e2e8f0',
      'text-background-color': '#0f172a',
      'text-background-opacity': 0.9,
      'text-background-padding': '2px',
      'text-rotation': 'autorotate',
      opacity: 0.75,
    },
  },
  { selector: 'edge.labeled, edge:selected', style: { label: 'data(display)', opacity: 1 } },
  { selector: 'edge[allDown = 1]', style: { 'line-style': 'dashed', 'line-color': '#64748b' } },
  { selector: '.faded', style: { opacity: 0.08 } },
  { selector: '.hit', style: { 'border-color': '#facc15', 'border-width': 4 } },
  { selector: 'node:selected', style: { 'overlay-color': '#38bdf8', 'overlay-opacity': 0.25, 'overlay-padding': 6 } },
] as unknown as cytoscape.CytoscapeOptions['style']

const TIER_COLOR = ['#818cf8', '#38bdf8', '#2dd4bf', '#a3e635', '#94a3b8']

function edgeWidth(capacity: number): number {
  return Math.min(1.5 + Math.log10(Math.max(capacity, 1)) * 2.2, 12)
}

function nodeDisplay(n: NodeData): string {
  switch (n.kind) {
    case 'area':
      return `${n.label}\n${n.pops} POP · ${n.devices} perangkat`
    case 'pop':
      return n.label
    case 'external_area':
    case 'external_pop':
      return `→ ${n.label}`
    default:
      return n.label
  }
}

/**
 * Compact schematic: a northern row (Sumatera, Kalimantan, Sulawesi, Maluku-Papua…) and a southern
 * row (Jawa, Bali-NT), each west→east; areas without a location (unmapped) on a row below.
 */
function areaLayout(view: View): Record<string, Pos> {
  const pos: Record<string, Pos> = {}
  const byLon = (a: NodeData, b: NodeData) => (a.lon ?? 0) - (b.lon ?? 0)
  const north = view.nodes.filter((n) => n.lon != null && (n.lat ?? 0) > -4).sort(byLon)
  const south = view.nodes.filter((n) => n.lon != null && (n.lat ?? 0) <= -4).sort(byLon)
  const other = view.nodes.filter((n) => n.lon == null)
  const dx = 270
  const width = Math.max(north.length, south.length, other.length) * dx
  const place = (row: NodeData[], y: number) =>
    row.forEach((n, i) => {
      pos[n.id] = { x: (width - row.length * dx) / 2 + i * dx, y }
    })
  place(north, 0)
  place(south, 230)
  place(other, 460)
  return pos
}

/**
 * Role rows with crossing reduction: each row is ordered by the average position of its
 * neighbours in the rows above/below (barycentre heuristic, a few sweeps), then long rows wrap.
 * Other POPs (external) form the top row.
 */
function layeredLayout(view: View): Record<string, Pos> {
  // Devices/POPs by role tier; other POPs/areas (external) form the top "uplink" row.
  const rowOf = (n: NodeData) => (n.kind === 'device' || n.kind === 'pop' ? (n.tier ?? 4) : -1)
  const rows = new Map<number, NodeData[]>()
  for (const n of view.nodes) {
    const r = rowOf(n)
    rows.set(r, [...(rows.get(r) ?? []), n])
  }
  const rowKeys = [...rows.keys()].sort((a, b) => a - b)
  for (const r of rowKeys) rows.get(r)!.sort((a, b) => (a.site ?? '').localeCompare(b.site ?? '') || a.label.localeCompare(b.label))

  const neighbours = new Map<string, string[]>()
  for (const e of view.edges) {
    neighbours.set(e.source, [...(neighbours.get(e.source) ?? []), e.target])
    neighbours.set(e.target, [...(neighbours.get(e.target) ?? []), e.source])
  }
  const order = new Map<string, number>()
  const index = () => rowKeys.forEach((r) => rows.get(r)!.forEach((n, i) => order.set(n.id, i / Math.max(rows.get(r)!.length - 1, 1))))
  index()
  const rowById = new Map(view.nodes.map((n) => [n.id, rowOf(n)]))
  const sweep = (keys: number[], towards: (r: number, other: number) => boolean) => {
    for (const r of keys) {
      const row = rows.get(r)!
      const bary = new Map<string, number>()
      for (const n of row) {
        const ref = (neighbours.get(n.id) ?? []).filter((o) => towards(r, rowById.get(o) ?? r)).map((o) => order.get(o) ?? 0)
        bary.set(n.id, ref.length ? ref.reduce((s, v) => s + v, 0) / ref.length : order.get(n.id) ?? 0)
      }
      row.sort((a, b) => bary.get(a.id)! - bary.get(b.id)!)
      row.forEach((n, i) => order.set(n.id, i / Math.max(row.length - 1, 1)))
    }
  }
  for (let pass = 0; pass < 4; pass++) {
    sweep(rowKeys.slice(1), (r, o) => o < r)
    sweep([...rowKeys].reverse().slice(1), (r, o) => o > r)
  }

  const pos: Record<string, Pos> = {}
  const perLine = 12
  const dx = 165
  const widest = Math.min(perLine, Math.max(...rowKeys.map((r) => rows.get(r)!.length)))
  let y = 0
  for (const r of rowKeys) {
    const row = rows.get(r)!
    row.forEach((n, i) => {
      const line = Math.floor(i / perLine)
      const inLine = Math.min(perLine, row.length - line * perLine)
      const offset = ((widest - inLine) * dx) / 2
      pos[n.id] = { x: offset + (i % perLine) * dx, y: y + line * 85 }
    })
    y += Math.ceil(row.length / perLine) * 85 + 70
  }
  return pos
}

function buildElements(view: View): ElementDefinition[] {
  const maxDevices = Math.max(1, ...view.nodes.map((n) => n.devices ?? 0))
  const nodes: ElementDefinition[] = view.nodes.map((n) => {
    const scale = Math.sqrt((n.devices ?? 0) / maxDevices)
    return {
      data: {
        ...n,
        display: nodeDisplay(n),
        roleColor: ROLE_COLOR[n.role ?? ''] ?? '#64748b',
        tierColor: TIER_COLOR[n.tier ?? 4],
        w: n.kind === 'area' ? 170 + 60 * scale : n.kind === 'pop' ? Math.max(60, n.label.length * 10 + 24) : Math.max(70, n.label.length * 7 + 26),
        h: 70 + 40 * scale,
      },
    }
  })
  const edges: ElementDefinition[] = view.edges.map((e) => ({
    data: {
      ...e,
      display: `${e.links > 1 ? `${e.links}× · ` : ''}${fmtG(e.capacity_gbps)} · ${e.committed_pct.toFixed(0)}%`,
      colorHex: CAPACITY_HEX[e.color],
      width: edgeWidth(e.capacity_gbps),
      allDown: e.down === e.links ? 1 : 0,
    },
  }))
  return [...nodes, ...edges]
}

// Map layout: pixels per degree, the on-screen size of nodes relative to their normal size (they stay
// that size at any zoom, like map markers), and the on-screen gap kept between overlapping nodes.
const MAP_SCALE: Partial<Record<Level, { pxPerDegree: number; markerScale: number; minScreenGap: number }>> = {
  area: { pxPerDegree: 60, markerScale: 0.9, minScreenGap: 10 },
  pop: { pxPerDegree: 200, markerScale: 0.9, minScreenGap: 6 },
  device: { pxPerDegree: 3000, markerScale: 1, minScreenGap: 4 },  // one city: ~30 px per km
  neighbors: { pxPerDegree: 1500, markerScale: 1, minScreenGap: 4 },
}

// Spacing never moves a node more than this many node heights from its place on the map.
const MAX_SHIFT_HEIGHTS = 3

/** Normal (zoom 1) size of a node, before the map's marker scale. */
function nodeSize(n: { kind: string; w?: number }): { width: number; height: number } {
  if (n.kind === 'area') return { width: 150, height: 56 }
  if (n.kind === 'pop') return { width: n.w ?? 80, height: 34 }
  if (n.kind.startsWith('external')) return { width: n.w ?? 120, height: 30 }
  return { width: 26, height: 26 }
}

/** On the map, keep nodes, labels and lines a constant size on screen whatever the zoom. */
function applyMarkerScale(cy: Core, level: Level) {
  const scale = MAP_SCALE[level]!.markerScale / cy.zoom()
  cy.batch(() => {
    cy.nodes().forEach((n) => {
      const kind = n.data('kind') as string
      const { width, height } = nodeSize({ kind, w: n.data('w') as number })
      n.style({
        width: width * scale,
        height: height * scale,
        'font-size': (kind === 'area' ? 13 : 12) * scale,
        'border-width': 2 * scale,
        'text-max-width': `${(kind === 'area' ? 150 : 160) * scale}px`,
      })
    })
    cy.edges().forEach((e) => {
      e.style({ width: (e.data('width') as number) * scale, 'font-size': 10 * scale })
    })
  })
}

/**
 * At device level the other POPs are tens or hundreds of km away: drawn at their real spot they would
 * shrink the local site map to a dot. They keep only their direction, on a ring just outside the devices.
 */
function ringExternal(view: View, home: Map<string, Pos>) {
  const local = view.nodes.filter((n) => n.kind === 'device').map((n) => home.get(n.id)!)
  if (!local.length) return
  const median = (values: number[]) => [...values].sort((a, b) => a - b)[Math.floor(values.length / 2)]
  const center = { x: median(local.map((p) => p.x)), y: median(local.map((p) => p.y)) }
  const distances = local.map((p) => Math.hypot(p.x - center.x, p.y - center.y)).sort((a, b) => a - b)
  const radius = Math.max(300, distances[Math.floor(distances.length * 0.9)] ?? 0) * 1.35
  for (const n of view.nodes) {
    if (n.kind !== 'external_pop') continue
    const spot = home.get(n.id)!
    const angle = Math.atan2(spot.y - center.y, spot.x - center.x) || 0
    home.set(n.id, { x: center.x + radius * Math.cos(angle), y: center.y + radius * Math.sin(angle) })
  }
}

function mapLayout(cy: Core, view: View) {
  const { pxPerDegree, markerScale, minScreenGap } = MAP_SCALE[view.level]!
  const home = new Map<string, Pos>()
  let parked = 0
  for (const n of view.nodes) {
    // Nodes without any position (the unmapped area) wait in a row south-west of the map.
    home.set(n.id, n.lat != null && n.lon != null ? project(n.lat, n.lon, pxPerDegree) : project(-12.5, 96 + 7 * parked++, pxPerDegree))
  }
  if (view.level === 'device' || view.level === 'neighbors') ringExternal(view, home)
  const elements = buildElements(view)
  const widthOf = new Map(elements.filter((e) => !e.data.source).map((e) => [e.data.id as string, e.data.w as number]))
  // Nodes keep a fixed size on screen, so how much they overlap depends on the zoom the view fits at,
  // and that zoom depends on how far they were pushed apart: settle it in a few rounds.
  let boxes = new Map<string, Box>()
  for (let round = 0; round < 4; round++) {
    const source = round === 0 ? home : new Map([...boxes].map(([id, b]) => [id, { x: b.x, y: b.y }]))
    const xs = [...source.values()].map((p) => p.x)
    const ys = [...source.values()].map((p) => p.y)
    const zoom = Math.max(0.01, Math.min(cy.width() / (Math.max(...xs) - Math.min(...xs) + 1),
                                         cy.height() / (Math.max(...ys) - Math.min(...ys) + 1)) * 0.8)
    const toModel = markerScale / zoom
    boxes = new Map()
    for (const n of view.nodes) {
      const { width, height } = nodeSize({ kind: n.kind, w: widthOf.get(n.id) })
      const start = home.get(n.id)!
      // A device is a small dot with its hostname written underneath: space it by the label.
      const boxWidth = n.kind === 'device' ? 110 : width
      boxes.set(n.id, { x: start.x, y: start.y, hw: (boxWidth * toModel) / 2, hh: (height * toModel + 16 * toModel) / 2 })
    }
    declutter(boxes, minScreenGap * toModel, MAX_SHIFT_HEIGHTS * 34 * toModel)
  }
  cy.layout({
    name: 'preset',
    padding: 40,
    positions: (n: cytoscape.NodeSingular) => boxes.get(n.id()) ?? { x: 0, y: 0 },
  } as cytoscape.LayoutOptions).run()
}

function runLayout(cy: Core, view: View, onMap: boolean) {
  if (onMap) return mapLayout(cy, view)
  if (view.level === 'area') {
    const pos = areaLayout(view)
    cy.layout({ name: 'preset', padding: 50, positions: (n: cytoscape.NodeSingular) => pos[n.id()] } as cytoscape.LayoutOptions).run()
    return
  }
  const auto = layeredLayout(view)
  const saved: Record<string, Pos> = {}
  if (view.level === 'device') {
    for (const n of view.nodes) if (n.kind === 'device' && n.x != null && n.y != null) saved[n.id] = { x: n.x, y: n.y }
  }
  cy.layout({
    name: 'preset',
    padding: 30,
    positions: (n: cytoscape.NodeSingular) => saved[n.id()] ?? auto[n.id()] ?? { x: 0, y: 0 },
  } as cytoscape.LayoutOptions).run()
}

export default function Topology() {
  const [params, setParams] = useSearchParams()
  const level = (params.get('level') as Level) || 'area'
  const area = params.get('area') ?? undefined
  const pop = params.get('pop') ?? undefined
  const deviceId = params.get('device_id') ?? undefined
  const depth = params.get('depth') ?? '1'
  const focus = params.get('focus') ?? undefined

  const container = useRef<HTMLDivElement>(null)
  const mapLayer = useRef<SVGGElement>(null)
  const cyRef = useRef<Core | null>(null)
  const [mapOn, setMapOn] = useState(storedMapOn)
  const [basemap, setBasemap] = useState<BasemapShape[]>([])
  const [view, setView] = useState<View | null>(null)
  // Devices go on the map only where at least one has its own site coordinates.
  const devicesLocated = view?.nodes.filter((n) => n.kind === 'device' && n.located).length ?? 0
  const mapAvailable = level === 'area' || level === 'pop' || devicesLocated > 0
  const onMap = mapOn && mapAvailable && view?.level === level
  const [loading, setLoading] = useState(false)
  const [selected, setSelected] = useState<{ node?: NodeData; edge?: EdgeData } | null>(null)
  const [message, setMessage] = useState<{ text: string; error?: boolean } | null>(null)
  const [colorFilter, setColorFilter] = useState<'all' | 'warn' | 'red'>('all')
  const [edgeLabels, setEdgeLabels] = useState(false)
  const [syncPhase, setSyncPhase] = useState<string | null>(null)
  const [dirty, setDirty] = useState(false)
  const [busy, setBusy] = useState(false)

  const go = useCallback(
    (next: { level: Level; area?: string; pop?: string; device_id?: number; depth?: number; focus?: string }) => {
      const p: Record<string, string> = { level: next.level }
      if (next.area) p.area = next.area
      if (next.pop) p.pop = next.pop
      if (next.device_id != null) p.device_id = String(next.device_id)
      if (next.depth) p.depth = String(next.depth)
      if (next.focus) p.focus = next.focus
      setParams(p)
    },
    [setParams],
  )

  const load = useCallback(async () => {
    setLoading(true)
    setSelected(null)
    setDirty(false)
    try {
      const q = new URLSearchParams({ level })
      if (area) q.set('area', area)
      if (pop) q.set('pop', pop)
      if (deviceId) q.set('device_id', deviceId)
      if (level === 'neighbors') q.set('depth', depth)
      setView(await api.get<View>(`/topology/view?${q}`))
    } catch (e) {
      setMessage({ text: (e as Error).message, error: true })
      setView(null)
    } finally {
      setLoading(false)
    }
  }, [level, area, pop, deviceId, depth])

  useEffect(() => {
    load()
  }, [load])

  // Edge labels on by default only where there are few edges (area level).
  useEffect(() => {
    setEdgeLabels(level === 'area')
  }, [level])

  // A running collector sync rebuilds topology itself and holds the DB writer; block manual discovery.
  useEffect(() => {
    let alive = true
    const check = () =>
      api
        .get<{ status: string; phase: string }>('/collectors/jobs/current')
        .then((j) => alive && setSyncPhase(j.status === 'running' ? j.phase : null))
        .catch(() => undefined)
    check()
    const t = setInterval(check, 10000)
    return () => {
      alive = false
      clearInterval(t)
    }
  }, [])

  useEffect(() => {
    if (onMap && !basemap.length) loadBasemap().then(setBasemap).catch(() => setMessage({ text: 'Peta dasar gagal dimuat', error: true }))
  }, [onMap, basemap.length])

  function toggleMap(on: boolean) {
    setMapOn(on)
    try {
      localStorage.setItem(MAP_KEY, on ? '1' : '0')
    } catch {
      /* storage blocked: the choice just isn't remembered */
    }
  }

  useEffect(() => {
    if (!view || !container.current) return
    const cy = cytoscape({
      container: container.current,
      elements: buildElements(view),
      style: STYLE,
      wheelSensitivity: 0.25,
      minZoom: 0.05,
      maxZoom: 3,
    })
    const highlight = (id: string | null) => {
      cy.elements().removeClass('faded')
      if (!id) return
      const el = cy.getElementById(id)
      // node: itself + neighbours and their links · edge: itself + its two ends
      const keep = el.union(el.neighborhood()).union(el.connectedNodes())
      cy.elements().not(keep).addClass('faded')
    }
    cy.on('tap', 'node', (e) => {
      setSelected({ node: e.target.data() as NodeData })
      highlight(e.target.id())
    })
    cy.on('tap', 'edge', (e) => {
      setSelected({ edge: e.target.data() as EdgeData })
      highlight(e.target.id())
    })
    cy.on('tap', (e) => {
      if (e.target === cy) {
        setSelected(null)
        highlight(null)
      }
    })
    cy.on('dbltap', 'node', (e) => {
      const n = e.target.data() as NodeData
      if (n.kind === 'area' || n.kind === 'external_area') go({ level: 'pop', area: n.label })
      else if (n.kind === 'pop' || n.kind === 'external_pop') go({ level: 'device', pop: n.label })
      else if (n.kind === 'device' && n.device_id != null) go({ level: 'neighbors', device_id: n.device_id, depth: 1 })
    })
    cy.on('dragfree', 'node[kind="device"]', () => setDirty(true))
    cyRef.current = cy
    // The basemap is drawn in degrees; keep it under the graph through every pan and zoom.
    let lastZoom = 0
    const syncMap = () => {
      if (!onMap) return
      const { x, y } = cy.pan()
      mapLayer.current?.setAttribute('transform', `translate(${x},${y}) scale(${cy.zoom() * MAP_SCALE[view.level]!.pxPerDegree})`)
      if (cy.zoom() !== lastZoom) {
        lastZoom = cy.zoom()
        applyMarkerScale(cy, view.level)
      }
    }
    cy.on('viewport', syncMap)
    runLayout(cy, view, onMap)
    cy.fit(undefined, 40)
    if (onMap) {
      // Marker sizes follow the zoom and the fit follows the sizes: settle both, then draw the map.
      applyMarkerScale(cy, view.level)
      cy.fit(undefined, 50)
      lastZoom = 0
    }
    syncMap()
    if (focus) {
      const hit = cy.nodes().filter((n) => (n.data('label') as string)?.toUpperCase() === focus.toUpperCase())
      if (hit.nonempty()) {
        hit.addClass('hit')
        cy.animate({ center: { eles: hit }, zoom: 1.2 }, { duration: 300 })
        setSelected({ node: hit.first().data() as NodeData })
        highlight(hit.first().id())
      }
    }
    return () => cy.destroy()
  }, [view, go, focus, onMap])

  useEffect(() => {
    const cy = cyRef.current
    if (!cy) return
    cy.edges().forEach((edge) => {
      const color = edge.data('color') as CapacityColor
      const visible = colorFilter === 'all' || (colorFilter === 'red' ? color === 'red' : color !== 'green')
      edge.style('display', visible ? 'element' : 'none')
      if (edgeLabels) edge.addClass('labeled')
      else edge.removeClass('labeled')
    })
  }, [colorFilter, edgeLabels, view])

  // A POP opens its devices; a device opens its neighbours, centred and highlighted.
  function openHit(hit: SearchHit) {
    if (hit.kind === 'pop') go({ level: 'device', pop: hit.code })
    else go({ level: 'neighbors', device_id: hit.deviceId, depth: 1, focus: hit.label })
  }

  async function areaSaved(code: string, area: string) {
    setMessage({ text: `Area POP ${code} disimpan: ${area}.` })
    await load()
  }

  async function savePositions() {
    const cy = cyRef.current
    if (!cy) return
    const positions = cy.nodes('[kind="device"]').map((n) => ({
      device_id: Number(n.data('device_id')),
      x: Math.round(n.position('x')),
      y: Math.round(n.position('y')),
    }))
    try {
      await api.put('/topology/positions', positions)
      setDirty(false)
      setMessage({ text: `Posisi ${positions.length} perangkat di ${pop} disimpan.` })
    } catch (e) {
      setMessage({ text: (e as Error).message, error: true })
    }
  }

  async function autoAssign() {
    try {
      const r = await api.post<{ assigned: number; devices_moved: number; pops_created: string[] }>('/areas/auto-assign')
      const left = await api.get<{ unmapped_pops: string[] }>('/areas')
      setMessage({
        text:
          `${r.assigned} POP diberi area` +
          (r.devices_moved ? `, ${r.devices_moved} perangkat dipindah dari UNASSIGNED ke POP kotanya` : '') +
          (r.pops_created.length ? ` (POP baru: ${r.pops_created.join(', ')})` : '') +
          `. ${left.unmapped_pops.length} POP belum dipetakan` +
          (left.unmapped_pops.length ? ` (isi kolom Area di Inventory → Metro POP): ${left.unmapped_pops.slice(0, 15).join(', ')}${left.unmapped_pops.length > 15 ? ', …' : ''}` : ''),
      })
      await load()
    } catch (e) {
      setMessage({ text: (e as Error).message, error: true })
    }
  }

  async function discover() {
    setBusy(true)
    try {
      const r = await api.post<DiscoveryResult>('/topology/discover')
      setMessage({ text: `Discovery: ${r.lldp_links} link LLDP, ${r.config_links} dari config · ${r.created} baru, ${r.marked_down} down` })
      await load()
    } catch (e) {
      setMessage({ text: (e as Error).message, error: true })
    } finally {
      setBusy(false)
    }
  }

  const stats = useMemo(() => {
    const edges = view?.edges ?? []
    return {
      groups: edges.length,
      links: edges.reduce((s, e) => s + e.links, 0),
      red: edges.filter((e) => e.color === 'red').length,
      yellow: edges.filter((e) => e.color === 'yellow').length,
      green: edges.filter((e) => e.color === 'green').length,
    }
  }, [view])

  const monitorGroups = useMemo(() => monitorItems(view), [view])
  const monitorTotals = useMemo(() => monitorGroups.reduce((total, item) => ({
    links: total.links + item.links,
    down: total.down + item.down,
    capacity: total.capacity + item.capacity_gbps,
    committed: total.committed + item.current_gbps + item.reserved_gbps,
    riskGroups: total.riskGroups + (item.down > 0 || item.color !== 'green' ? 1 : 0),
  }), { links: 0, down: 0, capacity: 0, committed: 0, riskGroups: 0 }), [monitorGroups])
  const committedPct = monitorTotals.capacity ? monitorTotals.committed / monitorTotals.capacity * 100 : 0

  const node = selected?.node
  const edge = selected?.edge
  const nameOf = (id: string) => view?.nodes.find((n) => n.id === id)?.label ?? id
  const viewArea = view?.area ?? area
  const viewPop = view?.pop ?? pop

  return (
    <div className="topology">
      <div className="toolbar card">
        <nav className="crumbs">
          <a href="#" onClick={(e) => { e.preventDefault(); go({ level: 'area' }) }} className={level === 'area' ? 'current' : ''}>
            Semua area
          </a>
          {level !== 'area' && viewArea && (
            <>
              <span>›</span>
              <a href="#" onClick={(e) => { e.preventDefault(); go({ level: 'pop', area: viewArea }) }} className={level === 'pop' ? 'current' : ''}>
                {viewArea}
              </a>
            </>
          )}
          {(level === 'device' || level === 'neighbors') && viewPop && (
            <>
              <span>›</span>
              <a href="#" onClick={(e) => { e.preventDefault(); go({ level: 'device', pop: viewPop }) }} className={level === 'device' ? 'current' : ''}>
                {viewPop}{level === 'device' && view?.pop_name && view.pop_name !== viewPop ? ` · ${view.pop_name}` : ''}
              </a>
            </>
          )}
          {level === 'neighbors' && view?.device && (
            <>
              <span>›</span>
              <span className="current">{view.device}</span>
            </>
          )}
        </nav>
        <TopologySearchBox onPick={openHit} />
        {level === 'neighbors' && (
          <div className="segmented">
            {[1, 2].map((d) => (
              <button key={d} className={depth === String(d) ? 'active' : ''} onClick={() => go({ level: 'neighbors', device_id: Number(deviceId), depth: d })}>
                {d} hop
              </button>
            ))}
          </div>
        )}
        <select value={colorFilter} onChange={(e) => setColorFilter(e.target.value as 'all' | 'warn' | 'red')}>
          <option value="all">Semua link</option>
          <option value="warn">Kuning + Merah</option>
          <option value="red">Merah saja</option>
        </select>
        <label className="check">
          <input type="checkbox" checked={edgeLabels} onChange={(e) => setEdgeLabels(e.target.checked)} /> Label link
        </label>
        {mapAvailable && (
          <label className="check" title="Tampilkan di atas peta sesuai koordinat (POP, atau site perangkat bila ada)">
            <input type="checkbox" checked={mapOn} onChange={(e) => toggleMap(e.target.checked)} /> Peta
          </label>
        )}
        <div className="spacer" />
        {level === 'area' && view?.nodes.some((n) => n.label === 'Belum dipetakan') && (
          <button onClick={autoAssign} title="Isi area POP yang masih kosong dari kode kota (tidak menimpa yang sudah diisi)">
            Isi area otomatis
          </button>
        )}
        {level === 'device' && !onMap && (
          <button onClick={savePositions} disabled={!dirty}>
            Simpan posisi
          </button>
        )}
        <button
          className="primary"
          onClick={discover}
          disabled={busy || !!syncPhase}
          title={syncPhase ? `Sync sedang berjalan (${syncPhase}); topologi dibangun otomatis di akhir sync` : undefined}
        >
          {busy ? 'Discovering…' : syncPhase ? 'Sync berjalan…' : 'Run discovery'}
        </button>
      </div>

      {message && (
        <div className={message.error ? 'error' : 'notice'} onClick={() => setMessage(null)}>
          {message.text}
        </div>
      )}
      {view?.truncated && <div className="notice">Terlalu banyak tetangga — ditampilkan 400 perangkat pertama.</div>}

      {view && <section className="monitor-summary card" aria-label="Ringkasan monitoring">
        <div className="monitor-heading">
          <div>
            <h2>Monitoring · {level === 'area' ? 'Semua area' : level === 'pop' ? view.area : level === 'device' ? `POP ${view.pop}` : view.device}</h2>
            <div className="muted small">Ringkasan kapasitas dan status link pada cakupan tampilan ini</div>
          </div>
          <span className={`monitor-util ${committedPct >= 80 ? 'critical' : committedPct >= 60 ? 'warning' : ''}`}>
            {committedPct.toFixed(1)}% committed
          </span>
        </div>
        <div className="monitor-kpis">
          <div><span>Link</span><b>{monitorTotals.links.toLocaleString('id-ID')}</b></div>
          <div className={monitorTotals.down ? 'critical' : ''}><span>Link down</span><b>{monitorTotals.down.toLocaleString('id-ID')}</b></div>
          <div className={monitorTotals.riskGroups ? 'warning' : ''}><span>Kelompok perlu perhatian</span><b>{monitorTotals.riskGroups.toLocaleString('id-ID')}</b></div>
          <div><span>Kapasitas / trafik + reserved</span><b>{fmtG(monitorTotals.capacity)} / {fmtG(monitorTotals.committed)}</b></div>
        </div>
        <div className="monitor-alerts">
          <b>Prioritas monitoring</b>
          {monitorGroups.filter((item) => item.down > 0 || item.color !== 'green').slice(0, 5).map((item) => (
            <button key={item.id} type="button" className="monitor-alert" onClick={() => {
              if (item.edge) {
                setSelected({ edge: item.edge })
                cyRef.current?.getElementById(item.edge.id).select()
              } else if (item.node?.kind === 'area') go({ level: 'pop', area: item.node.label })
              else if (item.node?.kind === 'pop') go({ level: 'device', pop: item.node.label })
            }}>
              <span className={`monitor-dot ${item.down ? 'down' : item.color}`} />
              <span className="monitor-alert-name">{item.label}</span>
              {item.down > 0 && <span className="pill pill-bad">{item.down} down</span>}
              <span className={`monitor-alert-pct ${item.color}`}>{item.committed_pct.toFixed(0)}%</span>
            </button>
          ))}
          {!monitorTotals.riskGroups && <span className="muted small">Tidak ada link down atau kelompok link berwarna kuning/merah.</span>}
          {monitorTotals.riskGroups > 5 && <span className="muted small">Menampilkan 5 dari {monitorTotals.riskGroups} kelompok prioritas.</span>}
        </div>
      </section>}

      <div className="topology-body">
        <div className="graph card">
          {onMap && (
            <svg className="map-layer" aria-hidden="true">
              <g ref={mapLayer}>
                {basemap.map((shape, i) => (
                  // Natural Earth reuses some ids (Australia's territories): the index keeps keys unique.
                  <path key={`${shape.id}-${i}`} d={shape.d} className={shape.indonesia ? 'land indonesia' : 'land'} />
                ))}
              </g>
            </svg>
          )}
          <div className="graph-canvas" ref={container} />
          {loading && <div className="graph-loading">Memuat…</div>}
          <div className="graph-status small muted">
            {stats.groups} jalur · {stats.links} link
            {stats.red ? ` · ${stats.red} merah` : ''}
            {onMap && level === 'pop' && view ? ` · ${view.nodes.filter((n) => n.estimated).length} POP tanpa koordinat (posisi perkiraan, garis putus-putus)` : ''}
            {onMap && (level === 'device' || level === 'neighbors') && view
              ? ` · ${devicesLocated} perangkat dengan koordinat site, ${view.nodes.filter((n) => n.kind === 'device' && !n.located).length} di sekitar POP (garis putus-putus)`
              : ''}
          </div>
          {(node || edge || (level === 'device' && view?.pop_id != null)) && (
            <aside className="detail-panel card">
              {(node || edge) && (
                <button className="panel-close" onClick={() => setSelected(null)} aria-label="Tutup">×</button>
              )}
          {level === 'device' && view?.pop_id != null && !node && !edge && (
            <div className="detail">
              <h3>POP {view.pop}</h3>
              <div className="small muted">Area: {view.area}</div>
              <PopAreaEditor popId={view.pop_id} region={view.region} onSaved={(a) => areaSaved(view.pop ?? '', a)} />
            </div>
          )}
          {node && (
            <div className="detail">
              <h3>{node.label}</h3>
              <dl>
                {node.kind === 'area' && (
                  <>
                    <dt>POP</dt><dd>{node.pops}</dd>
                    <dt>Perangkat</dt><dd>{node.devices}</dd>
                  </>
                )}
                {node.kind === 'pop' && (
                  <>
                    <dt>Nama</dt><dd>{node.name}</dd>
                    <dt>Area</dt><dd>{node.area}{node.region ? '' : ' (otomatis)'}</dd>
                    <dt>Perangkat</dt><dd>{node.devices} di {node.sites} site</dd>
                    <dt>Kategori</dt>
                    <dd>{Object.entries(node.categories ?? {}).map(([c, n]) => `${CATEGORY_LABEL[c] ?? c} ${n}`).join(' · ')}</dd>
                  </>
                )}
                {node.kind === 'device' && (
                  <>
                    <dt>POP · Site</dt><dd>{node.pop} · {node.site?.replace('site:', '')}</dd>
                    <dt>Role</dt><dd>{node.role?.toUpperCase()} ({TIER_LABEL[node.tier ?? 4]})</dd>
                    <dt>Vendor</dt><dd>{node.vendor ?? '–'}</dd>
                    <dt>Status</dt><dd>{node.status}</dd>
                  </>
                )}
                {(node.kind === 'external_area' || node.kind === 'external_pop') && (
                  <>
                    <dt>Area</dt><dd>{node.area ?? node.label}</dd>
                    {node.devices != null && (<><dt>Perangkat terhubung</dt><dd>{node.devices}</dd></>)}
                  </>
                )}
              </dl>
              {node.kind === 'pop' && node.pop_id != null && (
                <PopAreaEditor popId={node.pop_id} region={node.region} onSaved={(a) => areaSaved(node.label, a)} />
              )}
              {node.internal && node.internal.links > 0 && (
                <p className="small muted">
                  Link internal: {node.internal.links} · {fmtG(node.internal.capacity_gbps)} · {node.internal.committed_pct}% committed
                  {node.internal.down ? ` · ${node.internal.down} down` : ''}
                </p>
              )}
              <div className="detail-actions">
                {(node.kind === 'area' || node.kind === 'external_area') && (
                  <button onClick={() => go({ level: 'pop', area: node.label })}>Buka POP di area ini →</button>
                )}
                {(node.kind === 'pop' || node.kind === 'external_pop') && (
                  <button onClick={() => go({ level: 'device', pop: node.label })}>Buka perangkat di {node.label} →</button>
                )}
                {node.kind === 'device' && node.device_id != null && (
                  <>
                    <button onClick={() => go({ level: 'neighbors', device_id: node.device_id, depth: 1 })}>Lihat tetangga perangkat →</button>
                    <RouterLink to={`/ports?q=${encodeURIComponent(node.label)}`}>Lihat port perangkat →</RouterLink>
                  </>
                )}
              </div>
            </div>
          )}
          {edge && (
            <div className="detail">
              <h3>{nameOf(edge.source)} ↔ {nameOf(edge.target)}</h3>
              <dl>
                <dt>Link</dt><dd>{edge.links}{edge.down ? ` (${edge.down} down)` : ''}</dd>
                <dt>Kapasitas</dt><dd>{fmtG(edge.capacity_gbps)}</dd>
                <dt>Trafik</dt><dd>{fmtG(edge.current_gbps)} ({edge.utilization_pct}%)</dd>
                <dt>Committed</dt><dd>{edge.committed_pct}%</dd>
              </dl>
              {(level === 'device' || level === 'neighbors') && <RouterLink to="/capacity">Detail kapasitas & forecast →</RouterLink>}
            </div>
          )}
            </aside>
          )}
        </div>
      </div>
    </div>
  )
}
