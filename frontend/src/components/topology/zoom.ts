/** Viewport arithmetic for the topology's zoom controls; Cytoscape draws a model point p at p * zoom + pan. */

export interface Point {
  x: number
  y: number
}

export interface Viewport {
  zoom: number
  pan: Point
}

/** A rectangle dragged on the canvas, in screen pixels relative to the canvas, corners in any order. */
export interface ScreenRect {
  x1: number
  y1: number
  x2: number
  y2: number
}

// A drag shorter than this is a click, not a zone to zoom into.
export const MIN_ZONE_PX = 8

/**
 * The viewport that shows exactly the dragged zone, centred and as large as the canvas allows (`padding` pixels
 * kept around it), the zoom kept within the canvas's limits. Null when the drag was too small to be a zone.
 */
export function viewportForZone(rect: ScreenRect, current: Viewport, canvas: { width: number; height: number },
                                limits: { min: number; max: number }, padding = 16): Viewport | null {
  const width = Math.abs(rect.x2 - rect.x1)
  const height = Math.abs(rect.y2 - rect.y1)
  if (width < MIN_ZONE_PX || height < MIN_ZONE_PX) return null
  // The zone in model coordinates, as it sits under the current viewport.
  const left = (Math.min(rect.x1, rect.x2) - current.pan.x) / current.zoom
  const top = (Math.min(rect.y1, rect.y2) - current.pan.y) / current.zoom
  const modelWidth = width / current.zoom
  const modelHeight = height / current.zoom
  const room = { width: Math.max(canvas.width - 2 * padding, 1), height: Math.max(canvas.height - 2 * padding, 1) }
  const zoom = Math.min(limits.max, Math.max(limits.min, Math.min(room.width / modelWidth, room.height / modelHeight)))
  const centre = { x: left + modelWidth / 2, y: top + modelHeight / 2 }
  return { zoom, pan: { x: canvas.width / 2 - centre.x * zoom, y: canvas.height / 2 - centre.y * zoom } }
}

/** One step of the + / − buttons, zooming about the canvas centre so what is in the middle stays there. */
export function zoomStep(current: Viewport, factor: number, canvas: { width: number; height: number },
                         limits: { min: number; max: number }): Viewport {
  const zoom = Math.min(limits.max, Math.max(limits.min, current.zoom * factor))
  const centre = { x: canvas.width / 2, y: canvas.height / 2 }
  const model = { x: (centre.x - current.pan.x) / current.zoom, y: (centre.y - current.pan.y) / current.zoom }
  return { zoom, pan: { x: centre.x - model.x * zoom, y: centre.y - model.y * zoom } }
}
