import { describe, expect, it } from 'vitest'
import { bandUse, hexIndex } from './ipv6Model'

describe('hexIndex', () => {
  it('should pad the node index to two hex digits', () => {
    expect(hexIndex(4)).toBe('0x04')
  })

  it('should show a dash when the router has no index', () => {
    expect(hexIndex(null)).toBe('-')
  })
})

describe('bandUse', () => {
  it('should show used, size and share when the band has nodes', () => {
    expect(bandUse(25, 48)).toBe('25/48 (52%)')
  })
})
