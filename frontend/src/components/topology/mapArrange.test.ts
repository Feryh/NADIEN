import { describe, expect, it } from 'vitest'
import { project } from '../basemap'
import { MAP_KEY, MAP_KEY_ELK, mapKeyFor, mapOverridePositions, mapViewKey, storedMapOn } from './mapArrange'

const storage = (values: Record<string, string>) => (key: string) => values[key] ?? null

describe('storedMapOn', () => {
  it('should default the map off when the level is drawn by ELK', () => {
    expect(storedMapOn(true, storage({ [MAP_KEY]: '1' }))).toBe(false)
  })

  it('should default the map on when the level is not drawn by ELK', () => {
    expect(storedMapOn(false, storage({}))).toBe(true)
  })

  it('should follow its own key when the ELK levels map choice was stored', () => {
    expect(storedMapOn(true, storage({ [MAP_KEY_ELK]: '1', [MAP_KEY]: '0' }))).toBe(true)
  })

  it('should fall back to the default when storage is blocked', () => {
    expect(storedMapOn(true, () => { throw new Error('blocked') })).toBe(false)
  })
})

describe('mapKeyFor', () => {
  it('should use a separate key when the level is drawn by ELK', () => {
    expect([mapKeyFor(true), mapKeyFor(false)]).toEqual(['nadien.topology.map.elk', 'nadien.topology.map'])
  })
})

describe('mapViewKey', () => {
  it('should prefix the layout key with map: when saving map positions', () => {
    expect(mapViewKey('device:JKT')).toBe('map:device:JKT')
  })
})

describe('mapOverridePositions', () => {
  const k = 3000
  it('should store dragged devices as lat/lon when they moved from their saved spot', () => {
    const dragged = project(-6.25, 106.85, k)
    const result = mapOverridePositions(
      [{ id: 'dev:1', pos: dragged }, { id: 'dev:2', pos: { x: 10, y: 10 } }],
      new Map([['dev:1', { x: 0, y: 0 }], ['dev:2', { x: 10, y: 10 }]]),
      {}, k,
    )
    expect(Object.keys(result)).toEqual(['dev:1'])
    expect(result['dev:1'].lat).toBeCloseTo(-6.25, 6)
    expect(result['dev:1'].lon).toBeCloseTo(106.85, 6)
  })

  it('should keep earlier overrides when those devices were not moved again', () => {
    const result = mapOverridePositions([{ id: 'dev:2', pos: { x: 10, y: 10 } }], new Map([['dev:2', { x: 10, y: 10 }]]),
      { 'dev:2': { lat: -6, lon: 106 } }, k)
    expect(result).toEqual({ 'dev:2': { lat: -6, lon: 106 } })
  })
})
