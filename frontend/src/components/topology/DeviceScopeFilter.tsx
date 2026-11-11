import { choose, scopeOptions, type Scope, type ScopeOption, type ScopeRow } from './scopes'

const count = (n: number) => n.toLocaleString('id-ID')

function ScopeSelect({ label, all, value, options, onChange }: {
  label: string
  all: string
  value: string | undefined
  options: ScopeOption[]
  onChange: (value: string | undefined) => void
}) {
  return (
    <label className="scope-select">
      <span className="muted small">{label}</span>
      <select value={value ?? ''} onChange={(e) => onChange(e.target.value || undefined)} disabled={!options.length}>
        <option value="">{all}</option>
        {options.map((o) => <option key={o.value} value={o.value}>{o.label} ({count(o.devices)})</option>)}
      </select>
    </label>
  )
}

/** Area > Provinsi > Kab/Kota > POP as filters of the device topology, each narrowing the next. */
export default function DeviceScopeFilter({ rows, scope, onChange }: {
  rows: ScopeRow[]
  scope: Scope
  onChange: (scope: Scope) => void
}) {
  const options = scopeOptions(rows, scope)
  const pick = (level: keyof Scope) => (value: string | undefined) => onChange(choose(scope, level, value))
  return (
    <div className="scope-filter">
      <ScopeSelect label="Area" all="Semua area" value={scope.area} options={options.areas} onChange={pick('area')} />
      <ScopeSelect label="Provinsi" all="Semua provinsi" value={scope.province} options={options.provinces} onChange={pick('province')} />
      <ScopeSelect label="Kab/Kota" all="Semua kab/kota" value={scope.regency} options={options.regencies} onChange={pick('regency')} />
      <ScopeSelect label="POP" all="Semua POP" value={scope.pop} options={options.pops} onChange={pick('pop')} />
      <span className="muted small">{count(options.devices)} perangkat</span>
    </div>
  )
}
