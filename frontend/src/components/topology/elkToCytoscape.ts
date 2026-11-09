import type { ElkExtendedEdge, ElkNode } from 'elkjs/lib/elk-api'

export interface Pos {
  x: number
  y: number
}

export interface EdgeRoute {
  curve: 'segments' | 'straight'
  weights: number[]
  distances: number[]
  sourceEndpoint: string  // "<dx>px <dy>px" relative to the source node center
  targetEndpoint: string
}

export interface CytoscapeLayout {
  positions: Record<string, Pos>  // centers of leaf nodes; Cytoscape sizes compounds from their children
  edges: Record<string, EdgeRoute>
}

// Cytoscape places a segment point at S + w*(T-S) + d*n with n = (-dy, dx)/|T-S| (edge-distances: node-position),
// so d is the cross product (T-S) x (P-S) over |T-S|, which keeps the sign convention identical to Cytoscape's.
export function toSegments(source: Pos, target: Pos, bends: Pos[]): { weights: number[]; distances: number[] } {
  const dx = target.x - source.x
  const dy = target.y - source.y
  const lengthSquared = dx * dx + dy * dy
  const length = Math.sqrt(lengthSquared)
  const weights: number[] = []
  const distances: number[] = []
  for (const bend of bends) {
    const px = bend.x - source.x
    const py = bend.y - source.y
    if (length === 0) {
      // Overlapping ends have no direction to project on: keep the bend at the source, offset by its plain distance.
      weights.push(0)
      distances.push(Math.hypot(px, py))
    } else {
      weights.push((px * dx + py * dy) / lengthSquared)
      distances.push((dx * py - dy * px) / length)
    }
  }
  return { weights, distances }
}

// `+ 0` turns -0 into 0 so the endpoint strings stay stable.
const pixels = (value: number): string => `${Math.round(value * 100) / 100 + 0}px`

function collect(
  node: ElkNode,
  origin: Pos,
  centers: Record<string, Pos>,
  edges: { edge: ElkExtendedEdge; origin: Pos }[],
): void {
  // ELK reports child coordinates relative to the parent and edge points relative to the edge's container.
  const absolute = { x: origin.x + (node.x ?? 0), y: origin.y + (node.y ?? 0) }
  for (const edge of node.edges ?? []) edges.push({ edge, origin: absolute })
  if (node.children?.length) {
    for (const child of node.children) collect(child, absolute, centers, edges)
  } else {
    centers[node.id] = { x: absolute.x + (node.width ?? 0) / 2, y: absolute.y + (node.height ?? 0) / 2 }
  }
}

function routeOf(edge: ElkExtendedEdge, origin: Pos, centers: Record<string, Pos>): EdgeRoute | null {
  const section = edge.sections?.[0]
  const sourceCenter = centers[edge.sources[0]]
  const targetCenter = centers[edge.targets[0]]
  if (!section || !sourceCenter || !targetCenter) return null
  const shift = (p: Pos): Pos => ({ x: p.x + origin.x, y: p.y + origin.y })
  const start = shift(section.startPoint)
  const end = shift(section.endPoint)
  const bends = (section.bendPoints ?? []).map(shift)
  const { weights, distances } = toSegments(sourceCenter, targetCenter, bends)
  return {
    curve: bends.length ? 'segments' : 'straight',
    weights,
    distances,
    sourceEndpoint: `${pixels(start.x - sourceCenter.x)} ${pixels(start.y - sourceCenter.y)}`,
    targetEndpoint: `${pixels(end.x - targetCenter.x)} ${pixels(end.y - targetCenter.y)}`,
  }
}

export function toCytoscape(result: ElkNode): CytoscapeLayout {
  const centers: Record<string, Pos> = {}
  const pending: { edge: ElkExtendedEdge; origin: Pos }[] = []
  // The root is the graph itself, not a drawn node, so it contributes no position of its own.
  const rootOrigin = { x: result.x ?? 0, y: result.y ?? 0 }
  for (const edge of result.edges ?? []) pending.push({ edge, origin: rootOrigin })
  for (const child of result.children ?? []) collect(child, rootOrigin, centers, pending)

  const edges: Record<string, EdgeRoute> = {}
  for (const { edge, origin } of pending) {
    const route = routeOf(edge, origin, centers)
    if (route) edges[edge.id] = route
  }
  return { positions: centers, edges }
}
