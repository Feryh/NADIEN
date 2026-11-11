import { freeLayout, type FreeEdge, type FreeNode, type FreeOptions, type Pos } from './freeLayout'

export interface FreeLayoutRequest {
  nodes: FreeNode[]
  edges: FreeEdge[]
  start: Record<string, Pos>
  fixed: Record<string, Pos>
  options: FreeOptions
}

// A POP of ~500 devices takes about two seconds: in a worker the page stays responsive meanwhile.
// tsconfig uses the DOM lib (no WebWorker), so `self` is narrowed by hand to the worker interface.
const scope = self as unknown as {
  onmessage: ((event: { data: FreeLayoutRequest }) => void) | null
  postMessage(message: { positions: Record<string, Pos> } | { error: string }): void
}

scope.onmessage = ({ data }) => {
  try {
    scope.postMessage({ positions: freeLayout(data.nodes, data.edges, data.start, data.fixed, data.options) })
  } catch (error) {
    scope.postMessage({ error: error instanceof Error ? error.message : String(error) })
  }
}
