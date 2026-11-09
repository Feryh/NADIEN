import ELK from 'elkjs/lib/elk.bundled.js'
import type { ElkNode } from 'elkjs/lib/elk-api'

// ELK pada graf kota besar memblokir thread beberapa detik; di worker, UI tetap responsif.
const elk = new ELK()

// tsconfig memakai lib DOM (tanpa WebWorker), jadi tipe `self` dipersempit manual ke antarmuka worker.
const scope = self as unknown as {
  onmessage: ((event: { data: { requestId: number; graph: ElkNode } }) => void) | null
  postMessage(message: { requestId: number; result: ElkNode } | { requestId: number; error: string }): void
}

scope.onmessage = ({ data }) => {
  const { requestId, graph } = data
  elk.layout(graph).then(
    (result) => scope.postMessage({ requestId, result }),
    (error: unknown) => scope.postMessage({ requestId, error: error instanceof Error ? error.message : String(error) }),
  )
}
