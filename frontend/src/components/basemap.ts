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
}

/**
 * Nudge labelled boxes that overlap (several POPs in one city, the five Java areas) just far enough apart
 * to read them. Each overlapping pair moves along the axis where it overlaps least — mostly north/south
 * for wide labels — so the west-to-east order of the map is kept. O(n²) per pass, fine for ≤ ~400 nodes.
 */
export function declutter(boxes: Map<string, Box>, gap: number, maxShift = Infinity, passes = 80) {
  const ids = [...boxes.keys()]
  const home = new Map(ids.map((id) => [id, { x: boxes.get(id)!.x, y: boxes.get(id)!.y }]))
  // A crowded cluster may stay partly overlapped rather than drift away from where it is on the map.
  const clamp = (id: string) => {
    const b = boxes.get(id)!
    const h = home.get(id)!
    b.x = Math.min(h.x + maxShift, Math.max(h.x - maxShift, b.x))
    b.y = Math.min(h.y + maxShift, Math.max(h.y - maxShift, b.y))
  }
  for (let pass = 0; pass < passes; pass++) {
    let moved = false
    for (let i = 0; i < ids.length; i++) {
      for (let j = i + 1; j < ids.length; j++) {
        const a = boxes.get(ids[i])!
        const b = boxes.get(ids[j])!
        const dx = b.x - a.x
        const dy = b.y - a.y
        const overlapX = a.hw + b.hw + gap - Math.abs(dx)
        const overlapY = a.hh + b.hh + gap - Math.abs(dy)
        if (overlapX <= 0 || overlapY <= 0) continue
        if (overlapY <= overlapX) {
          const push = overlapY / 2 + 0.01
          const dir = dy === 0 ? (i % 2 ? 1 : -1) : Math.sign(dy)
          a.y -= dir * push
          b.y += dir * push
        } else {
          const push = overlapX / 2 + 0.01
          const dir = dx === 0 ? (j % 2 ? 1 : -1) : Math.sign(dx)
          a.x -= dir * push
          b.x += dir * push
        }
        clamp(ids[i])
        clamp(ids[j])
        moved = true
      }
    }
    if (!moved) break
  }
}
