import { FormEvent, useCallback, useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { api } from '../api'
import { Role, ROLE_LABELS, useAuth } from '../auth'
import { fmtTime } from './Changes'

// Pengaturan Akses: own account for everyone; users, SSO (OIDC) and the audit log for admins.

type Tab = 'account' | 'users' | 'sso' | 'audit'

interface UserRow {
  id: number
  username: string
  full_name: string | null
  email: string | null
  role: Role
  active: boolean
  auth_source: 'local' | 'oidc'
  customer_id: number | null
  customer: string | null
  must_change_password: boolean
  locked_minutes: number
  last_login_at: string | null
}
interface CustomerOption { id: number; name: string; account_no: string }
interface AuditRow { id: number; at: string; username: string; action: string; target: string; detail: string | null; ip: string | null }
interface SsoForm {
  enabled: boolean
  display_name: string
  issuer_url: string | null
  client_id: string | null
  has_client_secret: boolean
  scopes: string
  redirect_uri: string | null
  role_claim: string
  role_mapping: Record<string, Role>
  default_role: Role | null
  auto_create: boolean
  allowed_domains: string | null
}

const ROLES: Role[] = ['viewer', 'engineer', 'approver', 'admin', 'customer']
const STAFF: Role[] = ['viewer', 'engineer', 'approver', 'admin']
const ROLE_HINT: Record<Role, string> = {
  customer: 'login portal, hanya melihat layanan miliknya',
  viewer: 'hanya melihat',
  engineer: 'mengubah data, mengajukan & mengeksekusi change',
  approver: 'engineer + approve change orang lain',
  admin: 'semua, termasuk pengguna, SSO & pengaturan',
}
const TABS: { key: Tab; label: string; admin: boolean }[] = [
  { key: 'account', label: 'Akun saya', admin: false },
  { key: 'users', label: 'Pengguna', admin: true },
  { key: 'sso', label: 'SSO (OIDC)', admin: true },
  { key: 'audit', label: 'Audit log', admin: true },
]
const strip = (message: string) => message.replace(/^\d+ — /, '')

export default function Access() {
  const { can, authEnabled } = useAuth()
  const [params, setParams] = useSearchParams()
  const tabs = TABS.filter((t) => !t.admin || can('admin'))
  const tab = (tabs.some((t) => t.key === params.get('tab')) ? params.get('tab') : tabs[0].key) as Tab
  return (
    <div className="capacity">
      <div className="card">
        <div className="toolbar"><h2>Pengaturan Akses</h2></div>
        {!authEnabled && <div className="notice">Login sedang nonaktif (NADIEN_AUTH_ENABLED=false): semua akses sebagai admin lokal.</div>}
        <div className="config-menu" role="tablist">
          {tabs.map((t) => (
            <button key={t.key} role="tab" aria-selected={tab === t.key} className={tab === t.key ? 'active' : ''}
              onClick={() => setParams({ tab: t.key })}>{t.label}</button>
          ))}
        </div>
        {tab === 'account' && <Account />}
        {tab === 'users' && <UserList />}
        {tab === 'sso' && <SsoSettings />}
        {tab === 'audit' && <AuditLog />}
      </div>
    </div>
  )
}

function Account() {
  const { user, passwordMinLength } = useAuth()
  const [form, setForm] = useState({ current_password: '', new_password: '', repeat: '' })
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null)

  const submit = (e: FormEvent) => {
    e.preventDefault()
    if (form.new_password !== form.repeat) {
      setMessage({ ok: false, text: 'Konfirmasi password tidak sama.' })
      return
    }
    api.post('/auth/password', { current_password: form.current_password, new_password: form.new_password })
      .then(() => { setForm({ current_password: '', new_password: '', repeat: '' }); setMessage({ ok: true, text: 'Password diganti.' }) })
      .catch((err: Error) => setMessage({ ok: false, text: strip(err.message) }))
  }

  return (
    <div className="access-grid">
      <div className="access-card">
        <h3>Profil</h3>
        <dl className="access-dl">
          <dt>Username</dt><dd>{user.username}</dd>
          <dt>Nama</dt><dd>{user.full_name ?? '-'}</dd>
          <dt>Role</dt><dd><span className="pill pill-muted">{ROLE_LABELS[user.role]}</span></dd>
          <dt>Login via</dt><dd>{user.auth_source === 'oidc' ? 'SSO (OIDC)' : 'Akun lokal'}</dd>
        </dl>
      </div>
      {user.auth_source === 'local' && user.id != null ? (
        <form className="access-card" onSubmit={submit}>
          <h3>Ganti password</h3>
          {message && <div className={message.ok ? 'notice' : 'error'}>{message.text}</div>}
          <div className="access-form">
            <label><span>Password saat ini</span><input type="password" value={form.current_password} required
              autoComplete="current-password" onChange={(e) => setForm({ ...form, current_password: e.target.value })} /></label>
            <label><span>Password baru</span><input type="password" value={form.new_password} required minLength={passwordMinLength}
              autoComplete="new-password" onChange={(e) => setForm({ ...form, new_password: e.target.value })} /></label>
            <label><span>Ulangi password baru</span><input type="password" value={form.repeat} required
              autoComplete="new-password" onChange={(e) => setForm({ ...form, repeat: e.target.value })} /></label>
          </div>
          <p className="hint">Minimal {passwordMinLength} karakter, berisi huruf dan angka, tidak memuat username.</p>
          <button type="submit" className="primary">Simpan password</button>
        </form>
      ) : (
        <div className="access-card"><h3>Password</h3><p className="hint">Password akun SSO dikelola di identity provider.</p></div>
      )}
    </div>
  )
}

function UserList() {
  const { user: me, passwordMinLength } = useAuth()
  const [users, setUsers] = useState<UserRow[]>([])
  const [customers, setCustomers] = useState<CustomerOption[]>([])
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const empty = { username: '', full_name: '', email: '', password: '', role: 'engineer' as Role, customer_id: '' }
  const [form, setForm] = useState(empty)

  const load = useCallback(() => {
    api.get<UserRow[]>('/users').then(setUsers).catch((e: Error) => setError(strip(e.message)))
  }, [])
  useEffect(load, [load])
  useEffect(() => {
    api.get<CustomerOption[]>('/customers?limit=10000').then(setCustomers).catch(() => setCustomers([]))
  }, [])

  const patch = (u: UserRow, body: Record<string, unknown>, done?: string) => {
    setError(null)
    setNotice(null)
    api.patch(`/users/${u.id}`, body).then(() => { load(); if (done) setNotice(done) })
      .catch((e: Error) => setError(strip(e.message)))
  }

  const create = (e: FormEvent) => {
    e.preventDefault()
    setError(null)
    api.post('/users', {
      ...form, full_name: form.full_name || null, email: form.email || null,
      customer_id: form.role === 'customer' && form.customer_id ? Number(form.customer_id) : null,
    }).then(() => { setForm(empty); setNotice(`Pengguna ${form.username} dibuat; wajib ganti password saat login pertama.`); load() })
      .catch((err: Error) => setError(strip(err.message)))
  }

  const resetPassword = (u: UserRow) => {
    const password = window.prompt(`Password sementara untuk ${u.username} (min. ${passwordMinLength} karakter, huruf + angka).\n`
      + 'Pengguna wajib menggantinya saat login berikutnya.')
    if (password) patch(u, { password }, `Password ${u.username} direset.`)
  }

  const remove = (u: UserRow) => {
    if (!window.confirm(`Hapus pengguna ${u.username}? Riwayat audit tetap tersimpan.`)) return
    api.del(`/users/${u.id}`).then(load).catch((e: Error) => setError(strip(e.message)))
  }

  const status = (u: UserRow) => !u.active ? <span className="pill pill-muted">Nonaktif</span>
    : u.locked_minutes ? <span className="pill pill-bad">Terkunci {u.locked_minutes} mnt</span>
    : u.must_change_password ? <span className="pill pill-warn">Wajib ganti password</span>
    : <span className="pill pill-ok">Aktif</span>

  return (
    <>
      {error && <div className="error">{error}</div>}
      {notice && <div className="notice">{notice}</div>}
      <div className="table-wrap">
        <table>
          <thead><tr><th>Pengguna</th><th>Role</th><th>Customer</th><th>Status</th><th>Login terakhir</th><th /></tr></thead>
          <tbody>
            {users.map((u) => (
              <tr key={u.id}>
                <td>
                  <b>{u.username}</b> {u.auth_source === 'oidc' && <span className="pill pill-muted">SSO</span>}
                  <div className="muted">{[u.full_name, u.email].filter(Boolean).join(' · ') || '-'}</div>
                </td>
                <td>
                  <select value={u.role} disabled={u.id === me.id}
                    onChange={(e) => patch(u, { role: e.target.value, ...(e.target.value === 'customer' && !u.customer_id && customers[0] ? { customer_id: customers[0].id } : {}) })}>
                    {(u.auth_source === 'oidc' ? STAFF : ROLES).map((r) => <option key={r} value={r}>{ROLE_LABELS[r]}</option>)}
                  </select>
                </td>
                <td>
                  {u.role === 'customer'
                    ? <select value={u.customer_id ?? ''} onChange={(e) => patch(u, { customer_id: Number(e.target.value) })}>
                        {customers.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
                      </select>
                    : <span className="muted">-</span>}
                </td>
                <td>{status(u)}</td>
                <td>{fmtTime(u.last_login_at)}</td>
                <td className="access-actions">
                  <button disabled={u.id === me.id} onClick={() => patch(u, { active: !u.active })}>{u.active ? 'Nonaktifkan' : 'Aktifkan'}</button>
                  {u.locked_minutes > 0 && <button onClick={() => patch(u, { unlock: true }, `${u.username} dibuka kuncinya.`)}>Buka kunci</button>}
                  {u.auth_source === 'local' && <button onClick={() => resetPassword(u)}>Reset password</button>}
                  <button className="danger" disabled={u.id === me.id} onClick={() => remove(u)}>Hapus</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <form className="access-card" onSubmit={create}>
        <h3>Tambah pengguna lokal</h3>
        <div className="access-form access-form-wide">
          <label><span>Username</span><input value={form.username} required pattern="[A-Za-z0-9._@\-]{2,64}"
            onChange={(e) => setForm({ ...form, username: e.target.value })} /></label>
          <label><span>Nama lengkap</span><input value={form.full_name} maxLength={128}
            onChange={(e) => setForm({ ...form, full_name: e.target.value })} /></label>
          <label><span>Email</span><input type="email" value={form.email} maxLength={256}
            onChange={(e) => setForm({ ...form, email: e.target.value })} /></label>
          <label><span>Password sementara</span><input type="password" value={form.password} required minLength={passwordMinLength}
            autoComplete="new-password" onChange={(e) => setForm({ ...form, password: e.target.value })} /></label>
          <label><span>Role</span>
            <select value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value as Role })}>
              {ROLES.map((r) => <option key={r} value={r}>{ROLE_LABELS[r]}</option>)}
            </select></label>
          {form.role === 'customer' && <label><span>Customer</span>
            <select value={form.customer_id} required onChange={(e) => setForm({ ...form, customer_id: e.target.value })}>
              <option value="">— pilih customer —</option>
              {customers.map((c) => <option key={c.id} value={c.id}>{c.name} ({c.account_no})</option>)}
            </select></label>}
        </div>
        <p className="hint">{ROLE_LABELS[form.role]}: {ROLE_HINT[form.role]}. Pengguna wajib mengganti password saat login pertama.</p>
        <button type="submit" className="primary">Tambah pengguna</button>
      </form>
    </>
  )
}

function SsoSettings() {
  const [form, setForm] = useState<SsoForm | null>(null)
  const [secret, setSecret] = useState('')
  const [mapping, setMapping] = useState<{ claim: string; role: Role }[]>([])
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null)
  const suggestedRedirect = `${window.location.origin}/api/auth/sso/callback`

  useEffect(() => {
    api.get<SsoForm>('/sso-settings').then((s) => {
      setForm(s)
      setMapping(Object.entries(s.role_mapping).map(([claim, role]) => ({ claim, role })))
    }).catch((e: Error) => setMessage({ ok: false, text: strip(e.message) }))
  }, [])

  if (!form) return message ? <div className="error">{message.text}</div> : <div className="muted">Memuat…</div>
  const set = (patch: Partial<SsoForm>) => setForm({ ...form, ...patch })

  const save = (e: FormEvent) => {
    e.preventDefault()
    setMessage(null)
    const role_mapping = Object.fromEntries(mapping.filter((m) => m.claim.trim()).map((m) => [m.claim.trim(), m.role]))
    api.put<SsoForm>('/sso-settings', {
      ...form, role_mapping, client_secret: secret ? secret : null,
      issuer_url: form.issuer_url || null, redirect_uri: form.redirect_uri || null, allowed_domains: form.allowed_domains || null,
    }).then((s) => { setForm(s); setSecret(''); setMessage({ ok: true, text: 'Pengaturan SSO disimpan.' }) })
      .catch((err: Error) => setMessage({ ok: false, text: strip(err.message) }))
  }

  const test = () => {
    setMessage(null)
    api.post<{ ok: boolean; message: string; issuer?: string }>('/sso-settings/test', { issuer_url: form.issuer_url })
      .then((r) => setMessage({ ok: r.ok, text: r.ok ? `Discovery OK — issuer ${r.issuer}` : r.message }))
      .catch((err: Error) => setMessage({ ok: false, text: strip(err.message) }))
  }

  return (
    <form onSubmit={save}>
      <p className="hint">
        Single sign-on OpenID Connect (Entra ID / Azure AD, Keycloak, Google Workspace, Okta). Daftarkan NADIEN sebagai
        aplikasi web di identity provider dengan redirect URI di bawah, lalu isi issuer, client ID, dan secret.
        Role diambil dari satu claim (mis. <code>groups</code> atau <code>roles</code>) dan diperbarui setiap login.
        Admin lokal tetap bisa login dengan password sebagai jalur darurat.
      </p>
      {message && <div className={message.ok ? 'notice' : 'error'}>{message.text}</div>}
      <div className="access-grid">
        <div className="access-card">
          <h3>Provider</h3>
          <label className="check"><input type="checkbox" checked={form.enabled} onChange={(e) => set({ enabled: e.target.checked })} />
            <span>Aktifkan tombol "Masuk dengan {form.display_name || 'SSO'}" di halaman login</span></label>
          <div className="access-form">
            <label><span>Nama tombol</span><input value={form.display_name} maxLength={64} onChange={(e) => set({ display_name: e.target.value })} /></label>
            <label><span>Issuer URL</span><input value={form.issuer_url ?? ''} placeholder="https://login.microsoftonline.com/<tenant>/v2.0"
              onChange={(e) => set({ issuer_url: e.target.value })} /></label>
            <label><span>Client ID</span><input value={form.client_id ?? ''} maxLength={256} onChange={(e) => set({ client_id: e.target.value })} /></label>
            <label><span>Client secret</span><input type="password" value={secret} autoComplete="new-password"
              placeholder={form.has_client_secret ? 'tersimpan (isi untuk mengganti)' : 'kosong untuk public client + PKCE'}
              onChange={(e) => setSecret(e.target.value)} /></label>
            <label><span>Scopes</span><input value={form.scopes} maxLength={256} onChange={(e) => set({ scopes: e.target.value })} /></label>
            <label><span>Redirect URI</span><input value={form.redirect_uri ?? ''} placeholder={suggestedRedirect}
              onChange={(e) => set({ redirect_uri: e.target.value })} /></label>
          </div>
          <div className="actions">
            <button type="button" onClick={() => set({ redirect_uri: suggestedRedirect })}>Pakai URL ini</button>
            <button type="button" disabled={!form.issuer_url} onClick={test}>Uji discovery</button>
          </div>
        </div>
        <div className="access-card">
          <h3>Pemetaan role</h3>
          <div className="access-form">
            <label><span>Claim role</span><input value={form.role_claim} maxLength={64} onChange={(e) => set({ role_claim: e.target.value })} /></label>
          </div>
          <table className="access-mapping">
            <thead><tr><th>Nilai claim (grup / app role)</th><th>Role NADIEN</th><th /></tr></thead>
            <tbody>
              {mapping.map((row, i) => (
                <tr key={i}>
                  <td><input value={row.claim} placeholder="mis. NOC-Engineer"
                    onChange={(e) => setMapping(mapping.map((r, j) => (j === i ? { ...r, claim: e.target.value } : r)))} /></td>
                  <td><select value={row.role} onChange={(e) => setMapping(mapping.map((r, j) => (j === i ? { ...r, role: e.target.value as Role } : r)))}>
                    {STAFF.map((r) => <option key={r} value={r}>{ROLE_LABELS[r]}</option>)}
                  </select></td>
                  <td><button type="button" onClick={() => setMapping(mapping.filter((_, j) => j !== i))}>Hapus</button></td>
                </tr>
              ))}
            </tbody>
          </table>
          <button type="button" onClick={() => setMapping([...mapping, { claim: '', role: 'viewer' }])}>+ Tambah pemetaan</button>
          <div className="access-form">
            <label><span>Bila tidak ada yang cocok</span>
              <select value={form.default_role ?? ''} onChange={(e) => set({ default_role: (e.target.value || null) as Role | null })}>
                <option value="">Tolak login</option>
                {STAFF.map((r) => <option key={r} value={r}>Beri role {ROLE_LABELS[r]}</option>)}
              </select></label>
            <label><span>Domain email yang diizinkan</span><input value={form.allowed_domains ?? ''} placeholder="perusahaan.co.id, anak.co.id"
              onChange={(e) => set({ allowed_domains: e.target.value })} /></label>
          </div>
          <label className="check"><input type="checkbox" checked={form.auto_create} onChange={(e) => set({ auto_create: e.target.checked })} />
            <span>Buat akun otomatis saat login SSO pertama</span></label>
        </div>
      </div>
      <button type="submit" className="primary">Simpan pengaturan SSO</button>
    </form>
  )
}

const ACTIONS = [
  { value: '', label: 'Semua aksi' }, { value: 'auth.', label: 'Login & logout' }, { value: 'user.', label: 'Pengguna' },
  { value: 'change.', label: 'Change & eksekusi' }, { value: 'sso.', label: 'SSO' }, { value: 'POST', label: 'POST (buat/aksi)' },
  { value: 'PUT', label: 'PUT' }, { value: 'PATCH', label: 'PATCH (ubah)' }, { value: 'DELETE', label: 'DELETE (hapus)' },
]

function AuditLog() {
  const [rows, setRows] = useState<AuditRow[]>([])
  const [filters, setFilters] = useState({ q: '', username: '', action: '', date_from: '', date_to: '' })
  const [error, setError] = useState<string | null>(null)
  const query = useMemo(() => {
    const q = new URLSearchParams()
    for (const [k, v] of Object.entries(filters)) if (v.trim()) q.set(k, v.trim())
    return q.toString()
  }, [filters])

  useEffect(() => {
    const timer = window.setTimeout(() => {
      api.get<AuditRow[]>(`/audit-logs?limit=500${query ? `&${query}` : ''}`).then(setRows)
        .catch((e: Error) => setError(strip(e.message)))
    }, 300)
    return () => window.clearTimeout(timer)
  }, [query])

  return (
    <>
      {error && <div className="error">{error}</div>}
      <div className="mop-meta access-filters">
        <label><span>Cari</span><input value={filters.q} placeholder="target, detail, …" maxLength={100}
          onChange={(e) => setFilters({ ...filters, q: e.target.value })} /></label>
        <label><span>User</span><input value={filters.username} maxLength={64}
          onChange={(e) => setFilters({ ...filters, username: e.target.value })} /></label>
        <label><span>Aksi</span><select value={filters.action} onChange={(e) => setFilters({ ...filters, action: e.target.value })}>
          {ACTIONS.map((a) => <option key={a.value} value={a.value}>{a.label}</option>)}
        </select></label>
        <label><span>Dari</span><input type="date" value={filters.date_from} onChange={(e) => setFilters({ ...filters, date_from: e.target.value })} /></label>
        <label><span>Sampai</span><input type="date" value={filters.date_to} onChange={(e) => setFilters({ ...filters, date_to: e.target.value })} /></label>
        <a className="button" href={`/api/audit-logs/export${query ? `?${query}` : ''}`}>Ekspor CSV</a>
      </div>
      <div className="table-wrap">
        <table>
          <thead><tr><th>Waktu</th><th>User</th><th>Aksi</th><th>Target</th><th>Detail</th><th>IP</th></tr></thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id}>
                <td>{fmtTime(r.at)}</td><td>{r.username}</td><td><code>{r.action}</code></td>
                <td>{r.target}</td><td>{r.detail ?? ''}</td><td className="muted">{r.ip ?? ''}</td>
              </tr>
            ))}
            {!rows.length && <tr><td colSpan={6} className="empty">Tidak ada catatan untuk filter ini.</td></tr>}
          </tbody>
        </table>
      </div>
    </>
  )
}
