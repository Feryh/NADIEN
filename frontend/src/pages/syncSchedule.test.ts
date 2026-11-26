import { describe, expect, it } from 'vitest'
import { formatSeconds, runReason, ScheduledRun, sourceLabel } from './syncSchedule'

const missed: ScheduledRun = { due_at: '2026-11-26T01:30:00+07:00', state: 'missed', job: null }

describe('sourceLabel', () => {
  it('should name the CronJob when the source is a cronjob', () => {
    expect(sourceLabel('cronjob:nadien-rrd')).toBe('CronJob nadien-rrd')
  })
})

describe('runReason', () => {
  it('should name the blocking sync when a missed run was blocked', () => {
    const run = { ...missed, blocked_by: { id: 'x', kind: 'observium', status: 'ok', started_at: null, finished_at: null, duration_s: 3000, error: null } }
    expect(runReason(run)).toContain('sync observium sedang berjalan')
  })

  it('should say no job started when nothing blocked the missed run', () => {
    expect(runReason(missed)).toBe('tidak ada job yang mulai pada waktunya')
  })
})

describe('formatSeconds', () => {
  it('should show hours and minutes when the run took over an hour', () => {
    expect(formatSeconds(3725)).toBe('1j 2m')
  })
})
