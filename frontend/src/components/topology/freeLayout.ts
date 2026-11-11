/**
 * "Susunan bebas": a free arrangement of a schematic, not tied to the map or to role rows. Linked nodes pull
 * together and every pair pushes apart (force-directed, Fruchterman–Reingold), then two hard rules are enforced:
 * no two nodes (label included) overlap, and no link runs through a node it does not belong to.
 * Deterministic: it starts from the role-row layout and uses no randomness, so a refresh draws the same picture.
 * Plain typed arrays throughout: a 400-node view runs O(n²) loops a few hundred times.
 */

export interface Pos {
  x: number
  y: number
}

export interface FreeNode {
  id: string
  /** Size on screen including the label written under the node. */
  width: number
  height: number
}

export interface FreeEdge {
  source: string
  target: string
}

export interface FreeOptions {
  /** Ideal distance between the centres of two linked nodes. */
  spacing?: number
  /** Empty space kept around every node, and between a node and a link passing it. */
  gap?: number
}

const ITERATIONS = 300
const SEPARATE_PASSES = 150
const AVOID_PASSES = 40
const MAX_ROUNDS = 20
// A node pushed off a link goes a bit further than just clear of it, so the next push to part two nodes does not
// put it straight back.
const AVOID_OVERSHOOT = 1.6
const GOLDEN_ANGLE = Math.PI * (3 - Math.sqrt(5))
// A group this small with this many links per node is drawn on a circle (see circleDenseGroups).
const DENSE_MAX_NODES = 40
const DENSE_LINKS_PER_NODE = 1.5

interface Graph {
  n: number
  x: Float64Array
  y: Float64Array
  hw: Float64Array  // half width / height of each node's box, gap included
  hh: Float64Array
  pinned: Uint8Array
  from: Int32Array  // link ends, as node indexes
  to: Int32Array
}

/**
 * `start`: where each node begins (the role-row layout); `fixed`: nodes placed by hand and saved, which never move.
 * Returns a position for every node.
 */
export function freeLayout(nodes: FreeNode[], edges: FreeEdge[], start: Record<string, Pos>,
                           fixed: Record<string, Pos> = {}, options: FreeOptions = {}): Record<string, Pos> {
  const spacing = options.spacing ?? 170
  const gap = options.gap ?? 18
  const index = new Map(nodes.map((node, i) => [node.id, i]))
  const n = nodes.length
  const g: Graph = {
    n,
    x: new Float64Array(n),
    y: new Float64Array(n),
    hw: new Float64Array(nodes.map((node) => (node.width + gap) / 2)),
    hh: new Float64Array(nodes.map((node) => (node.height + gap) / 2)),
    pinned: new Uint8Array(nodes.map((node) => (fixed[node.id] ? 1 : 0))),
    from: new Int32Array(0),
    to: new Int32Array(0),
  }
  nodes.forEach((node, i) => {
    const p = fixed[node.id] ?? start[node.id] ?? { x: i * spacing, y: 0 }
    // Role rows put whole rows on one line, and forces between nodes on one line stay on it: nudge each free node
    // off its start along a golden-angle spiral (fixed, not random) so the drawing can open up in two dimensions.
    const nudge = fixed[node.id] ? 0 : spacing * 0.35
    g.x[i] = p.x + Math.cos(i * GOLDEN_ANGLE) * nudge
    g.y[i] = p.y + Math.sin(i * GOLDEN_ANGLE) * nudge
  })
  const links = edges
    .map((e) => [index.get(e.source), index.get(e.target)] as const)
    .filter((pair): pair is readonly [number, number] => pair[0] !== undefined && pair[1] !== undefined && pair[0] !== pair[1])
  g.from = new Int32Array(links.map((pair) => pair[0]))
  g.to = new Int32Array(links.map((pair) => pair[1]))

  // Groups that are not linked to each other (an OLT pair, a lone switch) only push one another away and drift to
  // the edge, shrinking the whole drawing to fit them. Each group is laid out on its own, then the groups are packed
  // side by side in rows, the biggest first.
  const group = components(g)
  forceDirected(g, spacing, group)
  circleDenseGroups(g, group, spacing)
  pack(g, group, spacing)
  // Taking a node off a link can push it onto another node and parting two nodes can put one on a link: alternate
  // until a round moves nothing off any link, right after the nodes were parted, so both rules hold at the end.
  for (let round = 0; round < MAX_ROUNDS; round++) {
    separate(g)
    if (!avoidLinks(g)) break
  }
  separate(g)
  return Object.fromEntries(nodes.map((node, i) => [node.id, { x: g.x[i], y: g.y[i] }]))
}

/**
 * A small group where nearly everything links to everything (the areas of Indonesia, a full mesh of PEs) has no
 * force-directed drawing free of links through nodes. On a circle every link is a chord and none passes a node:
 * such a group is put on one, in the angular order the forces gave it so linked neighbours stay close.
 */
function circleDenseGroups(g: Graph, group: Int32Array, spacing: number) {
  const members = new Map<number, number[]>()
  for (let i = 0; i < g.n; i++) members.set(group[i], [...(members.get(group[i]) ?? []), i])
  const linksOf = new Map<number, number>()
  for (let e = 0; e < g.from.length; e++) linksOf.set(group[g.from[e]], (linksOf.get(group[g.from[e]]) ?? 0) + 1)
  for (const [c, ids] of members) {
    const n = ids.length
    if (n < 4 || n > DENSE_MAX_NODES || (linksOf.get(c) ?? 0) < n * DENSE_LINKS_PER_NODE || ids.some((i) => g.pinned[i])) continue
    const cx = ids.reduce((s, i) => s + g.x[i], 0) / n
    const cy = ids.reduce((s, i) => s + g.y[i], 0) / n
    const order = [...ids].sort((a, b) => Math.atan2(g.y[a] - cy, g.x[a] - cx) - Math.atan2(g.y[b] - cy, g.x[b] - cx))
    // Neighbouring nodes on the circle must not touch: the circumference holds every box plus a spacing's share.
    const around = ids.reduce((s, i) => s + Math.max(g.hw[i], g.hh[i]) * 2 + spacing / 3, 0)
    const radius = Math.max(spacing, around / (2 * Math.PI))
    order.forEach((i, k) => {
      const angle = (2 * Math.PI * k) / n - Math.PI / 2
      g.x[i] = cx + Math.cos(angle) * radius
      g.y[i] = cy + Math.sin(angle) * radius
    })
  }
}

/** Connected component of every node (index of its group), linked nodes sharing one. */
function components(g: Graph): Int32Array {
  const parent = Int32Array.from({ length: g.n }, (_, i) => i)
  const root = (i: number): number => {
    while (parent[i] !== i) {
      parent[i] = parent[parent[i]]
      i = parent[i]
    }
    return i
  }
  for (let e = 0; e < g.from.length; e++) parent[root(g.from[e])] = root(g.to[e])
  const group = new Int32Array(g.n)
  for (let i = 0; i < g.n; i++) group[i] = root(i)
  return group
}

/**
 * Shelf packing of the groups: rows about as wide as the drawing is tall, biggest group first, `spacing` apart.
 * A group holding a node placed by hand stays where it is; the others are packed under it.
 */
function pack(g: Graph, group: Int32Array, spacing: number) {
  const members = new Map<number, number[]>()
  for (let i = 0; i < g.n; i++) members.set(group[i], [...(members.get(group[i]) ?? []), i])
  const boxes = [...members.values()].map((ids) => {
    let left = Infinity
    let right = -Infinity
    let top = Infinity
    let bottom = -Infinity
    for (const i of ids) {
      left = Math.min(left, g.x[i] - g.hw[i])
      right = Math.max(right, g.x[i] + g.hw[i])
      top = Math.min(top, g.y[i] - g.hh[i])
      bottom = Math.max(bottom, g.y[i] + g.hh[i])
    }
    return { ids, left, top, width: right - left, height: bottom - top, held: ids.some((i) => g.pinned[i]) }
  })
  if (boxes.length < 2) return
  const held = boxes.filter((b) => b.held)
  const free = boxes.filter((b) => !b.held).sort((a, b) => b.width * b.height - a.width * a.height || a.ids[0] - b.ids[0])
  const area = boxes.reduce((sum, b) => sum + (b.width + spacing) * (b.height + spacing), 0)
  const rowWidth = Math.max(Math.sqrt(area) * 1.3, ...boxes.map((b) => b.width))
  // Start under whatever is held in place, else at the origin.
  const originX = held.length ? Math.min(...held.map((b) => b.left)) : 0
  let y = held.length ? Math.max(...held.map((b) => b.top + b.height)) + spacing : 0
  let x = originX
  let rowHeight = 0
  for (const b of free) {
    if (x > originX && x + b.width > originX + rowWidth) {
      x = originX
      y += rowHeight + spacing
      rowHeight = 0
    }
    const dx = x - b.left
    const dy = y - b.top
    for (const i of b.ids) {
      g.x[i] += dx
      g.y[i] += dy
    }
    x += b.width + spacing
    rowHeight = Math.max(rowHeight, b.height)
  }
}

/** Force-directed layout; nodes only push nodes of their own group (`group`), and are pulled to its middle. */
function forceDirected(g: Graph, k: number, group: Int32Array) {
  const { n, x, y, pinned, from, to } = g
  if (n < 2) return
  let minX = Infinity
  let maxX = -Infinity
  const sumX = new Float64Array(n)
  const sumY = new Float64Array(n)
  const count = new Float64Array(n)
  for (let i = 0; i < n; i++) {
    minX = Math.min(minX, x[i])
    maxX = Math.max(maxX, x[i])
  }
  // Cool from a tenth of the drawing's width down to almost nothing: big moves first, then fine tuning.
  let temperature = Math.max(k, (maxX - minX) / 10)
  const cooling = Math.pow(1 / temperature, 1 / ITERATIONS)
  const gravity = k / 500  // a little pull to the group's middle keeps it in one piece
  const sx = new Float64Array(n)
  const sy = new Float64Array(n)
  const k2 = k * k
  for (let iteration = 0; iteration < ITERATIONS; iteration++) {
    sx.fill(0)
    sy.fill(0)
    sumX.fill(0)
    sumY.fill(0)
    count.fill(0)
    for (let i = 0; i < n; i++) {
      sumX[group[i]] += x[i]
      sumY[group[i]] += y[i]
      count[group[i]] += 1
    }
    for (let i = 0; i < n; i++) {
      for (let j = i + 1; j < n; j++) {
        if (group[i] !== group[j]) continue
        let dx = x[i] - x[j]
        let dy = y[i] - y[j]
        if (dx === 0 && dy === 0) {
          dx = (i - j) * 0.1  // two nodes on one spot: part them along a fixed direction, no randomness
          dy = 0.1
        }
        const d2 = dx * dx + dy * dy
        const f = k2 / d2  // (k² / d) / d: repulsion along the unit vector
        sx[i] += dx * f
        sy[i] += dy * f
        sx[j] -= dx * f
        sy[j] -= dy * f
      }
    }
    for (let e = 0; e < from.length; e++) {
      const a = from[e]
      const b = to[e]
      const dx = x[a] - x[b]
      const dy = y[a] - y[b]
      const f = Math.sqrt(dx * dx + dy * dy) / k  // (d² / k) / d
      sx[a] -= dx * f
      sy[a] -= dy * f
      sx[b] += dx * f
      sy[b] += dy * f
    }
    for (let i = 0; i < n; i++) {
      if (pinned[i]) continue
      const c = group[i]
      const fx = sx[i] + (sumX[c] / count[c] - x[i]) * gravity
      const fy = sy[i] + (sumY[c] / count[c] - y[i]) * gravity
      const length = Math.sqrt(fx * fx + fy * fy)
      if (length > 0) {
        const step = Math.min(length, temperature) / length
        x[i] += fx * step
        y[i] += fy * step
      }
    }
    temperature *= cooling
  }
}

/**
 * No two node boxes overlap: each overlapping pair moves apart along the axis where it overlaps least, a pinned
 * node taking none of the push.
 */
function separate(g: Graph) {
  const { n, x, y, hw, hh, pinned } = g
  for (let pass = 0; pass < SEPARATE_PASSES; pass++) {
    let moved = false
    for (let i = 0; i < n; i++) {
      for (let j = i + 1; j < n; j++) {
        const dx = x[j] - x[i]
        const dy = y[j] - y[i]
        const overlapX = hw[i] + hw[j] - Math.abs(dx)
        if (overlapX <= 0) continue
        const overlapY = hh[i] + hh[j] - Math.abs(dy)
        if (overlapY <= 0 || (pinned[i] && pinned[j])) continue
        const shareI = pinned[i] ? 0 : pinned[j] ? 1 : 0.5
        const shareJ = 1 - shareI
        if (overlapY <= overlapX) {
          const push = overlapY + 0.5
          const dir = dy === 0 ? (i % 2 ? 1 : -1) : Math.sign(dy)
          y[i] -= dir * push * shareI
          y[j] += dir * push * shareJ
        } else {
          const push = overlapX + 0.5
          const dir = dx === 0 ? (j % 2 ? 1 : -1) : Math.sign(dx)
          x[i] -= dir * push * shareI
          x[j] += dir * push * shareJ
        }
        moved = true
      }
    }
    if (!moved) return
  }
}

/**
 * Push every node a link runs through off that link, perpendicular to it. Returns whether anything moved.
 * A node only meets the links of other nodes here: its own links end at it.
 */
function avoidLinks(g: Graph): boolean {
  const { n, x, y, hw, hh, pinned, from, to } = g
  let movedAny = false
  for (let pass = 0; pass < AVOID_PASSES; pass++) {
    let moved = false
    for (let e = 0; e < from.length; e++) {
      const a = from[e]
      const b = to[e]
      // The link's bounding box, grown by the largest box: nodes outside it cannot touch the link.
      const left = Math.min(x[a], x[b])
      const right = Math.max(x[a], x[b])
      const top = Math.min(y[a], y[b])
      const bottom = Math.max(y[a], y[b])
      for (let i = 0; i < n; i++) {
        if (i === a || i === b || pinned[i]) continue
        if (x[i] + hw[i] < left || x[i] - hw[i] > right || y[i] + hh[i] < top || y[i] - hh[i] > bottom) continue
        const hit = crossing({ x: x[a], y: y[a] }, { x: x[b], y: y[b] }, { x: x[i], y: y[i] }, hw[i], hh[i])
        if (!hit) continue
        x[i] += hit.x * AVOID_OVERSHOOT
        y[i] += hit.y * AVOID_OVERSHOOT
        moved = true
      }
    }
    if (!moved) break
    movedAny = true
  }
  return movedAny
}

/**
 * If segment a–b passes through the box of half sizes hw×hh around p, the smallest move of p, perpendicular to the
 * segment, that clears it; else null.
 */
export function crossing(a: Pos, b: Pos, p: Pos, hw: number, hh: number): Pos | null {
  const dx = b.x - a.x
  const dy = b.y - a.y
  const length2 = dx * dx + dy * dy
  if (length2 === 0) return null
  const t = ((p.x - a.x) * dx + (p.y - a.y) * dy) / length2
  if (t <= 0 || t >= 1) return null
  const cx = a.x + t * dx
  const cy = a.y + t * dy
  // How far the box reaches towards the segment's normal: its support along that direction.
  const length = Math.sqrt(length2)
  const nx = -dy / length
  const ny = dx / length
  const reach = Math.abs(nx) * hw + Math.abs(ny) * hh
  const offset = (p.x - cx) * nx + (p.y - cy) * ny
  if (Math.abs(offset) >= reach) return null
  const side = offset === 0 ? 1 : Math.sign(offset)
  const push = reach - Math.abs(offset) + 1
  return { x: nx * side * push, y: ny * side * push }
}
