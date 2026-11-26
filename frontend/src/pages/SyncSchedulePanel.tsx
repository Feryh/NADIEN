import { useEffect, useState } from 'react'
import { api } from '../api'
import { STEP_LABELS } from './syncAllReport'
import {
  formatSeconds, HistoryJob, runReason, ScheduleRow, ScheduleStatus, sourceLabel, STATE_LABELS, STATE_PILLS,
  successRatio,
} from './syncSchedule'

function when(iso: string | null | undefined): string {
  return iso ? new Date(iso).toLocaleString('id-ID', { dateStyle: 'short', timeStyle: 'short' }) : '-'
}

function ScheduleLine({ row, open, onToggle }: { row: ScheduleRow; open: boolean; onToggle: () => void }) {
  const last = row.last
  return (
    <>
      <tr className={row.problems.length ? 'clickable' : undefined} onClick={row.problems.length ? onToggle : undefined}>
        <td><b>{row.label}</b><div className="small muted">{sourceLabel(row.source)}</div></td>
        <td>{row.when}</td>
        <td>
          {last ? <span className={`pill ${STATE_PILLS[last.state]}`}>{STATE_LABELS[last.state]}</span> : '-'}
          <div className="small muted">{last ? when(last.due_at) : 'belum ada jadwal lewat'}</div>
          {last && runReason(last) && (
            <div className={`small ${last.state === 'late' ? 'muted' : 'text-bad'}`}>{runReason(last)}</div>
          )}
        </td>
        <td>{row.last_ok ? <>{when(row.last_ok.finished_at)}<div className="small muted">{formatSeconds(row.last_ok.duration_s)}</div></> : '-'}</td>
        <td className="num">
          {successRatio(row)}
          {row.problems.length > 0 && (
            <div className={`small ${row.counts.missed || row.counts.failed ? 'text-bad' : 'muted'}`}>
              {[row.counts.missed && `${row.counts.missed} terlewat`, row.counts.failed && `${row.counts.failed} gagal`,
                row.counts.late && `${row.counts.late} terlambat`].filter(Boolean).join(' · ')} {open ? '▲' : '▼'}
            </div>
          )}
        </td>
      </tr>
      {open && row.problems.map((run) => (
        <tr key={run.due_at} className="sub-row">
          <td />
          <td className="small">{when(run.due_at)}</td>
          <td colSpan={3} className="small">
            <span className={`pill ${STATE_PILLS[run.state]}`}>{STATE_LABELS[run.state]}</span> {runReason(run)}
          </td>
        </tr>
      ))}
    </>
  )
}

/** Expected sync schedules vs. what ran, and the recent job history. */
export default function SyncSchedulePanel({ refreshKey }: { refreshKey?: string | null }) {
  const [schedule, setSchedule] = useState<ScheduleStatus | null>(null)
  const [jobs, setJobs] = useState<HistoryJob[] | null>(null)
  const [kind, setKind] = useState('')
  const [open, setOpen] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api.get<ScheduleStatus>('/collectors/schedule').then(setSchedule).catch((e: Error) => setError(e.message))
  }, [refreshKey])

  useEffect(() => {
    const query = kind ? `&kind=${encodeURIComponent(kind)}` : ''
    api.get<{ jobs: HistoryJob[] }>(`/collectors/jobs?limit=100${query}`)
      .then((r) => setJobs(r.jobs)).catch((e: Error) => setError(e.message))
  }, [refreshKey, kind])

  const kinds = [...new Set([...(schedule?.schedules.flatMap((s) => s.kinds) ?? []), ...(jobs?.map((j) => j.kind) ?? [])])].sort()

  return (
    <div className="card sync-schedule">
      <div className="toolbar">
        <h2>Jadwal sync</h2>
        {schedule && (schedule.problems
          ? <span className="pill pill-bad">{schedule.problems} jadwal terlewat / gagal</span>
          : <span className="pill pill-ok">semua jadwal jalan</span>)}
        <div className="spacer" />
        {schedule && <span className="small muted">zona waktu {schedule.timezone}</span>}
      </div>
      {error && <div className="error">{error}</div>}
      {schedule?.error && <div className="error">{schedule.error}</div>}
      {schedule && !schedule.schedules.length && !schedule.error && (
        <p className="muted">Belum ada jadwal yang dideklarasikan (NADIEN_SYNC_SCHEDULES) dan scheduler harian mati.</p>
      )}
      {schedule && schedule.schedules.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr><th>Sync</th><th>Jadwal</th><th>Run terakhir</th><th>Terakhir sukses</th><th className="num">Sukses</th></tr>
            </thead>
            <tbody>
              {schedule.schedules.map((row) => (
                <ScheduleLine key={row.key} row={row} open={open === row.key}
                  onToggle={() => setOpen(open === row.key ? null : row.key)} />
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="hint">
        Tiap jadwal dicocokkan dengan job yang mulai dalam toleransinya (harian: 7 hari terakhir, interval: 24 jam).
        Job yang baru mulai setelah toleransi tapi sebelum jadwal berikutnya dihitung terlambat, misalnya scheduler
        worker yang mengejar run harian setelah server menyala. Runner hanya menjalankan satu sync sekaligus; jadwal
        yang jatuh saat sync lain berjalan ditolak dan tercatat terlewat beserta sync yang menghalanginya.
      </p>

      <div className="toolbar">
        <h3>Riwayat job</h3>
        <div className="spacer" />
        <label className="small">
          Jenis{' '}
          <select value={kind} onChange={(e) => setKind(e.target.value)}>
            <option value="">Semua</option>
            {kinds.map((k) => <option key={k} value={k}>{k}</option>)}
          </select>
        </label>
      </div>
      <div className="table-wrap job-history">
        <table>
          <thead>
            <tr><th>Mulai</th><th>Jenis</th><th>Status</th><th className="num">Durasi</th><th>Ringkasan</th></tr>
          </thead>
          <tbody>
            {jobs?.map((job) => (
              <tr key={job.id}>
                <td>{when(job.started_at)}</td>
                <td>{job.kind}</td>
                <td>
                  <span className={`pill ${job.status === 'ok' ? 'pill-ok' : job.status === 'failed' ? 'pill-bad' : 'pill-muted'}`}>
                    {job.status}
                  </span>
                </td>
                <td className="num">{formatSeconds(job.duration_s)}</td>
                <td className="small">
                  {job.steps
                    ? job.steps.map((s) => `${STEP_LABELS[s.step] ?? s.step} ${s.status === 'ok' ? '✓' : s.status === 'skipped' ? '–' : '✗'}`).join(' · ')
                    : job.counts ? Object.entries(job.counts).slice(0, 4).map(([k, v]) => `${k} ${v.toLocaleString('id-ID')}`).join(' · ') : ''}
                  {job.error && <div className="text-bad">{job.error}</div>}
                </td>
              </tr>
            ))}
            {jobs && !jobs.length && <tr><td colSpan={5} className="empty">Belum ada job.</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  )
}
