import { describe, expect, it } from 'vitest'
import { combineTrend } from './HotspotPanels'

const DAY = 86_400_000
const NOW = Date.parse('2026-11-27T12:00:00Z')

describe('combineTrend', () => {
  it('should add the uplinks up per day when an OLT has several uplink links', () => {
    const a = [{ t: NOW - 2 * DAY, gbps: 6 }, { t: NOW - DAY, gbps: 7 }]
    const b = [{ t: NOW - 2 * DAY + 60_000, gbps: 3 }, { t: NOW - DAY + 60_000, gbps: 5 }]
    expect(combineTrend([a, b], 20, NOW).map((p) => [p.gbps, p.pct])).toEqual([[9, 45], [12, 60]])
  })

  it('should leave out days older than the trend window when history goes further back', () => {
    const history = [{ t: NOW - 10 * DAY, gbps: 9 }, { t: NOW - DAY, gbps: 4 }]
    expect(combineTrend([history], 10, NOW).map((p) => p.gbps)).toEqual([4])
  })
})
