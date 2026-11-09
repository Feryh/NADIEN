import { describe, expect, it } from 'vitest'
import { shortName } from './shared'

describe('shortName', () => {
  it('should drop the POP prefix when the hostname starts with it', () => {
    expect(shortName('JKT-NDC-P1-8201', 'JKT')).toBe('NDC-P1-8201')
  })

  it('should keep the hostname when the POP code is a regex character', () => {
    // Unescaped, "-?" would match an optional dash and strip one from the hostname.
    expect(shortName('CORE-', '?')).toBe('CORE-')
  })
})
