import type { FreeEdge, FreeNode, FreeOptions, Pos } from './freeLayout'

/** Rejection of a free layout a newer one replaced; the caller ignores it, the newer result gets drawn. */
export class FreeLayoutSuperseded extends Error {
  constructor() {
    super('Susunan bebas diganti permintaan yang lebih baru')
    this.name = 'FreeLayoutSuperseded'
  }
}

let cancelRunning: (() => void) | null = null

/** The free layout in a web worker (a 500-device POP takes about two seconds); a newer call cancels an older one. */
export function freeLayoutInWorker(nodes: FreeNode[], edges: FreeEdge[], start: Record<string, Pos>,
                                   fixed: Record<string, Pos>, options: FreeOptions): Promise<Record<string, Pos>> {
  cancelRunning?.()
  const worker = new Worker(new URL('./freeLayout.worker.ts', import.meta.url), { type: 'module' })
  return new Promise((resolve, reject) => {
    const settle = () => {
      worker.terminate()
      if (cancelRunning === cancel) cancelRunning = null
    }
    const cancel = () => {
      settle()
      reject(new FreeLayoutSuperseded())
    }
    cancelRunning = cancel
    worker.onmessage = ({ data }: MessageEvent<{ positions: Record<string, Pos> } | { error: string }>) => {
      settle()
      if ('error' in data) reject(new Error(data.error))
      else resolve(data.positions)
    }
    worker.onerror = () => {
      settle()
      reject(new Error('Worker susunan bebas berhenti secara tak terduga'))
    }
    worker.postMessage({ nodes, edges, start, fixed, options })
  })
}
