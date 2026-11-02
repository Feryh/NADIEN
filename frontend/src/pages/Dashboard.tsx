import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, CapacityColor, DashboardSummary, PopUtilization } from '../api'
import { CAPACITY_HEX, ColorBadge, fmtG, fmtMonths, Kpi, UtilBar } from '../components/ui'

const COLORS: CapacityColor[] = ['green', 'yellow', 'red']

export default function Dashboard() {
  const [summary, setSummary] = useState<DashboardSummary | null>(null)
  const [pops, setPops] = useState<PopUtilization[]>([])
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    Promise.all([api.get<DashboardSummary>('/dashboard/summary'), api.get<PopUtilization[]>('/capacity/pops')])
      .then(([s, p]) => {
        setSummary(s)
        setPops(p)
      })
      .catch((e: Error) => setError(e.message))
  }, [])

  if (error) return <div className="error">Backend tidak bisa dihubungi: {error}</div>
  if (!summary) return <div className="muted">Memuat…</div>

  const c = summary.counts
  const totalLinks = c.links || 1

  return (
    <div className="dashboard">
      <div className="kpis">
        <Kpi label="Metro POP" value={c.pops} sub={`${c.devices} router · ${c.interfaces} interface`} />
        <Kpi label="Links" value={c.links} sub={`${summary.links_down} down`} />
        <Kpi label="Available Ports" value={summary.available_ports} sub="physical, admin up, belum terpakai" />
        <Kpi
          label="Network Capacity"
          value={fmtG(summary.capacity.total_gbps)}
          sub={`${fmtG(summary.capacity.available_gbps)} available`}
        />
        <Kpi label="Utilization" value={`${summary.capacity.utilization_pct}%`} sub={`${fmtG(summary.capacity.current_gbps)} traffic`} />
        <Kpi label="Customers" value={c.customers} sub={`${c.services} service aktif`} />
      </div>

      <div className="grid-2">
        <div className="card">
          <h2>Link Capacity Health</h2>
          <div className="stackbar">
            {COLORS.map((color) => {
              const n = summary.link_colors[color] ?? 0
              return n ? (
                <div key={color} style={{ width: `${(n / totalLinks) * 100}%`, background: CAPACITY_HEX[color] }} title={`${color}: ${n}`}>
                  {n}
                </div>
              ) : null
            })}
          </div>
          <h3>Segera habis (ke ambang 80%)</h3>
          <table>
            <tbody>
              {summary.exhausting_soon.map((l) => (
                <tr key={l.link_id}>
                  <td>
                    <ColorBadge color={l.color}>{l.committed_pct.toFixed(0)}%</ColorBadge>
                  </td>
                  <td>
                    <Link to={`/capacity?link=${l.link_id}`}>
                      {l.a_device} ↔ {l.b_device}
                    </Link>
                  </td>
                  <td className="num">{fmtG(l.capacity_gbps)}</td>
                  <td className="num">{fmtMonths(l.months_to_threshold)}</td>
                </tr>
              ))}
              {!summary.exhausting_soon.length && (
                <tr>
                  <td className="empty">Tidak ada link dengan growth positif</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>

        <div className="card">
          <h2>Customer Services</h2>
          <table>
            <thead>
              <tr>
                <th>Type</th>
                <th className="num">Services</th>
                <th className="num">Bandwidth</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(summary.services_by_type).map(([type, n]) => (
                <tr key={type}>
                  <td>{type.toUpperCase()}</td>
                  <td className="num">{n}</td>
                  <td className="num">{fmtG((summary.bandwidth_by_type_mbps[type] ?? 0) / 1000)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <h3>External data (via API)</h3>
          <div className="muted">
            {c.fibers} fiber · {c.dwdm_nodes} DWDM node — <Link to="/inventory/integration-sources">kelola sumber API</Link>
          </div>
        </div>
      </div>

      <div className="card">
        <h2>POP Utilization</h2>
        <table>
          <thead>
            <tr>
              <th>POP</th>
              <th className="num">Router</th>
              <th className="num">Links</th>
              <th style={{ width: '45%' }}>Capacity</th>
              <th className="num">Committed</th>
            </tr>
          </thead>
          <tbody>
            {pops.map((p) => (
              <tr key={p.pop_id}>
                <td>
                  <b>{p.code}</b> <span className="muted">{p.name}</span>
                </td>
                <td className="num">{p.devices}</td>
                <td className="num">{p.links}</td>
                <td>
                  <UtilBar capacity={p.capacity_gbps} current={p.current_gbps} reserved={p.reserved_gbps} />
                </td>
                <td className="num">
                  <ColorBadge color={p.color}>{p.committed_pct.toFixed(1)}%</ColorBadge>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
