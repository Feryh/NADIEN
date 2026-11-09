import { describe, expect, it } from 'vitest'
import { type Box, declutter, project, unproject } from './basemap'

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
})
