import { createContext, ReactNode, useCallback, useContext, useEffect, useState } from 'react'
import { api, UNAUTHORIZED_EVENT } from './api'
import { ChangePasswordPage, LoginPage } from './pages/Login'

export type Role = 'customer' | 'viewer' | 'engineer' | 'approver' | 'admin'
export interface CurrentUser {
  id: number | null
  username: string
  role: Role
  full_name: string | null
  customer_id: number | null
  must_change_password: boolean
  auth_source: 'local' | 'oidc'
}
interface AuthStatus {
  auth_enabled: boolean
  user: CurrentUser | null
  sso: { enabled: boolean; display_name: string }
  password_min_length: number
}

const RANK: Record<Role, number> = { customer: -1, viewer: 0, engineer: 1, approver: 2, admin: 3 }
export const ROLE_LABELS: Record<Role, string> = {
  customer: 'Customer', viewer: 'Viewer', engineer: 'Engineer', approver: 'Approver', admin: 'Admin',
}

interface AuthValue {
  user: CurrentUser
  authEnabled: boolean
  passwordMinLength: number
  /** Staff role check; a customer account never passes it. */
  can: (role: Role) => boolean
  logout: () => void
  refresh: () => void
}

const LOCAL: CurrentUser = {
  id: null, username: 'local', role: 'admin', full_name: 'Auth nonaktif', customer_id: null,
  must_change_password: false, auth_source: 'local',
}
const AuthContext = createContext<AuthValue>({
  user: LOCAL, authEnabled: false, passwordMinLength: 10, can: () => true, logout: () => {}, refresh: () => {},
})

export const useAuth = () => useContext(AuthContext)

/** Login until there is a session, then a forced password change if pending, then the app. */
export function AuthGate({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AuthStatus | null>(null)
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(() => {
    api.get<AuthStatus>('/auth/status').then(setStatus).catch((e: Error) => setError(e.message))
  }, [])
  const logout = useCallback(() => { api.post('/auth/logout').finally(refresh) }, [refresh])

  useEffect(refresh, [refresh])
  useEffect(() => {
    window.addEventListener(UNAUTHORIZED_EVENT, refresh)
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, refresh)
  }, [refresh])

  if (error) return <div className="login-page"><div className="error">{error}</div></div>
  if (!status) return <div className="login-page muted">Memuat…</div>
  if (status.auth_enabled && !status.user) return <LoginPage sso={status.sso} onDone={refresh} />
  if (status.user?.must_change_password) {
    return <ChangePasswordPage username={status.user.username} minLength={status.password_min_length}
      onDone={refresh} onLogout={logout} />
  }
  const user = status.user ?? LOCAL
  const value: AuthValue = {
    user,
    authEnabled: status.auth_enabled,
    passwordMinLength: status.password_min_length,
    can: (role) => user.role !== 'customer' && RANK[user.role] >= RANK[role],
    logout,
    refresh,
  }
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}
