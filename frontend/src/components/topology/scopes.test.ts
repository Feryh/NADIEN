import { describe, expect, it } from 'vitest'
import { busiestArea, choose, scopeOptions, type ScopeRow } from './scopes'

const rows: ScopeRow[] = [
  { area: 'Jabodetabek', province: 'DKI Jakarta', regency: 'Jakarta', pop: 'JKT', pop_name: 'Jakarta - NDC', devices: 490 },
  { area: 'Jabodetabek', province: 'DKI Jakarta', regency: 'Jakarta', pop: 'JKT2', pop_name: 'JKT2', devices: 25 },
  { area: 'Jabodetabek', province: 'Banten', regency: 'Kota Tangerang', pop: 'TNG', pop_name: 'TNG', devices: 148 },
  { area: 'Sumatera', province: 'Sumatera Utara', regency: 'Medan', pop: 'MDN', pop_name: 'MDN', devices: 300 },
]

describe('scopeOptions', () => {
  it('should offer only the provinces of the chosen area, with their device counts', () => {
    expect(scopeOptions(rows, { area: 'Jabodetabek' }).provinces).toEqual([
      { value: 'Banten', label: 'Banten', devices: 148 },
      { value: 'DKI Jakarta', label: 'DKI Jakarta', devices: 515 },
    ])
  })

  it('should count the devices the whole choice covers', () => {
    expect(scopeOptions(rows, { area: 'Jabodetabek', province: 'DKI Jakarta', regency: 'Jakarta', pop: 'jkt2' }).devices).toBe(25)
  })

  it('should name a POP by its code and, when different, its name', () => {
    const pops = scopeOptions(rows, { regency: 'Jakarta' }).pops.map((o) => o.label)
    expect(pops).toEqual(['JKT · Jakarta - NDC', 'JKT2'])
  })
})

describe('choose', () => {
  it('should clear the choices under the one that changed', () => {
    expect(choose({ area: 'Jabodetabek', province: 'Banten', regency: 'Kota Tangerang', pop: 'TNG' }, 'province', 'DKI Jakarta'))
      .toEqual({ area: 'Jabodetabek', province: 'DKI Jakarta' })
  })

  it('should drop the level itself when "all" is chosen', () => {
    expect(choose({ area: 'Jabodetabek', province: 'Banten' }, 'province', undefined)).toEqual({ area: 'Jabodetabek' })
  })
})

describe('busiestArea', () => {
  it('should open on the area with the most devices', () => {
    expect(busiestArea(rows)).toBe('Jabodetabek')
  })
})
