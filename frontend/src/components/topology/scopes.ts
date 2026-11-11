/** The device topology's filters: area > province > regency (kab/kota) > POP, each narrowing the next. */

export interface ScopeRow {
  area: string
  province: string
  regency: string
  pop: string
  pop_name: string
  devices: number
}

export interface Scope {
  area?: string
  province?: string
  regency?: string
  pop?: string
}

export interface ScopeOption {
  value: string
  label: string
  devices: number
}

export interface ScopeOptions {
  areas: ScopeOption[]
  provinces: ScopeOption[]
  regencies: ScopeOption[]
  pops: ScopeOption[]
  /** Devices the current choice covers. */
  devices: number
}

function grouped(rows: ScopeRow[], key: (r: ScopeRow) => string, label: (r: ScopeRow) => string = key): ScopeOption[] {
  const totals = new Map<string, ScopeOption>()
  for (const r of rows) {
    const value = key(r)
    const option = totals.get(value) ?? { value, label: label(r), devices: 0 }
    option.devices += r.devices
    totals.set(value, option)
  }
  return [...totals.values()].sort((a, b) => a.label.localeCompare(b.label))
}

/** What each dropdown offers given the choices above it, with device counts. */
export function scopeOptions(rows: ScopeRow[], scope: Scope): ScopeOptions {
  const inArea = rows.filter((r) => !scope.area || r.area === scope.area)
  const inProvince = inArea.filter((r) => !scope.province || r.province === scope.province)
  const inRegency = inProvince.filter((r) => !scope.regency || r.regency === scope.regency)
  const chosen = inRegency.filter((r) => !scope.pop || r.pop.toUpperCase() === scope.pop.toUpperCase())
  return {
    areas: grouped(rows, (r) => r.area),
    provinces: grouped(inArea, (r) => r.province),
    regencies: grouped(inProvince, (r) => r.regency),
    pops: grouped(inRegency, (r) => r.pop, (r) => (r.pop_name && r.pop_name !== r.pop ? `${r.pop} · ${r.pop_name}` : r.pop)),
    devices: chosen.reduce((sum, r) => sum + r.devices, 0),
  }
}

/** A new choice at one level clears the levels under it: they may not exist inside the new choice. */
export function choose(scope: Scope, level: keyof Scope, value: string | undefined): Scope {
  const order: (keyof Scope)[] = ['area', 'province', 'regency', 'pop']
  const next: Scope = {}
  for (const key of order) {
    if (key === level) {
      if (value) next[key] = value
      break
    }
    if (scope[key]) next[key] = scope[key]
  }
  return next
}

/** Where the device topology opens first: the area with the most devices. */
export function busiestArea(rows: ScopeRow[]): string | undefined {
  return scopeOptions(rows, {}).areas.sort((a, b) => b.devices - a.devices)[0]?.value
}
