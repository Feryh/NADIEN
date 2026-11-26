import { describe, expect, it } from 'vitest'
import { parseNumberList, Reservation, reservationTarget } from './registryModel'

const base: Reservation = {
  id: 1, kind: 'vc_id', device_id: 1, device: 'PE-A', peer_device_id: 2, peer_device: 'PE-B', value: 12490,
  prefix: null, pool_id: null, status: 'reserved', source: 'manual', label: 'X', note: null, created_by: 'u',
  created_at: null, expires_at: null,
}

describe('reservationTarget', () => {
  it('should show both PEs when the reservation is a VC id', () => {
    expect(reservationTarget(base)).toBe('VC id 12490 · PE-A ↔ PE-B')
  })

  it('should show the prefix when the reservation is IPv4', () => {
    expect(reservationTarget({ ...base, kind: 'ipv4', prefix: '10.20.0.0/30' })).toBe('10.20.0.0/30')
  })
})

describe('parseNumberList', () => {
  it('should expand ranges when the list is valid', () => {
    expect(parseNumberList('1480-1482,1490')).toEqual([1480, 1481, 1482, 1490])
  })

  it('should return null when a VLAN is out of range', () => {
    expect(parseNumberList('4095')).toBeNull()
  })
})
