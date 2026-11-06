import { FormEvent, useEffect, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { api } from '../api'
import { Pager, usePaged } from '../components/Pager'

// Port capacity: physical ports split used (oper up) / idle (admin up, oper down) / disabled (admin down),
// in total and per device (with the ports behind each count).

interface Usage {
  total: number
  used: number
  idle: number
  disabled: number
  used_pct: number
}
interface Summary {
  totals: Usage
  by_speed: { speed: string }[]
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
interface InterfaceRow {
  id: number
  name: string
  speed_gbps: number | null
  admin_status: string
  oper_status: string
  description: string | null
}
type PortState = 'used' | 'idle'

const STANDARD_SPEEDS: [number, string][] = [
  [0.01, '10M'], [0.1, '100M'], [1, '1G'], [2.5, '2.5G'], [10, '10G'], [25, '25G'], [40, '40G'],
  [50, '50G'], [100, '100G'], [200, '200G'], [400, '400G'], [800, '800G'],
]
function speedBucket(speed: number | null): string {
  if (speed == null || speed <= 0) return 'Unknown'
  return STANDARD_SPEEDS.find(([nominal]) => Math.abs(speed - nominal) <= nominal * 0.02)?.[1] ?? 'Non-standar'
}
function speedOrder(bucket: string): number {
  const index = STANDARD_SPEEDS.findIndex(([, label]) => label === bucket)
  return index >= 0 ? index : bucket === 'Non-standar' ? -1 : -2
}

const CATEGORY_TABS = [
  { key: '', label: 'Semua' },
  { key: 'router', label: 'Router' },
  { key: 'metro', label: 'Metro' },
  { key: 'switch', label: 'Switch (DS)' },
  { key: 'csr', label: 'CSR' },
  { key: 'olt', label: 'OLT' },
  { key: 'access', label: 'Access/Switch' },
  { key: 'other', label: 'Lainnya' },
]

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
  const [expanded, setExpanded] = useState<{ deviceId: number; speed: string; state: PortState } | null>(null)
  const [expandedInterfaces, setExpandedInterfaces] = useState<InterfaceRow[]>([])
  const [interfaceLoading, setInterfaceLoading] = useState(false)
  const [interfaceError, setInterfaceError] = useState<string | null>(null)

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
    p.set('limit', '5000')  // every device of the filter; the table pages them
    if (speed) p.set('speed', speed)
    if (q) p.set('q', q)
    api.get<{ total_devices: number; devices: DeviceRow[] }>(`/inventory/ports/devices?${p}`).then(setDevices).catch((e: Error) => setError(e.message))
  }, [filterQuery, speed, q, sort])

  const paged = usePaged(devices?.devices ?? [], devices)

  function submitSearch(e: FormEvent) {
    e.preventDefault()
    set('q', search.trim())
  }

  async function showInterfaces(deviceId: number, speedClass: string, state: PortState) {
    if (expanded?.deviceId === deviceId && expanded.speed === speedClass && expanded.state === state) {
      setExpanded(null)
      return
    }
    setExpanded({ deviceId, speed: speedClass, state })
    setExpandedInterfaces([])
    setInterfaceError(null)
    setInterfaceLoading(true)
    try {
      const interfaces = await api.get<InterfaceRow[]>(`/interfaces?device_id=${deviceId}&if_type=physical&limit=10000`)
      setExpandedInterfaces(interfaces.filter((iface) => {
        if (speedBucket(iface.speed_gbps) !== speedClass) return false
        return state === 'used'
          ? iface.admin_status !== 'down' && iface.oper_status === 'up'
          : iface.admin_status !== 'down' && iface.oper_status !== 'up'
      }))
    } catch (e) {
      setInterfaceError((e as Error).message)
    } finally {
      setInterfaceLoading(false)
    }
  }

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
          <select value={speed} onChange={(e) => set('speed', e.target.value)}>
            <option value="">Semua kapasitas</option>
            {summary?.by_speed.map((s) => <option key={s.speed} value={s.speed}>{s.speed}</option>)}
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
                <th>Kapasitas (terpakai/idle)</th>
              </tr>
            </thead>
            <tbody>
              {paged.pageRows.flatMap((d) => {
                const classes = Object.entries(d.speeds).sort(([a], [b]) => speedOrder(b) - speedOrder(a))
                const rows = [
                  <tr key={d.device_id}>
                    <td>
                      <Link to={`/devices/${d.device_id}`} title="Detail port, kapasitas dan utilisasi">{d.hostname}</Link>
                    </td>
                    <td>{d.pop} · {d.site}</td>
                    <td className="muted">{d.area}</td>
                    <td>{summary?.categories[d.category] ?? d.category}</td>
                    <td>{d.vendor ?? '–'}</td>
                    <td>
                      {classes.map(([speedClass, counts], index) => <span key={speedClass}>
                        {index > 0 && <span className="muted"> ; </span>}
                        <b>{speedClass}</b> ({' '}
                        <button type="button" className="port-count-link" onClick={() => showInterfaces(d.device_id, speedClass, 'used')} title={`Lihat port ${speedClass} yang terpakai`}>{n(counts.used)}</button>
                        /{' '}
                        <button type="button" className="port-count-link" onClick={() => showInterfaces(d.device_id, speedClass, 'idle')} title={`Lihat port ${speedClass} yang idle`}>{n(counts.idle)}</button>
                        )
                      </span>)}
                    </td>
                  </tr>,
                ]
                if (expanded?.deviceId === d.device_id) {
                  const selectedClass = classes.find(([speedClass]) => speedClass === expanded.speed)
                  rows.push(
                    <tr key={`${d.device_id}-interfaces-${expanded.speed}-${expanded.state}`}>
                      <td colSpan={6}>
                        <b>{d.hostname} · {expanded.speed} · {expanded.state === 'used' ? 'Terpakai' : 'Idle'}</b>
                        {interfaceLoading && <div className="muted">Memuat interface…</div>}
                        {interfaceError && <div className="error">{interfaceError}</div>}
                        {!interfaceLoading && !interfaceError && <div className="interface-list">
                          {expandedInterfaces.length ? expandedInterfaces.map((iface) => (
                            <span key={iface.id} className="speed-chip" title={iface.description ?? undefined}>{iface.name}</span>
                          )) : <span className="muted">Tidak ada interface pada status ini</span>}
                        </div>}
                        {selectedClass && <span className="muted small">{` · ${n(selectedClass[1][expanded.state])} interface`}</span>}
                      </td>
                    </tr>,
                  )
                }
                return rows
              })}
              {devices && !devices.devices.length && (
                <tr><td colSpan={6} className="empty">Tidak ada perangkat untuk filter ini</td></tr>
              )}
            </tbody>
          </table>
        </div>
        <Pager paged={paged} unit="perangkat" />
        {devices && devices.total_devices > devices.devices.length && (
          <p className="hint">Dimuat {n(devices.devices.length)} dari {n(devices.total_devices)} perangkat — persempit dengan filter atau pencarian.</p>
        )}
      </div>
    </div>
  )
}
