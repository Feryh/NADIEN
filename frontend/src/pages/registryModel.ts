// Resource registry types and formatting (backend/app/services/resources.py): VLAN, VC id and IPv4 usage and reservations.

export type ResourceKind = 'vlan' | 'vc_id' | 'ipv4'
export type ReservationStatus = 'reserved' | 'fulfilled' | 'released' | 'expired'

export interface Usage { source: 'interface' | 'config' | 'reservation'; detail: string; reservation_id?: number }

export interface Reservation {
  id: number
  kind: ResourceKind
  device_id: number | null
  device: string | null
  peer_device_id: number | null
  peer_device: string | null
  value: number | null
  prefix: string | null
  pool_id: number | null
  status: ReservationStatus
  source: string
  label: string
  note: string | null
  created_by: string
  created_at: string | null
  expires_at: string | null
}

export interface IpPool {
  id: number
  name: string
  cidr: string
  purpose: 'p2p' | 'loopback' | 'customer' | 'other'
  default_prefix_len: number | null
  size: number
  used_addresses: number
  used_pct: number
  note: string | null
  usage?: { prefix: string; address: string | null; source: string; detail: string; reservation_id?: number }[]
}

export const KIND_LABELS: Record<ResourceKind, string> = { vlan: 'VLAN', vc_id: 'VC id', ipv4: 'IPv4' }

export const STATUS_LABELS: Record<ReservationStatus, string> = {
  reserved: 'Direservasi', fulfilled: 'Sudah di config', released: 'Dilepas', expired: 'Kedaluwarsa',
}

export const STATUS_PILLS: Record<ReservationStatus, string> = {
  reserved: 'pill-warn', fulfilled: 'pill-ok', released: 'pill-muted', expired: 'pill-muted',
}

export const SOURCE_LABELS: Record<Usage['source'], string> = {
  interface: 'Interface', config: 'Config RANCID', reservation: 'Reservasi',
}

/** What a reservation holds, in one line: "VLAN 1480 · PE-A", "VC id 12490 · PE-A ↔ PE-B", "10.20.0.0/30". */
export function reservationTarget(r: Reservation): string {
  if (r.kind === 'ipv4') return r.prefix ?? '-'
  const where = r.kind === 'vc_id' ? `${r.device ?? r.device_id} ↔ ${r.peer_device ?? r.peer_device_id}` : `${r.device ?? r.device_id}`
  return `${KIND_LABELS[r.kind]} ${r.value} · ${where}`
}

/** "1480-1482,1490" -> [1480, 1481, 1482, 1490]; null when the text is not a VLAN list. */
export function parseNumberList(text: string, max = 4094): number[] | null {
  const out = new Set<number>()
  for (const part of text.split(/[,\s]+/).filter(Boolean)) {
    const match = /^(\d+)(?:-(\d+))?$/.exec(part)
    if (!match) return null
    const low = Number(match[1])
    const high = Number(match[2] ?? match[1])
    if (low < 1 || high > max || low > high || high - low > 512) return null
    for (let v = low; v <= high; v++) out.add(v)
  }
  return out.size ? [...out].sort((a, b) => a - b) : null
}
