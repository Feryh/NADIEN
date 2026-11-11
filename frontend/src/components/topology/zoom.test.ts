import { describe, expect, it } from 'vitest'
import { viewportForZone, zoomStep, type Viewport } from './zoom'

const canvas = { width: 1000, height: 600 }
const limits = { min: 0.05, max: 3 }
const at = (v: Viewport, p: { x: number; y: number }) => ({ x: p.x * v.zoom + v.pan.x, y: p.y * v.zoom + v.pan.y })

describe('viewportForZone', () => {
  it('should put the middle of the dragged zone in the middle of the canvas', () => {
    const current = { zoom: 0.5, pan: { x: 100, y: 50 } }
    const zone = { x1: 200, y1: 150, x2: 400, y2: 270 }
    const next = viewportForZone(zone, current, canvas, limits)!
    const middle = { x: (300 - 100) / 0.5, y: (210 - 50) / 0.5 }  // the zone's middle in model coordinates
    expect(at(next, middle)).toEqual({ x: 500, y: 300 })
  })

  it('should zoom in until the zone fills the canvas, keeping the padding', () => {
    const next = viewportForZone({ x1: 0, y1: 0, x2: 484, y2: 100 }, { zoom: 1, pan: { x: 0, y: 0 } }, canvas, limits, 16)!
    expect(next.zoom).toBeCloseTo((1000 - 32) / 484)  // width is the tighter side
  })

  it('should accept a zone dragged from bottom-right to top-left', () => {
    const current = { zoom: 1, pan: { x: 0, y: 0 } }
    expect(viewportForZone({ x1: 400, y1: 300, x2: 200, y2: 100 }, current, canvas, limits))
      .toEqual(viewportForZone({ x1: 200, y1: 100, x2: 400, y2: 300 }, current, canvas, limits))
  })

  it('should not zoom past the canvas limit for a tiny zone', () => {
    expect(viewportForZone({ x1: 0, y1: 0, x2: 10, y2: 10 }, { zoom: 2, pan: { x: 0, y: 0 } }, canvas, limits)!.zoom).toBe(3)
  })

  it('should treat a drag of a few pixels as a click and change nothing', () => {
    expect(viewportForZone({ x1: 100, y1: 100, x2: 104, y2: 130 }, { zoom: 1, pan: { x: 0, y: 0 } }, canvas, limits)).toBeNull()
  })
})

describe('zoomStep', () => {
  it('should keep what is in the middle of the canvas in the middle', () => {
    const current = { zoom: 0.8, pan: { x: -120, y: 40 } }
    const middle = { x: (500 + 120) / 0.8, y: (300 - 40) / 0.8 }
    const next = zoomStep(current, 1.25, canvas, limits)
    expect(next.zoom).toBe(1)
    expect(at(next, middle).x).toBeCloseTo(500)
    expect(at(next, middle).y).toBeCloseTo(300)
  })
})
