import { useCallback, useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { api, TopologySearch } from '../api'
import { fmtG, Kpi } from '../components/ui'
import './DigitalTwin.css'

// Phase 10 — Digital Twin: saved what-if scenarios (failures, growth, upgrades, virtual links) simulated on a copy
// of the network, and IS-IS/SRv6 migration readiness per area. Production data is never changed.

type Change =
  | { type: 'fail_link'; link_id: number; label?: string }
  | { type: 'fail_device'; device_id: number; label?: string }
  | { type: 'fail_pop'; pop_id: number; label?: string }
  | { type: 'fail_fiber'; fiber_id: number; label?: string }
  | { type: 'growth'; pct: number; area: string | null; label?: string }
  | { type: 'upgrade_link'; link_id: number; capacity_gbps: number; label?: string }
  | { type: 'add_link'; a_device_id: number; b_device_id: number; capacity_gbps: number; name?: string; label?: string }

interface LinkRow {
  link_id: number | null
  name: string
  a_device: string | null
  b_device: string | null
  capacity_gbps: number
  before_pct: number | null
  after_pct: number
  load_gbps: number
  level: 'critical' | 'warning' | 'normal'
  virtual: boolean
}

interface ServiceRow { service_id: number; circuit_id: string; service_type: string; bandwidth_mbps: number; reason: string }

interface Result {
  changes: string[]
  totals: { failed_links: number; failed_devices: number; rerouted_gbps: number; lost_unroutable_gbps: number;
    lost_terminating_gbps: number; isolated_devices: number }
  link_levels: { before: Record<string, number>; after: Record<string, number> }
  links: LinkRow[]
  links_changed: number
  areas: { area: string; before_critical: number; after_critical: number; before_max_pct: number; after_max_pct: number }[]
  isolated: { hostname: string; area: string }[]
  unroutable: { link: string; traffic_gbps: number }[]
  services: { down: ServiceRow[]; on_backup: ServiceRow[]; unprotected: ServiceRow[]; counts: Record<string, number> }
  notes: string[]
}

interface ScenarioSummary {
  id: number
  name: string
  description: string | null
  changes: number
  run_at: string | null
  totals: Result['totals'] | null
  services: Record<string, number> | null
}

interface Scenario extends Omit<ScenarioSummary, 'changes'> {
  changes: Change[]
  result: Result | null
}

export default function DigitalTwin() {
  const [params, setParams] = useSearchParams()
  const tab = params.get('tab') === 'srv6' ? 'srv6' : 'scenarios'
  return (
    <div className="capacity">
      <div className="tabs">
        <button className={tab === 'scenarios' ? 'active' : ''} onClick={() => setParams({})}>Skenario what-if</button>
        <button className={tab === 'srv6' ? 'active' : ''} onClick={() => setParams({ tab: 'srv6' })}>Migrasi IS-IS/SRv6</button>
      </div>
      {tab === 'scenarios' ? <Scenarios /> : <Srv6Migration />}
    </div>
  )
}

// --------------------------------------------------------------------------- scenarios

const EMPTY: Scenario = { id: 0, name: '', description: '', changes: [], result: null, run_at: null, totals: null, services: null }

function Scenarios() {
  const [list, setList] = useState<ScenarioSummary[]>([])
  const [current, setCurrent] = useState<Scenario | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(() => {
    api.get<ScenarioSummary[]>('/twin/scenarios').then(setList).catch((e: Error) => setError(e.message))
  }, [])
  useEffect(load, [load])

  const open = (id: number) => {
    setError(null)
    api.get<Scenario>(`/twin/scenarios/${id}`).then(setCurrent).catch((e: Error) => setError(e.message))
  }

  const body = (s: Scenario) => ({ name: s.name.trim(), description: s.description || null, changes: s.changes })

  const save = (run: boolean) => {
    if (!current) return
    setBusy(true)
    setError(null)
    const saved = current.id ? api.patch<Scenario>(`/twin/scenarios/${current.id}`, body(current)) : api.post<Scenario>('/twin/scenarios', body(current))
    saved
      .then((s) => (run ? api.post<Scenario>(`/twin/scenarios/${s.id}/run`) : Promise.resolve(s)))
      .then((s) => { setCurrent(s); load() })
      .catch((e: Error) => setError(e.message))
      .finally(() => setBusy(false))
  }

  const tryUnsaved = () => {
    if (!current) return
    setBusy(true)
    setError(null)
    api.post<Result>('/twin/simulate', { changes: current.changes })
      .then((result) => setCurrent({ ...current, result, run_at: null }))
      .catch((e: Error) => setError(e.message))
      .finally(() => setBusy(false))
  }

  const remove = () => {
    if (!current?.id || !window.confirm(`Hapus skenario ${current.name}?`)) return
    api.del(`/twin/scenarios/${current.id}`).then(() => { setCurrent(null); load() }).catch((e: Error) => setError(e.message))
  }

  return (
    <>
      <div className="card">
        <div className="toolbar">
          <h2>Skenario</h2>
          <div className="spacer" />
          <button className="primary" onClick={() => { setError(null); setCurrent({ ...EMPTY }) }}>Skenario baru</button>
        </div>
        <p className="hint">
          Matikan link, device, POP, atau fiber (SRLG) secara virtual, naikkan trafik per area, upgrade link, atau tambah
          link virtual, lalu lihat dampaknya: trafik yang dirutekan ulang, link yang jadi overload, perangkat yang
          terisolasi, dan layanan yang putus atau kehilangan proteksi. Data produksi tidak diubah.
        </p>
        {error && <div className="error">{error}</div>}
        <div className="table-wrap twin-list">
          <table>
            <thead><tr><th>Nama</th><th className="num">Perubahan</th><th>Terakhir dijalankan</th><th>Ringkasan hasil</th></tr></thead>
            <tbody>
              {list.map((s) => (
                <tr key={s.id} className={`clickable ${current?.id === s.id ? 'selected' : ''}`} onClick={() => open(s.id)}>
                  <td><b>{s.name}</b>{s.description && <div className="small muted">{s.description}</div>}</td>
                  <td className="num">{s.changes}</td>
                  <td>{s.run_at ? s.run_at.slice(0, 16).replace('T', ' ') : <span className="muted">belum</span>}</td>
                  <td className="small">{s.totals ? `${fmtG(s.totals.rerouted_gbps)} dirutekan ulang · ${fmtG(s.totals.lost_unroutable_gbps + s.totals.lost_terminating_gbps)} hilang · ${s.totals.isolated_devices} device terisolasi · ${s.services?.down ?? 0} layanan putus` : '—'}</td>
                </tr>
              ))}
              {!list.length && <tr><td colSpan={4} className="empty">Belum ada skenario.</td></tr>}
            </tbody>
          </table>
        </div>
      </div>

      {current && (
        <div className="card">
          <div className="toolbar">
            <h2>{current.id ? current.name : 'Skenario baru'}</h2>
            <div className="spacer" />
            <button onClick={tryUnsaved} disabled={busy || !current.changes.length}>Coba tanpa simpan</button>
            <button onClick={() => save(false)} disabled={busy || !current.name.trim()}>Simpan</button>
            <button className="primary" onClick={() => save(true)} disabled={busy || !current.name.trim() || !current.changes.length}>
              {busy ? 'Menghitung…' : 'Simpan & jalankan'}
            </button>
            {current.id > 0 && <button className="danger" onClick={remove}>Hapus</button>}
            <button onClick={() => setCurrent(null)}>Tutup</button>
          </div>
          <div className="mop-meta">
            <label><span>Nama skenario</span><input value={current.name} maxLength={128} onChange={(e) => setCurrent({ ...current, name: e.target.value })} /></label>
            <label className="twin-wide"><span>Keterangan</span><input value={current.description ?? ''} maxLength={2000} onChange={(e) => setCurrent({ ...current, description: e.target.value })} /></label>
          </div>
          <h3>Perubahan</h3>
          <ol className="twin-changes">
            {current.changes.map((c, i) => (
              <li key={i}>
                <span>{c.label ?? changeText(c)}</span>
                <button onClick={() => setCurrent({ ...current, changes: current.changes.filter((_, j) => j !== i), result: null })}>Hapus</button>
              </li>
            ))}
            {!current.changes.length && <li className="muted">Belum ada perubahan.</li>}
          </ol>
          <ChangePicker onAdd={(c) => setCurrent({ ...current, changes: [...current.changes, c], result: null })} />
          {current.result && <ResultView result={current.result} runAt={current.run_at} />}
        </div>
      )}
    </>
  )
}

function changeText(c: Change): string {
  switch (c.type) {
    case 'fail_link': return `Link #${c.link_id} mati`
    case 'fail_device': return `Device #${c.device_id} mati`
    case 'fail_pop': return `POP #${c.pop_id} mati`
    case 'fail_fiber': return `Fiber #${c.fiber_id} putus`
    case 'growth': return `Trafik ${c.pct > 0 ? '+' : ''}${c.pct}% di ${c.area ?? 'seluruh jaringan'}`
    case 'upgrade_link': return `Upgrade link #${c.link_id} ke ${c.capacity_gbps}G`
    case 'add_link': return `Link virtual ${c.capacity_gbps}G`
  }
}

// --------------------------------------------------------------------------- change picker

type PickerKind = Change['type']

const KIND_LABEL: Record<PickerKind, string> = {
  fail_link: 'Link mati', fail_device: 'Device mati', fail_pop: 'POP mati', fail_fiber: 'Fiber/SRLG putus',
  growth: 'Pertumbuhan trafik', upgrade_link: 'Upgrade kapasitas link', add_link: 'Tambah link virtual',
}

interface LinkOption { link_id: number; name: string; a_device: string; b_device: string; capacity_gbps: number; utilization_pct: number | null }
interface DeviceOption { device_id: number; hostname: string; pop: string }

function ChangePicker({ onAdd }: { onAdd: (c: Change) => void }) {
  const [kind, setKind] = useState<PickerKind>('fail_link')
  const [query, setQuery] = useState('')
  const [links, setLinks] = useState<LinkOption[]>([])
  const [search, setSearch] = useState<TopologySearch | null>(null)
  const [areas, setAreas] = useState<string[]>([])
  const [pct, setPct] = useState('20')
  const [area, setArea] = useState('')
  const [capacity, setCapacity] = useState('100')
  const [fiber, setFiber] = useState('')
  const [deviceA, setDeviceA] = useState<DeviceOption | null>(null)

  useEffect(() => {
    api.get<{ areas: string[] }>('/areas/options').then((d) => setAreas(d.areas)).catch(() => setAreas([]))
  }, [])

  useEffect(() => {
    setLinks([])
    setSearch(null)
    const q = query.trim()
    if (q.length < 2) return
    const timer = window.setTimeout(() => {
      if (kind === 'fail_link' || kind === 'upgrade_link') {
        api.get<LinkOption[]>(`/twin/links?q=${encodeURIComponent(q)}&limit=20`).then(setLinks).catch(() => setLinks([]))
      } else if (kind === 'fail_device' || kind === 'fail_pop' || kind === 'add_link') {
        api.get<TopologySearch>(`/topology/search?q=${encodeURIComponent(q)}&limit=12`).then(setSearch).catch(() => setSearch(null))
      }
    }, 300)
    return () => window.clearTimeout(timer)
  }, [kind, query])

  const reset = () => { setQuery(''); setLinks([]); setSearch(null) }
  const pickLink = (l: LinkOption) => {
    const ends = `${l.a_device} ↔ ${l.b_device}`
    if (kind === 'fail_link') onAdd({ type: 'fail_link', link_id: l.link_id, label: `Link ${ends} (${fmtG(l.capacity_gbps)}) mati` })
    else onAdd({ type: 'upgrade_link', link_id: l.link_id, capacity_gbps: Number(capacity),
      label: `Upgrade ${ends}: ${fmtG(l.capacity_gbps)} → ${fmtG(Number(capacity))}` })
    reset()
  }
  const pickDevice = (d: DeviceOption) => {
    if (kind === 'fail_device') { onAdd({ type: 'fail_device', device_id: d.device_id, label: `Device ${d.hostname} (${d.pop}) mati` }); reset(); return }
    if (!deviceA) { setDeviceA(d); reset(); return }
    onAdd({ type: 'add_link', a_device_id: deviceA.device_id, b_device_id: d.device_id, capacity_gbps: Number(capacity),
      label: `Link virtual ${deviceA.hostname} ↔ ${d.hostname} ${fmtG(Number(capacity))}` })
    setDeviceA(null)
    reset()
  }
  const needsCapacity = kind === 'upgrade_link' || kind === 'add_link'

  return (
    <div className="twin-picker">
      <div className="mop-meta">
        <label><span>Tambah perubahan</span>
          <select value={kind} onChange={(e) => { setKind(e.target.value as PickerKind); setDeviceA(null); reset() }}>
            {(Object.keys(KIND_LABEL) as PickerKind[]).map((k) => <option key={k} value={k}>{KIND_LABEL[k]}</option>)}
          </select>
        </label>
        {needsCapacity && (
          <label><span>Kapasitas baru (Gbps)</span><input value={capacity} inputMode="decimal" onChange={(e) => setCapacity(e.target.value)} /></label>
        )}
        {kind === 'growth' && (
          <>
            <label><span>Perubahan trafik (%)</span><input value={pct} inputMode="decimal" onChange={(e) => setPct(e.target.value)} /></label>
            <label><span>Area</span>
              <select value={area} onChange={(e) => setArea(e.target.value)}>
                <option value="">Seluruh jaringan</option>
                {areas.map((a) => <option key={a} value={a}>{a}</option>)}
              </select>
            </label>
            <label><span>&nbsp;</span>
              <button disabled={!Number.isFinite(Number(pct)) || pct === ''} onClick={() => onAdd({ type: 'growth', pct: Number(pct), area: area || null })}>Tambah</button>
            </label>
          </>
        )}
        {kind === 'fail_fiber' && (
          <>
            <label><span>ID fiber</span><input value={fiber} inputMode="numeric" onChange={(e) => setFiber(e.target.value)} /></label>
            <label><span>&nbsp;</span>
              <button disabled={!/^\d+$/.test(fiber)} onClick={() => { onAdd({ type: 'fail_fiber', fiber_id: Number(fiber) }); setFiber('') }}>Tambah</button>
            </label>
          </>
        )}
        {kind !== 'growth' && kind !== 'fail_fiber' && (
          <label className="twin-wide"><span>
            {kind === 'fail_pop' ? 'Cari POP' : kind === 'fail_link' || kind === 'upgrade_link' ? 'Cari link (hostname / nama)'
              : kind === 'add_link' ? (deviceA ? `Ujung B (ujung A: ${deviceA.hostname})` : 'Ujung A (cari hostname)') : 'Cari hostname'}
          </span>
          <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="min. 2 huruf" /></label>
        )}
      </div>
      {links.length > 0 && (
        <div className="twin-options">
          {links.map((l) => (
            <button key={l.link_id} onClick={() => pickLink(l)}>
              {l.a_device} ↔ {l.b_device} <span className="muted">{fmtG(l.capacity_gbps)}{l.utilization_pct != null ? ` · ${l.utilization_pct}%` : ''}</span>
            </button>
          ))}
        </div>
      )}
      {search && kind === 'fail_pop' && (
        <div className="twin-options">
          {search.pops.map((p) => (
            <button key={p.pop_id} onClick={() => { onAdd({ type: 'fail_pop', pop_id: p.pop_id, label: `POP ${p.code} (${p.devices} device) mati` }); reset() }}>
              {p.code} <span className="muted">{p.name} · {p.devices} device</span>
            </button>
          ))}
          {!search.pops.length && <span className="muted">Tidak ada POP cocok.</span>}
        </div>
      )}
      {search && (kind === 'fail_device' || kind === 'add_link') && (
        <div className="twin-options">
          {search.devices.map((d) => (
            <button key={d.device_id} onClick={() => pickDevice({ device_id: d.device_id, hostname: d.hostname, pop: d.pop })}>
              {d.hostname} <span className="muted">{d.pop}</span>
            </button>
          ))}
          {!search.devices.length && <span className="muted">Tidak ada device cocok.</span>}
        </div>
      )}
    </div>
  )
}

// --------------------------------------------------------------------------- result

const LEVEL_CLASS: Record<string, string> = { critical: 'pill-bad', warning: 'pill-warn', normal: 'pill-ok' }

function ResultView({ result, runAt }: { result: Result; runAt: string | null }) {
  const t = result.totals
  const s = result.services
  return (
    <div className="twin-result">
      <h3>Hasil {runAt ? `(dijalankan ${runAt.slice(0, 16).replace('T', ' ')})` : '(belum disimpan)'}</h3>
      <div className="kpis">
        <Kpi label="Link/device mati" value={`${t.failed_links} / ${t.failed_devices}`} sub="link / device" />
        <Kpi label="Dirutekan ulang" value={fmtG(t.rerouted_gbps)} sub="ke jalur pengganti" />
        <Kpi label="Trafik hilang" value={fmtG(t.lost_unroutable_gbps + t.lost_terminating_gbps)}
             sub={`${fmtG(t.lost_unroutable_gbps)} tanpa jalur · ${fmtG(t.lost_terminating_gbps)} berakhir di device mati`} />
        <Kpi label="Device terisolasi" value={t.isolated_devices} sub="lepas dari jaringan utama" />
        <Kpi label="Link kritis" value={`${result.link_levels.before.critical} → ${result.link_levels.after.critical}`}
             sub={`warning ${result.link_levels.before.warning} → ${result.link_levels.after.warning}`} />
        <Kpi label="Layanan" value={`${s.counts.down} putus`} sub={`${s.counts.on_backup} pindah ke backup · ${s.counts.unprotected} tanpa proteksi`} />
      </div>
      <div className="grid-2">
        <div>
          <h3>Link yang berubah ({result.links_changed})</h3>
          <div className="table-wrap">
            <table>
              <thead><tr><th>Link</th><th className="num">Kapasitas</th><th className="num">Sebelum</th><th className="num">Sesudah</th></tr></thead>
              <tbody>
                {result.links.map((l, i) => (
                  <tr key={`${l.link_id ?? 'v'}-${i}`}>
                    <td>{l.link_id ? <Link to={`/capacity?link=${l.link_id}`}>{l.a_device} ↔ {l.b_device}</Link> : <>{l.name}</>}
                      {l.virtual && <span className="pill pill-muted">virtual</span>}</td>
                    <td className="num">{fmtG(l.capacity_gbps)}</td>
                    <td className="num">{l.before_pct != null ? `${l.before_pct}%` : '—'}</td>
                    <td className="num"><span className={`pill ${LEVEL_CLASS[l.level]}`}>{l.after_pct}%</span></td>
                  </tr>
                ))}
                {!result.links.length && <tr><td colSpan={4} className="empty">Tidak ada link yang bebannya berubah.</td></tr>}
              </tbody>
            </table>
          </div>
        </div>
        <div>
          <h3>Per area</h3>
          <table>
            <thead><tr><th>Area</th><th className="num">Link kritis</th><th className="num">Link terburuk</th></tr></thead>
            <tbody>
              {result.areas.map((a) => (
                <tr key={a.area}>
                  <td>{a.area}</td>
                  <td className="num">{a.before_critical} → <b>{a.after_critical}</b></td>
                  <td className="num">{a.before_max_pct}% → <b>{a.after_max_pct}%</b></td>
                </tr>
              ))}
            </tbody>
          </table>
          <ServiceList title="Layanan putus" rows={s.down} />
          <ServiceList title="Pindah ke backup" rows={s.on_backup} />
          <ServiceList title="Kehilangan proteksi" rows={s.unprotected} />
          {result.isolated.length > 0 && (
            <>
              <h3>Device terisolasi</h3>
              <div className="small">{result.isolated.map((d) => `${d.hostname} (${d.area})`).join(' · ')}</div>
            </>
          )}
          {result.unroutable.length > 0 && (
            <>
              <h3>Trafik tanpa jalur pengganti</h3>
              <div className="small">{result.unroutable.map((u) => `${u.link}: ${fmtG(u.traffic_gbps)}`).join(' · ')}</div>
            </>
          )}
        </div>
      </div>
      <ul className="small muted twin-notes">{result.notes.map((n) => <li key={n}>{n}</li>)}</ul>
    </div>
  )
}

function ServiceList({ title, rows }: { title: string; rows: ServiceRow[] }) {
  if (!rows.length) return null
  return (
    <>
      <h3>{title} ({rows.length})</h3>
      <table>
        <tbody>
          {rows.map((r) => (
            <tr key={r.service_id}>
              <td><Link to={`/validation?service=${r.service_id}`}>{r.circuit_id}</Link> <span className="muted">{r.service_type.toUpperCase()}</span></td>
              <td className="small">{r.reason}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  )
}

// --------------------------------------------------------------------------- SRv6 migration

interface Srv6Area {
  area: string
  routers: number
  ready: number
  needs_config: number
  blocked: number
  core: number
  edge: number
  services: number
  services_ready: number
  ready_pct: number
  blocked_pct: number
  wave: number
}

interface Srv6Device {
  device_id: number
  hostname: string
  role: string
  pop: string | null
  vendor: string
  software: string | null
  state: 'ready' | 'needs_config' | 'blocked'
  blockers: string[]
  tier: 'core' | 'edge'
}

const STATE_LABEL = { ready: 'siap', needs_config: 'perlu konfigurasi', blocked: 'terblokir' }
const STATE_CLASS = { ready: 'pill-ok', needs_config: 'pill-warn', blocked: 'pill-bad' }

function Srv6Migration() {
  const [data, setData] = useState<{ totals: Record<string, number>; blockers: { label: string; routers: number }[]; areas: Srv6Area[]; notes: string[] } | null>(null)
  const [area, setArea] = useState<string | null>(null)
  const [devices, setDevices] = useState<Srv6Device[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api.get<typeof data>('/twin/migration/srv6').then(setData).catch((e: Error) => setError(e.message))
  }, [])
  useEffect(() => {
    setDevices(null)
    if (!area) return
    api.get<Srv6Device[]>(`/twin/migration/srv6/devices?area=${encodeURIComponent(area)}`).then(setDevices).catch((e: Error) => setError(e.message))
  }, [area])

  if (error) return <div className="error">{error}</div>
  if (!data) return <div className="muted">Memuat…</div>
  const t = data.totals
  return (
    <>
      <div className="card">
        <h2>Kesiapan migrasi IS-IS / SRv6</h2>
        <p className="hint">
          Per router (P, PE, agg, BNG, DCI): <b>siap</b> = loopback + IS-IS NET + SRv6 locator; <b>perlu konfigurasi</b> =
          underlay lengkap, belum ada locator; <b>terblokir</b> = tanpa loopback atau IS-IS NET. Wave mengurutkan area dari
          yang paling sedikit terblokir dan paling sedikit layanannya.
        </p>
        <div className="kpis">
          <Kpi label="Router" value={t.routers} sub="P / PE / agg / BNG / DCI" />
          <Kpi label="Siap SRv6" value={t.ready} sub={`${t.routers ? Math.round((t.ready / t.routers) * 100) : 0}%`} />
          <Kpi label="Perlu konfigurasi" value={t.needs_config} sub="tambah SRv6 locator" />
          <Kpi label="Terblokir" value={t.blocked} sub="perbaiki loopback / IS-IS dulu" />
        </div>
        <h3>Penghambat terbanyak</h3>
        <div className="small">{data.blockers.map((b) => `${b.label}: ${b.routers} router`).join(' · ') || 'Tidak ada.'}</div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr><th className="num">Wave</th><th>Area</th><th className="num">Router</th><th className="num">Siap</th>
                <th className="num">Perlu konfigurasi</th><th className="num">Terblokir</th><th className="num">Core / edge</th>
                <th className="num">Layanan (siap pindah)</th></tr>
            </thead>
            <tbody>
              {data.areas.map((a) => (
                <tr key={a.area} className={`clickable ${area === a.area ? 'selected' : ''}`} onClick={() => setArea(area === a.area ? null : a.area)}>
                  <td className="num"><b>{a.wave}</b></td>
                  <td><b>{a.area}</b></td>
                  <td className="num">{a.routers}</td>
                  <td className="num">{a.ready} <span className="muted">({a.ready_pct}%)</span></td>
                  <td className="num">{a.needs_config}</td>
                  <td className="num">{a.blocked} <span className="muted">({a.blocked_pct}%)</span></td>
                  <td className="num">{a.core} / {a.edge}</td>
                  <td className="num">{a.services} ({a.services_ready})</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <ul className="small muted twin-notes">{data.notes.map((n) => <li key={n}>{n}</li>)}</ul>
      </div>
      {area && (
        <div className="card">
          <div className="toolbar">
            <h2>{area}: urutan router</h2>
            <div className="spacer" />
            <button onClick={() => setArea(null)}>Tutup</button>
          </div>
          {!devices ? <div className="muted">Memuat…</div> : (
            <div className="table-wrap">
              <table>
                <thead><tr><th className="num">#</th><th>Router</th><th>Tier</th><th>Vendor</th><th>Software</th><th>Status</th><th>Penghambat</th></tr></thead>
                <tbody>
                  {devices.map((d, i) => (
                    <tr key={d.device_id}>
                      <td className="num">{i + 1}</td>
                      <td><b>{d.hostname}</b> <span className="muted">{d.pop}</span></td>
                      <td>{d.tier === 'core' ? 'core' : 'edge'} <span className="muted">({d.role})</span></td>
                      <td>{d.vendor}</td>
                      <td className="small">{d.software ?? '—'}</td>
                      <td><span className={`pill ${STATE_CLASS[d.state]}`}>{STATE_LABEL[d.state]}</span></td>
                      <td className="small">{d.blockers.join(', ') || '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </>
  )
}
