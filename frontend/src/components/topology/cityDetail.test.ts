import { describe, expect, it } from 'vitest'
import { bundleOf, clusterSummary, formatLastPoll, formatUptime, linkEnd } from './cityDetail'
import type { CityLink } from './types'

function link(id: number, source: string, target: string): CityLink {
  return {
    id, source, target, a_device: 'A', a_interface: null, b_device: 'B', b_interface: null, capacity_gbps: 10, current_gbps: 1,
    utilization_pct: 10, committed_pct: 10, color: 'green', status: 'up', segment: 'backbone', spof: false,
  }
}

describe('formatUptime', () => {
  it('should show hours and minutes when uptime is over an hour', () => {
    expect(formatUptime(90061)).toBe('25 h 1 m')
  })
  it('should show only minutes when uptime is under an hour', () => {
    expect(formatUptime(125)).toBe('2 m')
  })
  it('should show a dash when uptime is unknown', () => {
    expect(formatUptime(null)).toBe('–')
  })
})

describe('formatLastPoll', () => {
  it('should show a dash when there was no poll', () => {
    expect(formatLastPoll(null)).toBe('–')
  })
  it('should show a dash when the timestamp is not a date', () => {
    expect(formatLastPoll('bukan tanggal')).toBe('–')
  })
  it('should format with the id-ID locale when the timestamp is valid', () => {
    const iso = '2026-11-09T03:04:05Z'
    expect(formatLastPoll(iso)).toBe(new Date(iso).toLocaleString('id-ID'))
  })
})

describe('bundleOf', () => {
  const links = [link(1, 'dev:1', 'dev:2'), link(2, 'dev:2', 'dev:1'), link(3, 'dev:1', 'dev:3')]
  it('should list links of the same pair in either orientation when one is clicked', () => {
    expect(bundleOf(links, 1).map((l) => l.id)).toEqual([1, 2])
  })
  it('should leave out links to other nodes', () => {
    expect(bundleOf(links, 3).map((l) => l.id)).toEqual([3])
  })
  it('should be empty when the clicked link no longer exists', () => {
    expect(bundleOf(links, 99)).toEqual([])
  })
})

describe('linkEnd', () => {
  it('should join host and interface when the interface is known', () => {
    expect(linkEnd('R1', 'Gi0/1')).toBe('R1:Gi0/1')
  })
  it('should be the host alone when there is no interface record', () => {
    expect(linkEnd('R1', null)).toBe('R1')
  })
})

describe('clusterSummary', () => {
  it('should describe the cluster kind and size', () => {
    expect(clusterSummary([{ id: 'c:1', kind: 'ring', pop: 'P', device_ids: [1, 2, 3] }], 'c:1')).toBe('Ring · 3 perangkat')
  })
  it('should be a dash when the device is in no cluster', () => {
    expect(clusterSummary([], null)).toBe('–')
  })
})
