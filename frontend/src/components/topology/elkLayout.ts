import type { ElkNode } from 'elkjs/lib/elk-api'
import { buildElkGraph } from './elkGraph'
import { toCytoscape, type EdgeRoute, type Pos } from './elkToCytoscape'
import type { CityNode, CityView } from './types'

export const LAYOUT_TIMEOUT_MS = 20_000

export type WorkerResponse = { requestId: number; result: ElkNode } | { requestId: number; error: string }

export interface WorkerLike {
  onmessage: ((event: { data: WorkerResponse }) => void) | null
  onerror: ((event: unknown) => void) | null
  postMessage(message: { requestId: number; graph: ElkNode }): void
  terminate(): void
}

// Dilempar ke pemanggil lama saat layout baru dimulai; pemanggil cukup mengabaikannya.
export class LayoutCancelled extends Error {
  constructor() {
    super('Tata letak dibatalkan karena ada permintaan baru')
    this.name = 'LayoutCancelled'
  }
}

interface Pending {
  requestId: number
  startedAt: number
  timer: ReturnType<typeof setTimeout>
  resolve: (value: { result: ElkNode; ms: number }) => void
  reject: (reason: Error) => void
}

export interface LayoutClient {
  layout(graph: ElkNode): Promise<{ result: ElkNode; ms: number }>
}

// Factory disuntik supaya pembukuan request bisa diuji tanpa Worker sungguhan.
export function createLayoutClient(factory: () => WorkerLike): LayoutClient {
  let worker: WorkerLike | null = null
  let pending: Pending | null = null
  let lastRequestId = 0

  function discardWorker(): void {
    worker?.terminate()
    worker = null
  }

  function settle(current: Pending): void {
    clearTimeout(current.timer)
    if (pending === current) pending = null
  }

  function handleMessage({ data }: { data: WorkerResponse }): void {
    const current = pending
    // requestId lama = hasil dari permintaan yang sudah dibatalkan.
    if (!current || data.requestId !== current.requestId) return
    settle(current)
    if ('error' in data) current.reject(new Error(data.error))
    else current.resolve({ result: data.result, ms: performance.now() - current.startedAt })
  }

  function handleCrash(): void {
    const current = pending
    discardWorker()
    if (!current) return
    settle(current)
    current.reject(new Error('Worker tata letak berhenti secara tak terduga'))
  }

  function ensureWorker(): WorkerLike {
    if (!worker) {
      worker = factory()
      worker.onmessage = handleMessage
      worker.onerror = handleCrash
    }
    return worker
  }

  return {
    layout(graph) {
      if (pending) {
        const previous = pending
        settle(previous)
        previous.reject(new LayoutCancelled())
        // ELK berjalan sinkron di worker; layout lama yang dibatalkan akan menahan permintaan baru
        // (dan memakan timeout-nya) bila worker yang sama dipakai ulang.
        discardWorker()
      }
      const requestId = ++lastRequestId
      return new Promise((resolve, reject) => {
        const current: Pending = {
          requestId,
          startedAt: performance.now(),
          resolve,
          reject,
          timer: setTimeout(() => {
            // Worker yang macet tidak bisa dibatalkan dari luar; buang dan buat baru saat dibutuhkan.
            settle(current)
            discardWorker()
            reject(new Error(`Tata letak terlalu lama (lebih dari ${LAYOUT_TIMEOUT_MS / 1000} detik)`))
          }, LAYOUT_TIMEOUT_MS),
        }
        pending = current
        ensureWorker().postMessage({ requestId, graph })
      })
    },
  }
}

const defaultClient = createLayoutClient(
  () => new Worker(new URL('./elk.worker.ts', import.meta.url), { type: 'module' }) as unknown as WorkerLike,
)

export async function layoutCity(
  view: CityView,
  sizeOf: (n: CityNode) => { width: number; height: number },
): Promise<{ positions: Record<string, Pos>; edges: Record<string, EdgeRoute>; ms: number }> {
  // Graf dibangun di main thread karena sizeOf berupa fungsi yang tidak bisa lewat postMessage.
  const { result, ms } = await defaultClient.layout(buildElkGraph(view, sizeOf))
  return { ...toCytoscape(result), ms }
}
