import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, HotspotLevel, Hotspots, OltCapacity } from '../api'
import { Pager, usePaged } from './Pager'
import { fmtG } from './ui'

// Dashboard lists: links at or above the utilization threshold, and OLTs whose GPON ports or uplinks are.

const LEVEL_PILL: Record<HotspotLevel, string> = { critical: 'pill-bad', warning: 'pill-warn', normal: 'pill-ok', suspect: 'pill-muted' }
const HOT: HotspotLevel[] = ['warning', 'critical']

function Pct({ value, level }: { value: number; level: HotspotLevel }) {
  return <span className={`pill ${LEVEL_PILL[level]}`}>{value.toFixed(1)}%</span>
}

function Device({ id, name }: { id: number | null; name: string }) {
  return id != null ? <Link to={`/devices/${id}`}>{name}</Link> : <>{name}</>
}

export function LinkHotspotsPanel() {
  const [data, setData] = useState<Hotspots | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [kind, setKind] = useState<'' | 'bundle' | 'physical'>('')
  const [q, setQ] = useState('')

  useEffect(() => {
    api.get<Hotspots>('/capacity/hotspots?limit=2000').then(setData).catch((e: Error) => setError(e.message))
  }, [])

  const needle = q.trim().toLowerCase()
  const rows = (data?.links.rows ?? []).filter((r) => HOT.includes(r.level)
    && (!kind || (kind === 'bundle') === r.bundle)
    && (!needle || [r.a_device, r.b_device, r.a_port, r.b_port, r.a_pop, r.b_pop].some((v) => v?.toLowerCase().includes(needle))))
  const paged = usePaged(rows, `${kind}|${needle}`)
  return (
    <div className="card">
      <div className="toolbar">
        <h2>Link utilisasi ≥ {data?.thresholds.warning_pct ?? 70}%</h2>
        {data && <span className="muted small">
          {data.links.counts.critical} kritis (≥ {data.thresholds.critical_pct}%) · {data.links.counts.warning} waspada
          {data.links.counts.suspect ? ` · ${data.links.counts.suspect} kapasitas mencurigakan tidak ditampilkan` : ''}
        </span>}
        <div className="spacer" />
        <input className="search" placeholder="Cari perangkat, interface, POP…" value={q} onChange={(e) => setQ(e.target.value)} />
        <select value={kind} onChange={(e) => setKind(e.target.value as '' | 'bundle' | 'physical')}>
          <option value="">Bundle & fisik</option>
          <option value="bundle">Bundle (LAG) saja</option>
          <option value="physical">Port fisik saja</option>
        </select>
      </div>
      {error && <div className="error">{error}</div>}
      <div className="table-wrap">
        <table>
          <thead>
            <tr><th>Perangkat A</th><th>Interface A</th><th>Perangkat B</th><th>Interface B</th><th>Tipe</th>
              <th className="num">Kapasitas</th><th className="num">Trafik</th><th>Utilisasi</th></tr>
          </thead>
          <tbody>
            {paged.pageRows.map((r) => (
              <tr key={r.link_id}>
                <td><Device id={r.a_device_id} name={r.a_device} /><div className="muted small">{r.a_pop ?? ''}</div></td>
                <td>{r.a_port ? <code>{r.a_port}</code> : <span className="muted">–</span>}</td>
                <td><Device id={r.b_device_id} name={r.b_device} /><div className="muted small">{r.b_pop ?? ''}</div></td>
                <td>{r.b_port ? <code>{r.b_port}</code> : <span className="muted">–</span>}</td>
                <td>{r.bundle ? <span className="pill pill-muted">Bundle</span> : <span className="muted small">Fisik</span>}
                  {r.status === 'down' && <> <span className="pill pill-bad">down</span></>}</td>
                <td className="num">{fmtG(r.capacity_gbps)}</td>
                <td className="num">{fmtG(r.current_gbps)}</td>
                <td><Pct value={r.utilization_pct} level={r.level} /></td>
              </tr>
            ))}
            {data && !rows.length && <tr><td colSpan={8} className="empty">Tidak ada link di atas threshold untuk filter ini</td></tr>}
            {!data && !error && <tr><td colSpan={8} className="empty">Memuat…</td></tr>}
          </tbody>
        </table>
      </div>
      <Pager paged={paged} unit="link" />
    </div>
  )
}

export function OltHotspotsPanel() {
  const [data, setData] = useState<OltCapacity | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api.get<OltCapacity>('/capacity/olts?limit=2000').then(setData).catch((e: Error) => setError(e.message))
  }, [])

  const rows = data?.rows ?? []
  const paged = usePaged(rows)
  return (
    <div className="card">
      <div className="toolbar">
        <h2>OLT perlu perhatian</h2>
        <span className="muted small">
          Port GPON terisi ≥ {data?.thresholds.warning_pct ?? 70}% dari {data?.thresholds.pon_max_onts ?? 64} ONT,
          atau uplink ≥ {data?.thresholds.warning_pct ?? 70}%{data ? ` · ${data.total} OLT` : ''}
        </span>
      </div>
      {error && <div className="error">{error}</div>}
      <div className="table-wrap">
        <table>
          <thead>
            <tr><th>OLT</th><th>POP</th><th>Port GPON terpadat</th><th className="num">Port GPON ≥ threshold</th>
              <th className="num">ONT online</th><th className="num">Uplink</th><th>Utilisasi uplink</th></tr>
          </thead>
          <tbody>
            {paged.pageRows.map((r) => (
              <tr key={r.device_id}>
                <td><Link to={`/devices/${r.device_id}`}>{r.hostname}</Link> <span className="muted small">{r.vendor ?? ''}</span></td>
                <td className="muted">{r.pop ?? '–'}</td>
                <td>{r.busiest_pon_pct != null && r.pon_level
                  ? <><Pct value={r.busiest_pon_pct} level={r.pon_level} /> <span className="muted small">{r.busiest_pon_onts} ONT</span></>
                  : <span className="muted small" title="OLT tidak melaporkan jumlah ONT">tanpa data ONT</span>}</td>
                <td className="num">{r.pon_hot_ports} <span className="muted small">/ {r.pon_total}</span></td>
                <td className="num">{r.onts_online.toLocaleString('id-ID')}</td>
                <td className="num">{r.uplinks ? `${r.uplinks} × · ${fmtG(r.uplink_capacity_gbps)}` : '–'}
                  {r.uplinks_down > 0 && <> <span className="pill pill-bad">{r.uplinks_down} down</span></>}</td>
                <td>{r.uplinks ? <><Pct value={r.uplink_utilization_pct} level={r.uplink_level} /> <span className="muted small">{fmtG(r.uplink_current_gbps)}</span></> : <span className="muted">tanpa link</span>}</td>
              </tr>
            ))}
            {data && !rows.length && <tr><td colSpan={7} className="empty">Tidak ada OLT di atas threshold</td></tr>}
            {!data && !error && <tr><td colSpan={7} className="empty">Memuat…</td></tr>}
          </tbody>
        </table>
      </div>
      <Pager paged={paged} unit="OLT" />
    </div>
  )
}
