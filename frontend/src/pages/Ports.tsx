import { FormEvent, useEffect, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { api } from '../api'

// Port capacity: physical ports per speed, split used (oper up) / idle (admin up, oper down) / disabled (admin down).

interface Usage {
  total: number
  used: number
  idle: number
  disabled: number
  used_pct: number
}
interface SpeedRow extends Usage {
  speed: string
}
interface CategoryRow extends Usage {
  category: string
  label: string
  devices: number
  by_speed: SpeedRow[]
}
interface Summary {
  totals: Usage
  by_speed: SpeedRow[]
  by_category: CategoryRow[]
  categories: Record<string, string>
  vendors: string[]
}
interface DeviceRow extends Usage {
  device_id: number
  hostname: string
  pop: string
  area: string
  site: string
  role: string
  category: string
  vendor: string | null
  speeds: Record<string, { used: number; idle: number; disabled: number }>
}

const CATEGORY_TABS = [
  { key: '', label: 'Semua' },
  { key: 'router', label: 'Router' },
  { key: 'metro', label: 'Metro' },
  { key: 'csr', label: 'CSR' },
  { key: 'olt', label: 'OLT' },
  { key: 'access', label: 'Access/Switch' },
  { key: 'other', label: 'Lainnya' },
]

function UsageBar({ u }: { u: Usage }) {
  const pct = (n: number) => (u.total ? (n / u.total) * 100 : 0)
  return (
    <div className="usage-bar" title={`terpakai ${u.used} · idle ${u.idle} · disabled ${u.disabled}`}>
      <div className="used" style={{ width: `${pct(u.used)}%` }} />
      <div className="idle" style={{ width: `${pct(u.idle)}%` }} />
      <div className="disabled" style={{ width: `${pct(u.disabled)}%` }} />
    </div>
  )
}

const n = (v: number) => v.toLocaleString('id-ID')

export default function Ports() {
  const [params, setParams] = useSearchParams()
  const category = params.get('category') ?? ''
  const area = params.get('area') ?? ''
  const pop = params.get('pop') ?? ''
  const vendor = params.get('vendor') ?? ''
  const speed = params.get('speed') ?? ''
  const q = params.get('q') ?? ''

  const [summary, setSummary] = useState<Summary | null>(null)
  const [devices, setDevices] = useState<{ total_devices: number; devices: DeviceRow[] } | null>(null)
  const [areas, setAreas] = useState<string[]>([])
  const [sort, setSort] = useState('idle')
  const [search, setSearch] = useState(q)
  const [error, setError] = useState<string | null>(null)

  const set = (key: string, value: string) => {
    const next = new URLSearchParams(params)
    if (value) next.set(key, value)
    else next.delete(key)
    setParams(next)
  }

  const filterQuery = useMemo(() => {
    const p = new URLSearchParams()
    if (category) p.set('category', category)
    if (area) p.set('area', area)
    if (pop) p.set('pop', pop)
    if (vendor) p.set('vendor', vendor)
    return p
  }, [category, area, pop, vendor])

  useEffect(() => {
    api.get<{ areas: { area: string }[] }>('/areas').then((r) => setAreas(r.areas.map((a) => a.area))).catch(() => undefined)
  }, [])

  useEffect(() => {
    setError(null)
    api.get<Summary>(`/inventory/ports/summary?${filterQuery}`).then(setSummary).catch((e: Error) => setError(e.message))
  }, [filterQuery])

  useEffect(() => {
    const p = new URLSearchParams(filterQuery)
    p.set('sort', sort)
    p.set('limit', '300')
    if (speed) p.set('speed', speed)
    if (q) p.set('q', q)
    api.get<{ total_devices: number; devices: DeviceRow[] }>(`/inventory/ports/devices?${p}`).then(setDevices).catch((e: Error) => setError(e.message))
  }, [filterQuery, speed, q, sort])

  function submitSearch(e: FormEvent) {
    e.preventDefault()
    set('q', search.trim())
  }

  const speeds = summary?.by_speed.map((s) => s.speed) ?? []

  return (
    <div className="capacity">
      <div className="card">
        <div className="toolbar">
          <h2>Kapasitas Port</h2>
          <div className="segmented">
            {CATEGORY_TABS.map((t) => (
              <button key={t.key} className={category === t.key ? 'active' : ''} onClick={() => set('category', t.key)}>
                {t.label}
              </button>
            ))}
          </div>
          <select value={area} onChange={(e) => set('area', e.target.value)}>
            <option value="">Semua area</option>
            {areas.map((a) => (
              <option key={a} value={a}>{a}</option>
            ))}
          </select>
          <input className="search narrow" placeholder="POP (mis. JKT)" defaultValue={pop}
            onKeyDown={(e) => e.key === 'Enter' && set('pop', (e.target as HTMLInputElement).value.trim())} />
          <select value={vendor} onChange={(e) => set('vendor', e.target.value)}>
            <option value="">Semua vendor</option>
            {summary?.vendors.map((v) => (
              <option key={v} value={v}>{v}</option>
            ))}
          </select>
        </div>
        <p className="hint">
          Port fisik saja (bundle/LAG dan sub-interface tidak dihitung). <b>Terpakai</b> = oper up · <b>Idle</b> = admin up tapi
          oper down (siap dipakai) · <b>Disabled</b> = admin down (shutdown).
        </p>
        {error && <div className="error">{error}</div>}
        {summary && (
          <div className="kpis">
            <div className="kpi card"><div className="kpi-label">Total port</div><div className="kpi-value">{n(summary.totals.total)}</div></div>
            <div className="kpi card"><div className="kpi-label">Terpakai</div><div className="kpi-value ok">{n(summary.totals.used)}</div><div className="kpi-sub">{summary.totals.used_pct}%</div></div>
            <div className="kpi card"><div className="kpi-label">Idle</div><div className="kpi-value warn">{n(summary.totals.idle)}</div><div className="kpi-sub">siap dipakai</div></div>
            <div className="kpi card"><div className="kpi-label">Disabled</div><div className="kpi-value muted">{n(summary.totals.disabled)}</div><div className="kpi-sub">admin down</div></div>
          </div>
        )}
      </div>

      {summary && (
        <div className="grid-2">
          <div className="card">
            <h2>Per kapasitas port</h2>
            <table>
              <thead>
                <tr>
                  <th>Speed</th><th className="num">Total</th><th className="num">Terpakai</th><th className="num">Idle</th>
                  <th className="num">Disabled</th><th style={{ width: '30%' }} />
                </tr>
              </thead>
              <tbody>
                {summary.by_speed.map((s) => (
                  <tr key={s.speed} className={`clickable ${speed === s.speed ? 'selected' : ''}`} onClick={() => set('speed', speed === s.speed ? '' : s.speed)}>
                    <td><b>{s.speed}</b></td>
                    <td className="num">{n(s.total)}</td>
                    <td className="num">{n(s.used)} <span className="muted small">({s.used_pct}%)</span></td>
                    <td className="num">{n(s.idle)}</td>
                    <td className="num">{n(s.disabled)}</td>
                    <td><UsageBar u={s} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="hint">Klik baris speed untuk memfilter daftar perangkat di bawah.</p>
          </div>

          <div className="card">
            <h2>Per kategori perangkat</h2>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Kategori</th><th className="num">Perangkat</th>
                    {speeds.map((s) => <th key={s} className="num">{s}<div className="muted small">pakai / idle</div></th>)}
                  </tr>
                </thead>
                <tbody>
                  {summary.by_category.map((c) => (
                    <tr key={c.category} className="clickable" onClick={() => set('category', c.category)}>
                      <td><b>{c.label}</b><UsageBar u={c} /></td>
                      <td className="num">{n(c.devices)}</td>
                      {speeds.map((s) => {
                        const r = c.by_speed.find((x) => x.speed === s)
                        return <td key={s} className="num">{r ? <>{n(r.used)} <span className="muted">/ {n(r.idle)}</span></> : <span className="muted">–</span>}</td>
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}

      <div className="card">
        <div className="toolbar">
          <h2>Perangkat {speed && <span className="pill pill-muted">{speed}</span>}</h2>
          <span className="muted small">{devices ? `${n(devices.total_devices)} perangkat` : ''}</span>
          <div className="spacer" />
          <form onSubmit={submitSearch} className="inline">
            <input className="search" placeholder="Cari hostname…" value={search} onChange={(e) => setSearch(e.target.value)} />
            <button type="submit">Cari</button>
          </form>
          <select value={sort} onChange={(e) => setSort(e.target.value)}>
            <option value="idle">Idle terbanyak</option>
            <option value="used">Terpakai terbanyak</option>
            <option value="used_pct">Utilisasi port tertinggi</option>
            <option value="total">Port terbanyak</option>
            <option value="hostname">Hostname</option>
          </select>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Hostname</th><th>POP · Site</th><th>Area</th><th>Kategori</th><th>Vendor</th>
                <th className="num">Total</th><th className="num">Terpakai</th><th className="num">Idle</th><th className="num">Disabled</th>
                <th>Per speed (pakai/idle)</th>
              </tr>
            </thead>
            <tbody>
              {devices?.devices.map((d) => (
                <tr key={d.device_id}>
                  <td>
                    <Link to={`/topology?level=device&pop=${encodeURIComponent(d.pop)}&focus=${encodeURIComponent(d.hostname)}`}>{d.hostname}</Link>
                  </td>
                  <td>{d.pop} · {d.site}</td>
                  <td className="muted">{d.area}</td>
                  <td>{summary?.categories[d.category] ?? d.category}</td>
                  <td>{d.vendor ?? '–'}</td>
                  <td className="num">{d.total}</td>
                  <td className="num">{d.used} <span className="muted small">({d.used_pct}%)</span></td>
                  <td className="num"><b>{d.idle}</b></td>
                  <td className="num muted">{d.disabled}</td>
                  <td className="small">
                    {Object.entries(d.speeds).map(([s, c]) => <span key={s} className="speed-chip">{s} {c.used}/{c.idle}</span>)}
                  </td>
                </tr>
              ))}
              {devices && !devices.devices.length && (
                <tr><td colSpan={10} className="empty">Tidak ada perangkat untuk filter ini</td></tr>
              )}
            </tbody>
          </table>
        </div>
        {devices && devices.total_devices > devices.devices.length && (
          <p className="hint">Menampilkan {devices.devices.length} dari {n(devices.total_devices)} perangkat — persempit dengan filter atau pencarian.</p>
        )}
      </div>
    </div>
  )
}
