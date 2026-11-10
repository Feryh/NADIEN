import { useEffect, useMemo, useState } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import { api } from '../api'
import { Pager, PagerControls, usePaged, usePageSize } from './Pager'
import { Kpi } from './ui'

// The two tabs next to the topology map: naming standard report and the change log.

type Segment = 'backbone' | 'distribution' | 'last_mile'
type DeviceStatus = 'standard' | 'needs_fix' | 'legacy' | 'non_standard'
type PortStatus = 'standard' | 'names_peer' | 'no_peer' | 'empty'

interface NamingSummary {
  devices: { total: number; status: Partial<Record<DeviceStatus, number>>; conventions: Record<string, number> }
  ports: { total: number; status: Partial<Record<PortStatus, number>>; segments: Record<Segment, Partial<Record<PortStatus, number>>> }
  areas: ({ area: string; total: number } & Partial<Record<DeviceStatus, number>>)[]
  status_labels: { devices: Record<DeviceStatus, string>; ports: Record<PortStatus, string> }
}

interface DeviceRow {
  device_id: number
  hostname: string
  role: string | null
  pop: string | null
  area: string | null
  vendor: string | null
  convention: string | null
  status: DeviceStatus
  status_label: string
  issues: string[]
  suggestion: string | null
}

interface PortRow {
  link_id: number
  device_id: number
  hostname: string
  area: string | null
  port: string
  description: string | null
  peer: string
  peer_port: string | null
  segment: Segment
  segment_label: string
  capacity_gbps: number | null
  link_status: string | null
  status: PortStatus
  status_label: string
  recommended: string
}

interface Page<T> {
  total: number
  rows: T[]
}

interface DeviceScript {
  device_id: number
  hostname: string
  dialect: string | null
  dialect_label: string
  fixes: { port: string; description: string; previous: string | null }[]
  config: string[]
  rollback: string[]
}

const ALL_ROWS = 100000  // "Semua": one request for every row of the filter
const SEGMENT_LABEL: Record<Segment, string> = { backbone: 'Backbone', distribution: 'Distribusi', last_mile: 'Last Mile' }
const DEVICE_PILL: Record<DeviceStatus, string> = { standard: 'pill-ok', needs_fix: 'pill-warn', legacy: 'pill-muted', non_standard: 'pill-bad' }
const PORT_PILL: Record<PortStatus, string> = { standard: 'pill-ok', names_peer: 'pill-warn', no_peer: 'pill-bad', empty: 'pill-bad' }

function count(n: number | undefined): string {
  return (n ?? 0).toLocaleString('id-ID')
}

function pct(part: number | undefined, total: number): string {
  return total ? `${(((part ?? 0) / total) * 100).toFixed(1)}%` : '-'
}

function queryOf(filters: Record<string, string>): string {
  const q = new URLSearchParams()
  for (const [k, v] of Object.entries(filters)) if (v.trim()) q.set(k, v.trim())
  return q.toString()
}

export function NamingStandard() {
  const [summary, setSummary] = useState<NamingSummary | null>(null)
  const [view, setView] = useState<'devices' | 'ports'>('devices')
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api.get<NamingSummary>('/topology/naming').then(setSummary).catch((e: Error) => setError(e.message))
  }, [])

  if (error) return <div className="error">{error}</div>
  if (!summary) return <div className="card">Memuat…</div>
  const { devices, ports } = summary
  return (
    <div className="naming-page">
      <div className="kpis">
        <Kpi label="Hostname standar" value={pct(devices.status.standard, devices.total)}
          sub={`${count(devices.status.standard)} dari ${count(devices.total)} perangkat`} />
        <Kpi label="Hostname perlu perbaikan" value={count(devices.status.needs_fix)}
          sub={`${count(devices.status.non_standard)} non-standar · ${count(devices.status.legacy)} legacy`} />
        <Kpi label="Deskripsi port standar" value={pct(ports.status.standard, ports.total)}
          sub={`${count(ports.status.standard)} dari ${count(ports.total)} ujung link`} />
        <Kpi label="Deskripsi tanpa lawan / kosong" value={count((ports.status.no_peer ?? 0) + (ports.status.empty ?? 0))}
          sub={`${count(ports.status.names_peer)} menyebut lawan dengan format lain`} />
      </div>
      <div className="card">
        <div className="toolbar">
          <div className="segmented">
            <button type="button" className={view === 'devices' ? 'active' : ''} onClick={() => setView('devices')}>Hostname perangkat</button>
            <button type="button" className={view === 'ports' ? 'active' : ''} onClick={() => setView('ports')}>Deskripsi port link</button>
          </div>
          <span className="muted small">
            {view === 'devices'
              ? 'Standar: <KOTA>-<SITE>-<FUNGSI><n>-<MODEL>, cell site <REG>-<PROV>-<KOTA>-<NNNN>-<FUNGSI><n>-<VENDOR>'
              : 'Standar: [BB|DS|LM]TO-<HOSTNAME-LAWAN>-<PORT-LAWAN>[KAPASITAS]'}
          </span>
        </div>
        {view === 'devices' ? <DeviceNaming summary={summary} /> : <PortNaming summary={summary} />}
      </div>
    </div>
  )
}

function DeviceNaming({ summary }: { summary: NamingSummary }) {
  const [filters, setFilters] = useState({ status: '', area: '', q: '' })
  const [offset, setOffset] = useState(0)
  const [pageSize, setPageSize] = usePageSize()
  const [page, setPage] = useState<Page<DeviceRow> | null>(null)
  const [script, setScript] = useState<DeviceScript | null>(null)
  const [error, setError] = useState<string | null>(null)
  const query = useMemo(() => queryOf(filters), [filters])

  useEffect(() => setOffset(0), [query, pageSize])
  useEffect(() => {
    const timer = window.setTimeout(() => {
      api.get<Page<DeviceRow>>(`/topology/naming/devices?limit=${pageSize || ALL_ROWS}&offset=${offset}${query ? `&${query}` : ''}`)
        .then(setPage).catch((e: Error) => setError(e.message))
    }, 300)
    return () => window.clearTimeout(timer)
  }, [query, offset, pageSize])

  async function openScript(deviceId: number) {
    try {
      setScript(await api.get<DeviceScript>(`/topology/naming/devices/${deviceId}/script`))
    } catch (e) {
      setError((e as Error).message)
    }
  }

  return (
    <>
      {error && <div className="error" onClick={() => setError(null)}>{error}</div>}
      <div className="mop-meta access-filters">
        <label><span>Cari</span><input value={filters.q} placeholder="hostname, POP, vendor, usulan" maxLength={100}
          onChange={(e) => setFilters({ ...filters, q: e.target.value })} /></label>
        <label><span>Status</span><select value={filters.status} onChange={(e) => setFilters({ ...filters, status: e.target.value })}>
          <option value="">Semua status</option>
          {Object.entries(summary.status_labels.devices).map(([k, label]) => <option key={k} value={k}>{label}</option>)}
        </select></label>
        <label><span>Area</span><select value={filters.area} onChange={(e) => setFilters({ ...filters, area: e.target.value })}>
          <option value="">Semua area</option>
          {summary.areas.map((a) => <option key={a.area} value={a.area}>{a.area} ({count(a.total)})</option>)}
        </select></label>
        <a className="button" href={`/api/topology/naming/devices/export${query ? `?${query}` : ''}`}>Ekspor CSV</a>
      </div>
      <div className="table-wrap">
        <table>
          <thead><tr><th>Hostname</th><th>Area / POP</th><th>Role</th><th>Vendor</th><th>Status</th><th>Masalah</th><th>Usulan</th><th /></tr></thead>
          <tbody>
            {page?.rows.map((r) => (
              <tr key={r.device_id}>
                <td><RouterLink to={`/topology?level=neighbors&device_id=${r.device_id}`}>{r.hostname}</RouterLink></td>
                <td>{r.area ?? '-'} · {r.pop ?? '-'}</td>
                <td>{(r.role ?? '-').toUpperCase()}</td>
                <td>{r.vendor ?? '-'}</td>
                <td><span className={`pill ${DEVICE_PILL[r.status]}`}>{r.status_label}</span></td>
                <td className="small">{r.issues.join('; ')}</td>
                <td>{r.suggestion ? <code>{r.suggestion}</code> : ''}</td>
                <td><button type="button" onClick={() => openScript(r.device_id)} title="Perbaikan deskripsi port link perangkat ini">Script port</button></td>
              </tr>
            ))}
            {page && !page.rows.length && <tr><td colSpan={8} className="empty">Tidak ada perangkat untuk filter ini.</td></tr>}
          </tbody>
        </table>
      </div>
      {page && <ServerPager total={page.total} offset={offset} pageSize={pageSize} onOffset={setOffset} onPageSize={setPageSize} />}
      {script && <ScriptPanel script={script} onClose={() => setScript(null)} />}
    </>
  )
}

function PortNaming({ summary }: { summary: NamingSummary }) {
  const [filters, setFilters] = useState({ status: '', segment: '', area: '', q: '' })
  const [offset, setOffset] = useState(0)
  const [pageSize, setPageSize] = usePageSize()
  const [page, setPage] = useState<Page<PortRow> | null>(null)
  const [error, setError] = useState<string | null>(null)
  const query = useMemo(() => queryOf(filters), [filters])

  useEffect(() => setOffset(0), [query, pageSize])
  useEffect(() => {
    const timer = window.setTimeout(() => {
      api.get<Page<PortRow>>(`/topology/naming/ports?limit=${pageSize || ALL_ROWS}&offset=${offset}${query ? `&${query}` : ''}`)
        .then(setPage).catch((e: Error) => setError(e.message))
    }, 300)
    return () => window.clearTimeout(timer)
  }, [query, offset, pageSize])

  return (
    <>
      {error && <div className="error" onClick={() => setError(null)}>{error}</div>}
      <div className="mop-meta access-filters">
        <label><span>Cari</span><input value={filters.q} placeholder="hostname, port, lawan, deskripsi" maxLength={100}
          onChange={(e) => setFilters({ ...filters, q: e.target.value })} /></label>
        <label><span>Status</span><select value={filters.status} onChange={(e) => setFilters({ ...filters, status: e.target.value })}>
          <option value="">Semua status</option>
          {Object.entries(summary.status_labels.ports).map(([k, label]) => <option key={k} value={k}>{label}</option>)}
        </select></label>
        <label><span>Segmen</span><select value={filters.segment} onChange={(e) => setFilters({ ...filters, segment: e.target.value })}>
          <option value="">Semua segmen</option>
          {(Object.keys(SEGMENT_LABEL) as Segment[]).map((s) => <option key={s} value={s}>{SEGMENT_LABEL[s]}</option>)}
        </select></label>
        <label><span>Area</span><select value={filters.area} onChange={(e) => setFilters({ ...filters, area: e.target.value })}>
          <option value="">Semua area</option>
          {summary.areas.map((a) => <option key={a.area} value={a.area}>{a.area}</option>)}
        </select></label>
        <a className="button" href={`/api/topology/naming/ports/export${query ? `?${query}` : ''}`}>Ekspor CSV</a>
      </div>
      <div className="table-wrap">
        <table>
          <thead><tr><th>Perangkat</th><th>Port</th><th>Segmen</th><th>Lawan</th><th>Deskripsi sekarang</th><th>Status</th><th>Rekomendasi</th></tr></thead>
          <tbody>
            {page?.rows.map((r) => (
              <tr key={`${r.link_id}-${r.device_id}`}>
                <td>{r.hostname}{r.link_status === 'down' && <> <span className="pill pill-bad">down</span></>}</td>
                <td><code>{r.port}</code></td>
                <td>{r.segment_label}</td>
                <td>{r.peer}{r.peer_port ? <span className="muted"> · {r.peer_port}</span> : ''}</td>
                <td>{r.description ? <code>{r.description}</code> : <span className="muted">kosong</span>}</td>
                <td><span className={`pill ${PORT_PILL[r.status]}`}>{r.status_label}</span></td>
                <td><code>{r.recommended}</code></td>
              </tr>
            ))}
            {page && !page.rows.length && <tr><td colSpan={7} className="empty">Tidak ada port untuk filter ini.</td></tr>}
          </tbody>
        </table>
      </div>
      {page && <ServerPager total={page.total} offset={offset} pageSize={pageSize} onOffset={setOffset} onPageSize={setPageSize} />}
    </>
  )
}

/** Rows-per-page choice and page buttons for the server-paged naming tables. */
function ServerPager({ total, offset, pageSize, onOffset, onPageSize }: {
  total: number
  offset: number
  pageSize: number
  onOffset: (offset: number) => void
  onPageSize: (size: number) => void
}) {
  const pages = pageSize ? Math.max(1, Math.ceil(total / pageSize)) : 1
  return (
    <PagerControls total={total} page={pageSize ? Math.floor(offset / pageSize) : 0} pages={pages} pageSize={pageSize}
      onPage={(page) => onOffset(page * pageSize)} onPageSize={onPageSize} />
  )
}

function ScriptPanel({ script, onClose }: { script: DeviceScript; onClose: () => void }) {
  return (
    <section className="naming-script card">
      <div className="toolbar">
        <h3>Perbaikan deskripsi · {script.hostname}</h3>
        <span className="muted small">{script.dialect_label} · {script.fixes.length} port</span>
        <div className="spacer" />
        <button type="button" onClick={onClose}>Tutup</button>
      </div>
      {!script.fixes.length && <div className="muted">Semua deskripsi port link perangkat ini sudah standar.</div>}
      {script.fixes.length > 0 && !script.config.length && (
        <div className="notice">Vendor perangkat ini belum didukung generator; pakai kolom rekomendasi di tab deskripsi port.</div>
      )}
      {script.config.length > 0 && (
        <div className="naming-script-body">
          <div><b className="small">Config</b><pre>{script.config.join('\n')}</pre></div>
          <div><b className="small">Rollback</b><pre>{script.rollback.join('\n')}</pre></div>
        </div>
      )}
    </section>
  )
}

type EventKind = 'device_added' | 'link_added' | 'link_down' | 'link_up' | 'discovery'

interface TopologyEventRow {
  id: string
  at: string
  kind: EventKind
  title: string
  detail: string | null
  device_id: number | null
  link_id: number | null
}

const EVENT_LABEL: Record<EventKind, string> = {
  device_added: 'Perangkat baru', link_added: 'Link baru', link_down: 'Link down', link_up: 'Link up kembali', discovery: 'Discovery',
}
const EVENT_PILL: Record<EventKind, string> = {
  device_added: 'pill-ok', link_added: 'pill-ok', link_down: 'pill-bad', link_up: 'pill-warn', discovery: 'pill-muted',
}

interface UnknownLldpPeer {
  device_id: number
  device: string
  port: string
  bundle: string | null
  lldp_neighbor: string
  lldp_port: string
  description: string
  /** null when the description names a box the inventory does not have (e.g. a replaced one). */
  described_device_id: number | null
  described_device: string
}

/** Ports where LLDP sees a box the inventory lacks: no link is drawn there until that box is in the NMS. */
export function UnknownLldpPeers() {
  const [rows, setRows] = useState<UnknownLldpPeer[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [q, setQ] = useState('')

  useEffect(() => {
    api.get<UnknownLldpPeer[]>('/topology/lldp-unknown-peers').then(setRows).catch((e: Error) => setError(e.message))
  }, [])
  const needle = q.trim().toLowerCase()
  const shown = (rows ?? []).filter((r) => !needle || [r.device, r.port, r.lldp_neighbor, r.described_device, r.description]
    .some((v) => v.toLowerCase().includes(needle)))
  const paged = usePaged(shown, needle)

  return (
    <div className="card">
      <div className="toolbar">
        <h2>LLDP ke perangkat di luar inventory</h2>
        <span className="muted small">
          LLDP adalah sumber utama: port ini melihat perangkat yang tidak ada di Observium, jadi link ke perangkat di
          deskripsi tidak digambar. Masukkan perangkat tetangga LLDP ke Observium agar link-nya muncul.
        </span>
        <div className="spacer" />
        <input placeholder="Cari perangkat, port, tetangga…" value={q} onChange={(e) => setQ(e.target.value)} />
      </div>
      {error && <div className="error" onClick={() => setError(null)}>{error}</div>}
      <div className="table-wrap">
        <table>
          <thead><tr><th>Perangkat</th><th>Port</th><th>Tetangga LLDP</th><th>Menurut deskripsi</th><th>Deskripsi</th></tr></thead>
          <tbody>
            {paged.pageRows.map((r) => (
              <tr key={`${r.device_id}|${r.port}|${r.lldp_neighbor}`}>
                <td><RouterLink to={`/topology?level=neighbors&device_id=${r.device_id}`}>{r.device}</RouterLink></td>
                <td className="mono">{r.port}{r.bundle ? <span className="muted"> ({r.bundle})</span> : null}</td>
                <td><span className="pill">?</span> {r.lldp_neighbor} <span className="mono muted">{r.lldp_port}</span></td>
                <td>{r.described_device_id != null
                  ? <RouterLink to={`/topology?level=neighbors&device_id=${r.described_device_id}`}>{r.described_device}</RouterLink>
                  : <span title="Tidak ada di inventory">{r.described_device} <span className="muted">(tidak di inventory)</span></span>}</td>
                <td className="small">{r.description}</td>
              </tr>
            ))}
            {rows && !shown.length && <tr><td colSpan={5} className="empty">Tidak ada.</td></tr>}
            {!rows && !error && <tr><td colSpan={5} className="empty">Memuat…</td></tr>}
          </tbody>
        </table>
      </div>
      <Pager paged={paged} unit="port" />
    </div>
  )
}

export function TopologyChanges() {
  const [kind, setKind] = useState<EventKind | ''>('')
  const [days, setDays] = useState(7)
  const [rows, setRows] = useState<TopologyEventRow[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const since = new Date(Date.now() - days * 24 * 3600_000).toISOString()
    const q = new URLSearchParams({ since, limit: '1000' })
    if (kind) q.set('kind', kind)
    setRows(null)
    api.get<TopologyEventRow[]>(`/topology/events?${q}`).then(setRows).catch((e: Error) => setError(e.message))
  }, [kind, days])
  const paged = usePaged(rows ?? [], `${kind}|${days}`)

  return (
    <div className="card">
      <div className="toolbar">
        <h2>Log perubahan topologi</h2>
        <span className="muted small">Dari discovery (link baru, down, up kembali) dan inventory (perangkat baru)</span>
      </div>
      {error && <div className="error" onClick={() => setError(null)}>{error}</div>}
      <div className="mop-meta access-filters">
        <label><span>Jenis</span><select value={kind} onChange={(e) => setKind(e.target.value as EventKind | '')}>
          <option value="">Semua jenis</option>
          {(Object.keys(EVENT_LABEL) as EventKind[]).map((k) => <option key={k} value={k}>{EVENT_LABEL[k]}</option>)}
        </select></label>
        <label><span>Periode</span><select value={days} onChange={(e) => setDays(Number(e.target.value))}>
          <option value={1}>24 jam terakhir</option>
          <option value={7}>7 hari terakhir</option>
          <option value={30}>30 hari terakhir</option>
        </select></label>
      </div>
      <div className="table-wrap">
        <table>
          <thead><tr><th>Waktu</th><th>Jenis</th><th>Perubahan</th><th>Detail</th></tr></thead>
          <tbody>
            {paged.pageRows.map((r) => (
              <tr key={r.id}>
                <td>{new Date(r.at).toLocaleString('id-ID')}</td>
                <td><span className={`pill ${EVENT_PILL[r.kind]}`}>{EVENT_LABEL[r.kind]}</span></td>
                <td>{r.device_id != null
                  ? <RouterLink to={`/topology?level=neighbors&device_id=${r.device_id}`}>{r.title}</RouterLink>
                  : r.title}</td>
                <td className="small">{r.detail ?? ''}</td>
              </tr>
            ))}
            {rows && !rows.length && <tr><td colSpan={4} className="empty">Tidak ada perubahan pada periode ini.</td></tr>}
            {!rows && !error && <tr><td colSpan={4} className="empty">Memuat…</td></tr>}
          </tbody>
        </table>
      </div>
      <Pager paged={paged} unit="perubahan" />
    </div>
  )
}
