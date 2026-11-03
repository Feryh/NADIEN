import { FormEvent, useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import { Role, ROLE_LABELS, useAuth } from '../auth'

// Admin: NADIEN users and roles, plus the audit log of every write and device run.

interface UserRow {
  id: number
  username: string
  full_name: string | null
  role: Role
  active: boolean
  last_login_at: string | null
}

interface AuditRow { id: number; at: string; username: string; action: string; target: string; detail: string | null }

const ROLES: Role[] = ['viewer', 'engineer', 'approver', 'admin']
const ROLE_HINT: Record<Role, string> = {
  viewer: 'hanya melihat', engineer: 'mengubah data, mengajukan & mengeksekusi change',
  approver: 'engineer + approve change orang lain', admin: 'semua, termasuk pengguna & pengaturan',
}
import { fmtTime } from './Changes'

export default function Users() {
  const { can, authEnabled } = useAuth()
  const [tab, setTab] = useState<'users' | 'audit'>('users')
  if (!can('admin')) return <div className="card"><div className="error">Halaman ini khusus admin.</div></div>
  return (
    <div className="capacity">
      <div className="card">
        <div className="toolbar"><h2>Pengguna &amp; Audit</h2></div>
        {!authEnabled && <div className="notice">Login sedang nonaktif (NADIEN_AUTH_ENABLED=false): semua akses sebagai admin lokal.</div>}
        <div className="config-menu" role="tablist">
          <button role="tab" aria-selected={tab === 'users'} className={tab === 'users' ? 'active' : ''} onClick={() => setTab('users')}>Pengguna</button>
          <button role="tab" aria-selected={tab === 'audit'} className={tab === 'audit' ? 'active' : ''} onClick={() => setTab('audit')}>Audit log</button>
        </div>
        {tab === 'users' ? <UserList /> : <AuditLog />}
      </div>
    </div>
  )
}

function UserList() {
  const [users, setUsers] = useState<UserRow[]>([])
  const [error, setError] = useState<string | null>(null)
  const [form, setForm] = useState({ username: '', full_name: '', password: '', role: 'engineer' as Role })

  const load = useCallback(() => {
    api.get<UserRow[]>('/users').then(setUsers).catch((e: Error) => setError(e.message))
  }, [])
  useEffect(load, [load])

  const patch = (u: UserRow, body: Partial<UserRow> & { password?: string }) => {
    setError(null)
    api.patch(`/users/${u.id}`, body).then(load).catch((e: Error) => setError(e.message))
  }

  const create = (e: FormEvent) => {
    e.preventDefault()
    setError(null)
    api.post('/users', { ...form, full_name: form.full_name || null })
      .then(() => { setForm({ username: '', full_name: '', password: '', role: 'engineer' }); load() })
      .catch((err: Error) => setError(err.message))
  }

  const resetPassword = (u: UserRow) => {
    const password = window.prompt(`Password baru untuk ${u.username} (min. 8 karakter):`)
    if (password) patch(u, { password })
  }

  return (
    <>
      {error && <div className="error">{error}</div>}
      <div className="table-wrap">
        <table>
          <thead><tr><th>Username</th><th>Nama</th><th>Role</th><th>Aktif</th><th>Login terakhir</th><th /></tr></thead>
          <tbody>
            {users.map((u) => (
              <tr key={u.id}>
                <td><b>{u.username}</b></td>
                <td>{u.full_name ?? '-'}</td>
                <td>
                  <select value={u.role} onChange={(e) => patch(u, { role: e.target.value as Role })}>
                    {ROLES.map((r) => <option key={r} value={r}>{ROLE_LABELS[r]}</option>)}
                  </select>
                </td>
                <td><input type="checkbox" checked={u.active} aria-label={`Aktif ${u.username}`}
                  onChange={(e) => patch(u, { active: e.target.checked })} /></td>
                <td>{fmtTime(u.last_login_at)}</td>
                <td><button onClick={() => resetPassword(u)}>Reset password</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <form className="service-parameters" onSubmit={create}>
        <h3>Tambah pengguna</h3>
        <div className="mop-meta">
          <label><span>Username</span><input value={form.username} required pattern="[A-Za-z0-9._-]{2,64}"
            onChange={(e) => setForm({ ...form, username: e.target.value })} /></label>
          <label><span>Nama lengkap</span><input value={form.full_name} maxLength={128}
            onChange={(e) => setForm({ ...form, full_name: e.target.value })} /></label>
          <label><span>Password awal</span><input type="password" value={form.password} required minLength={8}
            autoComplete="new-password" onChange={(e) => setForm({ ...form, password: e.target.value })} /></label>
          <label><span>Role</span>
            <select value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value as Role })}>
              {ROLES.map((r) => <option key={r} value={r}>{ROLE_LABELS[r]}</option>)}
            </select></label>
        </div>
        <p className="hint">{ROLE_LABELS[form.role]}: {ROLE_HINT[form.role]}.</p>
        <button type="submit" className="primary">Tambah</button>
      </form>
    </>
  )
}

function AuditLog() {
  const [rows, setRows] = useState<AuditRow[]>([])
  const [q, setQ] = useState('')
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const timer = window.setTimeout(() => {
      api.get<AuditRow[]>(`/audit-logs?limit=300${q.trim() ? `&q=${encodeURIComponent(q.trim())}` : ''}`)
        .then(setRows).catch((e: Error) => setError(e.message))
    }, 300)
    return () => window.clearTimeout(timer)
  }, [q])

  return (
    <>
      {error && <div className="error">{error}</div>}
      <input className="audit-search" value={q} placeholder="Cari user, aksi, atau target…" maxLength={100}
        onChange={(e) => setQ(e.target.value)} />
      <div className="table-wrap">
        <table>
          <thead><tr><th>Waktu</th><th>User</th><th>Aksi</th><th>Target</th><th>Detail</th></tr></thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id}>
                <td>{fmtTime(r.at)}</td><td>{r.username}</td><td><code>{r.action}</code></td>
                <td>{r.target}</td><td>{r.detail ?? ''}</td>
              </tr>
            ))}
            {!rows.length && <tr><td colSpan={5} className="empty">Belum ada catatan.</td></tr>}
          </tbody>
        </table>
      </div>
    </>
  )
}
