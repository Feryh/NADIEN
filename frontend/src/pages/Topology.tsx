import cytoscape, { Core, ElementDefinition } from 'cytoscape'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import { api, DiscoveryResult, TopologyData } from '../api'
import { CAPACITY_HEX, UtilBar } from '../components/ui'

type LayoutMode = 'saved' | 'geo' | 'cose'
type ColorFilter = 'all' | 'warn' | 'red'
type Selection = { kind: 'device' | 'link'; id: number } | null
type Pos = { x: number; y: number }

const ROLE_COLOR: Record<string, string> = {
  p: '#6366f1',
  pe: '#0ea5e9',
  agg: '#14b8a6',
  dci: '#f97316',
  access: '#64748b',
  olt: '#a3e635',
  bng: '#e879f9',
}
const ROLE_SHAPE: Record<string, string> = {
  p: 'ellipse',
  pe: 'round-rectangle',
  agg: 'diamond',
  dci: 'hexagon',
  access: 'triangle',
  olt: 'vee',
  bng: 'octagon',
}

const STYLE = [
  {
    selector: 'node[kind="pop"]',
    style: {
      shape: 'round-rectangle',
      'background-color': '#1e293b',
      'background-opacity': 0.55,
      'border-color': '#334155',
      'border-width': 1,
      label: 'data(label)',
      'text-valign': 'top',
      'text-halign': 'center',
      color: '#94a3b8',
      'font-size': 15,
      'font-weight': 'bold',
      padding: '22px',
    },
  },
  {
    selector: 'node[kind="device"]',
    style: {
      width: 36,
      height: 36,
      shape: 'data(shape)',
      'background-color': 'data(roleColor)',
      'border-color': '#0f172a',
      'border-width': 2,
      label: 'data(label)',
      color: '#e2e8f0',
      'font-size': 12,
      'text-valign': 'bottom',
      'text-margin-y': 4,
    },
  },
  {
    selector: 'edge',
    style: {
      width: 'data(width)',
      'line-color': 'data(color)',
      'curve-style': 'bezier',
      label: 'data(label)',
      'font-size': 11,
      color: '#cbd5e1',
      'text-background-color': '#0f172a',
      'text-background-opacity': 0.85,
      'text-background-padding': '2px',
      'text-rotation': 'autorotate',
    },
  },
  { selector: 'edge[status="down"]', style: { 'line-style': 'dashed', opacity: 0.6 } },
  { selector: 'edge[source_type="config"]', style: { 'line-style': 'dotted' } },
  { selector: ':selected', style: { 'overlay-color': '#38bdf8', 'overlay-opacity': 0.25, 'overlay-padding': 6 } },
] as unknown as cytoscape.CytoscapeOptions['style']

const MIN_POP_SPACING = 300

function geoPositions(data: TopologyData): Record<string, Pos> {
  const pops = data.pops.filter((p) => p.latitude != null && p.longitude != null)
  const result: Record<string, Pos> = {}
  if (!pops.length) return result
  const lons = pops.map((p) => p.longitude as number)
  const lats = pops.map((p) => p.latitude as number)
  const minLon = Math.min(...lons)
  const maxLat = Math.max(...lats)
  const span = Math.max(Math.max(...lons) - minLon, maxLat - Math.min(...lats), 0.01)
  const k = 1200 / span
  const center = new Map(pops.map((p) => [p.id, { x: ((p.longitude as number) - minLon) * k, y: (maxLat - (p.latitude as number)) * k }]))

  // Nearby POPs (e.g. JKT–BDG) would overlap at map scale: push centers apart to a minimum
  // spacing while keeping their geographic direction.
  const pts = [...center.values()]
  for (let iter = 0; iter < 100; iter++) {
    let moved = false
    for (let i = 0; i < pts.length; i++) {
      for (let j = i + 1; j < pts.length; j++) {
        const dx = pts[j].x - pts[i].x
        const dy = pts[j].y - pts[i].y
        const d = Math.hypot(dx, dy) || 1
        if (d >= MIN_POP_SPACING) continue
        const push = (MIN_POP_SPACING - d) / 2
        pts[i].x -= (dx / d) * push
        pts[i].y -= (dy / d) * push
        pts[j].x += (dx / d) * push
        pts[j].y += (dy / d) * push
        moved = true
      }
    }
    if (!moved) break
  }

  let orphan = 0
  for (const pop of data.pops) {
    const devs = data.devices.filter((d) => d.pop_id === pop.id)
    const c = center.get(pop.id) ?? { x: orphan++ * 250, y: span * k + 300 }
    const r = devs.length > 1 ? 45 + devs.length * 10 : 0
    devs.forEach((d, i) => {
      const angle = (i / devs.length) * 2 * Math.PI - Math.PI / 2
      result[`dev-${d.id}`] = { x: c.x + r * Math.cos(angle), y: c.y + r * Math.sin(angle) }
    })
  }
  return result
}

function buildElements(data: TopologyData): ElementDefinition[] {
  const nodes: ElementDefinition[] = [
    ...data.pops.map((p) => ({ data: { id: `pop-${p.id}`, label: `${p.code} · ${p.name}`, kind: 'pop' } })),
    ...data.devices.map((d) => ({
      data: {
        id: `dev-${d.id}`,
        parent: `pop-${d.pop_id}`,
        label: d.hostname,
        kind: 'device',
        roleColor: ROLE_COLOR[d.role] ?? '#64748b',
        shape: ROLE_SHAPE[d.role] ?? 'ellipse',
      },
    })),
  ]
  const edges: ElementDefinition[] = data.links.map((l) => ({
    data: {
      id: `link-${l.id}`,
      source: `dev-${l.source}`,
      target: `dev-${l.target}`,
      label: `${Math.round(l.current_gbps)}/${Math.round(l.capacity_gbps)}G`,
      color: l.status === 'down' ? '#64748b' : CAPACITY_HEX[l.color],
      width: Math.min(2 + l.capacity_gbps / 60, 10),
      status: l.status,
      source_type: l.discovery_source,
    },
  }))
  return [...nodes, ...edges]
}

export default function Topology() {
  const container = useRef<HTMLDivElement>(null)
  const cyRef = useRef<Core | null>(null)
  const [data, setData] = useState<TopologyData | null>(null)
  const [mode, setMode] = useState<LayoutMode>('saved')
  const [colorFilter, setColorFilter] = useState<ColorFilter>('all')
  const [selection, setSelection] = useState<Selection>(null)
  const [dirty, setDirty] = useState(false)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<{ text: string; error?: boolean } | null>(null)

  const load = useCallback(async () => {
    try {
      setData(await api.get<TopologyData>('/topology'))
    } catch (e) {
      setMessage({ text: (e as Error).message, error: true })
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const runLayout = useCallback(
    (cy: Core, layout: LayoutMode) => {
      if (!data) return
      if (layout === 'cose') {
        cy.layout({
          name: 'cose',
          animate: false,
          padding: 40,
          nodeRepulsion: () => 40000,
          nodeOverlap: 40,
          idealEdgeLength: () => 160,
          nestingFactor: 0.6,
          gravity: 0.2,
          componentSpacing: 120,
          numIter: 3000,
          randomize: true,
        } as cytoscape.LayoutOptions).run()
        return
      }
      const geo = geoPositions(data)
      const saved: Record<string, Pos> = {}
      if (layout === 'saved') {
        for (const d of data.devices) if (d.x != null && d.y != null) saved[`dev-${d.id}`] = { x: d.x, y: d.y }
      }
      cy.layout({
        name: 'preset',
        padding: 40,
        positions: (node: cytoscape.NodeSingular) => saved[node.id()] ?? geo[node.id()] ?? { x: 0, y: 0 },
      } as cytoscape.LayoutOptions).run()
    },
    [data],
  )

  // (Re)create the graph whenever the data changes.
  useEffect(() => {
    if (!data || !container.current) return
    const cy = cytoscape({
      container: container.current,
      elements: buildElements(data),
      style: STYLE,
      wheelSensitivity: 0.2,
      minZoom: 0.1,
      maxZoom: 3,
    })
    cy.on('tap', 'edge', (e) => setSelection({ kind: 'link', id: Number(e.target.id().slice(5)) }))
    cy.on('tap', 'node[kind="device"]', (e) => setSelection({ kind: 'device', id: Number(e.target.id().slice(4)) }))
    cy.on('tap', (e) => {
      if (e.target === cy) setSelection(null)
    })
    cy.on('dragfree', 'node', () => setDirty(true))
    cyRef.current = cy
    runLayout(cy, mode)
    return () => cy.destroy()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data])

  useEffect(() => {
    const cy = cyRef.current
    if (!cy) return
    cy.edges().forEach((edge) => {
      const link = data?.links.find((l) => `link-${l.id}` === edge.id())
      const visible =
        colorFilter === 'all' || (colorFilter === 'red' ? link?.color === 'red' : link?.color !== 'green')
      edge.style('display', visible ? 'element' : 'none')
    })
  }, [colorFilter, data])

  function changeLayout(next: LayoutMode) {
    setMode(next)
    if (cyRef.current) runLayout(cyRef.current, next)
    if (next !== 'saved') setDirty(true)
  }

  async function savePositions() {
    const cy = cyRef.current
    if (!cy) return
    const positions = cy.nodes('[kind="device"]').map((n) => ({
      device_id: Number(n.id().slice(4)),
      x: Math.round(n.position('x')),
      y: Math.round(n.position('y')),
    }))
    try {
      await api.put('/topology/positions', positions)
      setDirty(false)
      setMessage({ text: `Posisi ${positions.length} node disimpan.` })
    } catch (e) {
      setMessage({ text: (e as Error).message, error: true })
    }
  }

  async function resetPositions() {
    await api.del('/topology/positions')
    setMode('geo')
    setDirty(false)
    await load()
  }

  async function discover() {
    setBusy(true)
    try {
      const r = await api.post<DiscoveryResult>('/topology/discover')
      setMessage({
        text:
          `Discovery: ${r.lldp_links} link via LLDP, ${r.config_links} via config · ${r.created} baru, ` +
          `${r.updated} update, ${r.marked_down} ditandai down` +
          (r.unresolved_neighbors.length ? ` · ${r.unresolved_neighbors.length} neighbor tak dikenal` : ''),
      })
      await load()
    } catch (e) {
      setMessage({ text: (e as Error).message, error: true })
    } finally {
      setBusy(false)
    }
  }

  const stats = useMemo(() => {
    const links = data?.links ?? []
    return {
      total: links.length,
      green: links.filter((l) => l.color === 'green').length,
      yellow: links.filter((l) => l.color === 'yellow').length,
      red: links.filter((l) => l.color === 'red').length,
      down: links.filter((l) => l.status === 'down').length,
      config: links.filter((l) => l.discovery_source === 'config').length,
    }
  }, [data])

  const selectedLink = selection?.kind === 'link' ? data?.links.find((l) => l.id === selection.id) : undefined
  const selectedDevice = selection?.kind === 'device' ? data?.devices.find((d) => d.id === selection.id) : undefined
  const deviceName = (id: number) => data?.devices.find((d) => d.id === id)?.hostname ?? `#${id}`

  return (
    <div className="topology">
      <div className="toolbar card">
        <h2>Metro Topology</h2>
        <div className="segmented">
          {(['saved', 'geo', 'cose'] as LayoutMode[]).map((m) => (
            <button key={m} className={mode === m ? 'active' : ''} onClick={() => changeLayout(m)}>
              {m === 'saved' ? 'Tersimpan' : m === 'geo' ? 'Geografis' : 'Auto'}
            </button>
          ))}
        </div>
        <select value={colorFilter} onChange={(e) => setColorFilter(e.target.value as ColorFilter)}>
          <option value="all">Semua link</option>
          <option value="warn">Kuning + Merah</option>
          <option value="red">Merah saja</option>
        </select>
        <div className="spacer" />
        <button onClick={resetPositions}>Reset posisi</button>
        <button onClick={savePositions} disabled={!dirty}>
          Simpan posisi
        </button>
        <button className="primary" onClick={discover} disabled={busy}>
          {busy ? 'Discovering…' : 'Run discovery (LLDP)'}
        </button>
      </div>

      {message && (
        <div className={message.error ? 'error' : 'notice'} onClick={() => setMessage(null)}>
          {message.text}
        </div>
      )}

      <div className="topology-body">
        <div className="graph card" ref={container} />
        <aside className="side card">
          <div className="legend">
            <div><span className="swatch" style={{ background: CAPACITY_HEX.green }} /> &lt; 60% ({stats.green})</div>
            <div><span className="swatch" style={{ background: CAPACITY_HEX.yellow }} /> 60–80% ({stats.yellow})</div>
            <div><span className="swatch" style={{ background: CAPACITY_HEX.red }} /> ≥ 80% ({stats.red})</div>
            <div><span className="swatch dashed" /> down ({stats.down})</div>
            <div><span className="swatch dotted" /> dari config, bukan LLDP ({stats.config})</div>
            <div className="muted small">Warna = committed (current + reserved) / capacity</div>
          </div>
          <hr />
          {selectedLink && (
            <div className="detail">
              <h3>Link #{selectedLink.id}</h3>
              <dl>
                <dt>A</dt>
                <dd>{deviceName(selectedLink.source)} · {selectedLink.a_interface}</dd>
                <dt>B</dt>
                <dd>{deviceName(selectedLink.target)} · {selectedLink.b_interface}</dd>
                <dt>Type</dt>
                <dd>{selectedLink.link_type}</dd>
                <dt>Status</dt>
                <dd>{selectedLink.status}</dd>
                <dt>Source</dt>
                <dd>{selectedLink.discovery_source}</dd>
                <dt>Latency</dt>
                <dd>{selectedLink.latency_ms ?? '–'} ms</dd>
              </dl>
              <UtilBar
                capacity={selectedLink.capacity_gbps}
                current={selectedLink.current_gbps}
                reserved={selectedLink.reserved_gbps}
              />
              <RouterLink to={`/capacity?link=${selectedLink.id}`}>Lihat forecast kapasitas →</RouterLink>
            </div>
          )}
          {selectedDevice && (
            <div className="detail">
              <h3>{selectedDevice.hostname}</h3>
              <dl>
                <dt>POP</dt>
                <dd>{data?.pops.find((p) => p.id === selectedDevice.pop_id)?.code}</dd>
                <dt>Role</dt>
                <dd>{selectedDevice.role.toUpperCase()}</dd>
                <dt>Vendor</dt>
                <dd>{selectedDevice.vendor ?? '–'}</dd>
                <dt>Loopback</dt>
                <dd>{selectedDevice.loopback_ipv4 ?? '–'}</dd>
                <dt>Links</dt>
                <dd>
                  {data?.links.filter((l) => l.source === selectedDevice.id || l.target === selectedDevice.id).length}
                </dd>
              </dl>
            </div>
          )}
          {!selection && <p className="muted">Klik node atau link untuk detail. Drag node untuk mengatur posisi.</p>}
          <p className="muted small">Total {stats.total} link.</p>
        </aside>
      </div>
    </div>
  )
}
