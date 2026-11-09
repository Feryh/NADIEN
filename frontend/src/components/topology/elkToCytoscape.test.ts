import { describe, expect, it } from 'vitest'
import type { ElkExtendedEdge, ElkNode } from 'elkjs/lib/elk-api'
import { toCytoscape, toSegments } from './elkToCytoscape'

describe('toSegments', () => {
  it('should project bends onto the source-target line when route has two bends', () => {
    const { weights, distances } = toSegments({ x: 0, y: 0 }, { x: 100, y: 0 }, [{ x: 0, y: 50 }, { x: 100, y: 50 }])
    expect(weights[0]).toBeCloseTo(0)
    expect(weights[1]).toBeCloseTo(1)
    expect(Math.abs(distances[0])).toBeCloseTo(50)
    expect(Math.abs(distances[1])).toBeCloseTo(50)
    expect(Math.sign(distances[0])).toBe(Math.sign(distances[1]))
  })

  it('should return finite values when source equals target', () => {
    const { weights, distances } = toSegments({ x: 5, y: 5 }, { x: 5, y: 5 }, [{ x: 5, y: 15 }])
    expect([...weights, ...distances].every(Number.isFinite)).toBe(true)
    expect(weights[0]).toBe(0)
    expect(distances[0]).toBeCloseTo(10)
  })
})

// group sits at (10,20); a and b are 100x20 nodes inside it, b below a.
function graph(bends: { x: number; y: number }[], container: 'root' | 'group' = 'root'): ElkNode {
  const offset = { x: 10, y: 20 }  // edge points are root-relative whichever node lists the edge
  const edge: ElkExtendedEdge = {
    id: 'link:1', sources: ['a'], targets: ['b'],
    sections: [{
      id: 's',
      startPoint: { x: offset.x + 50, y: offset.y + 20 },
      endPoint: { x: offset.x + 50, y: offset.y + 100 },
      bendPoints: bends.map((p) => ({ x: p.x + offset.x, y: p.y + offset.y })),
    }],
  }
  return {
    id: 'root',
    edges: container === 'root' ? [edge] : [],
    children: [
      {
        id: 'group', x: 10, y: 20, width: 300, height: 300,
        edges: container === 'group' ? [edge] : [],
        children: [
          { id: 'a', x: 0, y: 0, width: 100, height: 20 },
          { id: 'b', x: 0, y: 100, width: 100, height: 20 },
        ],
      },
    ],
  }
}

describe('toCytoscape', () => {
  it('should sum parent offsets when node is nested', () => {
    const { positions } = toCytoscape(graph([]))
    expect(positions.a).toEqual({ x: 60, y: 30 })
    expect(positions.b).toEqual({ x: 60, y: 130 })
  })

  it('should report straight curve when edge has no bends', () => {
    const route = toCytoscape(graph([])).edges['link:1']
    expect(route.curve).toBe('straight')
    expect(route.weights).toEqual([])
  })

  it('should report segments curve and endpoints relative to centers when edge has bends', () => {
    const route = toCytoscape(graph([{ x: 50, y: 60 }, { x: 80, y: 60 }])).edges['link:1']
    expect(route.curve).toBe('segments')
    expect(route.weights).toHaveLength(2)
    expect(route.sourceEndpoint).toBe('0px 10px')
    expect(route.targetEndpoint).toBe('0px -10px')
  })

  it('should treat edge points as root-relative when edge is listed on a nested node', () => {
    const route = toCytoscape(graph([], 'group')).edges['link:1']
    expect(route.sourceEndpoint).toBe('0px 10px')
    expect(route.targetEndpoint).toBe('0px -10px')
  })
})
