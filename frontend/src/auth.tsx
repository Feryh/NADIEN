import { createContext, FormEvent, ReactNode, useCallback, useContext, useEffect, useState } from 'react'
import { api, UNAUTHORIZED_EVENT } from './api'

export type Role = 'viewer' | 'engineer' | 'approver' | 'admin'
export interface CurrentUser { id: number | null; username: string; role: Role; full_name: string | null }
interface AuthStatus { auth_enabled: boolean; setup_needed: boolean; user: CurrentUser | null }

const RANK: Record<Role, number> = { viewer: 0, engineer: 1, approver: 2, admin: 3 }
export const ROLE_LABELS: Record<Role, string> = {
  viewer: 'Viewer', engineer: 'Engineer', approver: 'Approver', admin: 'Admin',
}

interface AuthValue {
  user: CurrentUser
  authEnabled: boolean
  can: (role: Role) => boolean
  logout: () => void
}

const LOCAL: CurrentUser = { id: null, username: 'local', role: 'admin', full_name: 'Auth nonaktif' }
const AuthContext = createContext<AuthValue>({ user: LOCAL, authEnabled: false, can: () => true, logout: () => {} })

export const useAuth = () => useContext(AuthContext)

/** Shows setup or login until there is a session, then the app with the user in context. */
export function AuthGate({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AuthStatus | null>(null)
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(() => {
    api.get<AuthStatus>('/auth/status').then(setStatus).catch((e: Error) => setError(e.message))
  }, [])

  useEffect(refresh, [refresh])
  useEffect(() => {
    window.addEventListener(UNAUTHORIZED_EVENT, refresh)
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, refresh)
  }, [refresh])

  if (error) return <div className="login-page"><div className="error">{error}</div></div>
  if (!status) return <div className="login-page muted">Memuat…</div>
  if (status.auth_enabled && !status.user) {
    return <LoginForm setup={status.setup_needed} onDone={refresh} />
  }
  const user = status.user ?? LOCAL
  const value: AuthValue = {
    user,
    authEnabled: status.auth_enabled,
    can: (role) => RANK[user.role] >= RANK[role],
    logout: () => { api.post('/auth/logout').finally(refresh) },
  }
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

function LoginForm({ setup, onDone }: { setup: boolean; onDone: () => void }) {
  const [username, setUsername] = useState('')
  const [fullName, setFullName] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const submit = (e: FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError(null)
    const body = setup ? { username, full_name: fullName || null, password } : { username, password }
    api.post(setup ? '/auth/setup' : '/auth/login', body)
      .then(onDone)
      .catch((err: Error) => setError(err.message))
      .finally(() => setBusy(false))
  }

  return (
    <div className="login-page">
      <form className="card login-card" onSubmit={submit}>
        <div className="brand">
          <div className="logo">N</div>
          <div>
            <div className="brand-name">NADIEN</div>
            <div className="brand-sub">{setup ? 'Buat akun admin pertama' : 'Login'}</div>
          </div>
        </div>
        {setup && <p className="hint">Belum ada pengguna. Akun ini menjadi admin dan bisa menambah pengguna lain.</p>}
        {error && <div className="error">{error}</div>}
        <label><span>Username</span>
          <input value={username} autoComplete="username" required maxLength={64}
            onChange={(e) => setUsername(e.target.value)} /></label>
        {setup && <label><span>Nama lengkap</span>
          <input value={fullName} maxLength={128} onChange={(e) => setFullName(e.target.value)} /></label>}
        <label><span>Password{setup ? ' (min. 8 karakter)' : ''}</span>
          <input type="password" value={password} required minLength={setup ? 8 : 1} maxLength={256}
            autoComplete={setup ? 'new-password' : 'current-password'} onChange={(e) => setPassword(e.target.value)} /></label>
        <button type="submit" className="primary" disabled={busy}>{setup ? 'Buat admin' : 'Login'}</button>
      </form>
    </div>
  )
}
