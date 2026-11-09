import type { ElkExtendedEdge, ElkNode, LayoutOptions } from 'elkjs/lib/elk-api'
import type { CityCluster, CityNode, CityView } from './types'

export const ELK_OPTIONS: LayoutOptions = {
  'elk.algorithm': 'layered',
  'elk.direction': 'DOWN',
  'elk.edgeRouting': 'ORTHOGONAL',
  'elk.hierarchyHandling': 'INCLUDE_CHILDREN',
  'elk.spacing.nodeNode': '30',
  'elk.layered.spacing.nodeNodeBetweenLayers': '50',
  'elk.spacing.edgeEdge': '6',
  'elk.layered.spacing.edgeEdgeBetweenLayers': '6',
  'elk.layered.crossingMinimization.strategy': 'LAYER_SWEEP',
  // Without this ELK ignores the per-node partition, i.e. the layer rows (Core on top, Access at the bottom).
  'elk.partitioning.activate': 'true',
  // Edges are declared on the root but ELK reports section points relative to the edges' common ancestor by default.
  'elk.json.edgeCoords': 'ROOT',
}

// Room above the children for the compound's label, which Cytoscape draws there.
const COMPOUND_PADDING = '[top=28,left=14,bottom=14,right=14]'

const PARTITION_KEY = 'elk.partitioning.partition'

// Partition 0 is reserved for POPs of other cities, so tier n (0 = Core) becomes n + 1.
function partitionOf(node: CityNode): string {
  if (node.kind === 'external_pop') return '0'
  return String(node.tier + 1)
}

function compound(id: string): ElkNode {
  return { id, children: [], layoutOptions: { 'elk.padding': COMPOUND_PADDING } }
}

function appendChild(parent: ElkNode, child: ElkNode): void {
  parent.children = [...(parent.children ?? []), child]
}

function buildEdges(view: CityView, knownIds: Set<string>): ElkExtendedEdge[] {
  const edges: ElkExtendedEdge[] = []
  for (const link of view.links) {
    // A summary-internal link has both ends on the same drawn node; it stays in the data but has nothing to route.
    if (link.source === link.target) continue
    if (!knownIds.has(link.source) || !knownIds.has(link.target)) continue
    edges.push({ id: `link:${link.id}`, sources: [link.source], targets: [link.target] })
  }
  return edges
}

// Hierarchy (spec 4.2): root -> city (city level only) -> pop:<code> -> cluster:<id> -> device.
// Every edge is listed on the root; ELK resolves the common ancestor itself with INCLUDE_CHILDREN.
export function buildElkGraph(view: CityView, sizeOf: (n: CityNode) => { width: number; height: number }): ElkNode {
  const root: ElkNode = { id: 'root', layoutOptions: { ...ELK_OPTIONS }, children: [], edges: [] }
  const top = view.level === 'city' ? compound('city') : root
  if (top !== root) appendChild(root, top)

  const popNodes = new Map<string, ElkNode>()
  const popContainer = (code: string): ElkNode => {
    let pop = popNodes.get(code)
    if (!pop) {
      pop = compound(`pop:${code}`)
      popNodes.set(code, pop)
    }
    return pop
  }
  for (const pop of [...view.pops].sort((a, b) => a.code.localeCompare(b.code))) popContainer(pop.code)

  const clusterById = new Map<string, CityCluster>(view.clusters.map((c) => [c.id, c]))
  const clusterNodes = new Map<string, ElkNode>()
  const clusterContainer = (cluster: CityCluster): ElkNode => {
    let node = clusterNodes.get(cluster.id)
    if (!node) {
      node = compound(`cluster:${cluster.id}`)
      clusterNodes.set(cluster.id, node)
    }
    return node
  }

  const knownIds = new Set<string>()
  for (const node of view.nodes) {
    knownIds.add(node.id)
    const leaf: ElkNode = { id: node.id, ...sizeOf(node), layoutOptions: { [PARTITION_KEY]: partitionOf(node) } }
    if (node.kind === 'external_pop') {
      appendChild(top, leaf)
      continue
    }
    const popCode = node.pop
    const parentPop = popCode ? popContainer(popCode) : top
    const cluster = node.kind === 'device' && node.cluster_id ? clusterById.get(node.cluster_id) : undefined
    appendChild(cluster ? clusterContainer(cluster) : parentPop, leaf)
  }

  // Clusters go in before the pops are placed, so a pop that only exists through a cluster is still kept.
  for (const [clusterId, node] of clusterNodes) {
    const cluster = clusterById.get(clusterId)
    // A cross-POP cluster (pop null) sits in the city (or root); a single-POP cluster sits in its swimlane.
    appendChild(cluster?.pop ? popContainer(cluster.pop) : top, node)
  }
  for (const [, pop] of [...popNodes].sort(([a], [b]) => a.localeCompare(b))) appendChild(top, pop)

  root.edges = buildEdges(view, knownIds)
  return root
}
