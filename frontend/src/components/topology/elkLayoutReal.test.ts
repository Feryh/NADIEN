import { describe, expect, it } from 'vitest'
import ELK from 'elkjs/lib/elk.bundled.js'
import { buildElkGraph } from './elkGraph'
import { toCytoscape } from './elkToCytoscape'
import type { CityLink, CityNode, CityView } from './types'

const WIDTH = 90
const HEIGHT = 28

function device(id: number, pop: string, tier: number, clusterId: string | null = null): CityNode {
  return {
    id: `dev:${id}`, kind: 'device', device_id: id, label: `D${id}`, pop, site: 'site:S', role: 'pe', category: 'core',
    tier, vendor: null, status: 'active', x: null, y: null, center: false, mgmt_ip: null, city: null,
    reachability: 'up', uptime_s: null, last_polled_at: null, cluster_id: clusterId, spof: false,
  }
}

function link(id: number, source: string, target: string): CityLink {
  return {
    id, source, target, a_device: '', a_interface: null, b_device: '', b_interface: null, capacity_gbps: 1,
    current_gbps: 1, utilization_pct: 1, committed_pct: 1, color: 'green', status: 'up', segment: 'backbone', spof: false,
  }
}

const view: CityView = {
  level: 'city',
  nodes: [
    device(1, 'P', 0), device(2, 'P', 0), device(3, 'P', 2, 'c:3'), device(4, 'P', 2, 'c:3'), device(5, 'Q', 3),
    { id: 'pop:X', kind: 'external_pop', code: 'X', devices: 1, label: 'X' },
  ],
  pops: [{ code: 'P', name: 'P', city: 'P', device_count: 4 }, { code: 'Q', name: 'Q', city: 'Q', device_count: 1 }],
  clusters: [{ id: 'c:3', kind: 'pair', pop: 'P', device_ids: [3, 4] }],
  links: [
    link(1, 'dev:1', 'dev:2'), link(2, 'dev:1', 'dev:3'), link(3, 'dev:2', 'dev:4'), link(4, 'dev:3', 'dev:4'),
    link(5, 'dev:4', 'dev:5'), link(6, 'pop:X', 'dev:1'), link(7, 'dev:1', 'dev:3'),
  ],
  edges: [], segments: {}, layout_key: 'k', hidden_links: 0,
}

describe('real elkjs layout converted to Cytoscape', () => {
  it('should keep every endpoint on its node and weights finite when edges cross nested compounds', async () => {
    const laidOut = await new ELK().layout(buildElkGraph(view, () => ({ width: WIDTH, height: HEIGHT })))
    const { edges } = toCytoscape(laidOut)
    expect(Object.keys(edges)).toHaveLength(view.links.length)
    for (const route of Object.values(edges)) {
      for (const endpoint of [route.sourceEndpoint, route.targetEndpoint]) {
        const [dx, dy] = endpoint.split(' ').map((part) => parseFloat(part))
        expect(Math.abs(dx)).toBeLessThanOrEqual(WIDTH / 2 + 0.5)
        expect(Math.abs(dy)).toBeLessThanOrEqual(HEIGHT / 2 + 0.5)
      }
      expect([...route.weights, ...route.distances].every(Number.isFinite)).toBe(true)
    }
  })
})
