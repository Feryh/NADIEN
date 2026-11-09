import { unproject } from '../basemap'

type Pos = { x: number; y: number }
export type GeoPoint = { lat: number; lon: number }

// The city / neighbours levels have their own "Peta" choice, off by default: their ELK schematic is the drawing
// with filters and the per-link panel, so opening them on the old map drawing would hide it.
export const MAP_KEY = 'nadien.topology.map'
export const MAP_KEY_ELK = 'nadien.topology.map.elk'

export const mapKeyFor = (elkLevel: boolean): string => (elkLevel ? MAP_KEY_ELK : MAP_KEY)

const readStorage = (key: string): string | null => localStorage.getItem(key)

export function storedMapOn(elkLevel: boolean, read: (key: string) => string | null = readStorage): boolean {
  const fallback = !elkLevel
  try {
    const value = read(mapKeyFor(elkLevel))
    return value == null ? fallback : value !== '0'
  } catch {
    return fallback
  }
}

/** Saved map positions live beside the schematic ones, under their own view key (map:device:JKT). */
export const mapViewKey = (layoutKey: string): string => `map:${layoutKey}`

/** Only devices are arranged on the map: POPs, groups and the ring of other POPs follow coordinates. */
export const MAP_DRAGGABLE = '[kind="device"]'

/** More than a pixel away from its saved place: a click without a drag must not count as a move. */
export function movedFrom(saved: Pos | undefined, pos: Pos): boolean {
  return !saved || Math.abs(saved.x - pos.x) > 1 || Math.abs(saved.y - pos.y) > 1
}

/**
 * Map overrides to store: devices moved more than a pixel from where they were drawn (their current, dragged spot,
 * after any ring / declutter shift) as lat/lon, plus the earlier overrides of devices not moved again. Devices never
 * dragged get no override, so they keep following their real site coordinates.
 */
export function mapOverridePositions(
  current: { id: string; pos: Pos }[],
  saved: Map<string, Pos>,
  existing: Record<string, GeoPoint>,
  pxPerDegree: number,
): Record<string, GeoPoint> {
  const result: Record<string, GeoPoint> = {}
  for (const { id, pos } of current) {
    if (movedFrom(saved.get(id), pos)) result[id] = unproject(pos, pxPerDegree)
    else if (existing[id]) result[id] = existing[id]
  }
  return result
}
