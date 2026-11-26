// IPv6 / SRv6 plan types and formatting (backend/app/services/ipv6_plan.py).

export type Band = 'backbone' | 'access' | 'overflow'
export type RowStatus = 'baru' | 'proposed' | 'confirmed'

export interface Summary {
  cities: number
  routers: number
  backbone: number
  access: number
  overflow: number
  oam: number
  unmapped_devices: number
  unmapped_pops: number
  code_conflicts: number
  promote: string[]
  backbone_full: string[]
  unsaved: number
  excluded: Record<string, number>
}

export interface CityCapacity {
  code: string
  name: string
  region: string
  tier: 'METRO' | 'STANDARD'
  city_index: string
  infra_block: string
  backbone: number
  backbone_size: number
  backbone_fit: string
  access: number
  access_size: number
  fit_50: string
  overflow: number
  oam: number
  oam48: string
  residential_block: string | null
}

export interface RouterRow {
  device_id: number
  hostname: string
  role: string
  vendor: string | null
  pop_code: string | null
  city: string
  region: string
  band: Band
  group: string
  node_index: number | null
  locator: string | null
  loopback6: string | null
  end_sid: string | null
  isis_area: string | null
  loopback4: string | null
  how: string
  status: RowStatus
  note: string
}

export interface OamRow {
  device_id: number
  hostname: string
  role: string
  vendor: string | null
  pop_code: string | null
  city: string
  oam48: string
  group: string
  subnet: string
  address: string
  status: RowStatus
}

export interface PopRow {
  pop_id: number
  pop_code: string
  pop_name: string | null
  area: string | null
  devices: number
  city: string | null
  method: string
  decision: string | null
  note: string
  region: string | null
  suggested_city: string | null
  suggested_km: number | null
}

export const STATUS_PILL: Record<RowStatus, string> = { baru: 'pill-warn', proposed: 'pill-muted', confirmed: 'pill-ok' }
export const STATUS_LABEL: Record<RowStatus, string> = { baru: 'Baru', proposed: 'Tersimpan', confirmed: 'Dikonfirmasi' }

export const METHOD_LABEL: Record<string, string> = {
  exact: 'Kode sama', alias: 'Kode beda (nama cocok)', 'name (kode beda)': 'Kode beda (nama cocok)', name: 'Nama cocok',
  manual: 'Keputusan planner', unmapped: 'Belum ada di register', ignored: 'Diabaikan',
}

export function hexIndex(index: number | null): string {
  return index == null ? '-' : `0x${index.toString(16).toUpperCase().padStart(2, '0')}`
}

/** "12/16" with the share used, for a band column. */
export function bandUse(used: number, size: number): string {
  return `${used}/${size} (${size ? Math.round((100 * used) / size) : 0}%)`
}
