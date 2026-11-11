import { describe, expect, it } from 'vitest'
import { crossing, freeLayout, type FreeEdge, type FreeNode, type Pos } from './freeLayout'

const node = (id: string): FreeNode => ({ id, width: 110, height: 60 })

function overlaps(a: Pos, b: Pos, w: number, h: number): boolean {
  return Math.abs(a.x - b.x) < w && Math.abs(a.y - b.y) < h
}

/** A hub with twelve spokes, two of them chained further, all starting stacked on one row (like a crowded map). */
function crowded(): { nodes: FreeNode[]; edges: FreeEdge[]; start: Record<string, Pos> } {
  const nodes = [node('hub'), ...Array.from({ length: 12 }, (_, i) => node(`s${i}`)), node('far0'), node('far1')]
  const edges = [...Array.from({ length: 12 }, (_, i) => ({ source: 'hub', target: `s${i}` })),
    { source: 's0', target: 'far0' }, { source: 's1', target: 'far1' }, { source: 'far0', target: 'far1' }]
  const start = Object.fromEntries(nodes.map((n, i) => [n.id, { x: i * 8, y: 0 }]))
  return { nodes, edges, start }
}

describe('freeLayout', () => {
  it('should leave no two nodes overlapping when they all start piled up', () => {
    const { nodes, edges, start } = crowded()
    const pos = freeLayout(nodes, edges, start)
    const ids = nodes.map((n) => n.id)
    const clashes = ids.flatMap((a, i) => ids.slice(i + 1).filter((b) => overlaps(pos[a], pos[b], 110, 60)))
    expect(clashes).toEqual([])
  })

  it('should route no link through a node it does not belong to', () => {
    const { nodes, edges, start } = crowded()
    const pos = freeLayout(nodes, edges, start)
    const through = edges.flatMap((e) => nodes
      .filter((n) => n.id !== e.source && n.id !== e.target)
      .filter((n) => crossing(pos[e.source], pos[e.target], pos[n.id], n.width / 2, n.height / 2) !== null)
      .map((n) => `${e.source}-${e.target} via ${n.id}`))
    expect(through).toEqual([])
  })

  it('should keep a node placed by hand exactly where it was put', () => {
    const { nodes, edges, start } = crowded()
    const pos = freeLayout(nodes, edges, start, { hub: { x: 500, y: -300 } })
    expect(pos.hub).toEqual({ x: 500, y: -300 })
  })

  it('should draw the same picture every time for the same input', () => {
    const { nodes, edges, start } = crowded()
    expect(freeLayout(nodes, edges, start)).toEqual(freeLayout(nodes, edges, start))
  })
})

describe('crossing', () => {
  it('should push a node off a link that runs through its middle', () => {
    expect(crossing({ x: -100, y: 0 }, { x: 100, y: 0 }, { x: 0, y: 5 }, 20, 10)).toEqual({ x: -0, y: 6 })
  })

  it('should leave a node alone when the link passes beside it', () => {
    expect(crossing({ x: -100, y: 0 }, { x: 100, y: 0 }, { x: 0, y: 40 }, 20, 10)).toBeNull()
  })
})

describe('freeLayout on a large view', () => {
  it('should finish 400 nodes and 800 links within a few seconds', () => {
    const nodes = Array.from({ length: 400 }, (_, i) => node(`n${i}`))
    const edges = nodes.flatMap((n, i) => [{ source: n.id, target: `n${(i + 1) % 400}` }, { source: n.id, target: `n${(i * 7 + 3) % 400}` }])
    const start = Object.fromEntries(nodes.map((n, i) => [n.id, { x: (i % 20) * 120, y: Math.floor(i / 20) * 90 }]))
    const began = performance.now()
    freeLayout(nodes, edges, start)
    expect(performance.now() - began).toBeLessThan(5000)  // runs in a worker: this only guards against a blow-up
  })
})

describe('freeLayout with unlinked nodes', () => {
  it('should pack nodes without a link next to the linked ones instead of letting them drift away', () => {
    const { nodes, edges, start } = crowded()
    const loners = Array.from({ length: 5 }, (_, i) => node(`alone${i}`))
    const pos = freeLayout([...nodes, ...loners], edges,
                           { ...start, ...Object.fromEntries(loners.map((n, i) => [n.id, { x: 5000 + i, y: -4000 }])) })
    const xs = nodes.map((n) => pos[n.id].x)
    const ys = nodes.map((n) => pos[n.id].y)
    const reach = 2 * 170 + 110  // one packing gap and a node beyond the linked group's box, with room to spare
    const near = (p: Pos) => p.x > Math.min(...xs) - reach && p.x < Math.max(...xs) + reach * 3
      && p.y > Math.min(...ys) - reach && p.y < Math.max(...ys) + reach * 3
    expect(loners.filter((n) => !near(pos[n.id])).map((n) => n.id)).toEqual([])
  })
})

describe('freeLayout on a full mesh', () => {
  it('should run no link through a node when every node links to every other', () => {
    const nodes = Array.from({ length: 11 }, (_, i) => node(`area${i}`))
    const edges = nodes.flatMap((a, i) => nodes.slice(i + 1).map((b) => ({ source: a.id, target: b.id })))
    const pos = freeLayout(nodes, edges, Object.fromEntries(nodes.map((n, i) => [n.id, { x: i * 270, y: i % 2 ? 230 : 0 }])))
    const through = edges.filter((e) => nodes.some((n) => n.id !== e.source && n.id !== e.target
      && crossing(pos[e.source], pos[e.target], pos[n.id], n.width / 2, n.height / 2) !== null))
    expect(through).toEqual([])
  })
})
