import { FormEvent, useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import {
  CartesianGrid,
  ComposedChart,
  Legend,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { api, CapacityColor, CapacityResult, LinkCapacity, LinkCapacityDetail } from '../api'
import { HotspotsPanel, NodeForecastPanel } from '../components/CapacityPanels'
import { CAPACITY_HEX, ColorBadge, fmtG, fmtMonths, UtilBar } from '../components/ui'

const GROWTH_SOURCE_LABEL = { manual: 'manual', trend: 'tren', input: 'input', none: '—' }
const TREND_WINDOWS = ['7', '30', '90']

function fmtTrend(v: number | null | undefined) {
  if (v == null) return '—'
  return `${v > 0 ? '+' : ''}${v.toFixed(1)}G`
}

/** Growth per month seen over the last 7 / 30 / 90 days of daily peaks. */
function TrendCells({ link }: { link: LinkCapacity }) {
  return (
    <>
      {TREND_WINDOWS.map((w) => {
        const v = link.growth_trends?.[w]
        return (
          <td key={w} className={`num ${v != null && v > 0 ? 'trend-up' : v != null && v < 0 ? 'trend-down' : ''}`}>
            {fmtTrend(v)}
          </td>
        )
      })}
    </>
  )
}

type Tab = 'links' | 'hotspots' | 'nodes'
const TABS: [Tab, string][] = [['links', 'Link & forecast'], ['hotspots', 'Hotspot'], ['nodes', 'Forecast per node']]

export default function Capacity() {
  const [params, setParams] = useSearchParams()
  const tab = (params.get('tab') as Tab) || 'links'
  const selectedId = params.get('link') ? Number(params.get('link')) : null
  const setTab = (t: Tab) => setParams(t === 'links' ? {} : { tab: t })
  const openLink = (id: number) => setParams({ ...(tab !== 'links' ? { tab } : {}), link: String(id) })

  return (
    <div className="capacity">
      <Calculator />

      {selectedId != null && <LinkDetail linkId={selectedId} onClose={() => setParams(tab !== 'links' ? { tab } : {})} />}

      <div className="card">
        <div className="tabs">
          {TABS.map(([t, label]) => (
            <button key={t} className={tab === t ? 'active' : ''} onClick={() => setTab(t)}>{label}</button>
          ))}
        </div>
        {tab === 'links' && <LinkTable selectedId={selectedId} onOpen={openLink} />}
        {tab === 'hotspots' && <HotspotsPanel onOpenLink={openLink} />}
        {tab === 'nodes' && <NodeForecastPanel />}
      </div>
    </div>
  )
}

function LinkTable({ selectedId, onOpen }: { selectedId: number | null; onOpen: (id: number) => void }) {
  const [links, setLinks] = useState<LinkCapacity[]>([])
  const [filter, setFilter] = useState<CapacityColor | ''>('')
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api
      .get<LinkCapacity[]>('/capacity/links')
      .then(setLinks)
      .catch((e: Error) => setError(e.message))
  }, [])

  // Thousands of links: render the first 1000 of the current filter (sorted soonest-to-exhaust).
  const rows = useMemo(() => (filter ? links.filter((l) => l.color === filter) : links).slice(0, 1000), [links, filter])

  return (
    <>
        <div className="toolbar">
          <h2>Link Capacity & Forecast</h2>
          <div className="spacer" />
          <select value={filter} onChange={(e) => setFilter(e.target.value as CapacityColor | '')}>
            <option value="">Semua warna</option>
            <option value="red">Merah</option>
            <option value="yellow">Kuning</option>
            <option value="green">Hijau</option>
          </select>
        </div>
        {error && <div className="error">{error}</div>}
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th />
                <th>Link</th>
                <th>POP</th>
                <th className="num">Capacity</th>
                <th className="num">Current</th>
                <th className="num">Reserved</th>
                <th className="num">Available</th>
                <th className="num">Util</th>
                <th className="num" title="Growth per bulan dari tren 7 hari terakhir">Tren 7h</th>
                <th className="num" title="Growth per bulan dari tren 30 hari terakhir">Tren 30h</th>
                <th className="num" title="Growth per bulan dari tren 90 hari terakhir">Tren 90h</th>
                <th className="num">Growth/bln dipakai</th>
                <th className="num" title="Titik awal forecast: nilai tren 90 hari hari ini + peak tipikal (P95), atau trafik kini bila histori < 14 hari">
                  Basis forecast
                </th>
                <th className="num">Ke 80%</th>
                <th className="num">Penuh</th>
                <th>Tgl penuh</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((l) => (
                <tr
                  key={l.link_id}
                  className={`clickable ${selectedId === l.link_id ? 'selected' : ''}`}
                  onClick={() => onOpen(l.link_id)}
                >
                  <td>
                    <ColorBadge color={l.color}>{l.committed_pct.toFixed(0)}%</ColorBadge>
                  </td>
                  <td>
                    {l.a_device} ↔ {l.b_device}
                    {l.status === 'down' && <span className="pill pill-bad">down</span>}
                    <div className="muted small">{l.link_type}</div>
                  </td>
                  <td>{l.a_pop === l.b_pop ? l.a_pop : `${l.a_pop}–${l.b_pop}`}</td>
                  <td className="num">{fmtG(l.capacity_gbps)}</td>
                  <td className="num">{fmtG(l.current_gbps)}</td>
                  <td className="num">{fmtG(l.reserved_gbps)}</td>
                  <td className="num">{fmtG(l.available_gbps)}</td>
                  <td className="num">{l.utilization_pct.toFixed(1)}%</td>
                  <TrendCells link={l} />
                  <td className="num">
                    {l.growth_gbps_per_month != null ? `${l.growth_gbps_per_month.toFixed(1)}G` : '—'}
                    <div className="muted small">{GROWTH_SOURCE_LABEL[l.growth_source]}</div>
                  </td>
                  <td className="num">
                    {fmtG(l.forecast_basis_gbps)}
                    <div className="muted small">{l.forecast_method === 'trend' ? `tren ${l.trend_days} hr` : 'trafik kini'}</div>
                  </td>
                  <td className="num">{fmtMonths(l.months_to_threshold)}</td>
                  <td className="num">{fmtMonths(l.months_to_full)}</td>
                  <td>{l.exhaustion_date ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {links.length > rows.length && (
          <div className="muted small">Ditampilkan {rows.length.toLocaleString()} dari {links.length.toLocaleString()} link (urut paling cepat habis).</div>
        )}
    </>
  )
}

function Calculator() {
  const [form, setForm] = useState({ capacity: '400', current: '250', reserved: '0', growth: '20', mode: 'abs' as 'abs' | 'pct' })
  const [result, setResult] = useState<CapacityResult | null>(null)
  const [error, setError] = useState<string | null>(null)

  async function calc(e?: FormEvent) {
    e?.preventDefault()
    setError(null)
    try {
      setResult(
        await api.post<CapacityResult>('/capacity/calculate', {
          capacity_gbps: Number(form.capacity),
          current_gbps: Number(form.current),
          reserved_gbps: Number(form.reserved || 0),
          growth_gbps_per_month: form.mode === 'abs' ? Number(form.growth || 0) : 0,
          growth_pct_per_month: form.mode === 'pct' ? Number(form.growth || 0) : null,
        }),
      )
    } catch (err) {
      setError((err as Error).message)
    }
  }

  useEffect(() => {
    calc()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const set = (k: keyof typeof form) => (e: { target: { value: string } }) => setForm({ ...form, [k]: e.target.value })

  return (
    <div className="card calculator">
      <h2>What-if Calculator</h2>
      <form onSubmit={calc} className="calc-form">
        <label>
          <span>Capacity (G)</span>
          <input type="number" step="any" value={form.capacity} onChange={set('capacity')} required />
        </label>
        <label>
          <span>Current (G)</span>
          <input type="number" step="any" value={form.current} onChange={set('current')} required />
        </label>
        <label>
          <span>Reserved (G)</span>
          <input type="number" step="any" value={form.reserved} onChange={set('reserved')} />
        </label>
        <label>
          <span>Growth / bulan</span>
          <div className="inline">
            <input type="number" step="any" value={form.growth} onChange={set('growth')} />
            <select value={form.mode} onChange={set('mode')}>
              <option value="abs">G</option>
              <option value="pct">%</option>
            </select>
          </div>
        </label>
        <button className="primary" type="submit">
          Hitung
        </button>
      </form>
      {error && <div className="error">{error}</div>}
      {result && (
        <div className="calc-result">
          <div className="big" style={{ color: CAPACITY_HEX[result.color] }}>
            {result.months_to_full == null ? 'Tidak penuh (growth ≤ 0)' : `Penuh dalam ${fmtMonths(result.months_to_full)}`}
          </div>
          <div className="muted">
            Ambang 80% dalam {fmtMonths(result.months_to_threshold)}
            {result.threshold_date && ` (${result.threshold_date})`}
            {result.exhaustion_date && ` · penuh ${result.exhaustion_date}`}
          </div>
          <UtilBar capacity={result.capacity_gbps} current={result.current_gbps} reserved={result.reserved_gbps} />
        </div>
      )}
    </div>
  )
}

function LinkDetail({ linkId, onClose }: { linkId: number; onClose: () => void }) {
  const [detail, setDetail] = useState<LinkCapacityDetail | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    setDetail(null)
    api
      .get<LinkCapacityDetail>(`/capacity/links/${linkId}?months=24`)
      .then(setDetail)
      .catch((e: Error) => setError(e.message))
  }, [linkId])

  const chart = useMemo(() => {
    if (!detail) return []
    // Numeric time axis so 90 daily samples and 24 monthly forecast points are spaced by real time.
    const history = detail.history.map((h) => ({
      t: Date.parse(h.ts),
      actual: Math.max(h.in_gbps, h.out_gbps),
      capacity: detail.capacity_gbps,
      threshold: detail.forecast[0].threshold_gbps,
    }))
    const forecast = detail.forecast.slice(1).map((f) => ({
      t: Date.parse(f.date),
      projected: f.projected_gbps,
      capacity: f.capacity_gbps,
      threshold: f.threshold_gbps,
    }))
    const today = detail.forecast[0]
    return [
      ...history,
      { t: Date.now(), actual: detail.current_gbps, projected: today.projected_gbps, capacity: today.capacity_gbps, threshold: today.threshold_gbps },
      ...forecast,
    ]
  }, [detail])

  return (
    <div className="card">
      <div className="toolbar">
        <h2>{detail ? `${detail.a_device} ↔ ${detail.b_device}` : `Link #${linkId}`}</h2>
        {detail && <ColorBadge color={detail.color}>{detail.committed_pct.toFixed(1)}% committed</ColorBadge>}
        <div className="spacer" />
        <button onClick={onClose}>Tutup</button>
      </div>
      {error && <div className="error">{error}</div>}
      {detail && (
        <>
          <div className="detail-stats">
            <div><span className="muted">Capacity</span> {fmtG(detail.capacity_gbps)}</div>
            <div><span className="muted">Current</span> {fmtG(detail.current_gbps)}</div>
            <div><span className="muted">Reserved</span> {fmtG(detail.reserved_gbps)}</div>
            <div><span className="muted">Available</span> {fmtG(detail.available_gbps)}</div>
            <div>
              <span className="muted">Growth</span>{' '}
              {detail.growth_gbps_per_month != null ? `${detail.growth_gbps_per_month.toFixed(2)}G/bln` : '—'} (
              {GROWTH_SOURCE_LABEL[detail.growth_source]})
            </div>
            <div>
              <span className="muted">Tren 7/30/90 hari</span>{' '}
              {TREND_WINDOWS.map((w) => fmtTrend(detail.growth_trends?.[w])).join(' · ')} per bln
            </div>
            <div><span className="muted">Histori</span> {detail.samples_days} hari</div>
            <div><span className="muted">Ke 80%</span> {fmtMonths(detail.months_to_threshold)}</div>
            <div><span className="muted">Penuh</span> {fmtMonths(detail.months_to_full)} {detail.exhaustion_date && `(${detail.exhaustion_date})`}</div>
          </div>
          <div style={{ height: 320 }}>
            <ResponsiveContainer>
              <ComposedChart data={chart} margin={{ top: 10, right: 20, bottom: 0, left: 0 }}>
                <CartesianGrid stroke="#1e293b" />
                <XAxis
                  dataKey="t"
                  type="number"
                  scale="time"
                  domain={['dataMin', 'dataMax']}
                  tickFormatter={(t: number) => new Date(t).toISOString().slice(0, 7)}
                  tick={{ fill: '#94a3b8', fontSize: 11 }}
                  minTickGap={40}
                />
                <YAxis tick={{ fill: '#94a3b8', fontSize: 11 }} unit="G" />
                <Tooltip
                  contentStyle={{ background: '#0f172a', border: '1px solid #334155' }}
                  labelFormatter={(t) => new Date(Number(t)).toISOString().slice(0, 10)}
                  formatter={(v) => `${Number(v).toFixed(1)}G`}
                />
                <Legend />
                <Line type="monotone" dataKey="actual" name="Aktual (peak)" stroke="#38bdf8" dot={false} strokeWidth={2} />
                <Line type="monotone" dataKey="projected" name="Forecast (+reserved)" stroke="#a78bfa" dot={false} strokeDasharray="6 4" strokeWidth={2} />
                <Line type="stepAfter" dataKey="threshold" name="Ambang 80%" stroke={CAPACITY_HEX.yellow} dot={false} />
                <Line type="stepAfter" dataKey="capacity" name="Capacity" stroke={CAPACITY_HEX.red} dot={false} />
              </ComposedChart>
            </ResponsiveContainer>
          </div>
        </>
      )}
    </div>
  )
}
