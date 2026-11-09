import { describe, expect, it } from 'vitest'
import { isProblemLink, layerOf, linkVisible, NO_FILTER, nodeVisible, vendorsIn, type CityFilter } from './cityFilters'
import type { CityNode } from './types'

const filter = (overrides: Partial<CityFilter>): CityFilter => ({ ...NO_FILTER, ...overrides })
const device = { kind: 'device', vendor: 'Huawei', tier: 1, health: 'up' } as const
const okLink = { status: 'up', capacity_gbps: 10, color: 'green' } as const

describe('nodeVisible', () => {
  it('should show every node when no filter is set', () => {
    expect(nodeVisible(device, false, NO_FILTER)).toBe(true)
  })
  it('should hide a device whose vendor is not selected', () => {
    expect(nodeVisible(device, false, filter({ vendors: new Set(['Cisco']) }))).toBe(false)
  })
  it('should match a device without vendor against "Tanpa vendor"', () => {
    expect(nodeVisible({ ...device, vendor: null }, false, filter({ vendors: new Set(['Tanpa vendor']) }))).toBe(true)
  })
  it('should hide a device outside the selected layers', () => {
    expect(nodeVisible(device, false, filter({ layers: new Set([0]) }))).toBe(false)
  })
  it('should treat an unknown tier as layer 4', () => {
    expect(layerOf(9)).toBe(4)
    expect(nodeVisible({ ...device, tier: 9 }, false, filter({ layers: new Set([4]) }))).toBe(true)
  })
  it('should hide a healthy device without problem link when only problems are shown', () => {
    expect(nodeVisible(device, false, filter({ problemsOnly: true }))).toBe(false)
  })
  it('should keep a down device when only problems are shown', () => {
    expect(nodeVisible({ ...device, health: 'down' }, false, filter({ problemsOnly: true }))).toBe(true)
  })
  it('should keep an unknown-reachability device that has a problem link', () => {
    expect(nodeVisible({ ...device, health: 'unknown' }, true, filter({ problemsOnly: true }))).toBe(true)
  })
  it('should keep compounds visible whatever the filter', () => {
    expect(nodeVisible({ kind: 'pop_lane', health: 'unknown' }, false, filter({ problemsOnly: true, vendors: new Set(['X']) }))).toBe(true)
  })
})

describe('linkVisible', () => {
  it('should hide an up green link when only problems are shown', () => {
    expect(linkVisible(okLink, filter({ problemsOnly: true }))).toBe(false)
  })
  it('should show a down link when only problems are shown', () => {
    expect(linkVisible({ ...okLink, status: 'down' }, filter({ problemsOnly: true }))).toBe(true)
  })
  it('should show every link when problems are not isolated', () => {
    expect(linkVisible(okLink, NO_FILTER)).toBe(true)
  })
})

describe('isProblemLink', () => {
  it('should not flag a red link of unknown capacity', () => {
    expect(isProblemLink({ status: 'up', capacity_gbps: 0, color: 'red' })).toBe(false)
  })
  it('should flag a yellow link of known capacity', () => {
    expect(isProblemLink({ ...okLink, color: 'yellow' })).toBe(true)
  })
})

describe('vendorsIn', () => {
  it('should list distinct vendors with "Tanpa vendor" last', () => {
    const nodes = [
      { kind: 'device', vendor: null }, { kind: 'device', vendor: 'Huawei' }, { kind: 'device', vendor: 'Cisco' }, { kind: 'device', vendor: 'Cisco' },
      { kind: 'summary' },
    ] as unknown as CityNode[]
    expect(vendorsIn({ nodes })).toEqual(['Cisco', 'Huawei', 'Tanpa vendor'])
  })
})
