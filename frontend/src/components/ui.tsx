import { ReactNode } from 'react'
import { CapacityColor } from '../api'

export const CAPACITY_HEX: Record<CapacityColor, string> = {
  green: '#22c55e',
  yellow: '#eab308',
  red: '#ef4444',
}

export function ColorBadge({ color, children }: { color: CapacityColor; children?: ReactNode }) {
  return (
    <span className="badge" style={{ background: `${CAPACITY_HEX[color]}22`, color: CAPACITY_HEX[color] }}>
      ● {children ?? color}
    </span>
  )
}

export function Kpi({ label, value, sub }: { label: string; value: ReactNode; sub?: ReactNode }) {
  return (
    <div className="kpi card">
      <div className="kpi-label">{label}</div>
      <div className="kpi-value">{value}</div>
      {sub && <div className="kpi-sub">{sub}</div>}
    </div>
  )
}

/** Stacked bar: current | reserved | free. */
export function UtilBar({ capacity, current, reserved }: { capacity: number; current: number; reserved: number }) {
  const pct = (v: number) => (capacity ? Math.min((v / capacity) * 100, 100) : 0)
  const committed = pct(current + reserved)
  const color: CapacityColor = committed >= 80 ? 'red' : committed >= 60 ? 'yellow' : 'green'
  return (
    <div className="utilbar-wrap">
      <div className="utilbar">
        <div style={{ width: `${pct(current)}%`, background: CAPACITY_HEX[color] }} />
        <div style={{ width: `${Math.min(pct(reserved), 100 - pct(current))}%`, background: `${CAPACITY_HEX[color]}66` }} />
      </div>
      <div className="small muted">
        {fmtG(current)} used · {fmtG(reserved)} reserved · {fmtG(Math.max(capacity - current - reserved, 0))} free /{' '}
        {fmtG(capacity)}
      </div>
    </div>
  )
}

export function fmtG(v: number | null | undefined): string {
  if (v == null) return '–'
  return v >= 1000 ? `${(v / 1000).toFixed(2)}T` : `${Number(v.toFixed(1))}G`
}

export function fmtMonths(v: number | null | undefined): string {
  if (v == null) return '—'
  if (v === 0) return 'sudah lewat'
  return `${v.toFixed(1)} bln`
}
