import { describe, expect, it } from 'vitest'
import { project, unproject } from './basemap'

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
