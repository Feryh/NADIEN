import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { ElkNode } from 'elkjs/lib/elk-api'
import { createLayoutClient, LAYOUT_TIMEOUT_MS, LayoutCancelled, type WorkerLike, type WorkerResponse } from './elkLayout'

class FakeWorker implements WorkerLike {
  onmessage: ((event: { data: WorkerResponse }) => void) | null = null
  onerror: ((event: unknown) => void) | null = null
  sent: { requestId: number; graph: ElkNode }[] = []
  terminated = false
  postMessage(message: { requestId: number; graph: ElkNode }) { this.sent.push(message) }
  terminate() { this.terminated = true }
  reply(response: WorkerResponse) { this.onmessage?.({ data: response }) }
}

const graph: ElkNode = { id: 'root', children: [{ id: 'a', x: 10, y: 20, width: 90, height: 28 }] }
const laidOut: ElkNode = { id: 'root', children: [{ id: 'a', x: 10, y: 20, width: 90, height: 28 }], edges: [] }

describe('layout client request bookkeeping', () => {
  let workers: FakeWorker[]
  const makeClient = () => createLayoutClient(() => {
    const worker = new FakeWorker()
    workers.push(worker)
    return worker
  })

  beforeEach(() => { workers = [] })
  afterEach(() => { vi.useRealTimers() })

  it('should resolve with the laid out graph when the worker replies', async () => {
    const client = makeClient()
    const promise = client.layout(graph)
    workers[0].reply({ requestId: workers[0].sent[0].requestId, result: laidOut })
    const { result, ms } = await promise
    expect(result).toBe(laidOut)
    expect(ms).toBeGreaterThanOrEqual(0)
  })

  it('should reject previous request with LayoutCancelled when new layout starts', async () => {
    const client = makeClient()
    const first = client.layout(graph)
    const second = client.layout(graph)
    await expect(first).rejects.toBeInstanceOf(LayoutCancelled)
    workers[0].reply({ requestId: workers[0].sent[1].requestId, result: laidOut })
    await expect(second).resolves.toMatchObject({ result: laidOut })
    expect(workers).toHaveLength(1)
  })

  it('should ignore stale result when requestId is old', async () => {
    const client = makeClient()
    const first = client.layout(graph)
    const second = client.layout(graph)
    first.catch(() => undefined)
    const [oldRequest, newRequest] = workers[0].sent
    const stale: ElkNode = { id: 'stale' }
    workers[0].reply({ requestId: oldRequest.requestId, result: stale })
    workers[0].reply({ requestId: newRequest.requestId, result: laidOut })
    await expect(second).resolves.toMatchObject({ result: laidOut })
  })

  it('should reject with the worker error message when the worker reports an error', async () => {
    const client = makeClient()
    const promise = client.layout(graph)
    workers[0].reply({ requestId: workers[0].sent[0].requestId, error: 'boom' })
    await expect(promise).rejects.toThrow('boom')
  })

  it('should reject with timeout error and restart worker when layout exceeds LAYOUT_TIMEOUT_MS', async () => {
    vi.useFakeTimers()
    const client = makeClient()
    const promise = client.layout(graph)
    const assertion = expect(promise).rejects.toThrow(/Tata letak terlalu lama/)
    await vi.advanceTimersByTimeAsync(LAYOUT_TIMEOUT_MS)
    await assertion
    expect(workers[0].terminated).toBe(true)

    const next = client.layout(graph)
    expect(workers).toHaveLength(2)
    workers[1].reply({ requestId: workers[1].sent[0].requestId, result: laidOut })
    await expect(next).resolves.toMatchObject({ result: laidOut })
  })

  it('should reject and restart worker when the worker crashes', async () => {
    const client = makeClient()
    const promise = client.layout(graph)
    workers[0].onerror?.({})
    await expect(promise).rejects.toThrow()
    expect(workers[0].terminated).toBe(true)
  })
})
