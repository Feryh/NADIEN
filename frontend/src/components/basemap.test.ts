import { describe, expect, it } from 'vitest'
import { type Box, declutter, fanOut, project, unproject } from './basemap'

describe('unproject', () => {
  it.each([
    [-6.2, 106.8, 3000],
    [3.59, 98.67, 1500],
    [-8.65, 115.22, 60],
    [0, 118, 200],
  ])('should return the original lat/lon when undoing project(%f, %f, %i)', (lat, lon, pxPerDegree) => {
    const back = unproject(project(lat, lon, pxPerDegree), pxPerDegree)
    expect(back.lat).toBeCloseTo(lat, 9)
    expect(back.lon).toBeCloseTo(lon, 9)
  })
})

describe('declutter', () => {
  it('should keep pinned box in place when declutter separates overlapping boxes', () => {
    const boxes = new Map<string, Box>([
      ['pinned', { x: 0, y: 0, hw: 10, hh: 10, fixed: true }],
      ['free', { x: 5, y: 2, hw: 10, hh: 10 }],
    ])
    declutter(boxes, 2)
    const pinned = boxes.get('pinned')!
    const free = boxes.get('free')!
    expect([pinned.x, pinned.y]).toEqual([0, 0])
    expect(Math.abs(free.x) >= 22 - 0.001 || Math.abs(free.y) >= 22 - 0.001).toBe(true)
  })

  // A small deterministic generator: the same crowd on every run.
  function crowd(count: number, spread: number, seed = 7): Map<string, Box> {
    let state = seed
    const next = () => (state = (state * 1103515245 + 12345) % 2 ** 31) / 2 ** 31
    return new Map(Array.from({ length: count }, (_, k) => [`d${k}`, { x: next() * spread, y: next() * spread, hw: 55, hh: 17 }]))
  }

  function overlapping(boxes: Map<string, Box>, gap: number): number {
    const list = [...boxes.values()]
    let count = 0
    for (let i = 0; i < list.length; i++) {
      for (let j = i + 1; j < list.length; j++) {
        const a = list[i]
        const b = list[j]
        if (a.hw + b.hw + gap - Math.abs(b.x - a.x) > 1e-6 && a.hh + b.hh + gap - Math.abs(b.y - a.y) > 1e-6) count++
      }
    }
    return count
  }

  it('should leave no two boxes overlapping when declutter separates a crowded cluster', () => {
    const boxes = crowd(300, 2500)
    expect(overlapping(boxes, 4)).toBeGreaterThan(0)
    declutter(boxes, 4)
    expect(overlapping(boxes, 4)).toBe(0)
  })

  it('should finish quickly when declutter spaces the 1,350 devices of a whole area', () => {
    // Jabodetabek on the map: 1,350 devices on ~60 sites, many sharing one site's coordinates, the shift capped,
    // so the crowd never fully separates and every pass runs.
    const sites = [...crowd(60, 4000, 11).values()]
    const boxes = new Map<string, Box>(Array.from({ length: 1350 }, (_, k) => {
      const site = sites[k % sites.length]
      return [`d${k}`, { x: site.x, y: site.y, hw: 55, hh: 17 }]
    }))
    const started = performance.now()
    for (let round = 0; round < 4; round++) declutter(boxes, 4, 34 * 6)  // as the map layout does: four rounds
    expect(performance.now() - started).toBeLessThan(2000)
  })
})

describe('fanOut', () => {
  it('should spread devices sharing one spot apart and leave a lone device where it is', () => {
    const points = new Map([
      ...Array.from({ length: 30 }, (_, k) => [`pop${k}`, { x: 100, y: 50 }] as [string, { x: number; y: number }]),
      ['alone', { x: 900, y: 400 }],
    ])
    const out = fanOut(points, 10)
    expect(out.get('alone')).toEqual({ x: 900, y: 400 })
    const spread = [...out].filter(([id]) => id.startsWith('pop')).map(([, p]) => p)
    expect(new Set(spread.map((p) => `${p.x.toFixed(3)},${p.y.toFixed(3)}`)).size).toBe(30)
    for (let i = 0; i < spread.length; i++) {
      for (let j = i + 1; j < spread.length; j++) {
        expect(Math.hypot(spread[i].x - spread[j].x, spread[i].y - spread[j].y)).toBeGreaterThan(5)
      }
    }
  })
})
