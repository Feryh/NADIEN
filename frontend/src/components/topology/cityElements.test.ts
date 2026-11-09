import { describe, expect, it } from 'vitest'
import type { ElementDefinition } from 'cytoscape'
import { buildCityElements, canUpdateInPlace, liveData, nodeStatus, sameShape } from './cityElements'
import type { CytoscapeLayout } from './elkToCytoscape'
import type { CityCluster, CityDeviceNode, CityLink, CityNode, CityView } from './types'

const options = { labelMode: 'full', colorMode: 'utilization' } as const

function device(id: number, pop: string, overrides: Partial<CityDeviceNode> = {}): CityDeviceNode {
  return {
    id: `dev:${id}`, kind: 'device', device_id: id, label: `${pop}-D${id}`, pop, site: 'site:S', role: 'pe', category: 'core', tier: 1,
    vendor: null, status: 'active', x: null, y: null, center: false, mgmt_ip: null, city: null, reachability: 'up',
    uptime_s: null, last_polled_at: null, cluster_id: null, spof: false, ...overrides,
  }
}

function link(id: number, source: string, target: string, overrides: Partial<CityLink> = {}): CityLink {
  return {
    id, source, target, a_device: 'A', a_interface: 'Gi0/1', b_device: 'B', b_interface: 'Gi0/2', capacity_gbps: 10,
    current_gbps: 1, utilization_pct: 10, committed_pct: 10, color: 'green', status: 'up', segment: 'backbone',
    spof: false, ...overrides,
  }
}

function view(nodes: CityNode[], links: CityLink[] = [], clusters: CityCluster[] = [], level: CityView['level'] = 'city'): CityView {
  return {
    level, nodes, links, clusters, edges: [], segments: {}, layout_key: 'k', regency: 'Kota X',
    pops: [{ code: 'P', name: 'Pusat', city: 'X', device_count: 2 }, { code: 'EMPTY', name: 'Kosong', city: 'X', device_count: 0 }],
  }
}

const byId = (elements: ElementDefinition[], id: string) => elements.find((e) => e.data.id === id)

describe('nodeStatus', () => {
  it('should be down when Observium reports the device down', () => {
    expect(nodeStatus(device(1, 'P', { reachability: 'down' }), [])).toBe('down')
  })

  it('should be warn when an up device has a link that is down', () => {
    expect(nodeStatus(device(1, 'P'), [link(1, 'dev:1', 'dev:2', { status: 'down' })])).toBe('warn')
  })

  it('should be warn when an up device has a red link', () => {
    expect(nodeStatus(device(1, 'P'), [link(1, 'dev:2', 'dev:1', { color: 'red' })])).toBe('warn')
  })

  it('should stay up when the only yellow link has no known capacity', () => {
    expect(nodeStatus(device(1, 'P'), [link(1, 'dev:1', 'dev:2', { color: 'yellow', capacity_gbps: 0 })])).toBe('up')
  })

  it('should ignore troubled links of other nodes', () => {
    expect(nodeStatus(device(1, 'P'), [link(1, 'dev:2', 'dev:3', { status: 'down' })])).toBe('up')
  })

  it('should be unknown when the node is not a device', () => {
    expect(nodeStatus({ id: 'summary:P', kind: 'summary', pop: 'P', label: '9 akses', devices: 9, tier: 3 }, [])).toBe('unknown')
  })
})

describe('buildCityElements', () => {
  it('should draw one edge per link when links are parallel', () => {
    const elements = buildCityElements(view([device(1, 'P'), device(2, 'P')], [link(1, 'dev:1', 'dev:2'), link(2, 'dev:1', 'dev:2')]), null, options)
    expect(elements.filter((e) => e.data.source).map((e) => e.data.id)).toEqual(['link:1', 'link:2'])
  })

  it('should skip a link when both ends are the same drawn node', () => {
    const summary: CityNode = { id: 'summary:P', kind: 'summary', pop: 'P', label: '2 akses', devices: 2, tier: 3 }
    const elements = buildCityElements(view([summary], [link(1, 'summary:P', 'summary:P')]), null, options)
    expect(elements.some((e) => e.data.source)).toBe(false)
  })

  it('should nest devices as the ELK graph does when a device is in a cluster', () => {
    const cluster: CityCluster = { id: 'c:1', kind: 'ring', pop: 'P', device_ids: [1, 2, 3] }
    const nodes = [1, 2, 3].map((id) => device(id, 'P', { cluster_id: 'c:1', tier: 2 }))
    const elements = buildCityElements(view([...nodes, device(4, 'P')], [], [cluster]), null, options)
    expect([byId(elements, 'dev:1')?.data.parent, byId(elements, 'cluster:c:1')?.data.parent, byId(elements, 'pop:P')?.data.parent,
      byId(elements, 'dev:4')?.data.parent]).toEqual(['cluster:c:1', 'pop:P', 'city', 'pop:P'])
  })

  it('should label compounds with the POP name and the cluster shape', () => {
    const cluster: CityCluster = { id: 'c:1', kind: 'pair', pop: 'P', device_ids: [1, 2] }
    const nodes = [1, 2].map((id) => device(id, 'P', { cluster_id: 'c:1' }))
    const elements = buildCityElements(view(nodes, [], [cluster]), null, options)
    expect([byId(elements, 'city')?.data.display, byId(elements, 'pop:P')?.data.display, byId(elements, 'cluster:c:1')?.data.display])
      .toEqual(['Kota X', 'POP P · Pusat', 'Redundan · 2'])
  })

  it('should leave out a POP compound when no node is drawn in it', () => {
    expect(byId(buildCityElements(view([device(1, 'P')]), null, options), 'pop:EMPTY')).toBeUndefined()
  })

  it('should not draw a city box when the view is neighbours', () => {
    const elements = buildCityElements(view([device(1, 'P')], [], [], 'neighbors'), null, options)
    expect([byId(elements, 'city'), byId(elements, 'pop:P')?.data.parent]).toEqual([undefined, undefined])
  })

  it('should carry the ELK route and position when a layout is given', () => {
    const layout: CytoscapeLayout = {
      positions: { 'dev:1': { x: 5, y: 6 }, 'dev:2': { x: 5, y: 106 } },
      edges: { 'link:1': { curve: 'segments', weights: [0.2, 0.8], distances: [10, 10], sourceEndpoint: '0px 15px', targetEndpoint: '0px -15px' } },
    }
    const elements = buildCityElements(view([device(1, 'P'), device(2, 'P')], [link(1, 'dev:1', 'dev:2')]), layout, options)
    const edge = byId(elements, 'link:1')
    expect([byId(elements, 'dev:1')?.position, edge?.data.curve, edge?.data.weights, edge?.data.sourceEndpoint])
      .toEqual([{ x: 5, y: 6 }, 'segments', '0.2 0.8', '0px 15px'])
  })

  it('should draw a link grey when its capacity is unknown', () => {
    const elements = buildCityElements(view([device(1, 'P'), device(2, 'P')], [link(1, 'dev:1', 'dev:2', { capacity_gbps: 0, color: 'red' })]), null, options)
    expect(byId(elements, 'link:1')?.data.colorHex).toBe('#94a3b8')
  })

  it('should not include route fields when built without a layout', () => {
    const elements = buildCityElements(view([device(1, 'P'), device(2, 'P')], [link(1, 'dev:1', 'dev:2')]), null, options)
    expect('curve' in (byId(elements, 'link:1')?.data ?? {})).toBe(false)
  })
})

describe('canUpdateInPlace', () => {
  const ready = { elkReady: true, onMap: false }
  const base = () => view([device(1, 'P'), device(2, 'P')], [link(1, 'dev:1', 'dev:2')])

  it('should update in place when only link status changed on a laid-out city', () => {
    const next = view([device(1, 'P'), device(2, 'P')], [link(1, 'dev:1', 'dev:2', { status: 'down' })])
    expect(canUpdateInPlace(base(), next, ready)).toBe(true)
  })

  it('should not update in place when the layout has not finished', () => {
    expect(canUpdateInPlace(base(), base(), { elkReady: false, onMap: false })).toBe(false)
  })

  it('should not update in place when the canvas is the map', () => {
    expect(canUpdateInPlace(base(), base(), { elkReady: true, onMap: true })).toBe(false)
  })

  it('should not update in place when the level changed', () => {
    expect(canUpdateInPlace(base(), { ...base(), level: 'neighbors' }, ready)).toBe(false)
  })
})

describe('liveData', () => {
  it('should drop identity and structure fields when updating an element in place', () => {
    expect(liveData({ data: { id: 'link:1', source: 'a', target: 'b', parent: 'p', colorHex: '#fff' } })).toEqual({ colorHex: '#fff' })
  })
})

describe('sameShape', () => {
  it('should be true when nodes and links are the same in another order', () => {
    const before = view([device(1, 'P'), device(2, 'P')], [link(1, 'dev:1', 'dev:2'), link(2, 'dev:1', 'dev:2')])
    const after = view([device(2, 'P', { reachability: 'down' }), device(1, 'P')], [link(2, 'dev:1', 'dev:2'), link(1, 'dev:1', 'dev:2', { status: 'down' })])
    expect(sameShape(before, after)).toBe(true)
  })

  it('should report different shape when a device moves to another cluster', () => {
    const clusters: CityCluster[] = [
      { id: 'c:1', kind: 'pair', pop: 'P', device_ids: [1, 2] }, { id: 'c:3', kind: 'pair', pop: 'P', device_ids: [3, 4] },
    ]
    const nodes = (moved: string) => [device(1, 'P', { cluster_id: 'c:1' }), device(2, 'P', { cluster_id: moved }),
      device(3, 'P', { cluster_id: 'c:3' }), device(4, 'P', { cluster_id: 'c:3' })]
    expect(sameShape(view(nodes('c:1'), [], clusters), view(nodes('c:3'), [], clusters))).toBe(false)
  })

  it('should report different shape when a link endpoint changes', () => {
    const nodes = [device(1, 'P'), device(2, 'P'), device(3, 'P')]
    expect(sameShape(view(nodes, [link(1, 'dev:1', 'dev:2')]), view(nodes, [link(1, 'dev:1', 'dev:3')]))).toBe(false)
  })

  it('should be false when a link record was added', () => {
    const before = view([device(1, 'P'), device(2, 'P')], [link(1, 'dev:1', 'dev:2')])
    const after = view([device(1, 'P'), device(2, 'P')], [link(1, 'dev:1', 'dev:2'), link(2, 'dev:1', 'dev:2')])
    expect(sameShape(before, after)).toBe(false)
  })
})
