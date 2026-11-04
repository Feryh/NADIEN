import { useCallback, useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { api } from '../api'
import { useAuth } from '../auth'

// Change requests: frozen scripts, approval by someone else, then pre-check / push / post-check / rollback per site.

type Phase = 'precheck' | 'push' | 'postcheck' | 'rollback'
type ChangeStatus = 'pending_approval' | 'approved' | 'rejected' | 'in_progress' | 'implemented' | 'failed' | 'rolled_back'

interface Site {
  label: 'A' | 'B'
  device_id: number | null
  hostname: string | null
  host: string | null
  dialect: string | null
  dialect_label: string
  precheck: string[]
  config: string[]
  rollback: string[]
  postcheck: string[]
  warnings: string[]
}

interface Run {
  id: number
  site: 'A' | 'B'
  phase: Phase
  protocol: string
  status: 'running' | 'ok' | 'failed'
  started_by: string
  device_user: string
  started_at: string
  finished_at: string | null
  error: string | null
  log?: string
}

interface Change {
  id: number
  title: string
  service_id: number | null
  status: ChangeStatus
  note: string | null
  created_by: string
  approved_by: string | null
  approved_at: string | null
  review_note: string | null
  created_at: string
  sites?: Site[]
  runs?: Run[]
}

const STATUS: Record<ChangeStatus, { label: string; pill: string }> = {
  pending_approval: { label: 'Menunggu approval', pill: 'pill-warn' },
  approved: { label: 'Disetujui', pill: 'pill-ok' },
  rejected: { label: 'Ditolak', pill: 'pill-bad' },
  in_progress: { label: 'Sedang dieksekusi', pill: 'pill-warn' },
  implemented: { label: 'Terimplementasi', pill: 'pill-ok' },
  failed: { label: 'Gagal', pill: 'pill-bad' },
  rolled_back: { label: 'Di-rollback', pill: 'pill-muted' },
}

const PHASES: { key: Phase; label: string; script: keyof Pick<Site, 'precheck' | 'config' | 'postcheck' | 'rollback'> }[] = [
  { key: 'precheck', label: 'Pre-check', script: 'precheck' },
  { key: 'push', label: 'Push config', script: 'config' },
  { key: 'postcheck', label: 'Post-check', script: 'postcheck' },
  { key: 'rollback', label: 'Rollback', script: 'rollback' },
]
const PHASE_LABEL: Record<Phase, string> = { precheck: 'Pre-check', push: 'Push', postcheck: 'Post-check', rollback: 'Rollback' }

export const fmtTime = (iso: string | null) => {
  if (!iso) return '-'
  // SQLite's server-default timestamps come back without a zone; they are UTC like the rest.
  return new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(iso) ? iso : `${iso}Z`).toLocaleString('id-ID')
}

export default function Changes() {
  const [params, setParams] = useSearchParams()
  const selected = params.get('id') ? Number(params.get('id')) : null
  const [list, setList] = useState<Change[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(() => {
    api.get<Change[]>('/changes').then(setList).catch((e: Error) => setError(e.message))
  }, [])
  useEffect(load, [load])

  return (
    <div className="capacity">
      <div className="card">
        <div className="toolbar">
          <h2>Change &amp; Eksekusi</h2>
          <div className="spacer" />
          <button onClick={load}>Muat ulang</button>
        </div>
        <p className="hint">
          Ajukan change dari <Link to="/configs">Config Generator</Link>. Script dibekukan saat diajukan, di-approve oleh
          orang lain, lalu dijalankan ke perangkat lewat SSH/telnet dengan kredensial Anda sendiri (tidak disimpan).
        </p>
        {error && <div className="error">{error}</div>}
        <div className="table-wrap mop-services">
          <table>
            <thead><tr><th>#</th><th>Change</th><th>Status</th><th>Diajukan</th><th>Approval</th></tr></thead>
            <tbody>
              {list?.map((c) => (
                <tr key={c.id} className={`clickable ${selected === c.id ? 'selected' : ''}`}
                  onClick={() => setParams({ id: String(c.id) })}>
                  <td>{c.id}</td>
                  <td><b>{c.title}</b></td>
                  <td><span className={`pill ${STATUS[c.status].pill}`}>{STATUS[c.status].label}</span></td>
                  <td>{c.created_by}<div className="muted">{fmtTime(c.created_at)}</div></td>
                  <td>{c.approved_by ?? '-'}</td>
                </tr>
              ))}
              {list && !list.length && <tr><td colSpan={5} className="empty">Belum ada change.</td></tr>}
            </tbody>
          </table>
        </div>
      </div>
      {selected != null && <ChangeDetail key={selected} id={selected} onChanged={load} />}
    </div>
  )
}

function ChangeDetail({ id, onChanged }: { id: number; onChanged: () => void }) {
  const { user, can, authEnabled } = useAuth()
  const [change, setChange] = useState<Change | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [reviewNote, setReviewNote] = useState('')
  const [site, setSite] = useState<'A' | 'B'>('A')
  const [openRun, setOpenRun] = useState<number | null>(null)

  const load = useCallback(() => {
    api.get<Change>(`/changes/${id}`).then(setChange).catch((e: Error) => setError(e.message))
  }, [id])
  useEffect(load, [load])

  const running = change?.runs?.some((r) => r.status === 'running')
  useEffect(() => {
    if (!running) return
    const timer = window.setInterval(load, 1500)
    return () => window.clearInterval(timer)
  }, [running, load])

  const review = (action: 'approve' | 'reject') => {
    setError(null)
    api.post<Change>(`/changes/${id}/${action}`, { note: reviewNote || null })
      .then((c) => { setChange(c); onChanged() })
      .catch((e: Error) => setError(e.message))
  }

  if (!change) return <div className="card">{error ? <div className="error">{error}</div> : <div className="muted">Memuat…</div>}</div>
  const current = change.sites?.find((s) => s.label === site)
  const ownChange = authEnabled && change.created_by === user.username
  const status = STATUS[change.status]

  return (
    <div className="card">
      <div className="toolbar">
        <h2>#{change.id} {change.title}</h2>
        <span className={`pill ${status.pill}`}>{status.label}</span>
        <div className="spacer" />
        {change.service_id && <Link className="button" to={`/configs?mode=l2vpn&service=${change.service_id}`}>Config Generator</Link>}
      </div>
      {error && <div className="error">{error}</div>}
      <div className="change-meta muted">
        Diajukan {change.created_by} ({fmtTime(change.created_at)})
        {change.approved_by && <> · {change.status === 'rejected' ? 'Ditolak' : 'Disetujui'} {change.approved_by} ({fmtTime(change.approved_at)})</>}
        {change.note && <div>Catatan: {change.note}</div>}
        {change.review_note && <div>Catatan review: {change.review_note}</div>}
      </div>

      {change.status === 'pending_approval' && (
        <div className="change-review">
          <h3>Approval</h3>
          {!can('approver') ? <p className="hint">Menunggu approver (role approver/admin) meninjau script di bawah.</p>
            : ownChange ? <p className="hint">Change ini Anda ajukan sendiri; approval harus oleh orang lain.</p>
            : <>
              <textarea value={reviewNote} placeholder="Catatan review (opsional)" maxLength={4000}
                onChange={(e) => setReviewNote(e.target.value)} />
              <div className="actions">
                <button className="primary" onClick={() => review('approve')}>Approve</button>
                <button onClick={() => review('reject')}>Tolak</button>
              </div>
            </>}
        </div>
      )}

      <div className="config-menu" role="tablist">
        {change.sites?.map((s) => (
          <button key={s.label} role="tab" aria-selected={site === s.label} className={site === s.label ? 'active' : ''}
            onClick={() => setSite(s.label)}>Site {s.label}: {s.hostname ?? '-'}</button>
        ))}
      </div>
      {current && <SiteScripts site={current} />}

      {can('engineer') && change.status !== 'rejected' && current && (
        <RunPanel change={change} site={current} onStarted={(run) => { setOpenRun(run.id); load(); onChanged() }} />
      )}

      <h3>Riwayat eksekusi</h3>
      <div className="table-wrap">
        <table>
          <thead><tr><th>#</th><th>Site</th><th>Tahap</th><th>Status</th><th>Oleh</th><th>Mulai</th><th>Keterangan</th></tr></thead>
          <tbody>
            {change.runs?.map((r) => (
              <tr key={r.id} className={`clickable ${openRun === r.id ? 'selected' : ''}`} onClick={() => setOpenRun(r.id)}>
                <td>{r.id}</td>
                <td>{r.site}</td>
                <td>{PHASE_LABEL[r.phase]} ({r.protocol})</td>
                <td><span className={`pill ${r.status === 'ok' ? 'pill-ok' : r.status === 'failed' ? 'pill-bad' : 'pill-warn'}`}>{r.status}</span></td>
                <td>{r.started_by}<div className="muted">login {r.device_user}</div></td>
                <td>{fmtTime(r.started_at)}</td>
                <td>{r.error ?? ''}</td>
              </tr>
            ))}
            {!change.runs?.length && <tr><td colSpan={7} className="empty">Belum ada eksekusi.</td></tr>}
          </tbody>
        </table>
      </div>
      {openRun != null && <RunLog key={openRun} changeId={change.id} runId={openRun} />}
    </div>
  )
}

function SiteScripts({ site }: { site: Site }) {
  return (
    <div className="change-site">
      <div className="muted">{site.dialect_label} · host {site.host ?? '(tidak ada IP manajemen)'}</div>
      {site.warnings.length > 0 && <ul className="warnings">{site.warnings.map((w) => <li key={w}>{w}</li>)}</ul>}
      <div className="change-scripts">
        {PHASES.map((p) => (
          <div key={p.key}>
            <h4>{p.label}</h4>
            <pre>{site[p.script].join('\n') || '(kosong)'}</pre>
          </div>
        ))}
      </div>
    </div>
  )
}

function RunPanel({ change, site, onStarted }: { change: Change; site: Site; onStarted: (run: Run) => void }) {
  const [protocol, setProtocol] = useState<'ssh' | 'telnet'>('ssh')
  const [port, setPort] = useState('')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [useJump, setUseJump] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const busy = change.runs?.some((r) => r.status === 'running')

  const start = (phase: Phase) => {
    if ((phase === 'push' || phase === 'rollback') && !window.confirm(
      `${PHASE_LABEL[phase]} ke ${site.hostname} (${site.host}) site ${site.label}?\n\nPerintah akan dikirim ke perangkat produksi.`)) return
    setError(null)
    api.post<Run>(`/changes/${change.id}/runs`, {
      site: site.label, phase, protocol, port: port ? Number(port) : null, username, password, use_jump: useJump,
    }).then(onStarted).catch((e: Error) => setError(e.message))
  }

  const ready = username.trim() && password && !busy
  return (
    <div className="change-run">
      <h3>Eksekusi site {site.label}</h3>
      <p className="hint">Kredensial perangkat hanya dipakai untuk sesi ini dan tidak disimpan. Urutan: pre-check → push site A →
        push site B → post-check; rollback memakai script rollback di atas.</p>
      {error && <div className="error">{error}</div>}
      <div className="mop-meta">
        <label><span>Protokol</span>
          <select value={protocol} onChange={(e) => setProtocol(e.target.value as 'ssh' | 'telnet')}>
            <option value="ssh">SSH</option><option value="telnet">Telnet</option>
          </select></label>
        <label><span>Port</span>
          <input type="number" min={1} max={65535} value={port} placeholder={protocol === 'ssh' ? '22' : '23'}
            onChange={(e) => setPort(e.target.value)} /></label>
        <label><span>Username perangkat</span>
          <input value={username} autoComplete="off" maxLength={64} onChange={(e) => setUsername(e.target.value)} /></label>
        <label><span>Password perangkat</span>
          <input type="password" value={password} autoComplete="new-password" maxLength={256}
            onChange={(e) => setPassword(e.target.value)} /></label>
        <label className="check">
          <input type="checkbox" checked={useJump} onChange={(e) => setUseJump(e.target.checked)} />
          <span>Lewat jump host (bila diatur di server)</span>
        </label>
      </div>
      <div className="actions">
        {PHASES.map((p) => (
          <button key={p.key} className={p.key === 'push' ? 'primary' : ''} disabled={!ready || !site[p.script].length}
            onClick={() => start(p.key)}>{p.label} site {site.label}</button>
        ))}
      </div>
    </div>
  )
}

function RunLog({ changeId, runId }: { changeId: number; runId: number }) {
  const [run, setRun] = useState<Run | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let timer: number | undefined
    const poll = () => api.get<Run>(`/changes/${changeId}/runs/${runId}`)
      .then((r) => { setRun(r); if (r.status === 'running') timer = window.setTimeout(poll, 1200) })
      .catch((e: Error) => setError(e.message))
    poll()
    return () => window.clearTimeout(timer)
  }, [changeId, runId])

  return (
    <div className="change-log">
      <h3>Transkrip eksekusi #{runId} {run && <span className="muted">— {run.status}</span>}</h3>
      {error && <div className="error">{error}</div>}
      <pre>{run?.log || 'Menunggu output…'}</pre>
    </div>
  )
}
