import { ReactNode, useEffect, useMemo, useState } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import { api, HotspotLevel, Hotspots, NodeForecast } from '../api'
import { ColorBadge, fmtG, fmtMonths } from './ui'

const LEVEL_LABEL: Record<HotspotLevel, string> = { critical: 'Critical', warning: 'Warning', normal: 'Normal', suspect: 'Cek data' }

export function LevelBadge({ level, children }: { level: HotspotLevel; children?: ReactNode }) {
  return <span className={`level level-${level}`}>{children ?? LEVEL_LABEL[level]}</span>
}

/** Busiest links (utilization) and devices (port occupancy), warning ≥ 70 % / critical ≥ 90 %. */
export function HotspotsPanel({ onOpenLink }: { onOpenLink: (linkId: number) => void }) {
  const [data, setData] = useState<Hotspots | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [onlyHot, setOnlyHot] = useState(true)
  const [showSuspect, setShowSuspect] = useState(false)

  useEffect(() => {
    api.get<Hotspots>('/capacity/hotspots?limit=300').then(setData).catch((e: Error) => setError(e.message))
  }, [])

  if (error) return <div className="error">{error}</div>
  if (!data) return <div className="muted">Memuat hotspot…</div>
  const { warning_pct: warn, critical_pct: crit, suspect_pct: suspect } = data.thresholds
  const visible = (level: HotspotLevel) => (level === 'suspect' ? showSuspect : !onlyHot || level !== 'normal')
  const links = data.links.rows.filter((r) => visible(r.level))
  const devices = data.devices.rows.filter((r) => visible(r.level))

  return (
    <>
      <div className="toolbar">
        <span className="muted small">
          Warning ≥ {warn}% · Critical ≥ {crit}%. Link: trafik / kapasitas. Perangkat: port terpakai (oper up) / total port fisik.
        </span>
        <div className="spacer" />
        <label className="check">
          <input type="checkbox" checked={onlyHot} onChange={(e) => setOnlyHot(e.target.checked)} /> Hanya warning + critical
        </label>
        <label className="check" title={`Utilisasi > ${suspect}%: kapasitas port dari SNMP kemungkinan salah`}>
          <input type="checkbox" checked={showSuspect} onChange={(e) => setShowSuspect(e.target.checked)} /> Tampilkan data kapasitas
          mencurigakan ({data.links.counts.suspect})
        </label>
      </div>
      <div className="hot-grid">
        <div>
          <h3>Link terpadat</h3>
          <div className="hot-counts">
            <LevelBadge level="critical">{data.links.counts.critical} critical</LevelBadge>
            <LevelBadge level="warning">{data.links.counts.warning} warning</LevelBadge>
          </div>
          <div className="table-wrap">
            <table>
              <thead>
                <tr><th /><th>Link</th><th className="num">Trafik</th><th className="num">Kapasitas</th><th className="num">Ke 80%</th></tr>
              </thead>
              <tbody>
                {links.map((l) => (
                  <tr key={l.link_id} className="clickable" onClick={() => onOpenLink(l.link_id)}>
                    <td><LevelBadge level={l.level}>{l.utilization_pct.toFixed(0)}%</LevelBadge></td>
                    <td>
                      {l.a_device} ↔ {l.b_device}
                      {l.status === 'down' && <span className="pill pill-bad">down</span>}
                      <div className="muted small">{l.a_pop === l.b_pop ? l.a_pop : `${l.a_pop}–${l.b_pop}`}</div>
                    </td>
                    <td className="num">{fmtG(l.current_gbps)}</td>
                    <td className="num">{fmtG(l.capacity_gbps)}</td>
                    <td className="num">{fmtMonths(l.months_to_threshold)}</td>
                  </tr>
                ))}
                {!links.length && <tr><td colSpan={5} className="empty">Tidak ada link di atas {warn}%</td></tr>}
              </tbody>
            </table>
          </div>
        </div>
        <div>
          <h3>Perangkat dengan port terpadat</h3>
          <div className="hot-counts">
            <LevelBadge level="critical">{data.devices.counts.critical} critical</LevelBadge>
            <LevelBadge level="warning">{data.devices.counts.warning} warning</LevelBadge>
          </div>
          <div className="table-wrap">
            <table>
              <thead>
                <tr><th /><th>Perangkat</th><th className="num">Terpakai</th><th className="num">Sisa</th></tr>
              </thead>
              <tbody>
                {devices.map((d) => (
                  <tr key={d.device_id}>
                    <td><LevelBadge level={d.level}>{d.used_pct.toFixed(0)}%</LevelBadge></td>
                    <td>
                      <RouterLink to={`/ports?q=${encodeURIComponent(d.hostname)}`}>{d.hostname}</RouterLink>
                      <div className="muted small">{d.pop} · {d.category}{d.vendor ? ` · ${d.vendor}` : ''}</div>
                    </td>
                    <td className="num">{d.used} / {d.total}</td>
                    <td className="num">{d.idle}{d.disabled ? ` (+${d.disabled} disabled)` : ''}</td>
                  </tr>
                ))}
                {!devices.length && <tr><td colSpan={4} className="empty">Tidak ada perangkat di atas {warn}%</td></tr>}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </>
  )
}

/** Forecast per device or POP from the 90-day trend of the total daily peak of its links. */
export function NodeForecastPanel() {
  const [level, setLevel] = useState<'device' | 'pop'>('pop')
  const [sort, setSort] = useState('months_to_threshold')
  const [q, setQ] = useState('')
  const [data, setData] = useState<{ total: number; nodes: NodeForecast[] } | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    setData(null)
    api
      .get<{ total: number; nodes: NodeForecast[] }>(`/capacity/nodes?level=${level}&sort=${sort}&limit=5000`)
      .then(setData)
      .catch((e: Error) => setError(e.message))
  }, [level, sort])

  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase()
    const all = data?.nodes ?? []
    return (needle ? all.filter((n) => n.label.toLowerCase().includes(needle) || (n.pop ?? '').toLowerCase().includes(needle)) : all).slice(0, 500)
  }, [data, q])

  return (
    <>
      <div className="toolbar">
        <div className="segmented">
          {(['pop', 'device'] as const).map((l) => (
            <button key={l} className={level === l ? 'active' : ''} onClick={() => setLevel(l)}>{l === 'pop' ? 'Per POP' : 'Per perangkat'}</button>
          ))}
        </div>
        <select value={sort} onChange={(e) => setSort(e.target.value)}>
          <option value="months_to_threshold">Paling cepat ke 80%</option>
          <option value="utilization_pct">Utilisasi tertinggi</option>
          <option value="growth">Growth tertinggi</option>
          <option value="capacity">Kapasitas terbesar</option>
        </select>
        <input className="search" placeholder="Cari hostname / POP…" value={q} onChange={(e) => setQ(e.target.value)} />
        <div className="spacer" />
        <span className="muted small">
          Total peak harian semua link node, tren least-squares 90 hari; forecast mulai dari nilai tren hari ini + peak tipikal (P95).
        </span>
      </div>
      {error && <div className="error">{error}</div>}
      {!data && !error && <div className="muted">Menghitung forecast…</div>}
      {data && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th />
                <th>{level === 'pop' ? 'POP' : 'Perangkat'}</th>
                <th className="num">Link</th>
                <th className="num">Kapasitas</th>
                <th className="num">Trafik</th>
                <th className="num">Basis forecast</th>
                <th className="num">Growth/bln</th>
                <th className="num" title="Kecocokan tren (R²)">R²</th>
                <th className="num">Ke 80%</th>
                <th className="num">Penuh</th>
                <th>Tgl 80%</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((n) => (
                <tr key={n.key}>
                  <td><ColorBadge color={n.color}>{n.utilization_pct.toFixed(0)}%</ColorBadge></td>
                  <td>{n.label}{level === 'device' && <div className="muted small">{n.pop}</div>}</td>
                  <td className="num">{n.links}</td>
                  <td className="num">{fmtG(n.capacity_gbps)}</td>
                  <td className="num">{fmtG(n.current_gbps)}</td>
                  <td className="num">
                    {fmtG(n.forecast_basis_gbps)}
                    <div className="muted small">{n.forecast_method === 'trend' ? `tren ${n.trend_days} hr` : 'trafik kini'}</div>
                  </td>
                  <td className="num">{n.growth_gbps_per_month != null ? `${n.growth_gbps_per_month > 0 ? '+' : ''}${n.growth_gbps_per_month.toFixed(1)}G` : '—'}</td>
                  <td className="num">{n.trend_r2 != null ? n.trend_r2.toFixed(2) : '—'}</td>
                  <td className="num">{fmtMonths(n.months_to_threshold)}</td>
                  <td className="num">{fmtMonths(n.months_to_full)}</td>
                  <td>{n.threshold_date ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="muted small">{data.total.toLocaleString()} {level === 'pop' ? 'POP' : 'perangkat'}{data.total > rows.length ? ` (ditampilkan ${rows.length})` : ''}</div>
        </div>
      )}
    </>
  )
}
