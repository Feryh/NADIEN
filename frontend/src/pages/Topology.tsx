import cytoscape, { Core, ElementDefinition } from 'cytoscape'
import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link as RouterLink, useSearchParams } from 'react-router-dom'
import { api, CapacityColor, DiscoveryResult } from '../api'
import { CAPACITY_HEX, fmtG } from '../components/ui'

// Drill-down: all areas -> POPs of one area -> devices of one POP (grouped by site, layered by role).
type Level = 'area' | 'pop' | 'device'

interface EdgeData {
  id: string
  source: string
  target: string
  links: number
  down: number
  capacity_gbps: number
  current_gbps: number
  utilization_pct: number
  committed_pct: number
  color: CapacityColor
}

interface NodeData {
  id: string
  kind: 'area' | 'pop' | 'device' | 'site' | 'external_area' | 'external_pop'
  label: string
  name?: string
  area?: string
  pops?: number
  devices?: number
  sites?: number
  links?: number
  categories?: Record<string, number>
  internal?: Omit<EdgeData, 'id' | 'source' | 'target'>
  lat?: number | null
  lon?: number | null
  site?: string
  role?: string
  category?: string
  tier?: number
  vendor?: string | null
  status?: string
  device_id?: number
  x?: number | null
  y?: number | null
}

interface View {
  level: Level
  area?: string
  pop?: string
  pop_name?: string
  nodes: NodeData[]
  edges: EdgeData[]
}

const ROLE_COLOR: Record<string, string> = {
  p: '#6366f1', dci: '#f97316', pe: '#0ea5e9', bng: '#e879f9', agg: '#14b8a6', access: '#64748b', olt: '#a3e635',
}
const TIER_LABEL = ['Core (P/DCI)', 'PE / BNG', 'Metro / Aggregation', 'OLT / Access', 'Lainnya']
const CATEGORY_LABEL: Record<string, string> = { router: 'Router', metro: 'Metro', olt: 'OLT', access: 'Access', other: 'Lainnya' }

const STYLE = [
  {
    selector: 'node',
    style: {
      label: 'data(display)',
      'text-wrap': 'wrap',
      color: '#e2e8f0',
      'font-size': 11,
      'min-zoomed-font-size': 7,
      'text-valign': 'bottom',
      'text-margin-y': 5,
      'background-color': '#334155',
    },
  },
  {
    selector: 'node[kind="area"], node[kind="pop"]',
    style: {
      shape: 'round-rectangle',
      width: 'data(size)',
      height: 'data(size)',
      'background-color': '#0ea5e9',
      'background-opacity': 0.25,
      'border-color': '#38bdf8',
      'border-width': 2,
      'text-valign': 'center',
      'text-margin-y': 0,
      'font-size': 12,
      'font-weight': 'bold',
    },
  },
  {
    selector: 'node[kind="external_area"], node[kind="external_pop"]',
    style: {
      shape: 'round-rectangle',
      width: 70,
      height: 34,
      'background-color': '#1e293b',
      'border-color': '#64748b',
      'border-style': 'dashed',
      'border-width': 1.5,
      'text-valign': 'center',
      'text-margin-y': 0,
      color: '#94a3b8',
      'font-size': 10,
    },
  },
  {
    selector: 'node[kind="site"]',
    style: {
      shape: 'round-rectangle',
      'background-color': '#1e293b',
      'background-opacity': 0.5,
      'border-color': '#334155',
      'border-width': 1,
      'text-valign': 'top',
      'text-margin-y': -4,
      color: '#94a3b8',
      'font-size': 13,
      'font-weight': 'bold',
      padding: '18px',
    },
  },
  {
    selector: 'node[kind="device"]',
    style: { width: 22, height: 22, 'background-color': 'data(roleColor)', 'font-size': 9, 'border-width': 1, 'border-color': '#0f172a' },
  },
  { selector: 'node[status="maintenance"], node[status="decommissioned"]', style: { opacity: 0.5 } },
  {
    selector: 'edge',
    style: {
      width: 'data(width)',
      'line-color': 'data(colorHex)',
      'curve-style': 'bezier',
      label: 'data(display)',
      'font-size': 9,
      'min-zoomed-font-size': 8,
      color: '#cbd5e1',
      'text-background-color': '#0f172a',
      'text-background-opacity': 0.85,
      'text-background-padding': '2px',
      'text-rotation': 'autorotate',
      opacity: 0.85,
    },
  },
  { selector: 'edge[allDown = 1]', style: { 'line-style': 'dashed', 'line-color': '#64748b' } },
  { selector: '.faded', style: { opacity: 0.12 } },
  { selector: '.hit', style: { 'border-color': '#facc15', 'border-width': 4 } },
  { selector: ':selected', style: { 'overlay-color': '#38bdf8', 'overlay-opacity': 0.25, 'overlay-padding': 6 } },
] as unknown as cytoscape.CytoscapeOptions['style']

function edgeWidth(capacity: number): number {
  return Math.min(1.5 + Math.log10(Math.max(capacity, 1)) * 2.2, 12)
}

function nodeDisplay(n: NodeData): string {
  switch (n.kind) {
    case 'area':
      return `${n.label}\n${n.pops} POP · ${n.devices} perangkat`
    case 'pop':
      return `${n.label}\n${n.devices} perangkat · ${n.sites} site`
    case 'external_area':
      return `→ ${n.label}`
    case 'external_pop':
      return `→ ${n.label}`
    default:
      return n.label
  }
}

/** Device level: sites as columns in a grid, devices in role rows inside each site, other POPs on the right. */
function deviceLayout(view: View): Record<string, { x: number; y: number }> {
  const pos: Record<string, { x: number; y: number }> = {}
  const devices = view.nodes.filter((n) => n.kind === 'device')
  const sites = view.nodes.filter((n) => n.kind === 'site').map((s) => s.id)
  const perRow = 9
  const colsPerSiteRow = Math.max(1, Math.ceil(Math.sqrt(sites.length * 1.6)))
  let siteX = 0
  let siteY = 0
  let rowHeight = 0
  sites.forEach((siteId, i) => {
    if (i > 0 && i % colsPerSiteRow === 0) {
      siteX = 0
      siteY += rowHeight + 140
      rowHeight = 0
    }
    const members = devices.filter((d) => d.site === siteId)
    const tiers = [...new Set(members.map((d) => d.tier ?? 4))].sort()
    let y = siteY
    let width = 0
    for (const tier of tiers) {
      const row = members.filter((d) => (d.tier ?? 4) === tier)
      row.forEach((d, k) => {
        pos[d.id] = { x: siteX + (k % perRow) * 110, y: y + Math.floor(k / perRow) * 70 }
      })
      width = Math.max(width, Math.min(row.length, perRow) * 110)
      y += Math.ceil(row.length / perRow) * 70 + 40
    }
    rowHeight = Math.max(rowHeight, y - siteY)
    siteX += width + 120
  })
  const maxX = Math.max(0, ...Object.values(pos).map((p) => p.x))
  view.nodes
    .filter((n) => n.kind === 'external_pop')
    .forEach((n, k) => {
      pos[n.id] = { x: maxX + 320 + Math.floor(k / 25) * 130, y: (k % 25) * 55 }
    })
  return pos
}

function buildElements(view: View): ElementDefinition[] {
  const maxDevices = Math.max(1, ...view.nodes.map((n) => n.devices ?? 0))
  const nodes: ElementDefinition[] = view.nodes.map((n) => ({
    data: {
      ...n,
      display: nodeDisplay(n),
      parent: n.kind === 'device' ? n.site : undefined,
      roleColor: ROLE_COLOR[n.role ?? ''] ?? '#64748b',
      size: n.kind === 'area' ? 70 + 90 * Math.sqrt((n.devices ?? 0) / maxDevices) : 46 + 50 * Math.sqrt((n.devices ?? 0) / maxDevices),
    },
  }))
  const edges: ElementDefinition[] = view.edges.map((e) => ({
    data: {
      ...e,
      display: `${e.links > 1 ? `${e.links}× · ` : ''}${fmtG(e.capacity_gbps)} · ${e.committed_pct.toFixed(0)}%`,
      colorHex: CAPACITY_HEX[e.color],
      width: edgeWidth(e.capacity_gbps),
      allDown: e.down === e.links ? 1 : 0,
    },
  }))
  return [...nodes, ...edges]
}

function runLayout(cy: Core, view: View) {
  if (view.level === 'device') {
    const auto = deviceLayout(view)
    const saved: Record<string, { x: number; y: number }> = {}
    for (const n of view.nodes) if (n.kind === 'device' && n.x != null && n.y != null) saved[n.id] = { x: n.x, y: n.y }
    cy.layout({
      name: 'preset',
      padding: 30,
      positions: (node: cytoscape.NodeSingular) => saved[node.id()] ?? auto[node.id()] ?? { x: 0, y: 0 },
    } as cytoscape.LayoutOptions).run()
    return
  }
  const mapped = view.nodes.filter((n) => n.lon != null && n.lat != null)
  if (view.level === 'area' && mapped.length >= Math.max(2, view.nodes.length - 1)) {
    const k = 120
    let unmapped = 0
    cy.layout({
      name: 'preset',
      padding: 40,
      positions: (node: cytoscape.NodeSingular) => {
        const n = node.data() as NodeData
        if (n.lon != null && n.lat != null) return { x: (n.lon - 95) * k, y: -n.lat * k }
        return { x: (unmapped++ * 300), y: 12 * k }
      },
    } as cytoscape.LayoutOptions).run()
    return
  }
  cy.layout({
    name: 'cose',
    animate: false,
    padding: 40,
    nodeRepulsion: () => 60000,
    idealEdgeLength: () => 170,
    gravity: 0.3,
    numIter: 2000,
    randomize: false,
  } as cytoscape.LayoutOptions).run()
}

export default function Topology() {
  const [params, setParams] = useSearchParams()
  const level = (params.get('level') as Level) || 'area'
  const area = params.get('area') ?? undefined
  const pop = params.get('pop') ?? undefined
  const focus = params.get('focus') ?? undefined

  const container = useRef<HTMLDivElement>(null)
  const cyRef = useRef<Core | null>(null)
  const [view, setView] = useState<View | null>(null)
  const [loading, setLoading] = useState(false)
  const [selected, setSelected] = useState<{ node?: NodeData; edge?: EdgeData } | null>(null)
  const [message, setMessage] = useState<{ text: string; error?: boolean } | null>(null)
  const [search, setSearch] = useState('')
  const [colorFilter, setColorFilter] = useState<'all' | 'warn' | 'red'>('all')
  const [syncPhase, setSyncPhase] = useState<string | null>(null)
  const [dirty, setDirty] = useState(false)
  const [busy, setBusy] = useState(false)

  const go = useCallback(
    (next: { level: Level; area?: string; pop?: string; focus?: string }) => {
      const p: Record<string, string> = { level: next.level }
      if (next.area) p.area = next.area
      if (next.pop) p.pop = next.pop
      if (next.focus) p.focus = next.focus
      setParams(p)
    },
    [setParams],
  )

  const load = useCallback(async () => {
    setLoading(true)
    setSelected(null)
    setDirty(false)
    try {
      const q = new URLSearchParams({ level })
      if (area) q.set('area', area)
      if (pop) q.set('pop', pop)
      setView(await api.get<View>(`/topology/view?${q}`))
    } catch (e) {
      setMessage({ text: (e as Error).message, error: true })
      setView(null)
    } finally {
      setLoading(false)
    }
  }, [level, area, pop])

  useEffect(() => {
    load()
  }, [load])

  // A running collector sync rebuilds topology itself and holds the DB writer; block manual discovery.
  useEffect(() => {
    let alive = true
    const check = () =>
      api
        .get<{ status: string; phase: string }>('/collectors/jobs/current')
        .then((j) => alive && setSyncPhase(j.status === 'running' ? j.phase : null))
        .catch(() => undefined)
    check()
    const t = setInterval(check, 10000)
    return () => {
      alive = false
      clearInterval(t)
    }
  }, [])

  useEffect(() => {
    if (!view || !container.current) return
    const cy = cytoscape({
      container: container.current,
      elements: buildElements(view),
      style: STYLE,
      wheelSensitivity: 0.25,
      minZoom: 0.05,
      maxZoom: 3,
    })
    cy.on('tap', 'node', (e) => setSelected({ node: e.target.data() as NodeData }))
    cy.on('tap', 'edge', (e) => setSelected({ edge: e.target.data() as EdgeData }))
    cy.on('tap', (e) => {
      if (e.target === cy) setSelected(null)
    })
    cy.on('dbltap', 'node', (e) => {
      const n = e.target.data() as NodeData
      if (n.kind === 'area' || n.kind === 'external_area') go({ level: 'pop', area: n.label })
      else if (n.kind === 'pop' || n.kind === 'external_pop') go({ level: 'device', pop: n.label })
    })
    cy.on('dragfree', 'node[kind="device"]', () => setDirty(true))
    cyRef.current = cy
    runLayout(cy, view)
    if (focus) {
      const hit = cy.nodes().filter((n) => (n.data('label') as string)?.toUpperCase() === focus.toUpperCase())
      if (hit.nonempty()) {
        hit.addClass('hit')
        cy.animate({ center: { eles: hit }, zoom: 1.4 }, { duration: 300 })
        setSelected({ node: hit.first().data() as NodeData })
      }
    }
    return () => cy.destroy()
  }, [view, go, focus])

  useEffect(() => {
    const cy = cyRef.current
    if (!cy) return
    cy.edges().forEach((edge) => {
      const color = edge.data('color') as CapacityColor
      const visible = colorFilter === 'all' || (colorFilter === 'red' ? color === 'red' : color !== 'green')
      edge.style('display', visible ? 'element' : 'none')
    })
  }, [colorFilter, view])

  async function findDevice(e: FormEvent) {
    e.preventDefault()
    const term = search.trim()
    if (!term) return
    try {
      const [devices, pops] = await Promise.all([
        api.get<{ hostname: string; pop_id: number }[]>(`/devices?q=${encodeURIComponent(term)}&limit=20`),
        api.get<{ id: number; code: string }[]>('/pops?limit=10000'),
      ])
      const popMatch = pops.find((p) => p.code.toUpperCase() === term.toUpperCase())
      if (popMatch) return go({ level: 'device', pop: popMatch.code })
      const exact = devices.find((d) => d.hostname.toUpperCase() === term.toUpperCase()) ?? devices[0]
      if (!exact) return setMessage({ text: `"${term}" tidak ditemukan`, error: true })
      const code = pops.find((p) => p.id === exact.pop_id)?.code
      if (code) go({ level: 'device', pop: code, focus: exact.hostname })
    } catch (err) {
      setMessage({ text: (err as Error).message, error: true })
    }
  }

  async function savePositions() {
    const cy = cyRef.current
    if (!cy) return
    const positions = cy.nodes('[kind="device"]').map((n) => ({
      device_id: Number(n.data('device_id')),
      x: Math.round(n.position('x')),
      y: Math.round(n.position('y')),
    }))
    try {
      await api.put('/topology/positions', positions)
      setDirty(false)
      setMessage({ text: `Posisi ${positions.length} perangkat di ${pop} disimpan.` })
    } catch (e) {
      setMessage({ text: (e as Error).message, error: true })
    }
  }

  async function autoAssign() {
    try {
      const r = await api.post<{ assigned: number; devices_moved: number; pops_created: string[] }>('/areas/auto-assign')
      const left = await api.get<{ unmapped_pops: string[] }>('/areas')
      setMessage({
        text:
          `${r.assigned} POP diberi area` +
          (r.devices_moved ? `, ${r.devices_moved} perangkat dipindah dari UNASSIGNED ke POP kotanya` : '') +
          (r.pops_created.length ? ` (POP baru: ${r.pops_created.join(', ')})` : '') +
          `. ${left.unmapped_pops.length} POP belum dipetakan` +
          (left.unmapped_pops.length ? ` (isi kolom Area di Inventory → Metro POP): ${left.unmapped_pops.slice(0, 15).join(', ')}${left.unmapped_pops.length > 15 ? ', …' : ''}` : ''),
      })
      await load()
    } catch (e) {
      setMessage({ text: (e as Error).message, error: true })
    }
  }

  async function discover() {
    setBusy(true)
    try {
      const r = await api.post<DiscoveryResult>('/topology/discover')
      setMessage({ text: `Discovery: ${r.lldp_links} link LLDP, ${r.config_links} dari config · ${r.created} baru, ${r.marked_down} down` })
      await load()
    } catch (e) {
      setMessage({ text: (e as Error).message, error: true })
    } finally {
      setBusy(false)
    }
  }

  const stats = useMemo(() => {
    const edges = view?.edges ?? []
    return {
      groups: edges.length,
      links: edges.reduce((s, e) => s + e.links, 0),
      red: edges.filter((e) => e.color === 'red').length,
      yellow: edges.filter((e) => e.color === 'yellow').length,
      green: edges.filter((e) => e.color === 'green').length,
    }
  }, [view])

  const node = selected?.node
  const edge = selected?.edge
  const nameOf = (id: string) => view?.nodes.find((n) => n.id === id)?.label ?? id

  return (
    <div className="topology">
      <div className="toolbar card">
        <nav className="crumbs">
          <a href="#" onClick={(e) => { e.preventDefault(); go({ level: 'area' }) }} className={level === 'area' ? 'current' : ''}>
            Semua area
          </a>
          {(level === 'pop' || level === 'device') && (view?.area || area) && (
            <>
              <span>›</span>
              <a href="#" onClick={(e) => { e.preventDefault(); go({ level: 'pop', area: view?.area ?? area }) }} className={level === 'pop' ? 'current' : ''}>
                {view?.area ?? area}
              </a>
            </>
          )}
          {level === 'device' && (
            <>
              <span>›</span>
              <span className="current">
                {pop} {view?.pop_name && view.pop_name !== pop ? `· ${view.pop_name}` : ''}
              </span>
            </>
          )}
        </nav>
        <form onSubmit={findDevice} className="inline">
          <input className="search" placeholder="Cari hostname / kode POP…" value={search} onChange={(e) => setSearch(e.target.value)} />
          <button type="submit">Cari</button>
        </form>
        <select value={colorFilter} onChange={(e) => setColorFilter(e.target.value as 'all' | 'warn' | 'red')}>
          <option value="all">Semua link</option>
          <option value="warn">Kuning + Merah</option>
          <option value="red">Merah saja</option>
        </select>
        <div className="spacer" />
        {level === 'area' && view?.nodes.some((n) => n.label === 'Belum dipetakan') && (
          <button onClick={autoAssign} title="Isi area POP yang masih kosong dari kode kota (tidak menimpa yang sudah diisi)">
            Isi area otomatis
          </button>
        )}
        {level === 'device' && (
          <button onClick={savePositions} disabled={!dirty}>
            Simpan posisi
          </button>
        )}
        <button
          className="primary"
          onClick={discover}
          disabled={busy || !!syncPhase}
          title={syncPhase ? `Sync sedang berjalan (${syncPhase}); topologi dibangun otomatis di akhir sync` : undefined}
        >
          {busy ? 'Discovering…' : syncPhase ? 'Sync berjalan…' : 'Run discovery'}
        </button>
      </div>

      {message && (
        <div className={message.error ? 'error' : 'notice'} onClick={() => setMessage(null)}>
          {message.text}
        </div>
      )}

      <div className="topology-body">
        <div className="graph card">
          <div className="graph-canvas" ref={container} />
          {loading && <div className="graph-loading">Memuat…</div>}
        </div>
        <aside className="side card">
          <div className="small muted">
            {level === 'area' && 'Klik dua kali area untuk melihat POP di dalamnya.'}
            {level === 'pop' && 'Klik dua kali POP untuk melihat perangkat per site; kotak putus-putus = area lain.'}
            {level === 'device' && 'Perangkat dikelompokkan per site, baris atas ke bawah: Core → PE/BNG → Metro → OLT/Access. Kotak putus-putus = POP lain (klik dua kali untuk pindah).'}
          </div>
          <hr />
          <div className="legend">
            <div><span className="swatch" style={{ background: CAPACITY_HEX.green }} /> &lt; 60% ({stats.green})</div>
            <div><span className="swatch" style={{ background: CAPACITY_HEX.yellow }} /> 60–80% ({stats.yellow})</div>
            <div><span className="swatch" style={{ background: CAPACITY_HEX.red }} /> ≥ 80% ({stats.red})</div>
            <div><span className="swatch dashed" /> semua link di jalur ini down</div>
            <div className="muted small">
              {stats.groups} jalur · {stats.links} link fisik/logis. Label: jumlah link · kapasitas · committed %.
            </div>
            {level === 'device' && (
              <div className="role-legend">
                {Object.entries(ROLE_COLOR).map(([role, color]) => (
                  <span key={role}><span className="dot" style={{ background: color }} /> {role.toUpperCase()}</span>
                ))}
              </div>
            )}
          </div>
          <hr />
          {node && (
            <div className="detail">
              <h3>{node.label}</h3>
              <dl>
                {node.kind === 'area' && (
                  <>
                    <dt>POP</dt><dd>{node.pops}</dd>
                    <dt>Perangkat</dt><dd>{node.devices}</dd>
                  </>
                )}
                {node.kind === 'pop' && (
                  <>
                    <dt>Nama</dt><dd>{node.name}</dd>
                    <dt>Perangkat</dt><dd>{node.devices} di {node.sites} site</dd>
                    <dt>Kategori</dt>
                    <dd>{Object.entries(node.categories ?? {}).map(([c, n]) => `${CATEGORY_LABEL[c] ?? c} ${n}`).join(' · ')}</dd>
                  </>
                )}
                {node.kind === 'device' && (
                  <>
                    <dt>Site</dt><dd>{node.site?.replace('site:', '')}</dd>
                    <dt>Role</dt><dd>{node.role?.toUpperCase()} ({TIER_LABEL[node.tier ?? 4]})</dd>
                    <dt>Vendor</dt><dd>{node.vendor ?? '–'}</dd>
                    <dt>Status</dt><dd>{node.status}</dd>
                  </>
                )}
                {(node.kind === 'external_area' || node.kind === 'external_pop') && (
                  <>
                    <dt>Area</dt><dd>{node.area ?? node.label}</dd>
                    {node.devices != null && (<><dt>Perangkat terhubung</dt><dd>{node.devices}</dd></>)}
                  </>
                )}
              </dl>
              {node.internal && node.internal.links > 0 && (
                <p className="small muted">
                  Link internal: {node.internal.links} · {fmtG(node.internal.capacity_gbps)} · {node.internal.committed_pct}% committed
                  {node.internal.down ? ` · ${node.internal.down} down` : ''}
                </p>
              )}
              {(node.kind === 'area' || node.kind === 'external_area') && (
                <button onClick={() => go({ level: 'pop', area: node.label })}>Buka POP di area ini →</button>
              )}
              {(node.kind === 'pop' || node.kind === 'external_pop') && (
                <button onClick={() => go({ level: 'device', pop: node.label })}>Buka perangkat di {node.label} →</button>
              )}
              {node.kind === 'device' && (
                <RouterLink to={`/ports?q=${encodeURIComponent(node.label)}`}>Lihat port perangkat →</RouterLink>
              )}
            </div>
          )}
          {edge && (
            <div className="detail">
              <h3>{nameOf(edge.source)} ↔ {nameOf(edge.target)}</h3>
              <dl>
                <dt>Link</dt><dd>{edge.links}{edge.down ? ` (${edge.down} down)` : ''}</dd>
                <dt>Kapasitas</dt><dd>{fmtG(edge.capacity_gbps)}</dd>
                <dt>Trafik</dt><dd>{fmtG(edge.current_gbps)} ({edge.utilization_pct}%)</dd>
                <dt>Committed</dt><dd>{edge.committed_pct}%</dd>
              </dl>
              {level === 'device' && <RouterLink to="/capacity">Detail kapasitas & forecast →</RouterLink>}
            </div>
          )}
        </aside>
      </div>
    </div>
  )
}
