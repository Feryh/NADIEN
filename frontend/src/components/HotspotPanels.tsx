import { MouseEvent, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { CartesianGrid, Line, LineChart, ReferenceLine, Tooltip, XAxis, YAxis } from 'recharts'
import { api, HotspotLevel, Hotspots, LinkCapacityDetail, OltCapacity } from '../api'
import { Pager, usePaged } from './Pager'
import { CHART_TOOLTIP, fmtG } from './ui'

// Dashboard lists: links at or above the utilization threshold, and OLTs whose GPON ports or uplinks are.

const LEVEL_PILL: Record<HotspotLevel, string> = { critical: 'pill-bad', warning: 'pill-warn', normal: 'pill-ok', suspect: 'pill-muted' }
const HOT: HotspotLevel[] = ['warning', 'critical']

function Pct({ value, level }: { value: number; level: HotspotLevel }) {
  return <span className={`pill ${LEVEL_PILL[level]}`}>{value.toFixed(1)}%</span>
}

function Device({ id, name }: { id: number | null; name: string }) {
  return id != null ? <Link to={`/devices/${id}`}>{name}</Link> : <>{name}</>
}

// ------------------------------------------------------------------ 7-day trend popover

const TREND_DAYS = 7
const POPOVER_WIDTH = 320

interface TrendPoint { t: number; gbps: number; pct: number }

interface DayPeak { t: number; gbps: number }

// One request per link while the page is open: hovering back and forth must not refetch.
const historyCache = new Map<number, Promise<DayPeak[]>>()

function loadHistory(linkId: number): Promise<DayPeak[]> {
  let pending = historyCache.get(linkId)
  if (!pending) {
    pending = api.get<LinkCapacityDetail>(`/capacity/links/${linkId}?months=1`).then((d) => d.history
      .map((h) => ({ t: Date.parse(h.ts), gbps: Math.max(h.in_gbps, h.out_gbps) })))
    pending.catch(() => historyCache.delete(linkId))  // a failed load is tried again on the next hover
    historyCache.set(linkId, pending)
  }
  return pending
}

/** The last TREND_DAYS of one link (every sample, as stored), or of several added up (an OLT's uplinks). A link's
 *  history holds one peak per day plus the live samples of the last day or two, so each link is reduced to its peak
 *  per day before the links are added: adding every sample of a day counted 61 samples of one 8 Gbps link as 394 G. */
export function combineTrend(histories: DayPeak[][], capacityGbps: number, now = Date.now()): TrendPoint[] {
  const since = now - TREND_DAYS * 86_400_000
  const recent = histories.map((history) => history.filter((p) => p.t >= since))
  const points = recent.length === 1 ? recent[0] : sumDailyPeaks(recent)
  return [...points].sort((a, b) => a.t - b.t)
    .map((p) => ({ t: p.t, gbps: p.gbps, pct: capacityGbps ? (p.gbps / capacityGbps) * 100 : 0 }))
}

function sumDailyPeaks(histories: DayPeak[][]): DayPeak[] {
  const total = new Map<string, DayPeak>()
  for (const history of histories) {
    const peaks = new Map<string, DayPeak>()
    for (const p of history) {
      const day = new Date(p.t).toISOString().slice(0, 10)
      const best = peaks.get(day)
      if (!best || p.gbps > best.gbps) peaks.set(day, p)
    }
    for (const [day, p] of peaks) {
      const sum = total.get(day)
      total.set(day, sum ? { t: Math.min(sum.t, p.t), gbps: sum.gbps + p.gbps } : { ...p })
    }
  }
  return [...total.values()]
}

function loadTrend(linkIds: number[], capacityGbps: number): Promise<TrendPoint[]> {
  return Promise.all(linkIds.map(loadHistory)).then((histories) => combineTrend(histories, capacityGbps))
}

const dayLabel = (t: number) => new Date(t).toLocaleDateString('id-ID', { day: '2-digit', month: 'short' })

function TrendPopover({ linkIds, capacityGbps, at, pinned, warningPct, criticalPct, empty }: {
  linkIds: number[]; capacityGbps: number; at: DOMRect; pinned: boolean; warningPct: number; criticalPct: number; empty?: string
}) {
  const [points, setPoints] = useState<TrendPoint[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const key = linkIds.join(",")
  useEffect(() => {
    setPoints(null)
    setError(null)
    loadTrend(linkIds, capacityGbps).then(setPoints).catch((e: Error) => setError(e.message))
  }, [key, capacityGbps])  // eslint-disable-line react-hooks/exhaustive-deps -- key stands for linkIds

  // Fixed to the viewport: the table scrolls sideways and would clip a popover inside it.
  const left = Math.max(8, Math.min(at.right - POPOVER_WIDTH, window.innerWidth - POPOVER_WIDTH - 8))
  const below = at.bottom + 190 < window.innerHeight
  const style = { left, width: POPOVER_WIDTH, ...(below ? { top: at.bottom + 4 } : { bottom: window.innerHeight - at.top + 4 }) }
  const peak = points?.length ? points.reduce((a, b) => (b.pct > a.pct ? b : a)) : null
  const yMax = Math.max(100, Math.ceil((peak?.pct ?? 0) / 10) * 10)
  return (
    <div className="trend-pop card" style={style} role="dialog" aria-label={`Tren utilisasi ${TREND_DAYS} hari`}>
      <div className="trend-pop-head">
        <b>Tren {TREND_DAYS} hari</b>
        {peak && <span className="muted small">puncak {peak.pct.toFixed(1)}% ({fmtG(peak.gbps)}) · {dayLabel(peak.t)}</span>}
        {pinned && <span className="muted small"> · klik lagi untuk menutup</span>}
      </div>
      {error && <div className="error-text small">{error}</div>}
      {!points && !error && <div className="muted small">Memuat…</div>}
      {points && !points.length && <div className="muted small">{empty ?? `Belum ada histori ${TREND_DAYS} hari terakhir untuk link ini.`}</div>}
      {points && points.length > 0 && (
        <LineChart width={POPOVER_WIDTH - 24} height={140} data={points} margin={{ top: 6, right: 8, bottom: 0, left: -18 }}>
          <CartesianGrid stroke="#64748b" strokeOpacity={0.25} />
          <XAxis dataKey="t" type="number" scale="time" domain={['dataMin', 'dataMax']} tickFormatter={dayLabel}
            tick={{ fill: '#94a3b8', fontSize: 10 }} minTickGap={24} />
          <YAxis domain={[0, yMax]} tick={{ fill: '#94a3b8', fontSize: 10 }} unit="%" />
          <ReferenceLine y={warningPct} stroke="#eab308" strokeDasharray="4 4" />
          <ReferenceLine y={criticalPct} stroke="#ef4444" strokeDasharray="4 4" />
          <Tooltip contentStyle={CHART_TOOLTIP} labelFormatter={(t) => new Date(Number(t)).toLocaleString('id-ID')}
            formatter={(_v, _n, item) => [`${(item.payload as TrendPoint).pct.toFixed(1)}% · ${fmtG((item.payload as TrendPoint).gbps)}`, 'Utilisasi']} />
          <Line type="monotone" dataKey="pct" stroke="#38bdf8" dot={points.length <= 14} strokeWidth={2} isAnimationActive={false} />
        </LineChart>
      )}
    </div>
  )
}

/** Hover shows a row's 7-day trend; a click keeps it open (touch screens have no hover). */
function useTrendHover() {
  const [trend, setTrend] = useState<{ id: number; at: DOMRect; pinned: boolean } | null>(null)
  const cellProps = (id: number) => ({
    className: 'trend-cell',
    onMouseEnter: (e: MouseEvent<HTMLElement>) => {
      const at = e.currentTarget.getBoundingClientRect()
      setTrend((t) => (t?.pinned ? t : { id, at, pinned: false }))
    },
    onMouseLeave: () => setTrend((t) => (t?.pinned ? t : null)),
    onClick: (e: MouseEvent<HTMLElement>) => {
      const at = e.currentTarget.getBoundingClientRect()
      setTrend((t) => (t?.pinned && t.id === id ? null : { id, at, pinned: true }))
    },
  })
  return { trend, cellProps }
}

export function LinkHotspotsPanel() {
  const [data, setData] = useState<Hotspots | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [kind, setKind] = useState<'' | 'bundle' | 'physical'>('')
  const [q, setQ] = useState('')
  const { trend, cellProps } = useTrendHover()

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
              <th className="num">Kapasitas</th>
              <th className="num" title="Persentil 95 trafik kemarin (jam sibuk), dasar utilisasi dan forecast">Trafik p95</th>
              <th className="num" title="Rate saat Full sync terakhir dari Observium, sisi port yang sama">Live</th>
              <th title="Arahkan mouse atau klik untuk melihat tren 7 hari">Utilisasi</th></tr>
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
                <td className="num muted">{r.live_gbps != null ? fmtG(r.live_gbps) : '–'}</td>
                <td {...cellProps(r.link_id)}>
                  <Pct value={r.utilization_pct} level={r.level} /> <span className="trend-hint" aria-hidden="true">📈</span>
                </td>
              </tr>
            ))}
            {data && !rows.length && <tr><td colSpan={9} className="empty">Tidak ada link di atas threshold untuk filter ini</td></tr>}
            {!data && !error && <tr><td colSpan={9} className="empty">Memuat…</td></tr>}
          </tbody>
        </table>
      </div>
      <Pager paged={paged} unit="link" />
      {trend && data && (() => {
        const row = data.links.rows.find((r) => r.link_id === trend.id)
        return row ? <TrendPopover linkIds={[row.link_id]} capacityGbps={row.capacity_gbps} at={trend.at} pinned={trend.pinned}
          warningPct={data.thresholds.warning_pct} criticalPct={data.thresholds.critical_pct} /> : null
      })()}
    </div>
  )
}

export function OltHotspotsPanel() {
  const [data, setData] = useState<OltCapacity | null>(null)
  const [error, setError] = useState<string | null>(null)
  const { trend, cellProps } = useTrendHover()

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
          Utilisasi GPON (ONT online dari {data?.thresholds.pon_max_onts ?? 64} ONT per port PON) atau uplink
          ≥ {data?.thresholds.warning_pct ?? 70}%, atau ada port PON ≥ {data?.thresholds.warning_pct ?? 70}%{data ? ` · ${data.total} OLT` : ''}
        </span>
      </div>
      {error && <div className="error">{error}</div>}
      <div className="table-wrap">
        <table>
          <thead>
            <tr><th>OLT</th><th>POP</th>
              <th title="Total ONT online dibagi kapasitas seluruh port PON (port × maks ONT per port)">Utilisasi GPON</th>
              <th className="num" title={`Port PON aktif (ada ONT) dari seluruh port PON; penuh = terisi ≥ ${data?.thresholds.warning_pct ?? 70}% dari ${data?.thresholds.pon_max_onts ?? 64} ONT; dan port terpadat`}>Port PON</th>
              <th className="num">Uplink</th>
              <th title="Link: peak hari lengkap terakhir (persentil 95 sampel 5 menit, lonjakan sekali dilewati). Tanpa link: rate live port uplink pada sync utilisasi terakhir. Arahkan mouse atau klik untuk tren 7 hari">Utilisasi uplink</th></tr>
          </thead>
          <tbody>
            {paged.pageRows.map((r) => (
              <tr key={r.device_id}>
                <td><Link to={`/devices/${r.device_id}`}>{r.hostname}</Link> <span className="muted small">{r.vendor ?? ''}</span></td>
                <td className="muted">{r.pop ?? '–'}</td>
                <td>{r.pon_utilization_pct != null && r.pon_level
                  ? <><Pct value={r.pon_utilization_pct} level={r.pon_level} />{' '}
                    <span className="muted small">{r.onts_online.toLocaleString('id-ID')} / {r.pon_capacity_onts.toLocaleString('id-ID')} ONT</span></>
                  : <span className="muted small" title="OLT tidak melaporkan jumlah ONT">tanpa data ONT</span>}</td>
                <td className="num">
                  <span className="muted small">aktif</span> {r.pon_used} <span className="muted small">/ {r.pon_total}</span>
                  {' · '}<span className="muted small">penuh</span> {r.pon_hot_ports}
                  {r.busiest_pon_pct != null && <div className="muted small">terpadat {r.busiest_pon_onts}/{data?.thresholds.pon_max_onts ?? 64} ONT</div>}</td>
                <td className="num">{r.uplinks ? `${r.uplinks} ${r.uplink_source === 'port' ? 'port' : 'link'} · ${fmtG(r.uplink_capacity_gbps)}` : '–'}
                  {r.uplinks_down > 0 && <> <span className="pill pill-bad">{r.uplinks_down} down</span></>}</td>
                <td {...(r.uplinks ? cellProps(r.device_id) : {})}>{r.uplinks
                  ? <><Pct value={r.uplink_utilization_pct} level={r.uplink_level} /> <span className="muted small">{fmtG(r.uplink_current_gbps)}</span>
                    {' '}<span className="trend-hint" aria-hidden="true">📈</span>
                    {r.uplink_source === 'port'
                      ? <div className="muted small" title="Perangkat lawan tidak ada di inventory/LLDP: dibaca dari port uplink OLT (rate live)">live · dari port, tanpa link</div>
                      : <div className="muted small" title="Peak hari lengkap terakhir: persentil 95 sampel 5 menit, lonjakan sekali dilewati">peak harian (p95)</div>}</>
                  : <span className="muted">tanpa uplink</span>}</td>
              </tr>
            ))}
            {data && !rows.length && <tr><td colSpan={6} className="empty">Tidak ada OLT di atas threshold</td></tr>}
            {!data && !error && <tr><td colSpan={6} className="empty">Memuat…</td></tr>}
          </tbody>
        </table>
      </div>
      <Pager paged={paged} unit="OLT" />
      {trend && data && (() => {
        const row = data.rows.find((r) => r.device_id === trend.id)
        return row ? <TrendPopover linkIds={row.uplink_link_ids} capacityGbps={row.uplink_capacity_gbps} at={trend.at}
          pinned={trend.pinned} warningPct={data.thresholds.warning_pct} criticalPct={data.thresholds.critical_pct}
          empty={row.uplink_link_ids.length ? undefined : 'Uplink dibaca dari port OLT (tanpa link): belum ada histori.'} /> : null
      })()}
    </div>
  )
}
