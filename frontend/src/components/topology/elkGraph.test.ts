import { describe, expect, it } from 'vitest'
import type { ElkNode } from 'elkjs/lib/elk-api'
import { buildElkGraph, ELK_OPTIONS } from './elkGraph'
import type { CityCluster, CityDeviceNode, CityLink, CityNode, CityView } from './types'

const sizeOf = () => ({ width: 100, height: 30 })

function device(id: number, pop: string | null, tier: number, clusterId: string | null = null): CityDeviceNode {
  return {
    id: `dev:${id}`, kind: 'device', device_id: id, label: `D${id}`, pop, role: null, tier, vendor: null,
    status: 'active', x: null, y: null, center: false, mgmt_ip: null, city: null, reachability: 'up',
    uptime_s: null, last_polled_at: null, cluster_id: clusterId, spof: false,
  }
}

function link(id: number, source: string, target: string): CityLink {
  return {
    id, source, target, a_device: 'A', a_interface: 'x', b_device: 'B', b_interface: 'y', capacity_gbps: 10,
    current_gbps: 1, utilization_pct: 10, committed_pct: 10, color: 'green', status: 'up', segment: 'backbone',
    spof: false,
  }
}

function view(
  nodes: CityNode[],
  links: CityLink[] = [],
  clusters: CityCluster[] = [],
  level: CityView['level'] = 'city',
): CityView {
  const popCodes = [...new Set(nodes.flatMap((n) => ((n.kind === 'device' || n.kind === 'summary') && n.pop ? [n.pop] : [])))]
  return {
    level, nodes, links, clusters, edges: [], segments: {}, layout_key: 'k',
    pops: popCodes.map((code) => ({ code, name: code, city: code, device_count: 1 })),
  }
}

function find(root: ElkNode, id: string): ElkNode | undefined {
  if (root.id === id) return root
  for (const child of root.children ?? []) {
    const hit = find(child, id)
    if (hit) return hit
  }
  return undefined
}

function parentOf(root: ElkNode, id: string): ElkNode | undefined {
  if ((root.children ?? []).some((c) => c.id === id)) return root
  for (const child of root.children ?? []) {
    const hit = parentOf(child, id)
    if (hit) return hit
  }
  return undefined
}

describe('buildElkGraph', () => {
  it('should put every link as its own edge when links are parallel', () => {
    const graph = buildElkGraph(view([device(1, 'P', 0), device(2, 'P', 1)], [link(1, 'dev:1', 'dev:2'), link(2, 'dev:1', 'dev:2')]), sizeOf)
    expect(graph.edges?.map((e) => e.id)).toEqual(['link:1', 'link:2'])
    expect(graph.edges?.[0].sources).toEqual(['dev:1'])
    expect(graph.edges?.[0].targets).toEqual(['dev:2'])
  })

  it('should nest device in pop and cluster when device has cluster_id', () => {
    const graph = buildElkGraph(
      view([device(1, 'P', 2, 'c:1'), device(2, 'P', 2, 'c:1'), device(3, 'P', 2)], [], [{ id: 'c:1', kind: 'pair', pop: 'P', device_ids: [1, 2] }]),
      sizeOf,
    )
    expect(parentOf(graph, 'dev:1')?.id).toBe('cluster:c:1')
    expect(parentOf(graph, 'cluster:c:1')?.id).toBe('pop:P')
    expect(parentOf(graph, 'dev:3')?.id).toBe('pop:P')
    expect(parentOf(graph, 'pop:P')?.id).toBe('city')
    expect(parentOf(graph, 'city')?.id).toBe(graph.id)
  })

  it('should place cluster under city when cluster pop is null', () => {
    const graph = buildElkGraph(
      view([device(1, 'P', 2, 'c:1'), device(2, 'Q', 2, 'c:1')], [], [{ id: 'c:1', kind: 'ring', pop: null, device_ids: [1, 2] }]),
      sizeOf,
    )
    expect(parentOf(graph, 'cluster:c:1')?.id).toBe('city')
    expect(parentOf(graph, 'dev:1')?.id).toBe('cluster:c:1')
  })

  it('should skip link when endpoint not in nodes', () => {
    const graph = buildElkGraph(view([device(1, 'P', 0)], [link(1, 'dev:1', 'dev:99')]), sizeOf)
    expect(graph.edges).toEqual([])
  })

  it('should skip link when source equals target', () => {
    const graph = buildElkGraph(view([device(1, 'P', 0)], [link(1, 'dev:1', 'dev:1')]), sizeOf)
    expect(graph.edges).toEqual([])
  })

  it('should build graph without edges when links empty', () => {
    const graph = buildElkGraph(view([device(1, 'P', 0)]), sizeOf)
    expect(graph.edges).toEqual([])
    expect(find(graph, 'dev:1')).toBeDefined()
  })

  it('should put external pop in top partition when node is external_pop', () => {
    const external: CityNode = { id: 'pop:KBM', kind: 'external_pop', code: 'KBM', devices: 1, label: 'Kebumen' }
    const graph = buildElkGraph(view([external, device(1, 'P', 0), device(2, 'P', 3)]), sizeOf)
    const partitionOf = (id: string) => find(graph, id)?.layoutOptions?.['elk.partitioning.partition']
    expect(partitionOf('pop:KBM')).toBe('0')
    expect(partitionOf('dev:1')).toBe('1')
    expect(partitionOf('dev:2')).toBe('4')
  })

  it('should omit city compound when level is neighbors', () => {
    const graph = buildElkGraph(view([device(1, 'P', 0)], [], [], 'neighbors'), sizeOf)
    expect(find(graph, 'city')).toBeUndefined()
    expect(parentOf(graph, 'pop:P')?.id).toBe(graph.id)
  })

  it('should use the size callback and layout options', () => {
    const graph = buildElkGraph(view([device(1, 'P', 0)]), () => ({ width: 77, height: 22 }))
    expect(find(graph, 'dev:1')).toMatchObject({ width: 77, height: 22 })
    expect(graph.layoutOptions).toMatchObject(ELK_OPTIONS)
  })
})
