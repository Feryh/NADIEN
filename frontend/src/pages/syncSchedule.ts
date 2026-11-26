// Expected sync schedules against the jobs that ran (backend/app/services/sync_schedule.py).

export type RunState = 'ok' | 'late' | 'failed' | 'running' | 'missed' | 'due'

export interface JobView {
  id: string
  kind: string
  status: string
  started_at: string | null
  finished_at: string | null
  duration_s: number | null
  error: string | null
}

export interface ScheduledRun {
  due_at: string
  state: RunState
  job: JobView | null
  blocked_by?: JobView
}

export interface ScheduleRow {
  key: string
  label: string
  kinds: string[]
  when: string
  source: string
  grace_minutes: number
  last: ScheduledRun | null
  next_due: ScheduledRun | null
  expected: number
  counts: Record<'ok' | 'late' | 'failed' | 'missed' | 'running', number>
  last_ok: JobView | null
  problems: ScheduledRun[]
}

export interface ScheduleStatus {
  timezone: string
  schedules: ScheduleRow[]
  problems: number
  error: string | null
}

export interface HistoryJob extends JobView {
  phase: string
  counts: Record<string, number> | null
  steps?: { step: string; status: string; duration_s: number | null }[]
}

export const STATE_LABELS: Record<RunState, string> = {
  ok: 'OK', late: 'Terlambat', failed: 'Gagal', running: 'Berjalan', missed: 'Terlewat', due: 'Menunggu',
}

export const STATE_PILLS: Record<RunState, string> = {
  ok: 'pill-ok', late: 'pill-warn', failed: 'pill-bad', running: 'pill-muted', missed: 'pill-bad', due: 'pill-muted',
}

/** "cronjob:nadien-rrd" -> "CronJob nadien-rrd"; "worker" -> "Scheduler worker". */
export function sourceLabel(source: string): string {
  if (source === 'worker') return 'Scheduler worker'
  return source.startsWith('cronjob:') ? `CronJob ${source.slice('cronjob:'.length)}` : source
}

export function formatSeconds(seconds: number | null): string {
  if (seconds == null) return '-'
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  return minutes < 60 ? `${minutes}m ${seconds % 60}s` : `${Math.floor(minutes / 60)}j ${minutes % 60}m`
}

/** Why a run is not OK, in one line. */
export function runReason(run: ScheduledRun): string {
  if (run.state === 'failed') return run.job?.error || 'job gagal'
  if (run.state === 'late') {
    const started = run.job?.started_at
    return started
      ? `jalan terlambat, mulai ${new Date(started).toLocaleTimeString('id-ID', { hour: '2-digit', minute: '2-digit' })}`
      : 'jalan terlambat'
  }
  if (run.state !== 'missed') return ''
  const blocker = run.blocked_by
  return blocker
    ? `tidak jalan: sync ${blocker.kind} sedang berjalan (runner hanya satu sync sekaligus)`
    : 'tidak ada job yang mulai pada waktunya'
}

/** Settled runs that ran and succeeded (late ones included), e.g. "143/144". */
export function successRatio(row: ScheduleRow): string {
  return `${row.counts.ok + row.counts.late}/${row.expected}`
}
