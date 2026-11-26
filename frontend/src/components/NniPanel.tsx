import { Fragment, useEffect, useMemo, useState } from 'react'
import { api, CapacityResult } from '../api'
import { ColorBadge, fmtG, fmtMonths } from './ui'

// Carrier NNI ports (and ports several sites share) planned as one object each: their own traffic and trend, with
// the sites behind them. Site links are read on the site's own port, so they never show the NNI filling up.

interface NniSite {
  link_id: number
  far_device: string | null
  far_port: string | null
  capacity_gbps: number
  current_gbps: number
}

export interface NniRow extends CapacityResult {
  interface_id: number
  device: string
  port: string
  description: string | null
  kind: 'nni' | 'shared'
  live_gbps: number | null
  samples_days: number
  sites: NniSite[]
}

const KIND_LABEL: Record<NniRow['kind'], string> = { nni: 'NNI carrier', shared: 'Port bersama' }

export default function NniPanel({ onOpenLink }: { onOpenLink: (id: number) => void }) {
  const [rows, setRows] = useState<NniRow[] | null>(null)
  const [kind, setKind] = useState<'' | NniRow['kind']>('nni')
  const [open, setOpen] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    api.get<NniRow[]>('/capacity/nnis').then(setRows).catch((e: Error) => setError(e.message))
  }, [])
  const shown = useMemo(() => (rows ?? []).filter((r) => !kind || r.kind === kind), [rows, kind])
  return (
    <>
      <div className="toolbar">
        <h2>NNI & port bersama</h2>
        <div className="spacer" />
        <select value={kind} onChange={(e) => setKind(e.target.value as '' | NniRow['kind'])}>
          <option value="nni">NNI carrier</option>
          <option value="shared">Port bersama lain</option>
          <option value="">Semua</option>
        </select>
      </div>
      {error && <div className="error">{error}</div>}
      <p className="hint">
        Port yang membawa banyak site (NNI ke carrier, atau port yang dipakai beberapa link) direncanakan sebagai satu
        objek: kapasitas port itu sendiri, trafik dan tren dari port itu. Link per site tetap dihitung dari port site
        (mis. 1G CSR) dan ditandai "via NNI" di tab Link. Histori terisi dari sync utilisasi dan import RRD berikutnya.
      </p>
      {!rows && !error && <div className="muted">Memuat…</div>}
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th />
              <th>Port</th>
              <th className="num">Kapasitas</th>
              <th className="num">Trafik</th>
              <th className="num">Util</th>
              <th className="num">Growth/bln</th>
              <th className="num">Histori</th>
              <th className="num">Ke 80%</th>
              <th className="num">Site</th>
            </tr>
          </thead>
          <tbody>
            {shown.map((r) => (
              <Fragment key={r.interface_id}>
                <tr className="clickable" onClick={() => setOpen(open === r.interface_id ? null : r.interface_id)}>
                  <td><ColorBadge color={r.color}>{r.committed_pct.toFixed(0)}%</ColorBadge></td>
                  <td>
                    <b>{r.device}</b> {r.port}
                    <div className="muted small">{KIND_LABEL[r.kind]}{r.description ? ` · ${r.description}` : ''}</div>
                  </td>
                  <td className="num">{fmtG(r.capacity_gbps)}</td>
                  <td className="num">{fmtG(r.current_gbps)}</td>
                  <td className="num">{r.utilization_pct.toFixed(1)}%</td>
                  <td className="num">{r.growth_gbps_per_month == null ? '—' : fmtG(r.growth_gbps_per_month)}</td>
                  <td className="num">{r.samples_days ? `${r.samples_days} hari` : 'belum ada'}</td>
                  <td className="num">{fmtMonths(r.months_to_threshold)}</td>
                  <td className="num">{r.sites.length} {open === r.interface_id ? '▲' : '▼'}</td>
                </tr>
                {open === r.interface_id && r.sites.map((s) => (
                  <tr key={s.link_id} className="clickable sub-row" onClick={() => onOpenLink(s.link_id)}>
                    <td />
                    <td className="small">↳ {s.far_device ?? '?'} {s.far_port ?? ''}</td>
                    <td className="num small">{fmtG(s.capacity_gbps)}</td>
                    <td className="num small">{fmtG(s.current_gbps)}</td>
                    <td colSpan={5} />
                  </tr>
                ))}
              </Fragment>
            ))}
            {rows && !shown.length && <tr><td colSpan={9} className="empty">Tidak ada port.</td></tr>}
          </tbody>
        </table>
      </div>
    </>
  )
}
