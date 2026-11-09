import { describe, expect, it } from 'vitest'
import { formatDuration, phaseLabel, stepSummary, syncAllSteps } from './syncAllReport'

describe('syncAllSteps', () => {
  it('should return the steps when the report comes from sync-all', () => {
    const steps = syncAllSteps({ status: 'ok', failed_steps: [], steps: [{ step: 'backup', status: 'ok', duration_s: 1 }] })
    expect(steps?.map((s) => s.step)).toEqual(['backup'])
  })

  it('should return null when the report comes from another job', () => {
    expect(syncAllSteps({ counts: {} })).toBeNull()
    expect(syncAllSteps(null)).toBeNull()
  })
})

describe('stepSummary', () => {
  it('should show the error when a step failed', () => {
    expect(stepSummary({ step: 'rrd', status: 'failed', duration_s: 2, error: 'RrdError: timeout' })).toBe('RrdError: timeout')
  })

  it('should show the reason when a step was skipped', () => {
    expect(stepSummary({ step: 'configs', status: 'skipped', duration_s: 0, detail: 'gitweb not configured' })).toBe(
      'gitweb not configured',
    )
  })

  it('should show device and port counts when an Observium step finished', () => {
    const step = { step: 'observium', status: 'ok' as const, duration_s: 5, report: { counts: { devices_seen: 1200, ports_seen: 30500 } } }
    expect(stepSummary(step)).toBe('1.200 device · 30.500 port')
  })

  it('should show backfilled links when an RRD step finished', () => {
    const step = {
      step: 'rrd',
      status: 'ok' as const,
      duration_s: 5,
      report: { links: 10, links_with_history: 8, links_backfilled: 2, samples: 40, error_count: 1 },
    }
    expect(stepSummary(step)).toBe('8/10 link · 2 backfill · 40 sampel · 1 error')
  })

  it('should show created and updated links when discovery finished', () => {
    const step = { step: 'discovery', status: 'ok' as const, duration_s: 5, report: { lldp_links: 5, config_links: 3, created: 2, updated: 6 } }
    expect(stepSummary(step)).toBe('5 LLDP · 3 config · 2 baru · 6 diperbarui')
  })
})

describe('formatDuration', () => {
  it('should show minutes and padded seconds when longer than a minute', () => {
    expect(formatDuration(65.4)).toBe('1m 05s')
    expect(formatDuration(9.6)).toBe('10s')
  })
})

describe('phaseLabel', () => {
  it('should name the sync-all step and its sub-phase when the phase is nested', () => {
    expect(phaseLabel('observium: ports', { ports: 'mengambil port' })).toBe('Observium utama — mengambil port')
    expect(phaseLabel('backup', { backup: 'backup database' })).toBe('backup database')
    expect(phaseLabel('rrd: rrd 200/900', {})).toBe('Histori RRD — rrd 200/900')
  })
})
