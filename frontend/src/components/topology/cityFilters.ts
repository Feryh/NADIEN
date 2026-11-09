import { linkTroubled, type NodeHealth } from './cityElements'
import type { CityLink, CityView } from './types'

export const NO_VENDOR = 'Tanpa vendor'
export const LAYER_LABEL = ['Core', 'PE / BNG', 'Metro / Agg', 'Akses', 'Lainnya'] as const

export interface CityFilter {
  /** Empty = every vendor. */
  vendors: ReadonlySet<string>
  problemsOnly: boolean
  /** Empty = every layer. Values are tiers 0..4. */
  layers: ReadonlySet<number>
}

export const NO_FILTER: CityFilter = { vendors: new Set(), problemsOnly: false, layers: new Set() }

export const vendorKey = (vendor: string | null | undefined): string => vendor || NO_VENDOR

/** Vendors present among the view's devices, sorted, with "Tanpa vendor" last. */
export function vendorsIn(view: Pick<CityView, 'nodes'>): string[] {
  const found = new Set<string>()
  for (const n of view.nodes) if (n.kind === 'device') found.add(vendorKey(n.vendor))
  return [...found].sort((a, b) => (a === NO_VENDOR ? 1 : b === NO_VENDOR ? -1 : a.localeCompare(b)))
}

export const layerOf = (tier: number | undefined): number => (tier != null && tier >= 0 && tier <= 3 ? tier : 4)

/** A link worth attention: down, or congested (yellow / red) with a known capacity. */
export const isProblemLink = (link: Pick<CityLink, 'status' | 'capacity_gbps' | 'color'>): boolean => linkTroubled(link)

/** Node needing attention: Observium down, or an end of a troubled link. */
export const isProblemNode = (health: NodeHealth): boolean => health === 'down' || health === 'warn'

export interface FilterableNode {
  kind: string
  vendor?: string | null
  tier?: number
  health: NodeHealth
}

const COMPOUND_KINDS = new Set(['city_box', 'pop_lane', 'cluster'])
export const isCompound = (kind: string): boolean => COMPOUND_KINDS.has(kind)

/**
 * Whether a drawn node stays visible. Compounds always do (hiding a lane would drag its children's frame along).
 * `hasProblemLink`: some link ending on the node is down or congested.
 */
export function nodeVisible(node: FilterableNode, hasProblemLink: boolean, filter: CityFilter): boolean {
  if (isCompound(node.kind)) return true
  if (node.kind === 'device' && filter.vendors.size > 0 && !filter.vendors.has(vendorKey(node.vendor))) return false
  if ((node.kind === 'device' || node.kind === 'summary') && filter.layers.size > 0 && !filter.layers.has(layerOf(node.tier))) return false
  if (filter.problemsOnly && (node.health === 'up' || node.health === 'unknown') && !hasProblemLink) return false
  return true
}

/** "Masalah saja" hides links that are up and green; hidden ends hide their links on their own (Cytoscape). */
export function linkVisible(link: Pick<CityLink, 'status' | 'capacity_gbps' | 'color'>, filter: CityFilter): boolean {
  return !filter.problemsOnly || isProblemLink(link)
}
