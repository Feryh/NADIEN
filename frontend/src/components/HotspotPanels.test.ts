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

  it('should add each link peak of the day when a link holds several live samples that day', () => {
    const live = [{ t: NOW - 3_600_000, gbps: 7.9 }, { t: NOW - 1_800_000, gbps: 8.4 }, { t: NOW - 600_000, gbps: 8.1 }]
    const other = [{ t: NOW - 3_600_000, gbps: 2 }]
    expect(combineTrend([live, other], 20, NOW).map((p) => p.gbps)).toEqual([10.4])
  })

  it('should keep every sample when the trend is of one link', () => {
    const live = [{ t: NOW - 3_600_000, gbps: 7.9 }, { t: NOW - 1_800_000, gbps: 8.4 }]
    expect(combineTrend([live], 10, NOW).map((p) => p.gbps)).toEqual([7.9, 8.4])
  })

  it('should leave out days older than the trend window when history goes further back', () => {
    const history = [{ t: NOW - 10 * DAY, gbps: 9 }, { t: NOW - DAY, gbps: 4 }]
    expect(combineTrend([history], 10, NOW).map((p) => p.gbps)).toEqual([4])
  })
})
