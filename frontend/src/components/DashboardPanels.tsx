import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { CartesianGrid, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { api, CapacityResult } from '../api'
import { BasemapShape, Box, declutter, LON0, loadBasemap } from './basemap'
import { StatusBadge } from './ValidationReport'
import { ColorBadge, fmtG, fmtMonths, Kpi, UtilBar } from './ui'

// Phase 8 — advanced dashboard panels, each backed by one /api/dashboard/* read model.

function useLoad<T>(path: string): { data: T | null; error: string | null } {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    api.get<T>(path).then(setData).catch((e: Error) => setError(e.message))
  }, [path])
  return { data, error }
}

function Loading({ error }: { error: string | null }) {
  return error ? <div className="error">{error}</div> : <div className="muted">Memuat…</div>
}

// --------------------------------------------------------------------------- trends & forecast

interface TrendNode extends CapacityResult {
  key: string
  label: string
  links: number
  history_days: number
  series: { day: string; peak_gbps: number }[]
}

interface Trends {
  network: TrendNode
  areas: TrendNode[]
  window_days: number
}

export function TrendsPanel() {
  const { data, error } = useLoad<Trends>('/dashboard/trends')
  const [area, setArea] = useState<string>('')
  if (!data) return <Loading error={error} />
  const node = data.areas.find((a) => a.label === area) ?? data.network
  return (
    <>
      <div className="card">
        <div className="toolbar">
          <h2>Tren puncak harian — {node.label}</h2>
          <div className="spacer" />
          <select value={area} onChange={(e) => setArea(e.target.value)}>
            <option value="">Seluruh jaringan</option>
            {data.areas.map((a) => <option key={a.key} value={a.label}>{a.label}</option>)}
          </select>
        </div>
        <div className="kpis">
          <Kpi label="Kapasitas" value={fmtG(node.capacity_gbps)} sub={`${node.links} link`} />
          <Kpi label="Utilisasi" value={`${node.utilization_pct}%`} sub={`${fmtG(node.current_gbps)} trafik`} />
          <Kpi label="Growth" value={node.growth_gbps_per_month != null ? `${fmtG(node.growth_gbps_per_month)}/bln` : '—'}
               sub={node.forecast_method === 'trend' ? `tren ${node.trend_days} hari` : 'histori kurang'} />
          <Kpi label="Ke ambang" value={fmtMonths(node.months_to_threshold)} sub={node.threshold_date ?? 'tidak terprediksi'} />
        </div>
        {node.series.length < 2 ? (
          <div className="muted">
            Histori utilisasi baru {node.series.length} hari. Grafik dan forecast butuh histori harian (import RRD Observium).
          </div>
        ) : (
          <div style={{ height: 280 }}>
            <ResponsiveContainer>
              <LineChart data={node.series} margin={{ top: 10, right: 20, bottom: 0, left: 0 }}>
                <CartesianGrid stroke="#334155" strokeDasharray="3 3" />
                <XAxis dataKey="day" stroke="#94a3b8" fontSize={11} />
                <YAxis stroke="#94a3b8" fontSize={11} unit="G" />
                <Tooltip contentStyle={{ background: '#0f172a', border: '1px solid #334155' }} />
                <ReferenceLine y={node.capacity_gbps * 0.8} stroke="#eab308" strokeDasharray="4 4" label={{ value: 'ambang 80%', fill: '#eab308', fontSize: 11 }} />
                <Line type="monotone" dataKey="peak_gbps" name="Puncak harian (Gbps)" stroke="#38bdf8" dot={false} />
              </LineChart>
            </ResponsiveContainer>
          </div>
        )}
      </div>
      <div className="card">
        <h2>Forecast per area</h2>
        <div className="table-wrap">
          <table>
            <thead>
              <tr><th>Area</th><th className="num">Link</th><th style={{ width: '32%' }}>Kapasitas</th>
                <th className="num">Committed</th><th className="num">Growth/bln</th><th className="num">Ke ambang</th>
                <th className="num">Histori</th></tr>
            </thead>
            <tbody>
              {data.areas.map((a) => (
                <tr key={a.key} className="clickable" onClick={() => setArea(a.label)}>
                  <td><b>{a.label}</b></td>
                  <td className="num">{a.links}</td>
                  <td><UtilBar capacity={a.capacity_gbps} current={a.current_gbps} reserved={a.reserved_gbps} /></td>
                  <td className="num"><ColorBadge color={a.color}>{a.committed_pct.toFixed(1)}%</ColorBadge></td>
                  <td className="num">{a.growth_gbps_per_month != null ? fmtG(a.growth_gbps_per_month) : '—'}</td>
                  <td className="num">{fmtMonths(a.months_to_threshold)}</td>
                  <td className="num">{a.history_days} hari</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </>
  )
}

// --------------------------------------------------------------------------- services & validation

interface ServicesInsight {
  total: number
  by_type: Record<string, number>
  by_status: Record<string, number>
  bandwidth_by_type_mbps: Record<string, number>
  validation: Record<'fail' | 'warn' | 'pass', number>
  draft_mops: { service_id: number; circuit_id: string; service_type: string; failed: string[] }[]
  top_findings: { title: string; services: number }[]
}

export function ServicesPanel() {
  const { data, error } = useLoad<ServicesInsight>('/dashboard/services')
  if (!data) return <Loading error={error} />
  return (
    <>
      <div className="kpis">
        <Kpi label="Layanan aktif" value={data.total} sub={Object.entries(data.by_status).map(([s, n]) => `${n} ${s}`).join(' · ') || '—'} />
        <Kpi label="Validasi gagal" value={data.validation.fail} sub="MoP akan berstatus DRAFT" />
        <Kpi label="Peringatan" value={data.validation.warn} sub="perlu ditinjau" />
        <Kpi label="Lulus" value={data.validation.pass} sub="siap diimplementasikan" />
      </div>
      <div className="grid-2">
        <div className="card">
          <h2>Layanan per tipe</h2>
          <table>
            <thead><tr><th>Tipe</th><th className="num">Layanan</th><th className="num">Bandwidth</th></tr></thead>
            <tbody>
              {Object.entries(data.by_type).map(([t, n]) => (
                <tr key={t}><td>{t.toUpperCase()}</td><td className="num">{n}</td>
                  <td className="num">{fmtG((data.bandwidth_by_type_mbps[t] ?? 0) / 1000)}</td></tr>
              ))}
              {!data.total && <tr><td colSpan={3} className="empty">Belum ada layanan.</td></tr>}
            </tbody>
          </table>
          <h3>Temuan validasi terbanyak</h3>
          <table>
            <tbody>
              {data.top_findings.map((f) => <tr key={f.title}><td>{f.title}</td><td className="num">{f.services} layanan</td></tr>)}
              {!data.top_findings.length && <tr><td className="empty">Tidak ada temuan gagal.</td></tr>}
            </tbody>
          </table>
        </div>
        <div className="card">
          <h2>MoP masih DRAFT</h2>
          <table>
            <thead><tr><th>Status</th><th>Circuit</th><th>Temuan</th></tr></thead>
            <tbody>
              {data.draft_mops.map((d) => (
                <tr key={d.service_id}>
                  <td><StatusBadge status="fail" /></td>
                  <td><Link to={`/validation?service=${d.service_id}`}><b>{d.circuit_id}</b></Link>{' '}
                    <span className="muted">{d.service_type.toUpperCase()}</span></td>
                  <td className="small">{d.failed.join(' · ')}</td>
                </tr>
              ))}
              {!data.draft_mops.length && <tr><td colSpan={3} className="empty">Semua layanan lolos validasi.</td></tr>}
            </tbody>
          </table>
        </div>
      </div>
    </>
  )
}

// --------------------------------------------------------------------------- data health

interface HealthCheck {
  key: string
  label: string
  count: number
  total: number
  severity: 'ok' | 'info' | 'warn'
  hint: string
}

interface SyncState {
  kind: string
  last_ok: string | null
  last_failed: string | null
  error: string | null
  age_hours?: number
}

const SYNC_LABEL: Record<string, string> = { observium: 'Observium (inventory, LLDP)', rrd: 'RRD (histori utilisasi)', gitweb: 'RANCID gitweb (config)' }

export function DataHealthPanel() {
  const { data, error } = useLoad<{ checks: HealthCheck[]; syncs: SyncState[] }>('/dashboard/data-health')
  if (!data) return <Loading error={error} />
  return (
    <div className="grid-2">
      <div className="card">
        <h2>Kelengkapan data</h2>
        <table>
          <tbody>
            {data.checks.map((c) => (
              <tr key={c.key}>
                <td><StatusBadge status={c.severity === 'ok' ? 'pass' : c.severity === 'warn' ? 'warn' : 'info'} /></td>
                <td><b>{c.label}</b><div className="small muted">{c.hint}</div></td>
                <td className="num">{c.count}{c.total ? <span className="muted"> / {c.total}</span> : null}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="card">
        <h2>Umur sync</h2>
        <table>
          <tbody>
            {data.syncs.map((s) => (
              <tr key={s.kind}>
                <td><StatusBadge status={!s.last_ok ? 'info' : (s.age_hours ?? 0) > 48 ? 'warn' : 'pass'} /></td>
                <td><b>{SYNC_LABEL[s.kind] ?? s.kind}</b>
                  {s.last_failed && <div className="small muted">gagal terakhir {s.last_failed.slice(0, 16).replace('T', ' ')}: {s.error}</div>}
                </td>
                <td className="num">{s.last_ok ? `${s.age_hours} jam lalu` : 'tidak ada catatan job'}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="small muted">Jalankan sync dari menu <Link to="/collectors">Collectors</Link>.</div>
      </div>
    </div>
  )
}

// --------------------------------------------------------------------------- area hotspot map

interface AreaRow {
  area: string
  latitude: number | null
  longitude: number | null
  position_estimated: boolean
  pops: number
  located_pops: number
  links: number
  capacity_gbps: number
  current_gbps: number
  critical: number
  warning: number
  max_utilization_pct: number
  committed_pct: number
  level: 'critical' | 'warning' | 'normal'
}

interface PopPoint {
  pop_id: number
  code: string
  name: string
  area: string
  latitude: number
  longitude: number
  level: 'critical' | 'warning' | 'normal' | 'suspect'
  max_utilization_pct: number
}

const LEVEL_HEX = { critical: '#ef4444', warning: '#eab308', normal: '#22c55e', suspect: '#64748b' }
// Indonesia in the basemap's degree units (x = lon − 118, y = −lat).
const VIEW = { lonMin: 94, lonMax: 142, latMin: -11.5, latMax: 7 }

export function AreaMapPanel() {
  const { data, error } = useLoad<{ areas: AreaRow[]; pops: PopPoint[]; thresholds: { warning_pct: number; critical_pct: number } }>('/dashboard/areas')
  const [basemap, setBasemap] = useState<BasemapShape[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  useEffect(() => {
    loadBasemap().then(setBasemap).catch(() => setBasemap([]))
  }, [])
  if (!data) return <Loading error={error} />
  const maxCapacity = Math.max(1, ...data.areas.map((a) => a.capacity_gbps))
  const viewBox = `${VIEW.lonMin - LON0} ${-VIEW.latMax} ${VIEW.lonMax - VIEW.lonMin} ${VIEW.latMax - VIEW.latMin}`
  const pops = data.pops.filter((p) => !selected || p.area === selected)
  const placed = data.areas.filter((a) => a.latitude != null && a.longitude != null)
  // The Java areas sit within a few degrees of each other: push their labels apart, keep the bubbles in place.
  const labels = new Map<string, Box>(placed.map((a) => [a.area, { x: a.longitude! - LON0, y: -a.latitude! - 1.1,
                                                                  hw: a.area.length * 0.13 + 0.15, hh: 0.42 }]))
  declutter(labels, 0.08, 3)
  return (
    <div className="card">
      <div className="toolbar">
        <h2>Peta hotspot per area</h2>
        <div className="spacer" />
        <span className="small muted">
          lingkaran = area (besar ∝ kapasitas, warna = link terburuk: kuning ≥ {data.thresholds.warning_pct}%, merah ≥ {data.thresholds.critical_pct}%) · titik = POP
        </span>
      </div>
      <svg className="area-map" viewBox={viewBox} role="img" aria-label="Peta hotspot area">
        {basemap.map((s, i) => <path key={`${s.id}-${i}`} d={s.d} className={s.indonesia ? 'land indonesia' : 'land'} />)}
        {pops.map((p) => (
          <circle key={p.pop_id} cx={p.longitude - LON0} cy={-p.latitude} r={0.12} fill={LEVEL_HEX[p.level]}>
            <title>{`${p.code} ${p.name}: link terburuk ${p.max_utilization_pct}%`}</title>
          </circle>
        ))}
        {placed.map((a) => {
          const cx = a.longitude! - LON0
          const cy = -a.latitude!
          const label = labels.get(a.area)!
          return (
          <g key={a.area} className="area-bubble" onClick={() => setSelected(selected === a.area ? null : a.area)}>
            <circle cx={cx} cy={cy} r={0.35 + 0.9 * Math.sqrt(a.capacity_gbps / maxCapacity)}
                    fill={LEVEL_HEX[a.level]} fillOpacity={selected === a.area ? 0.55 : 0.3} stroke={LEVEL_HEX[a.level]}
                    strokeDasharray={a.position_estimated ? '0.2 0.15' : undefined} />
            <line x1={cx} y1={cy} x2={label.x} y2={label.y + label.hh} className="leader" />
            <text x={label.x} y={label.y} textAnchor="middle">{a.area}</text>
            <text x={label.x} y={label.y + 0.4} textAnchor="middle" className="sub">
              {a.critical ? `${a.critical} kritis` : a.warning ? `${a.warning} warning` : `${a.committed_pct}%`}
            </text>
            <title>{`${a.area}: ${a.links} link, ${fmtG(a.capacity_gbps)}, committed ${a.committed_pct}%`
              + (a.position_estimated ? ' (posisi perkiraan: belum ada POP berkoordinat)' : '')}</title>
          </g>
          )
        })}
      </svg>
      <div className="table-wrap">
        <table>
          <thead>
            <tr><th>Area</th><th className="num">POP (berkoordinat)</th><th className="num">Link</th>
              <th className="num">Kritis</th><th className="num">Warning</th><th className="num">Link terburuk</th>
              <th className="num">Committed</th></tr>
          </thead>
          <tbody>
            {data.areas.map((a) => (
              <tr key={a.area} className={`clickable ${selected === a.area ? 'selected' : ''}`}
                  onClick={() => setSelected(selected === a.area ? null : a.area)}>
                <td><span className="dot" style={{ background: LEVEL_HEX[a.level] }} /> <b>{a.area}</b></td>
                <td className="num">{a.pops} ({a.located_pops})</td>
                <td className="num">{a.links}</td>
                <td className="num">{a.critical || '—'}</td>
                <td className="num">{a.warning || '—'}</td>
                <td className="num">{a.max_utilization_pct ? `${a.max_utilization_pct}%` : '—'}</td>
                <td className="num">{a.committed_pct}%</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="small muted">Daftar link kritis lengkap ada di <Link to="/capacity">Capacity Planning → Hotspot</Link>.</div>
    </div>
  )
}
