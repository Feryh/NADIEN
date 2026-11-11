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
/** Where the free arrangement (Susunan bebas) of a view is saved, apart from its schematic and its map. */
export const freeViewKey = (layoutKey: string): string => `free:${layoutKey}`

/** Arranged on the map: devices and the other POPs / cities they link to. Group boxes follow coordinates. */
export const MAP_DRAGGABLE = '[kind="device"], [kind="external_pop"]'

/**
 * At device level the other POPs are tens or hundreds of km away: drawn at their real spot they would shrink the
 * local site map to a dot. They keep only their direction, on a ring just outside the devices — except those the
 * user dragged and saved (`pinned`), which stay exactly where they were put.
 */
export function ringExternal(
  nodes: { id: string; kind: string }[],
  home: Map<string, Pos>,
  pinned: ReadonlySet<string>,
): void {
  const local = nodes.filter((n) => n.kind === 'device').map((n) => home.get(n.id)!)
  if (!local.length) return
  const median = (values: number[]) => [...values].sort((a, b) => a - b)[Math.floor(values.length / 2)]
  const center = { x: median(local.map((p) => p.x)), y: median(local.map((p) => p.y)) }
  const distances = local.map((p) => Math.hypot(p.x - center.x, p.y - center.y)).sort((a, b) => a - b)
  const radius = Math.max(300, distances[Math.floor(distances.length * 0.9)] ?? 0) * 1.35
  for (const n of nodes) {
    if (n.kind !== 'external_pop' || pinned.has(n.id)) continue
    const spot = home.get(n.id)!
    const angle = Math.atan2(spot.y - center.y, spot.x - center.x) || 0
    home.set(n.id, { x: center.x + radius * Math.cos(angle), y: center.y + radius * Math.sin(angle) })
  }
}

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
