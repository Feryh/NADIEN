// Site B of an L2VPN without an access port: per VLAN a new VSI/VPLS or one more peer on an instance already on PE B.

export interface VplsNeighbour { ip: string; pw_id: number | null; pw: string | null }

/** One VPLS instance on a PE as read from its RANCID config by /configs/l2vpn/vpls (all four dialects). */
export interface VplsInstance {
  key: string
  name: string
  vsi_id: number | null
  interfaces: string[]
  neighbours: VplsNeighbour[]
  vlans: number[]
  pw_ids: number[]
}

export interface VplsList {
  hostname: string
  dialect: string | null
  supported: boolean
  instances: VplsInstance[]
  error: string | null
}

/** "3-4,55" -> [3, 4, 55]; null while the list is invalid (the backend reports why). */
export function expandVlans(text: string | null): number[] | null {
  if (!text || !text.trim()) return null
  const vlans = new Set<number>()
  for (const part of text.trim().split(/[,\s]+/)) {
    const match = /^(\d{1,4})(?:-(\d{1,4}))?$/.exec(part)
    if (!match) return null
    const low = Number(match[1])
    const high = Number(match[2] ?? match[1])
    if (low < 1 || high > 4094 || low > high) return null
    for (let vlan = low; vlan <= high; vlan++) vlans.add(vlan)
  }
  return [...vlans].sort((a, b) => a - b)
}

/** Instances that look like they carry the VLAN: on an attachment circuit, as a PW / VSI id, or in the name. */
export function matchesVlan(instance: VplsInstance, vlan: number): boolean {
  if (instance.vlans.includes(vlan) || instance.pw_ids.includes(vlan) || instance.vsi_id === vlan) return true
  return new RegExp(`(^|[^0-9])${vlan}$`).test(instance.name)
}

/** The id site A must signal to reach this instance: Huawei / FiberHome peers use the vsi-id. */
export function instanceVcId(instance: VplsInstance): number | null {
  return instance.vsi_id ?? (instance.pw_ids.length === 1 ? instance.pw_ids[0] : null)
}

export function instanceLabel(instance: VplsInstance, peLoopback: string | null): string {
  const id = instance.vsi_id != null ? `vsi-id ${instance.vsi_id}`
    : instance.pw_ids.length ? `pw-id ${instance.pw_ids.slice(0, 3).join(', ')}${instance.pw_ids.length > 3 ? ', …' : ''}` : 'tanpa id'
  const peers = instance.neighbours.map((n) => n.ip)
  const already = peLoopback && peers.includes(peLoopback) ? ' · PE A sudah peer' : ''
  return `${instance.name} · ${id} · ${peers.length} peer${already}`
}

/** b_join query values ("VLAN=key") for the VLANs still in the list. */
export function joinParams(joins: Record<number, string>, vlans: number[] | null): string[] {
  return (vlans ?? []).filter((vlan) => joins[vlan]).map((vlan) => `${vlan}=${joins[vlan]}`)
}
