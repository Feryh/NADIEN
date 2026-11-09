import type { ElementDefinition } from 'cytoscape'
import type { ElkNode } from 'elkjs/lib/elk-api'
import { CAPACITY_HEX, fmtG } from '../ui'
import type { CapacityColor } from '../../api'
import { buildElkGraph } from './elkGraph'
import type { CytoscapeLayout } from './elkToCytoscape'
import { edgeWidth, ROLE_COLOR, SEGMENT_COLOR, shortName, UNKNOWN_HEX } from './shared'
import type { CityCluster, CityLink, CityNode, CityView } from './types'

export type NodeHealth = 'up' | 'down' | 'warn' | 'unknown'
export type CityLabelMode = 'full' | 'short'
export type CityColorMode = 'utilization' | 'segment'

export const HEALTH_FILL: Record<NodeHealth, string> = {
  up: '#166534', down: '#b91c1c', warn: '#a16207', unknown: '#334155',
}

const NODE_HEIGHT = 30

// A link only counts as congested when its capacity is known: with capacity 0 the backend colour means nothing
// and the link is drawn grey, so it must not turn its devices yellow either.
function linkTroubled(link: CityLink): boolean {
  if (link.status === 'down') return true
  return link.capacity_gbps > 0 && (link.color === 'yellow' || link.color === 'red')
}

/**
 * Fill colour state of a drawn node. `links` may be every link of the view: only those ending on `n` count.
 * Only devices carry Observium reachability; POPs of other cities and summary nodes stay `unknown`.
 */
export function nodeStatus(n: CityNode, links: CityLink[]): NodeHealth {
  if (n.kind !== 'device') return 'unknown'
  if (n.reachability !== 'up') return n.reachability
  const own = links.filter((l) => l.source === n.id || l.target === n.id)
  return own.some(linkTroubled) ? 'warn' : 'up'
}

function nodeDisplay(n: CityNode, labelMode: CityLabelMode): string {
  if (n.kind === 'external_pop') return `→ ${n.label}`
  if (n.kind === 'device' && labelMode === 'short') return shortName(n.label, n.pop)
  return n.label
}

/** ELK node size; the Cytoscape node is drawn at exactly this size so ELK's routes end on its border. */
export function citySizeOf(labelMode: CityLabelMode): (n: CityNode) => { width: number; height: number } {
  return (n) => ({ width: Math.max(70, nodeDisplay(n, labelMode).length * 7 + 26), height: NODE_HEIGHT })
}

export function linkColor(link: Pick<CityLink, 'capacity_gbps' | 'color' | 'segment'>, mode: CityColorMode): string {
  if (mode === 'segment') return SEGMENT_COLOR[link.segment as keyof typeof SEGMENT_COLOR] ?? UNKNOWN_HEX
  return link.capacity_gbps > 0 ? CAPACITY_HEX[link.color as CapacityColor] ?? UNKNOWN_HEX : UNKNOWN_HEX
}

function linkDisplay(link: CityLink): string {
  if (!(link.capacity_gbps > 0)) return 'kapasitas ?'
  return `${fmtG(link.capacity_gbps)} · ${link.committed_pct.toFixed(0)}%`
}

const CLUSTER_LABEL: Record<CityCluster['kind'], (n: number) => string> = {
  ring: (n) => `Ring · ${n} perangkat`,
  mesh: (n) => `Mesh · ${n}`,
  pair: (n) => `Redundan · ${n}`,
}

interface Compound {
  id: string
  parent?: string
  children: number
}

// The Cytoscape hierarchy is read from the ELK graph itself so the drawn boxes can never disagree with what ELK
// laid out. Empty compounds (a POP with no drawn device) are left out: they would render as stray leaf nodes.
function hierarchy(graph: ElkNode): { parentOf: Map<string, string>; compounds: Compound[] } {
  const parentOf = new Map<string, string>()
  const compounds: Compound[] = []
  const walk = (node: ElkNode, parent: string | undefined) => {
    for (const child of node.children ?? []) {
      if (parent) parentOf.set(child.id, parent)
      if (child.children?.length) {
        compounds.push({ id: child.id, parent, children: child.children.length })
        walk(child, child.id)
      }
    }
  }
  walk(graph, undefined)
  return { parentOf, compounds }
}

function compoundData(c: Compound, view: CityView): { kind: string; label: string; [key: string]: unknown } {
  if (c.id === 'city') return { kind: 'city_box', label: view.regency ?? 'Kab/Kota' }
  if (c.id.startsWith('cluster:')) {
    const cluster = view.clusters.find((k) => `cluster:${k.id}` === c.id)
    const kind = cluster?.kind ?? 'mesh'
    return { kind: 'cluster', cluster_kind: kind, label: CLUSTER_LABEL[kind](c.children) }
  }
  const code = c.id.slice('pop:'.length)
  const pop = view.pops.find((p) => p.code === code)
  const name = pop?.name && pop.name !== code ? ` · ${pop.name}` : ''
  return { kind: 'pop_lane', code, pop: code, label: `POP ${code}${name}` }
}

export interface CityDrawOptions {
  labelMode: CityLabelMode
  colorMode: CityColorMode
}

function routeData(id: string, layout: CytoscapeLayout): Record<string, unknown> {
  const route = layout.edges[id]
  if (!route) return { curve: 'straight' }
  return {
    curve: route.curve,
    // Space-separated strings: Cytoscape parses data-mapped multi-value properties from either form, and a string
    // keeps the element data flat.
    weights: route.weights.join(' '),
    distances: route.distances.join(' '),
    sourceEndpoint: route.sourceEndpoint,
    targetEndpoint: route.targetEndpoint,
  }
}

/**
 * Cytoscape elements for the city / neighbours levels: one edge per entry of `links[]`, compounds city / pop /
 * cluster mirroring the ELK graph. Without `layout` only the data is built (for live updates in place):
 * no positions and no route fields, so existing routes are not overwritten.
 */
export function buildCityElements(view: CityView, layout: CytoscapeLayout | null, options: CityDrawOptions): ElementDefinition[] {
  const sizeOf = citySizeOf(options.labelMode)
  const { parentOf, compounds } = hierarchy(buildElkGraph(view, sizeOf))
  const linksOf = new Map<string, CityLink[]>()
  for (const link of view.links) {
    for (const end of [link.source, link.target]) linksOf.set(end, [...(linksOf.get(end) ?? []), link])
  }

  const elements: ElementDefinition[] = compounds.map((c) => {
    const data = compoundData(c, view)
    return { data: { id: c.id, ...(c.parent ? { parent: c.parent } : {}), ...data, display: data.label } }
  })
  const known = new Set<string>()
  for (const n of view.nodes) {
    known.add(n.id)
    const display = nodeDisplay(n, options.labelMode)
    const health = nodeStatus(n, linksOf.get(n.id) ?? [])
    const parent = parentOf.get(n.id)
    const { width, height } = sizeOf(n)
    const role = n.kind === 'device' ? n.role : ''
    elements.push({
      data: {
        ...n, ...(parent ? { parent } : {}), display, w: width, h: height, health, healthColor: HEALTH_FILL[health],
        roleColor: ROLE_COLOR[role] ?? '#64748b',
      },
      ...(layout ? { position: layout.positions[n.id] ?? { x: 0, y: 0 } } : {}),
    })
  }
  for (const link of view.links) {
    // ELK skipped these (both ends on one drawn node, or an end not drawn): there is no route to draw.
    if (link.source === link.target || !known.has(link.source) || !known.has(link.target)) continue
    const id = `link:${link.id}`
    elements.push({
      data: {
        ...link, id, link_id: link.id, display: linkDisplay(link), colorHex: linkColor(link, options.colorMode),
        width: edgeWidth(link.capacity_gbps), down: link.status === 'down',
        sourceLabel: link.a_interface ?? '', targetLabel: link.b_interface ?? '',
        ...(layout ? routeData(id, layout) : {}),
      },
    })
  }
  return elements
}

/** Fields an existing element may not take through `data()`: identity and structure. */
const STRUCTURAL = new Set(['id', 'parent', 'source', 'target'])

export function liveData(definition: ElementDefinition): Record<string, unknown> {
  return Object.fromEntries(Object.entries(definition.data).filter(([key]) => !STRUCTURAL.has(key)))
}

const sortedIds = (ids: (string | number)[]): string => ids.map(String).sort().join('\n')

/** Same drawn nodes and same link records: a live refresh can update data in place instead of a new layout. */
export function sameShape(before: CityView, after: CityView): boolean {
  return sortedIds(before.nodes.map((n) => n.id)) === sortedIds(after.nodes.map((n) => n.id))
    && sortedIds(before.links.map((l) => l.id)) === sortedIds(after.links.map((l) => l.id))
}
