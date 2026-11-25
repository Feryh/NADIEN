import { feature } from 'topojson-client'
import type { GeometryCollection, Topology } from 'topojson-specification'
import type { Feature, Geometry, Position } from 'geojson'

// Equirectangular projection centred on Indonesia, in degrees: x = lon - 118, y = -lat.
// Near the equator this keeps shapes close to what people know from a map; the topology scales it
// by its own pixels-per-degree, so the basemap and the nodes share one coordinate system.
export const LON0 = 118

export function project(lat: number, lon: number, pxPerDegree: number): { x: number; y: number } {
  return { x: (lon - LON0) * pxPerDegree, y: -lat * pxPerDegree }
}

/** Inverse of `project`: where on the map a drawing position is, so a dragged node can be saved as lat/lon. */
export function unproject(pos: { x: number; y: number }, pxPerDegree: number): { lat: number; lon: number } {
  return { lat: -pos.y / pxPerDegree, lon: pos.x / pxPerDegree + LON0 }
}

export interface BasemapShape {
  id: string
  name: string
  indonesia: boolean
  d: string
}

// The region drawn behind the topology: Indonesia and the neighbours that frame it.
const BOUNDS = { lonMin: 90, lonMax: 145, latMin: -13, latMax: 9 }
const INDONESIA_ID = '360'

let cached: Promise<BasemapShape[]> | null = null

/** Country outlines (Natural Earth 1:50m, bundled: works on an internal network without map tiles). */
export function loadBasemap(): Promise<BasemapShape[]> {
  cached = cached ?? import('world-atlas/countries-50m.json').then((mod) => {
    const topo = mod.default as unknown as Topology<{ countries: GeometryCollection<{ name: string }> }>
    const countries = feature(topo, topo.objects.countries)
    return countries.features
      .filter((f) => intersects(f))
      .map((f) => ({ id: String(f.id), name: f.properties?.name ?? '', indonesia: String(f.id) === INDONESIA_ID, d: toPath(f.geometry) }))
      .filter((s) => s.d)
  })
  return cached
}

function rings(geometry: Geometry): Position[][] {
  if (geometry.type === 'Polygon') return geometry.coordinates
  if (geometry.type === 'MultiPolygon') return geometry.coordinates.flat()
  return []
}

function intersects(f: Feature<Geometry>): boolean {
  return rings(f.geometry).some((ring) =>
    ring.some(([lon, lat]) => lon >= BOUNDS.lonMin && lon <= BOUNDS.lonMax && lat >= BOUNDS.latMin && lat <= BOUNDS.latMax),
  )
}

function toPath(geometry: Geometry): string {
  return rings(geometry)
    .filter((ring) => ring.some(([lon, lat]) => lon >= BOUNDS.lonMin - 10 && lon <= BOUNDS.lonMax + 10 && lat >= BOUNDS.latMin - 10 && lat <= BOUNDS.latMax + 10))
    .map((ring) => 'M' + ring.map(([lon, lat]) => `${(lon - LON0).toFixed(3)},${(-lat).toFixed(3)}`).join('L') + 'Z')
    .join('')
}

export interface Box {
  x: number
  y: number
  hw: number  // half width
  hh: number  // half height
  fixed?: boolean  // placed by hand: never moved, the others are pushed around it
}

const GOLDEN_ANGLE = Math.PI * (3 - Math.sqrt(5))

/**
 * Points that share one spot (the devices of one POP, all on its coordinates) spread in a sunflower around it, each
 * `spacing` from its neighbours; a point alone on its spot stays. Linear: the map of a whole area (1,350 devices
 * whose labels overlap at the zoom that shows them all) uses it instead of declutter, which never settles there.
 */
export function fanOut(points: Map<string, { x: number; y: number }>, spacing: number): Map<string, { x: number; y: number }> {
  const bySpot = new Map<string, string[]>()
  for (const [id, p] of points) {
    const spot = `${p.x},${p.y}`
    const ids = bySpot.get(spot)
    if (ids) ids.push(id)
    else bySpot.set(spot, [id])
  }
  const out = new Map<string, { x: number; y: number }>()
  for (const ids of bySpot.values()) {
    const { x, y } = points.get(ids[0])!
    ids.forEach((id, k) => {
      const r = spacing * Math.sqrt(k)
      out.set(id, { x: x + r * Math.cos(k * GOLDEN_ANGLE), y: y + r * Math.sin(k * GOLDEN_ANGLE) })
    })
  }
  return out
}

/**
 * Boxes near each box, by index, from a grid built where the boxes stand: two boxes can only overlap when they are
 * closer than the two biggest boxes plus the gap, so each one is checked against its own cell and the eight around it.
 */
function neighbourIndexes(list: Box[], gap: number): number[][] {
  const cellW = Math.max(1e-9, 2 * Math.max(...list.map((b) => b.hw)) + gap)
  const cellH = Math.max(1e-9, 2 * Math.max(...list.map((b) => b.hh)) + gap)
  const cells = new Map<string, number[]>()
  const cellOf = list.map((b, k) => {
    const cx = Math.floor(b.x / cellW)
    const cy = Math.floor(b.y / cellH)
    const key = `${cx},${cy}`
    const members = cells.get(key)
    if (members) members.push(k)
    else cells.set(key, [k])
    return [cx, cy]
  })
  return list.map((_, i) => {
    const [cx, cy] = cellOf[i]
    const near: number[] = []
    for (let x = cx - 1; x <= cx + 1; x++) {
      for (let y = cy - 1; y <= cy + 1; y++) {
        for (const j of cells.get(`${x},${y}`) ?? []) if (j > i) near.push(j)
      }
    }
    return near.sort((p, q) => p - q)  // the order the all-pairs loop used, so the same pushes happen
  })
}

/**
 * Nudge labelled boxes that overlap (several POPs in one city, the five Java areas) just far enough apart
 * to read them. Each overlapping pair moves along the axis where it overlaps least — mostly north/south
 * for wide labels — so the west-to-east order of the map is kept. Each pass only checks boxes near each other
 * (neighbourIndexes): comparing every pair froze the page 13 s for the 1,350 devices of Jabodetabek on the map.
 */
export function declutter(boxes: Map<string, Box>, gap: number, maxShift = Infinity, passes = 80) {
  const ids = [...boxes.keys()]
  const list = ids.map((id) => boxes.get(id)!)
  const home = list.map((b) => ({ x: b.x, y: b.y }))
  // A crowded cluster may stay partly overlapped rather than drift away from where it is on the map.
  const clamp = (k: number) => {
    const b = list[k]
    const h = home[k]
    b.x = Math.min(h.x + maxShift, Math.max(h.x - maxShift, b.x))
    b.y = Math.min(h.y + maxShift, Math.max(h.y - maxShift, b.y))
  }
  for (let pass = 0; pass < passes && list.length > 1; pass++) {
    let moved = false
    const near = neighbourIndexes(list, gap)  // rebuilt every pass: boxes moved in the last one
    for (let i = 0; i < list.length; i++) {
      for (const j of near[i]) {
        const a = list[i]
        const b = list[j]
        const dx = b.x - a.x
        const dy = b.y - a.y
        const overlapX = a.hw + b.hw + gap - Math.abs(dx)
        const overlapY = a.hh + b.hh + gap - Math.abs(dy)
        if (overlapX <= 0 || overlapY <= 0 || (a.fixed && b.fixed)) continue
        // Two free boxes share the push; a free box next to a fixed one takes all of it.
        const shareA = a.fixed ? 0 : b.fixed ? 1 : 0.5
        const shareB = 1 - shareA
        if (overlapY <= overlapX) {
          const push = overlapY + 0.02
          const dir = dy === 0 ? (i % 2 ? 1 : -1) : Math.sign(dy)
          a.y -= dir * push * shareA
          b.y += dir * push * shareB
        } else {
          const push = overlapX + 0.02
          const dir = dx === 0 ? (j % 2 ? 1 : -1) : Math.sign(dx)
          a.x -= dir * push * shareA
          b.x += dir * push * shareB
        }
        clamp(i)
        clamp(j)
        moved = true
      }
    }
    if (!moved) break
  }
}
