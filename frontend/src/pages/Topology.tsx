import cytoscape, { Core, ElementDefinition } from 'cytoscape'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link as RouterLink, useSearchParams } from 'react-router-dom'
import { api, CapacityColor, DiscoveryResult } from '../api'
import { BasemapShape, Box, declutter, loadBasemap, project } from '../components/basemap'
import { NamingStandard, TopologyChanges } from '../components/TopologyInsights'
import TopologyLegend from '../components/TopologyLegend'
import { PopAreaEditor, SearchHit, TopologySearchBox } from '../components/TopologyTools'
import TopologyDetail, { CityFilterBar } from '../components/TopologyDetail'
import { fmtG } from '../components/ui'
import { buildCityElements, canUpdateInPlace, citySizeOf, linkColor, liveData, type NodeHealth } from '../components/topology/cityElements'
import { isCompound, isProblemLink, isProblemNode, linkVisible, NO_FILTER, nodeVisible, vendorsIn, type CityFilter } from '../components/topology/cityFilters'
import { CITY_STYLE } from '../components/topology/cityStyle'
import { layoutCity, LayoutCancelled } from '../components/topology/elkLayout'
import { edgeWidth, ROLE_COLOR, SEGMENT_COLOR, shortName } from '../components/topology/shared'
import type { CityLink, CityView } from '../components/topology/types'

// Drill-down topology:
//   area      — areas west→east as a schematic, trunks between them
//   province  — provinces of one area (other areas they link to drawn dashed)
//   regency   — kabupaten/kota of one province (other provinces / areas drawn dashed)
//   pop       — POPs of one area, province or regency in rows by their highest role, other groups on top
//   device    — devices of one POP in role rows (Core → PE/BNG → Metro → OLT/Access), other POPs on top
//   city      — one kabupaten/kota: POP swimlanes, ring/mesh clusters and every link, laid out by ELK
//   neighbors — one device and everything within 1–2 hops, laid out by ELK like the city
type Level = 'area' | 'province' | 'regency' | 'city' | 'pop' | 'device' | 'neighbors'
type GroupKind = 'area' | 'province' | 'regency'
type NodeKind = GroupKind | 'pop' | 'device' | 'external_area' | 'external_province' | 'external_regency' | 'external_pop'
  | 'summary' | 'pop_lane' | 'cluster' | 'city_box'

const GROUP_KINDS: string[] = ['area', 'province', 'regency']
const isGroup = (kind: string) => GROUP_KINDS.includes(kind)
const isGroupLevel = (level: Level) => level === 'area' || level === 'province' || level === 'regency'
/** Levels drawn from per-link entries with an ELK layout (positions are computed, never dragged or saved). */
const isElkLevel = (level: Level) => level === 'city' || level === 'neighbors'
const usesElk = (level: Level, onMap: boolean) => isElkLevel(level) && !onMap

export type Segment = 'backbone' | 'distribution' | 'last_mile'
export const SEGMENTS: Segment[] = ['backbone', 'distribution', 'last_mile']
export const SEGMENT_LABEL: Record<Segment, string> = { backbone: 'Backbone', distribution: 'Distribusi', last_mile: 'Last Mile' }
const SEGMENT_HINT: Record<Segment, string> = {
  backbone: 'P/DCI/PE/BNG ↔ P/DCI/PE/BNG',
  distribution: 'Metro/Agg ↔ Agg/PE/BNG/P',
  last_mile: 'ke OLT, CSR, atau switch akses',
}
// Line weight per function: the backbone reads first, last mile recedes.
const SEGMENT_WEIGHT: Record<Segment, number> = { backbone: 1.25, distribution: 1, last_mile: 0.7 }

interface Totals {
  links: number
  down: number
  capacity_gbps: number
  current_gbps: number
  utilization_pct: number
  committed_pct: number
  color: CapacityColor
  capacity_unknown: number
}

interface EdgeData extends Totals {
  id: string
  source: string
  target: string
  segment: Segment
}

interface NodeData {
  id: string
  kind: NodeKind
  label: string  // POPs: the city they serve; `code` is the POP code
  code?: string
  name?: string
  area?: string
  province?: string
  regency?: string
  city?: string | null
  pop?: string
  pops?: number
  devices?: number
  sites?: number
  links?: number
  categories?: Record<string, number>
  internal?: Totals & { reserved_gbps: number }
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
  // city / neighbours levels
  mgmt_ip?: string | null
  reachability?: string
  spof?: boolean
}

interface View {
  level: Level
  area?: string
  province?: string
  regency?: string
  city?: string | null
  pop?: string
  pop_name?: string
  pop_id?: number
  region?: string | null
  device?: string
  device_id?: number
  depth?: number
  truncated?: boolean
  layout_key?: string  // which saved arrangement this view uses
  layout?: Record<string, Pos>  // node id -> position arranged by hand and saved
  nodes: NodeData[]
  edges: EdgeData[]
  segments: Record<Segment, Totals>
  links?: CityLink[]  // city / neighbours: one entry per link record (the full shape is CityView)
}

// The city / neighbours responses are CityView; View describes every level loosely, so the two node unions differ.
const asCityView = (view: View): CityView => view as unknown as CityView

type Pos = { x: number; y: number }

const MAP_KEY = 'nadien.topology.map'

function storedMapOn(): boolean {
  try {
    return localStorage.getItem(MAP_KEY) !== '0'  // map on by default
  } catch {
    return true
  }
}

const TIER_LABEL = ['Core (P/DCI)', 'PE / BNG', 'Metro / Aggregation', 'CSR / OLT / Access', 'Lainnya']
const CATEGORY_LABEL: Record<string, string> = {
  router: 'Router', metro: 'Metro', switch: 'Switch', csr: 'CSR', olt: 'OLT', access: 'Access', other: 'Lainnya',
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
      // Labels disappear when zoomed out too far to read them, instead of piling up.
      'min-zoomed-font-size': 7,
    },
  },
  {
    // Tier band: a labelled box around one row of the schematic (Core, PE/BNG, Metro, Akses).
    selector: 'node[kind="band"]',
    style: {
      shape: 'round-rectangle',
      'background-color': 'data(bandColor)',
      'background-opacity': 0.06,
      'border-color': 'data(bandColor)',
      'border-opacity': 0.35,
      'border-width': 1,
      'border-style': 'dashed',
      label: 'data(display)',
      'text-valign': 'top',
      'text-halign': 'center',
      'text-margin-y': -4,
      'font-size': 12,
      'font-weight': 'bold',
      color: 'data(bandColor)',
      'text-transform': 'uppercase',
      padding: '22px',
      'min-zoomed-font-size': 5,
      events: 'no',
    },
  },
  {
    selector: 'node[kind="area"], node[kind="province"], node[kind="regency"]',
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
    selector: 'node[kind="external_area"], node[kind="external_province"], node[kind="external_regency"], node[kind="external_pop"]',
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
      'line-style': 'data(lineStyle)',
      'min-zoomed-font-size': 7,
      opacity: 0.75,
    },
  },
  { selector: 'edge.labeled, edge:selected', style: { label: 'data(display)', opacity: 1 } },
  { selector: 'edge[allDown = 1]', style: { 'line-style': 'dashed', 'line-color': '#64748b' } },
  // Live updates: what appeared since the page last loaded, and links that just went down.
  { selector: 'node.fresh', style: { 'border-color': '#4ade80', 'border-width': 5, 'underlay-color': '#4ade80', 'underlay-opacity': 0.35, 'underlay-padding': 8 } },
  { selector: 'edge.fresh', style: { 'underlay-color': '#4ade80', 'underlay-opacity': 0.45, 'underlay-padding': 5, opacity: 1 } },
  { selector: 'edge.went-down', style: { 'underlay-color': '#ef4444', 'underlay-opacity': 0.55, 'underlay-padding': 6, opacity: 1 } },
  { selector: '.faded', style: { opacity: 0.08 } },
  { selector: '.hit', style: { 'border-color': '#facc15', 'border-width': 4 } },
  { selector: 'node:selected', style: { 'overlay-color': '#38bdf8', 'overlay-opacity': 0.25, 'overlay-padding': 6 } },
] as unknown as cytoscape.CytoscapeOptions['style']

const TIER_COLOR = ['#818cf8', '#38bdf8', '#2dd4bf', '#a3e635', '#94a3b8']

function nodeDisplay(n: NodeData): string {
  switch (n.kind) {
    case 'area':
    case 'province':
    case 'regency':
      return `${n.label}\n${n.pops} POP · ${n.devices} perangkat`
    case 'external_area':
    case 'external_province':
    case 'external_regency':
    case 'external_pop':
      return `→ ${n.label}`
    default:
      return n.label
  }
}

interface Target {
  level: Level
  area?: string
  province?: string
  regency?: string
  pop?: string
  device_id?: number
  depth?: number
  focus?: string
}

const NO_POP = '?'

/** What opening a node shows: one level down in Area > Provinsi > Kab/Kota > POP > Perangkat. */
function drillTarget(n: NodeData): Target | null {
  switch (n.kind) {
    case 'area':
    case 'external_area':
      return { level: 'province', area: n.area ?? n.label }
    case 'province':
    case 'external_province':
      return { level: 'regency', area: n.area, province: n.province }
    case 'regency':
    case 'external_regency':
      // The city schematic is the default view of a kabupaten/kota; its POP list stays one breadcrumb away.
      return { level: 'city', area: n.area, province: n.province, regency: n.regency }
    case 'pop':
    case 'pop_lane':
    case 'external_pop': {
      // "?" is the placeholder for devices without a POP, not a POP that can be opened.
      const code = n.code ?? n.label
      return code === NO_POP ? null : { level: 'device', pop: code }
    }
    case 'summary':
      // Collapsed access devices of one POP: that POP's own device level shows them all.
      return n.pop && n.pop !== NO_POP ? { level: 'device', pop: n.pop } : null
    case 'device':
      return n.device_id != null ? { level: 'neighbors', device_id: n.device_id, depth: 1 } : null
    case 'cluster':
    case 'city_box':
      return null
  }
}

const DRILL_LABEL: Record<NodeKind, string> = {
  area: 'Buka provinsi di area ini', external_area: 'Buka provinsi di area ini',
  province: 'Buka kabupaten/kota', external_province: 'Buka kabupaten/kota',
  regency: 'Buka topologi kota', external_regency: 'Buka topologi kota',
  pop: 'Buka perangkat', external_pop: 'Buka perangkat', device: 'Lihat tetangga perangkat',
  pop_lane: 'Buka perangkat POP ini', summary: 'Buka semua perangkat akses POP ini', cluster: '', city_box: '',
}

const GROUP_LABEL: Record<string, string> = {
  area: 'Area', province: 'Provinsi', regency: 'Kab/Kota', external_area: 'Area', external_province: 'Provinsi',
  external_regency: 'Kab/Kota', external_pop: 'POP',
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

/** Provinces or regencies west→east in a few rows, the groups outside this one on a row above. */
function groupLayout(view: View): Record<string, Pos> {
  const pos: Record<string, Pos> = {}
  const byLon = (a: NodeData, b: NodeData) => (a.lon ?? 999) - (b.lon ?? 999)
  const inside = view.nodes.filter((n) => isGroup(n.kind)).sort(byLon)
  const outside = view.nodes.filter((n) => !isGroup(n.kind)).sort(byLon)
  const dx = 250
  const dy = 190
  const perLine = Math.max(4, Math.ceil(Math.sqrt(inside.length * 2)))
  const width = Math.min(perLine, Math.max(inside.length, outside.length, 1)) * dx
  const place = (row: NodeData[], top: number) =>
    row.forEach((n, i) => {
      const line = Math.floor(i / perLine)
      const inLine = Math.min(perLine, row.length - line * perLine)
      pos[n.id] = { x: (width - inLine * dx) / 2 + (i % perLine) * dx, y: top + line * dy }
    })
  place(outside, 0)
  place(inside, outside.length ? Math.ceil(outside.length / perLine) * 120 + 80 : 0)
  return pos
}

/**
 * Role rows with crossing reduction: each row is ordered by the average position of its
 * neighbours in the rows above/below (barycentre heuristic, a few sweeps), then long rows wrap.
 * Other POPs (external) form the top row.
 */
function layeredLayout(view: View, rowGap = 70): Record<string, Pos> {
  // Devices/POPs by role tier; other POPs/areas (external) form the top "uplink" row.
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
    y += Math.ceil(row.length / perLine) * 85 + rowGap
  }
  return pos
}

export type ColorMode = 'utilization' | 'segment'
export type LabelMode = 'full' | 'short'

interface BuildOptions {
  bands: boolean
  colorMode: ColorMode
  labelMode: LabelMode
}

const BAND_LABEL: Record<number, string> = {
  [-1]: 'Terhubung ke luar', 0: 'Core · P / DCI', 1: 'Edge · PE / BNG', 2: 'Distribusi · Metro / Agg', 3: 'Akses · OLT / CSR / Switch', 4: 'Lainnya',
}
const POP_BAND_LABEL: Record<number, string> = {
  [-1]: 'Wilayah lain', 0: 'POP core', 1: 'POP edge', 2: 'POP distribusi', 3: 'POP akses', 4: 'POP lainnya',
}

/** Row a node sits in on the schematic: other POPs/areas (external) on top, then by role tier. */
function rowOf(n: NodeData): number {
  return n.kind === 'device' || n.kind === 'pop' ? (n.tier ?? 4) : -1
}

// Same rule for aggregate edges and per-link edges (city / neighbours), which carry the same three fields.
const edgeColor = (e: Pick<EdgeData, 'capacity_gbps' | 'color' | 'segment'>, mode: ColorMode): string => linkColor(e, mode)

function edgeDisplay(e: EdgeData): string {
  const count = e.links > 1 ? `${e.links}× · ` : ''
  if (!(e.capacity_gbps > 0)) return `${count}kapasitas ?`
  return `${count}${fmtG(e.capacity_gbps)} · ${e.committed_pct.toFixed(0)}%`
}

function buildElements(view: View, options: BuildOptions): ElementDefinition[] {
  const maxDevices = Math.max(1, ...view.nodes.map((n) => n.devices ?? 0))
  const bands: ElementDefinition[] = []
  if (options.bands) {
    const labels = view.level === 'pop' ? POP_BAND_LABEL : BAND_LABEL
    for (const row of new Set(view.nodes.map(rowOf))) {
      bands.push({
        data: { id: `band:${row}`, kind: 'band', display: labels[row], bandColor: row < 0 ? '#94a3b8' : TIER_COLOR[row] },
        selectable: false,
        grabbable: false,
      })
    }
  }
  const nodes: ElementDefinition[] = view.nodes.map((n) => {
    const scale = Math.sqrt((n.devices ?? 0) / maxDevices)
    const display = n.kind === 'device' && options.labelMode === 'short' ? shortName(n.label, n.pop) : nodeDisplay(n)
    return {
      data: {
        ...n,
        ...(options.bands ? { parent: `band:${rowOf(n)}` } : {}),
        display,
        roleColor: ROLE_COLOR[n.role ?? ''] ?? '#64748b',
        tierColor: TIER_COLOR[n.tier ?? 4],
        w: isGroup(n.kind) ? 170 + 60 * scale : n.kind === 'pop' ? Math.max(60, n.label.length * 10 + 24) : Math.max(70, display.length * 7 + 26),
        h: 70 + 40 * scale,
      },
    }
  })
  const edges: ElementDefinition[] = view.edges.map((e) => ({
    data: {
      ...e,
      display: edgeDisplay(e),
      colorHex: edgeColor(e, options.colorMode),
      width: edgeWidth(e.capacity_gbps) * SEGMENT_WEIGHT[e.segment],
      lineStyle: e.segment === 'last_mile' ? 'dotted' : 'solid',
      allDown: e.down === e.links ? 1 : 0,
    },
  }))
  return [...bands, ...nodes, ...edges]
}

// Map layout: pixels per degree, the on-screen size of nodes relative to their normal size (they stay
// that size at any zoom, like map markers), and the on-screen gap kept between overlapping nodes.
const MAP_SCALE: Partial<Record<Level, { pxPerDegree: number; markerScale: number; minScreenGap: number }>> = {
  area: { pxPerDegree: 60, markerScale: 0.9, minScreenGap: 10 },
  province: { pxPerDegree: 100, markerScale: 0.9, minScreenGap: 10 },
  regency: { pxPerDegree: 160, markerScale: 0.9, minScreenGap: 8 },
  pop: { pxPerDegree: 200, markerScale: 0.9, minScreenGap: 6 },
  city: { pxPerDegree: 1500, markerScale: 1, minScreenGap: 4 },  // a whole kabupaten/kota: wider than one POP
  device: { pxPerDegree: 3000, markerScale: 1, minScreenGap: 4 },  // one city: ~30 px per km
  neighbors: { pxPerDegree: 1500, markerScale: 1, minScreenGap: 4 },
}

// Spacing never moves a node more than this many node heights from its place on the map.
const MAX_SHIFT_HEIGHTS = 3

/** Normal (zoom 1) size of a node, before the map's marker scale. */
function nodeSize(n: { kind: string; w?: number }): { width: number; height: number } {
  if (isGroup(n.kind)) return { width: 150, height: 56 }
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
        'font-size': (isGroup(kind) ? 13 : 12) * scale,
        'border-width': 2 * scale,
        'text-max-width': `${(isGroup(kind) ? 150 : 160) * scale}px`,
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
  if (view.level === 'device' || view.level === 'neighbors' || view.level === 'city') ringExternal(view, home)
  const elements = buildElements(view, { bands: false, colorMode: 'utilization', labelMode: 'full' })
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

/** Schematic views with tier bands: POPs and devices, not the area / province / regency overviews or the map. */
function usesBands(level: Level, onMap: boolean): boolean {
  return !onMap && !isGroupLevel(level)
}

/**
 * `keep`: positions of nodes from before a live refresh, so the drawing does not jump around; nodes that are new
 * get their place from the layout. Band nodes are never positioned: they wrap their rows.
 */
function runLayout(cy: Core, view: View, onMap: boolean, keep?: Map<string, Pos>) {
  if (onMap) return mapLayout(cy, view)
  // Saved arrangement of this view first (every node kind), then the device positions saved the old way.
  const arranged: Record<string, Pos> = { ...(view.layout ?? {}) }
  if (view.level === 'device') {
    for (const n of view.nodes) {
      if (n.kind === 'device' && n.x != null && n.y != null && !arranged[n.id]) arranged[n.id] = { x: n.x, y: n.y }
    }
  }
  const auto = view.level === 'area' ? areaLayout(view) : isGroupLevel(view.level) ? groupLayout(view) : layeredLayout(view, 120)
  cy.nodes().not('[kind="band"]').layout({
    name: 'preset',
    padding: isGroupLevel(view.level) ? 50 : 30,
    positions: (n: cytoscape.NodeSingular) => keep?.get(n.id()) ?? arranged[n.id()] ?? auto[n.id()] ?? { x: 0, y: 0 },
  } as cytoscape.LayoutOptions).run()
}

/** Every node the user can arrange: all but the role bands, which wrap their rows. */
function nodePositions(cy: Core): { id: string; pos: Pos }[] {
  return cy.nodes().not('[kind="band"]').nodes().map((n) => ({ id: n.id(), pos: { ...n.position() } }))
}

/** Nodes more than a pixel away from their saved place (a click without a drag must not count). */
function countMoved(cy: Core, saved: Map<string, Pos>): number {
  return nodePositions(cy).filter((d) => {
    const s = saved.get(d.id)
    return !s || Math.abs(s.x - d.pos.x) > 1 || Math.abs(s.y - d.pos.y) > 1
  }).length
}

function confirmDiscard(dirty: boolean): boolean {
  return !dirty || window.confirm('Ada perubahan tata letak topologi yang belum disimpan. Tinggalkan tanpa menyimpan?')
}

const LIVE_INTERVAL_MS = 30_000
const FRESH_FOR_MS = 10 * 60_000  // how long new elements and links that went down stay highlighted

function stored<T extends string>(key: string, fallback: T): T {
  try {
    return (localStorage.getItem(key) as T | null) ?? fallback
  } catch {
    return fallback
  }
}

function remember(key: string, value: string) {
  try {
    localStorage.setItem(key, value)
  } catch {
    /* storage blocked: the choice just isn't remembered */
  }
}

/** The area overview shows the trunks between regions; drilled-in views show every function. */
function defaultSegments(level: Level): Set<Segment> {
  return new Set(level === 'area' ? ['backbone', 'distribution'] : SEGMENTS)
}

/** Drawing state carried over a live refresh, so the map updates in place. */
interface LiveKeep {
  positions: Map<string, Pos>
  zoom: number
  pan: Pos
}

type Tab = 'map' | 'naming' | 'changes'
const TABS: { key: Tab; label: string }[] = [
  { key: 'map', label: 'Peta topologi' }, { key: 'naming', label: 'Standar penamaan & label' }, { key: 'changes', label: 'Log perubahan' },
]

export default function Topology() {
  const [params, setParams] = useSearchParams()
  const tab = (TABS.some((t) => t.key === params.get('tab')) ? params.get('tab') : 'map') as Tab
  // Leaving the map tab unmounts it: unsaved position changes would be lost silently.
  const mapDirtyRef = useRef(false)
  return (
    <div className="topology-page">
      <div className="config-menu topology-tabs" role="tablist" aria-label="Topologi">
        {TABS.map((t) => (
          <button key={t.key} type="button" role="tab" aria-selected={tab === t.key} className={tab === t.key ? 'active' : ''}
            onClick={() => {
              if (t.key === tab || !confirmDiscard(tab === 'map' && mapDirtyRef.current)) return
              mapDirtyRef.current = false
              setParams(t.key === 'map' ? {} : { tab: t.key })
            }}>{t.label}</button>
        ))}
      </div>
      {tab === 'map' && <TopologyMap dirtyRef={mapDirtyRef} />}
      {tab === 'naming' && <NamingStandard />}
      {tab === 'changes' && <TopologyChanges />}
    </div>
  )
}

function TopologyMap({ dirtyRef }: { dirtyRef: React.MutableRefObject<boolean> }) {
  const [params, setParams] = useSearchParams()
  const level = (params.get('level') as Level) || 'area'
  const area = params.get('area') ?? undefined
  const province = params.get('province') ?? undefined
  const regency = params.get('regency') ?? undefined
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
  // The canvas is rebuilt from the latest data (drawnRef) but only when drawRevision changes. A live refresh of the
  // city / neighbours levels with an unchanged structure updates drawnRef without a new revision, so the ELK layout
  // is kept, and any later rebuild (label mode, map toggle, focus) still draws the current statuses.
  const drawnRef = useRef<View | null>(null)
  const [drawRevision, setDrawRevision] = useState(0)
  const show = useCallback((next: View | null) => {
    drawnRef.current = next
    setView(next)
    setDrawRevision((revision) => revision + 1)
  }, [])
  const [arranging, setArranging] = useState(false)
  const [layoutMs, setLayoutMs] = useState<number | null>(null)
  // Bumped once ELK elements are on the canvas, so filters and colours (applied per element) run again for them.
  const [layoutTick, setLayoutTick] = useState(0)
  const elkReadyRef = useRef(false)
  // Devices go on the map only where at least one has its own site coordinates.
  const devicesLocated = view?.nodes.filter((n) => n.kind === 'device' && n.located).length ?? 0
  const mapAvailable = isGroupLevel(level) || level === 'pop' || devicesLocated > 0
  const onMap = mapOn && mapAvailable && view?.level === level
  // City / neighbours drawn by ELK: the only drawing that has the filters, problem highlight and the per-link panel.
  const cityMode = !!view && usesElk(view.level, onMap)
  const onMapRef = useRef(onMap)
  onMapRef.current = onMap
  const [loading, setLoading] = useState(false)
  // City / neighbours keep only the id of the tapped link: the panel reads its details from the current view.
  const [selected, setSelected] = useState<{ node?: NodeData; edge?: EdgeData; linkId?: number } | null>(null)
  const [message, setMessage] = useState<{ text: string; error?: boolean } | null>(null)
  const [colorFilter, setColorFilter] = useState<'all' | 'warn' | 'red'>('all')
  const [edgeLabels, setEdgeLabels] = useState(false)
  const [syncPhase, setSyncPhase] = useState<string | null>(null)
  // Device id -> position as last saved (or as first laid out); moved = devices that differ from it.
  const savedPosRef = useRef(new Map<string, Pos>())
  const [moved, setMoved] = useState(0)
  const [saving, setSaving] = useState(false)
  const dirty = moved > 0
  dirtyRef.current = dirty
  const [busy, setBusy] = useState(false)
  const [segmentsShown, setSegmentsShown] = useState<Set<Segment>>(() => defaultSegments(level))
  const [colorMode, setColorMode] = useState<ColorMode>(() => stored('nadien.topology.color', 'utilization'))
  const [labelMode, setLabelMode] = useState<LabelMode>(() => stored('nadien.topology.labels', 'short'))
  const [legendOpen, setLegendOpen] = useState(() => stored('nadien.topology.legend', '1') === '1')
  const [live, setLive] = useState(() => stored('nadien.topology.live', '1') === '1')
  const [cityFilter, setCityFilter] = useState<CityFilter>(NO_FILTER)
  const [highlightProblems, setHighlightProblems] = useState(() => stored<string>('nadien.topology.highlight-problems', '0') === '1')
  const [highlightSpof, setHighlightSpof] = useState(() => stored<string>('nadien.topology.highlight-spof', '0') === '1')
  const [lastCheck, setLastCheck] = useState<Date | null>(null)
  const [liveNote, setLiveNote] = useState<string | null>(null)
  const viewRef = useRef<View | null>(null)
  const keepRef = useRef<LiveKeep | null>(null)
  const signatureRef = useRef<string | null>(null)
  // Element id -> until when it stays highlighted (new since the page loaded / link that just went down).
  const freshRef = useRef(new Map<string, number>())
  const wentDownRef = useRef(new Map<string, number>())
  const drawRef = useRef({ colorMode, labelMode })
  drawRef.current = { colorMode, labelMode }
  viewRef.current = view

  const go = useCallback(
    (next: Target, replace = false) => {
      if (!replace && !confirmDiscard(dirtyRef.current)) return
      const p: Record<string, string> = { level: next.level }
      if (next.area) p.area = next.area
      if (next.province) p.province = next.province
      if (next.regency) p.regency = next.regency
      if (next.pop) p.pop = next.pop
      if (next.device_id != null) p.device_id = String(next.device_id)
      if (next.depth) p.depth = String(next.depth)
      if (next.focus) p.focus = next.focus
      setParams(p, { replace })
    },
    [setParams],
  )

  const viewPath = useMemo(() => {
    const q = new URLSearchParams({ level })
    if (area) q.set('area', area)
    if (province) q.set('province', province)
    if (regency) q.set('regency', regency)
    if (pop) q.set('pop', pop)
    if (deviceId) q.set('device_id', deviceId)
    if (level === 'neighbors') q.set('depth', depth)
    return `/topology/view?${q}`
  }, [level, area, province, regency, pop, deviceId, depth])

  // A province/regency holding a single group comes back one level deeper: the URL follows it, and the
  // view already fetched is used for that new URL instead of being fetched again.
  const deeperRef = useRef<View | null>(null)
  const load = useCallback(async () => {
    setLoading(true)
    setSelected(null)
    setMoved(0)
    try {
      const ready = deeperRef.current
      deeperRef.current = null
      if (ready && ready.level === level && ready.area === area && ready.province === province && ready.regency === regency) {
        show(ready)
        return
      }
      const next = await api.get<View>(viewPath)
      if (next.level !== level) {
        deeperRef.current = next
        go({ level: next.level, area: next.area, province: next.province, regency: next.regency }, true)
        return
      }
      show(next)
    } catch (e) {
      setMessage({ text: (e as Error).message, error: true })
      show(null)
    } finally {
      setLoading(false)
    }
  }, [viewPath, level, area, province, regency, go, show])

  useEffect(() => {
    load()
  }, [load])

  useEffect(() => {
    setSegmentsShown(defaultSegments(level))
  }, [level])

  /** Live highlight classes: new since the page loaded, and links that just went down, until they expire. */
  const markLive = useCallback((cy: Core) => {
    const now = Date.now()
    cy.elements().forEach((el) => {
      el.toggleClass('fresh', (freshRef.current.get(el.id()) ?? 0) > now)
      el.toggleClass('went-down', (wentDownRef.current.get(el.id()) ?? 0) > now)
    })
  }, [])

  /** Live refresh: same view again, drawn in place, with what changed marked on the map. */
  const refreshLive = useCallback(async () => {
    const before = viewRef.current
    const next = await api.get<View>(viewPath)
    const cy = cyRef.current
    // City / neighbours with the very same nodes and links: only status, colours and labels can have changed,
    // so the ELK layout (seconds on a large city) is kept and the element data is updated in place.
    const inPlace = !!cy && canUpdateInPlace(before && asCityView(before), asCityView(next),
      { elkReady: elkReadyRef.current, onMap: onMapRef.current })
    if (cy && before && !inPlace) {
      keepRef.current = {
        positions: new Map(cy.nodes().not('[kind="band"]').nodes().map((n) => [n.id(), { ...n.position() }] as [string, Pos])),
        zoom: cy.zoom(),
        pan: { ...cy.pan() },
      }
    }
    if (before) {
      const until = Date.now() + FRESH_FOR_MS
      const oldNodes = new Set(before.nodes.map((n) => n.id))
      const oldDown = new Map(before.edges.map((e) => [e.id, e.down]))
      const addedNodes = next.nodes.filter((n) => !oldNodes.has(n.id))
      const addedEdges = next.edges.filter((e) => !oldDown.has(e.id))
      const wentDown = next.edges.filter((e) => oldDown.has(e.id) && e.down > (oldDown.get(e.id) ?? 0))
      const recovered = next.edges.filter((e) => oldDown.has(e.id) && e.down < (oldDown.get(e.id) ?? 0))
      addedNodes.forEach((n) => freshRef.current.set(n.id, until))
      addedEdges.forEach((e) => freshRef.current.set(e.id, until))
      wentDown.forEach((e) => wentDownRef.current.set(e.id, until))
      // City / neighbours draw one edge per link record (link:<id>), not the aggregate edges above.
      const oldLinkStatus = new Map((before.links ?? []).map((l) => [l.id, l.status]))
      for (const l of next.links ?? []) {
        if (!oldLinkStatus.has(l.id)) freshRef.current.set(`link:${l.id}`, until)
        else if (l.status === 'down' && oldLinkStatus.get(l.id) !== 'down') wentDownRef.current.set(`link:${l.id}`, until)
      }
      const parts = [
        addedNodes.length && `${addedNodes.length} node baru`,
        addedEdges.length && `${addedEdges.length} jalur baru`,
        wentDown.length && `${wentDown.length} jalur dengan link down baru`,
        recovered.length && `${recovered.length} jalur pulih`,
      ].filter(Boolean)
      const time = new Date().toLocaleTimeString('id-ID', { hour: '2-digit', minute: '2-digit' })
      setLiveNote(`Diperbarui otomatis ${time}: ${parts.length ? parts.join(' · ') : 'status/trafik berubah'}`)
    }
    if (inPlace && cy) {
      cy.batch(() => {
        for (const definition of buildCityElements(asCityView(next), null, drawRef.current)) {
          const el = cy.getElementById(definition.data.id as string)
          if (el.nonempty()) el.data(liveData(definition))
        }
      })
      markLive(cy)
      // No new revision: the drawing stays, but a later rebuild must start from these statuses, not the old ones.
      drawnRef.current = next
      setView(next)
    } else {
      show(next)
    }
  }, [viewPath, show, markLive])

  // Live: poll a cheap signature of the network; reload the view in place only when it changed.
  useEffect(() => {
    signatureRef.current = null
    if (!live) return
    let alive = true
    const tick = async () => {
      if (document.hidden) return
      try {
        const { signature } = await api.get<{ signature: string }>('/topology/signature')
        if (!alive) return
        setLastCheck(new Date())
        if (signatureRef.current && signature !== signatureRef.current) await refreshLive()
        signatureRef.current = signature
      } catch {
        /* a failed poll just waits for the next one */
      }
    }
    tick()
    const timer = setInterval(tick, LIVE_INTERVAL_MS)
    return () => {
      alive = false
      clearInterval(timer)
    }
  }, [live, refreshLive])

  // Edge labels on by default only where there are few edges (area level).
  useEffect(() => {
    setEdgeLabels(isGroupLevel(level))
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
    if (!confirmDiscard(dirty)) return
    setMoved(0)
    setMapOn(on)
    try {
      localStorage.setItem(MAP_KEY, on ? '1' : '0')
    } catch {
      /* storage blocked: the choice just isn't remembered */
    }
  }

  // Label mode sets the ELK node widths, so on the city / neighbours levels a change of it means a new layout.
  const elkLabelMode = view && usesElk(view.level, onMap) ? labelMode : null

  useEffect(() => {
    const view = drawnRef.current
    if (!view || !container.current) return
    const elk = usesElk(view.level, onMap)
    const keep = keepRef.current
    keepRef.current = null
    elkReadyRef.current = false
    const cy = cytoscape({
      container: container.current,
      // ELK elements are added once the worker has laid them out; added earlier they would flash at (0,0).
      elements: elk ? [] : buildElements(view, { bands: usesBands(view.level, onMap), ...drawRef.current }),
      style: elk ? CITY_STYLE : STYLE,
      wheelSensitivity: 0.25,
      minZoom: 0.05,
      maxZoom: 3,
    })
    const highlight = (id: string | null) => {
      cy.elements().removeClass('faded')
      if (!id) return
      const el = cy.getElementById(id)
      // node: itself + neighbours and their links · edge: itself + its two ends
      let keep = el.union(el.neighborhood()).union(el.connectedNodes())
      if (elk) {
        // A compound box passes its opacity on to its children: a tapped box keeps its contents (and their links)
        // lit, and the boxes around whatever stays lit are kept too.
        const inside = el.nodes().descendants()
        keep = keep.union(inside).union(inside.neighborhood())
        keep = keep.union(keep.nodes().parents())
      }
      cy.elements().not(keep).addClass('faded')
    }
    cy.on('tap', 'node', (e) => {
      setSelected({ node: e.target.data() as NodeData })
      highlight(e.target.id())
    })
    cy.on('tap', 'edge', (e) => {
      // City / neighbours edges are single link records, the other levels draw aggregates of node pairs.
      setSelected(elk ? { linkId: e.target.data('link_id') as number } : { edge: e.target.data() as EdgeData })
      highlight(e.target.id())
    })
    cy.on('tap', (e) => {
      if (e.target === cy) {
        setSelected(null)
        highlight(null)
      }
    })
    cy.on('dbltap', 'node', (e) => {
      const target = drillTarget(e.target.data() as NodeData)
      if (target) go(target)
    })
    // Every schematic view can be arranged and saved. On the map positions follow coordinates, so nodes stay put
    // there instead of moving and silently snapping back on the next visit. City / neighbours positions come from
    // ELK every time (spec: no drag there), so they are fixed too.
    const positionsEditable = !onMap && !elk
    if (positionsEditable) cy.on('dragfree', 'node', () => setMoved(countMoved(cy, savedPosRef.current)))
    else cy.autoungrabify(true)
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
    const finish = () => {
      if (keep) {
        // Live refresh: stay where the user was looking.
        cy.viewport({ zoom: keep.zoom, pan: keep.pan })
        if (onMap) applyMarkerScale(cy, view.level)
      } else {
        cy.fit(undefined, 40)
        if (onMap) {
          // Marker sizes follow the zoom and the fit follows the sizes: settle both, then draw the map.
          applyMarkerScale(cy, view.level)
          cy.fit(undefined, 50)
          lastZoom = 0
        }
      }
      syncMap()
      markLive(cy)
      if (focus) {
        const hit = cy.nodes().filter((n) => (n.data('label') as string)?.toUpperCase() === focus.toUpperCase())
        if (hit.nonempty()) {
          hit.addClass('hit')
          cy.animate({ center: { eles: hit }, zoom: 1.2 }, { duration: 300 })
          setSelected({ node: hit.first().data() as NodeData })
          highlight(hit.first().id())
        }
      }
    }
    let disposed = false
    if (elk) {
      savedPosRef.current = new Map()
      setMoved(0)
      setArranging(true)
      const city = asCityView(view)
      layoutCity(city, citySizeOf(drawRef.current.labelMode))
        .then((layout) => {
          if (disposed) return
          cy.add(buildCityElements(city, layout, drawRef.current))
          elkReadyRef.current = true
          setLayoutMs(Math.round(layout.ms))
          finish()
          setLayoutTick((tick) => tick + 1)
        })
        .catch((error: unknown) => {
          // A newer layout superseded this one: its own result will be drawn.
          if (disposed || error instanceof LayoutCancelled) return
          setMessage({ text: `Tata letak gagal: ${error instanceof Error ? error.message : String(error)}`, error: true })
        })
        .finally(() => {
          if (!disposed) setArranging(false)
        })
    } else {
      runLayout(cy, view, onMap, keep?.positions)
      if (positionsEditable) {
        // After a live refresh the unsaved moves survive in `keep`: compare against the saved positions, not the kept ones.
        const before = keep ? savedPosRef.current : new Map<string, Pos>()
        savedPosRef.current = new Map(nodePositions(cy).map((d) => [d.id, before.get(d.id) ?? d.pos] as [string, Pos]))
        setMoved(countMoved(cy, savedPosRef.current))
      } else {
        savedPosRef.current = new Map()
        setMoved(0)
      }
      finish()
    }
    return () => {
      disposed = true
      elkReadyRef.current = false
      setArranging(false)
      cy.destroy()
    }
  }, [drawRevision, go, focus, onMap, elkLabelMode, markLive])

  useEffect(() => {
    const cy = cyRef.current
    if (!cy) return
    cy.edges().forEach((edge) => {
      const color = edge.data('color') as CapacityColor
      const capacityOk = colorFilter === 'all' || (colorFilter === 'red' ? color === 'red' : color !== 'green')
      const visible = capacityOk && segmentsShown.has(edge.data('segment') as Segment)
        && (!cityMode || linkVisible(edge.data() as CityLink, cityFilter))
      edge.style('display', visible ? 'element' : 'none')
      if (edgeLabels) edge.addClass('labeled')
      else edge.removeClass('labeled')
    })
  }, [colorFilter, edgeLabels, segmentsShown, view, onMap, layoutTick, cityMode, cityFilter])

  // A vendor or layer picked in one city says nothing about the next one.
  const viewKey = view?.layout_key
  useEffect(() => {
    setCityFilter((filter) => ({ ...filter, vendors: new Set(), layers: new Set() }))
  }, [viewKey])

  // City / neighbours: node filters and the problem / SPOF highlights. Only display and classes change, so the ELK
  // layout stays. Compounds are never dimmed or hidden: they pass their opacity on to the children they hold.
  useEffect(() => {
    const cy = cyRef.current
    if (!cy || !cityMode) return
    cy.batch(() => {
      cy.nodes().forEach((node) => {
        const data = node.data() as { kind: string; vendor?: string | null; tier?: number; health: NodeHealth; spof?: boolean }
        if (isCompound(data.kind)) return
        const problemLink = node.connectedEdges().some((e) => isProblemLink(e.data() as CityLink))
        node.style('display', nodeVisible(data, problemLink, cityFilter) ? 'element' : 'none')
        node.toggleClass('calm', highlightProblems && !isProblemNode(data.health) && !problemLink)
        node.toggleClass('spof', highlightSpof && !!data.spof)
      })
      cy.edges().forEach((edge) => {
        edge.toggleClass('calm', highlightProblems && !isProblemLink(edge.data() as CityLink))
        edge.toggleClass('spof', highlightSpof && !!edge.data('spof'))
      })
    })
  }, [cityMode, cityFilter, highlightProblems, highlightSpof, view, layoutTick])

  // Colour and label modes restyle the drawing in place (no new layout, the view stays put).
  useEffect(() => {
    const cy = cyRef.current
    if (!cy) return
    cy.batch(() => {
      cy.edges().forEach((edge) => {
        edge.data('colorHex', edgeColor(edge.data() as EdgeData, colorMode))
      })
      cy.nodes('[kind="device"]').forEach((node) => {
        const n = node.data() as NodeData
        node.data('display', labelMode === 'short' ? shortName(n.label, n.pop) : n.label)
      })
    })
  }, [colorMode, labelMode, view, onMap, layoutTick])

  function toggleSegment(segment: Segment) {
    const next = new Set(segmentsShown)
    if (next.has(segment)) next.delete(segment)
    else next.add(segment)
    setSegmentsShown(next)
  }

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
    const viewKey = viewRef.current?.layout_key
    if (!cy || !viewKey || saving) return
    // Every node is stored, not only the moved ones, so the whole drawing reopens exactly as it is now.
    const current = nodePositions(cy)
    const positions = Object.fromEntries(current.map((d) => [d.id, { x: Math.round(d.pos.x), y: Math.round(d.pos.y) }]))
    setSaving(true)
    try {
      await api.put('/topology/layouts', { view_key: viewKey, positions })
      savedPosRef.current = new Map(current.map((d) => [d.id, d.pos] as [string, Pos]))
      setMessage({ text: `Tata letak disimpan: ${moved} node dipindah. Tampilan ini akan dibuka dengan susunan yang sama.` })
      setMoved(0)
    } catch (e) {
      setMessage({ text: (e as Error).message, error: true })
    } finally {
      setSaving(false)
    }
  }

  async function resetLayout() {
    const viewKey = view?.layout_key
    if (!viewKey || !window.confirm('Kembalikan tampilan ini ke tata letak otomatis? Susunan yang tersimpan dihapus.')) return
    try {
      await api.del(`/topology/layouts?view_key=${encodeURIComponent(viewKey)}`)
      setMessage({ text: 'Tata letak dikembalikan ke susunan otomatis.' })
      await load()
    } catch (e) {
      setMessage({ text: (e as Error).message, error: true })
    }
  }

  function discardMoves() {
    const cy = cyRef.current
    if (!cy) return
    cy.nodes().not('[kind="band"]').forEach((n) => {
      const pos = savedPosRef.current.get(n.id())
      if (pos) n.animate({ position: pos }, { duration: 250 })
    })
    setMoved(0)
  }

  const saveRef = useRef(savePositions)
  saveRef.current = savePositions
  // While there are unsaved moves: Ctrl+S saves, and closing or reloading the tab asks first.
  useEffect(() => {
    if (!dirty) return
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
        e.preventDefault()
        saveRef.current()
      }
    }
    const onUnload = (e: BeforeUnloadEvent) => {
      e.preventDefault()
      e.returnValue = ''
    }
    window.addEventListener('keydown', onKey)
    window.addEventListener('beforeunload', onUnload)
    return () => {
      window.removeEventListener('keydown', onKey)
      window.removeEventListener('beforeunload', onUnload)
    }
  }, [dirty])

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
    const edges = (view?.edges ?? []).filter((e) => segmentsShown.has(e.segment))
    return {
      groups: edges.length,
      links: edges.reduce((s, e) => s + e.links, 0),
      red: edges.filter((e) => e.color === 'red').length,
      yellow: edges.filter((e) => e.color === 'yellow').length,
      green: edges.filter((e) => e.color === 'green').length,
    }
  }, [view, segmentsShown])

  const node = selected?.node
  const edge = selected?.edge
  const linkId = selected?.linkId
  const nameOf = (id: string) => view?.nodes.find((n) => n.id === id)?.label ?? id
  const viewArea = view?.area ?? area
  const viewProvince = view?.province ?? province
  const viewRegency = view?.regency ?? regency
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
              <a href="#" onClick={(e) => { e.preventDefault(); go({ level: 'province', area: viewArea }) }}
                className={level === 'province' || (level === 'pop' && !viewProvince) ? 'current' : ''}>
                {viewArea}
              </a>
            </>
          )}
          {level !== 'area' && level !== 'province' && viewArea && viewProvince && (
            <>
              <span>›</span>
              <a href="#" onClick={(e) => { e.preventDefault(); go({ level: 'regency', area: viewArea, province: viewProvince }) }}
                className={level === 'regency' || (level === 'pop' && !viewRegency) ? 'current' : ''}>
                {viewProvince}
              </a>
            </>
          )}
          {(level === 'city' || level === 'pop' || level === 'device' || level === 'neighbors') && viewArea && viewProvince && viewRegency && (
            <>
              <span>›</span>
              <a href="#" onClick={(e) => { e.preventDefault(); go({ level: 'city', area: viewArea, province: viewProvince, regency: viewRegency }) }}
                className={level === 'city' ? 'current' : ''} title="Topologi kota: POP, cluster dan semua link">
                {viewRegency}
              </a>
            </>
          )}
          {(level === 'city' || level === 'pop' || level === 'device') && viewArea && viewProvince && viewRegency && (
            <>
              <span>›</span>
              <a href="#" onClick={(e) => { e.preventDefault(); go({ level: 'pop', area: viewArea, province: viewProvince, regency: viewRegency }) }}
                className={level === 'pop' ? 'current' : 'crumb-aside'} title="Daftar POP kabupaten/kota ini">
                Daftar POP
              </a>
            </>
          )}
          {(level === 'device' || level === 'neighbors') && viewPop && (
            <>
              <span>›</span>
              <a href="#" onClick={(e) => { e.preventDefault(); go({ level: 'device', pop: viewPop }) }} className={level === 'device' ? 'current' : ''}
                title={`POP ${viewPop}${view?.pop_name && view.pop_name !== viewPop ? ` · ${view.pop_name}` : ''}`}>
                {(level === 'device' && view?.city) || viewPop}
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
        <div className="spacer" />
        <button type="button" className={`live-toggle ${live ? 'on' : ''}`} aria-pressed={live}
          title={live ? `Live: dicek tiap ${LIVE_INTERVAL_MS / 1000} detik${lastCheck ? `, terakhir ${lastCheck.toLocaleTimeString('id-ID')}` : ''}. Klik untuk menghentikan.`
            : 'Live mati: peta tidak diperbarui otomatis. Klik untuk menyalakan.'}
          onClick={() => { setLive(!live); remember('nadien.topology.live', live ? '0' : '1') }}>
          <span className="live-dot" />{live ? 'Live' : 'Live mati'}
        </button>
        {!onMap && view && !isElkLevel(view.level) && (Object.keys(view.layout ?? {}).length > 0 || view.nodes.some((n) => n.kind === 'device' && n.x != null)) && (
          <button onClick={resetLayout} title="Hapus susunan yang tersimpan untuk tampilan ini dan kembali ke tata letak otomatis">
            Atur ulang tata letak
          </button>
        )}
        {level === 'area' && view?.nodes.some((n) => n.label === 'Belum dipetakan') && (
          <button onClick={autoAssign} title="Isi area POP yang masih kosong dari kode kota (tidak menimpa yang sudah diisi)">
            Isi area otomatis
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

      <div className="display-bar card" aria-label="Tampilan peta">
        <div className="segment-chips" role="group" aria-label="Fungsi link">
          <span className="muted small">Fungsi link</span>
          {SEGMENTS.map((s) => (
            <button key={s} type="button" aria-pressed={segmentsShown.has(s)} className={`segment-chip ${segmentsShown.has(s) ? 'on' : ''}`}
              style={{ '--seg': SEGMENT_COLOR[s] } as React.CSSProperties} title={SEGMENT_HINT[s]} onClick={() => toggleSegment(s)}>
              <span className={`segment-line ${s}`} />{SEGMENT_LABEL[s]}
            </button>
          ))}
        </div>
        <label className="inline-field"><span>Warna garis</span>
          <select value={colorMode} onChange={(e) => { setColorMode(e.target.value as ColorMode); remember('nadien.topology.color', e.target.value) }}>
            <option value="utilization">Utilisasi (committed)</option>
            <option value="segment">Fungsi link</option>
          </select>
        </label>
        <label className="inline-field"><span>Tampilkan</span>
          <select value={colorFilter} onChange={(e) => setColorFilter(e.target.value as 'all' | 'warn' | 'red')}>
            <option value="all">Semua link</option>
            <option value="warn">Kuning + Merah</option>
            <option value="red">Merah saja</option>
          </select>
        </label>
        <label className="inline-field" title="Ringkas: nama perangkat tanpa kode POP yang sedang dibuka">
          <span>Nama perangkat</span>
          <select value={labelMode} onChange={(e) => { setLabelMode(e.target.value as LabelMode); remember('nadien.topology.labels', e.target.value) }}>
            <option value="short">Ringkas</option>
            <option value="full">Lengkap</option>
          </select>
        </label>
        <label className="check">
          <input type="checkbox" checked={edgeLabels} onChange={(e) => setEdgeLabels(e.target.checked)} /> Label link
        </label>
        {mapAvailable && (
          <label className="check" title="Tampilkan di atas peta sesuai koordinat (POP, atau site perangkat bila ada)">
            <input type="checkbox" checked={mapOn} onChange={(e) => toggleMap(e.target.checked)} /> Peta
          </label>
        )}
      </div>

      {cityMode && view && (
        <CityFilterBar vendors={vendorsIn(asCityView(view))} filter={cityFilter} onFilter={setCityFilter}
          highlightProblems={highlightProblems} highlightSpof={highlightSpof}
          onHighlightProblems={(on) => { setHighlightProblems(on); remember('nadien.topology.highlight-problems', on ? '1' : '0') }}
          onHighlightSpof={(on) => { setHighlightSpof(on); remember('nadien.topology.highlight-spof', on ? '1' : '0') }} />
      )}

      {liveNote && (
        <div className="notice live-note" onClick={() => setLiveNote(null)} title="Klik untuk menutup">
          {liveNote}. Yang baru ditandai hijau, link yang baru down ditandai merah, selama 10 menit.
        </div>
      )}

      {message && (
        <div className={message.error ? 'error' : 'notice'} onClick={() => setMessage(null)}>
          {message.text}
        </div>
      )}
      {view?.truncated && <div className="notice">Terlalu banyak tetangga — ditampilkan 400 perangkat pertama.</div>}

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
          {dirty && (
            <div className="unsaved-bar" role="status">
              <span className="unsaved-dot" />
              <span><b>{moved} node</b> dipindah · tata letak belum disimpan</span>
              <button type="button" onClick={discardMoves} disabled={saving}>Batalkan</button>
              <button type="button" className="primary" onClick={savePositions} disabled={saving} title="Simpan tata letak tampilan ini (Ctrl+S)">
                {saving ? 'Menyimpan…' : 'Simpan perubahan'}
              </button>
            </div>
          )}
          {loading ? <div className="graph-loading">Memuat…</div> : arranging && <div className="graph-loading arranging">Menyusun tata letak…</div>}
          <TopologyLegend open={legendOpen} colorMode={colorMode} level={level}
            onToggle={() => { setLegendOpen(!legendOpen); remember('nadien.topology.legend', legendOpen ? '0' : '1') }} />
          <div className="graph-status small muted">
            {stats.groups} jalur · {stats.links} link
            {stats.red ? ` · ${stats.red} merah` : ''}
            {view && usesElk(view.level, onMap) && layoutMs != null && !arranging ? ` · tata letak ${layoutMs} ms` : ''}
            {onMap && level === 'pop' && view ? ` · ${view.nodes.filter((n) => n.estimated).length} POP tanpa koordinat (posisi perkiraan, garis putus-putus)` : ''}
            {onMap && (level === 'device' || level === 'neighbors') && view
              ? ` · ${devicesLocated} perangkat dengan koordinat site, ${view.nodes.filter((n) => n.kind === 'device' && !n.located).length} di sekitar POP (garis putus-putus)`
              : ''}
          </div>
          {cityMode && view && (node || linkId != null) && (
            <TopologyDetail view={asCityView(view)} node={node} linkId={linkId} onClose={() => setSelected(null)}
              drill={node && drillTarget(node) ? { label: DRILL_LABEL[node.kind], run: () => go(drillTarget(node)!) } : null} />
          )}
          {!cityMode && (node || edge || (level === 'device' && view?.pop_id != null)) && (
            <aside className="detail-panel card">
              {(node || edge) && (
                <button className="panel-close" onClick={() => setSelected(null)} aria-label="Tutup">×</button>
              )}
          {level === 'device' && view?.pop_id != null && !node && !edge && (
            <div className="detail">
              <h3>{view.city ?? `POP ${view.pop}`}</h3>
              <div className="small muted">POP {view.pop} · {[view.regency, view.province, view.area].filter(Boolean).join(' · ')}</div>
              <PopAreaEditor popId={view.pop_id} region={view.region} onSaved={(a) => areaSaved(view.pop ?? '', a)} />
            </div>
          )}
          {node && (
            <div className="detail">
              <h3>{node.label}</h3>
              <dl>
                {isGroup(node.kind) && (
                  <>
                    {node.kind !== 'area' && (<><dt>Wilayah</dt><dd>{[node.province !== node.label && node.province, node.area].filter(Boolean).join(' · ')}</dd></>)}
                    <dt>POP</dt><dd>{node.pops}</dd>
                    <dt>Perangkat</dt><dd>{node.devices}</dd>
                  </>
                )}
                {node.kind === 'pop' && (
                  <>
                    <dt>Kode POP</dt><dd>{node.code}</dd>
                    <dt>Nama</dt><dd>{node.name}</dd>
                    <dt>Kab/Kota</dt><dd>{node.regency} · {node.province}</dd>
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
                    {node.reachability && (<><dt>Observium</dt><dd>{node.reachability}{node.mgmt_ip ? ` · ${node.mgmt_ip}` : ''}</dd></>)}
                  </>
                )}
                {node.kind.startsWith('external') && (
                  <>
                    <dt>{GROUP_LABEL[node.kind]}</dt><dd>{node.kind === 'external_pop' ? `${node.label} (${node.code})` : node.label}</dd>
                    {node.kind !== 'external_area' && <><dt>Area</dt><dd>{node.area}</dd></>}
                    {node.links != null && (<><dt>Link ke sini</dt><dd>{node.links}</dd></>)}
                    {node.devices != null && (<><dt>Perangkat terhubung</dt><dd>{node.devices}</dd></>)}
                  </>
                )}
              </dl>
              {node.kind === 'pop' && node.pop_id != null && (
                <PopAreaEditor popId={node.pop_id} region={node.region} onSaved={(a) => areaSaved(node.code ?? node.label, a)} />
              )}
              {node.internal && node.internal.links > 0 && (
                <p className="small muted">
                  Link internal: {node.internal.links} · {fmtG(node.internal.capacity_gbps)} · {node.internal.committed_pct}% committed
                  {node.internal.down ? ` · ${node.internal.down} down` : ''}
                </p>
              )}
              <div className="detail-actions">
                {drillTarget(node) && (
                  <button onClick={() => go(drillTarget(node)!)}>{DRILL_LABEL[node.kind]} →</button>
                )}
                {node.kind === 'device' && (
                  <RouterLink to={`/ports?q=${encodeURIComponent(node.label)}`}>Lihat port perangkat →</RouterLink>
                )}
              </div>
            </div>
          )}
          {edge && (
            <div className="detail">
              <h3>{nameOf(edge.source)} ↔ {nameOf(edge.target)}</h3>
              <dl>
                <dt>Fungsi</dt><dd><span className={`segment-line ${edge.segment}`} /> {SEGMENT_LABEL[edge.segment]}</dd>
                <dt>Link</dt><dd>{edge.links}{edge.down ? ` (${edge.down} down)` : ''}</dd>
                <dt>Kapasitas</dt><dd>{fmtG(edge.capacity_gbps)}
                  {edge.capacity_unknown > 0 && <span className="muted"> · {edge.capacity_unknown} link belum diketahui kapasitasnya</span>}</dd>
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
