import { describe, expect, it } from 'vitest'
import { expandVlans, instanceLabel, joinParams, matchesVlan, VplsInstance } from './l2vpnVpls'

const vsi: VplsInstance = {
  key: 'vsi|CUST_1490', name: 'CUST_1490', vsi_id: 12490, interfaces: ['Virtual-Ethernet0/1/0.2290'],
  neighbours: [{ ip: '10.20.0.52', pw_id: 12490, pw: null }], vlans: [2290], pw_ids: [12490],
}

describe('expandVlans', () => {
  it('should expand ranges and lists when the VLAN text is valid', () => {
    expect(expandVlans('3-4,55')).toEqual([3, 4, 55])
  })

  it('should return null when the VLAN text is invalid', () => {
    expect(expandVlans('3-x')).toBeNull()
  })
})

describe('matchesVlan', () => {
  it('should match an instance when its name ends with the VLAN', () => {
    expect(matchesVlan(vsi, 1490)).toBe(true)
  })

  it('should not match an instance when only a longer number ends with the VLAN', () => {
    expect(matchesVlan(vsi, 490)).toBe(false)
  })
})

describe('instanceLabel', () => {
  it('should flag the instance when PE A is already one of its peers', () => {
    expect(instanceLabel(vsi, '10.20.0.52')).toContain('PE A sudah peer')
  })
})

describe('joinParams', () => {
  it('should drop joins of VLANs no longer in the list', () => {
    expect(joinParams({ 3: 'vsi|A', 9: 'vsi|B' }, [3, 4])).toEqual(['3=vsi|A'])
  })
})
