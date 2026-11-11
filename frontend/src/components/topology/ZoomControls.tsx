import type { Core } from 'cytoscape'
import { type PointerEvent, useEffect, useRef, useState } from 'react'
import { type ScreenRect, viewportForZone, type Viewport, zoomStep } from './zoom'

const STEP = 1.4  // one + / − press
const ANIMATE = { duration: 220 }

/**
 * Zoom buttons over the topology canvas (+, −, show everything) and "Zoom area": drag a rectangle over a zone and
 * the view zooms to fit it. While dragging the canvas itself does not pan; Esc or the button again cancels.
 */
export default function ZoomControls({ getCy }: { getCy: () => Core | null }) {
  const [zoneMode, setZoneMode] = useState(false)
  const [rect, setRect] = useState<ScreenRect | null>(null)
  const overlay = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!zoneMode) return
    const cancel = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        setZoneMode(false)
        setRect(null)
      }
    }
    window.addEventListener('keydown', cancel)
    return () => window.removeEventListener('keydown', cancel)
  }, [zoneMode])

  function canvasOf(cy: Core) {
    return { width: cy.width(), height: cy.height() }
  }

  // A new zoom replaces one still animating: queued, it would wait behind it.
  function apply(cy: Core, next: Viewport) {
    cy.stop(true, false)
    cy.animate({ zoom: next.zoom, pan: next.pan }, { ...ANIMATE, queue: false })
  }

  function step(factor: number) {
    const cy = getCy()
    if (!cy) return
    apply(cy, zoomStep({ zoom: cy.zoom(), pan: cy.pan() }, factor, canvasOf(cy), { min: cy.minZoom(), max: cy.maxZoom() }))
  }

  function showAll() {
    const cy = getCy()
    if (!cy) return
    cy.stop(true, false)
    cy.animate({ fit: { eles: cy.elements(), padding: 40 } }, { ...ANIMATE, queue: false })
  }

  function point(e: PointerEvent<HTMLDivElement>) {
    const box = overlay.current!.getBoundingClientRect()
    return { x: e.clientX - box.left, y: e.clientY - box.top }
  }

  function start(e: PointerEvent<HTMLDivElement>) {
    const p = point(e)
    try {
      overlay.current!.setPointerCapture(e.pointerId)  // keep receiving the drag when it leaves the canvas
    } catch {
      /* a pointer the browser does not track (synthetic): the drag still works inside the canvas */
    }
    setRect({ x1: p.x, y1: p.y, x2: p.x, y2: p.y })
  }

  function drag(e: PointerEvent<HTMLDivElement>) {
    if (!rect) return
    const p = point(e)
    setRect({ ...rect, x2: p.x, y2: p.y })
  }

  function finish() {
    const cy = getCy()
    if (cy && rect) {
      const next = viewportForZone(rect, { zoom: cy.zoom(), pan: cy.pan() }, canvasOf(cy),
                                   { min: cy.minZoom(), max: cy.maxZoom() })
      if (next) {
        apply(cy, next)
        setZoneMode(false)  // one zone per press: back to normal panning and clicking
      }
    }
    setRect(null)
  }

  return (
    <>
      <div className="zoom-controls" role="toolbar" aria-label="Zoom topologi">
        <button type="button" title="Perbesar" aria-label="Perbesar" onClick={() => step(STEP)}>+</button>
        <button type="button" title="Perkecil" aria-label="Perkecil" onClick={() => step(1 / STEP)}>−</button>
        <button type="button" title="Tampilkan semua" aria-label="Tampilkan semua" onClick={showAll}>⤢</button>
        <button type="button" className={zoneMode ? 'active' : ''} aria-pressed={zoneMode}
          title={zoneMode ? 'Batal zoom area (Esc)' : 'Zoom area: seret persegi di atas zona yang ingin difokuskan'}
          aria-label="Zoom area" onClick={() => { setZoneMode(!zoneMode); setRect(null) }}>⬚</button>
      </div>
      {zoneMode && (
        <div ref={overlay} className="zoom-zone-overlay" onPointerDown={start} onPointerMove={drag} onPointerUp={finish}
          onPointerCancel={() => setRect(null)}>
          {!rect && <div className="zoom-zone-hint">Seret persegi di atas zona yang ingin difokuskan · Esc untuk batal</div>}
          {rect && (
            <div className="zoom-zone-rect" style={{
              left: Math.min(rect.x1, rect.x2), top: Math.min(rect.y1, rect.y2),
              width: Math.abs(rect.x2 - rect.x1), height: Math.abs(rect.y2 - rect.y1),
            }} />
          )}
        </div>
      )}
    </>
  )
}
