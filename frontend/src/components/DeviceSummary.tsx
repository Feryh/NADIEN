import { useEffect, useState } from 'react'
import { NavLink } from 'react-router-dom'
import { api, DeviceSummary as Summary } from '../api'

// Inventory page key per backend category (entities.ts DEVICE_CATEGORIES).
const PAGE: Record<string, string> = {
  router: 'devices-router', metro: 'devices-metro', csr: 'devices-csr', olt: 'devices-olt', access: 'devices-access',
}

function top(counts: Record<string, number>, n = 3): string {
  return Object.entries(counts)
    .slice(0, n)
    .map(([k, v]) => `${k} ${v.toLocaleString()}`)
    .join(' · ')
}

/** Device count per category (with its top vendors/areas); each card opens that category's list. */
export default function DeviceSummary() {
  const [data, setData] = useState<Summary | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api.get<Summary>('/inventory/devices/summary').then(setData).catch((e: Error) => setError(e.message))
  }, [])

  if (error) return <div className="error">{error}</div>
  if (!data) return <div className="card muted small">Memuat ringkasan perangkat…</div>
  return (
    <div className="device-summary">
      <div className="summary-card total card">
        <div className="kpi-label">Total perangkat</div>
        <div className="kpi-value">{data.total.toLocaleString()}</div>
      </div>
      {data.categories.map((c) => {
        const card = (
          <>
            <div className="kpi-label">{c.label}</div>
            <div className="kpi-value">{c.devices.toLocaleString()}</div>
            <div className="kpi-sub" title={Object.entries(c.vendors).map(([k, v]) => `${k}: ${v}`).join('\n')}>
              {top(c.vendors)}
            </div>
            <div className="kpi-sub muted" title={Object.entries(c.areas).map(([k, v]) => `${k}: ${v}`).join('\n')}>
              {top(c.areas, 2)}
            </div>
          </>
        )
        return PAGE[c.category] ? (
          <NavLink key={c.category} to={`/inventory/${PAGE[c.category]}`} className="summary-card card">
            {card}
          </NavLink>
        ) : (
          <div key={c.category} className="summary-card card">{card}</div>
        )
      })}
    </div>
  )
}
