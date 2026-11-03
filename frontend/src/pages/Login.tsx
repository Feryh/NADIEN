import { FormEvent, KeyboardEvent, ReactNode, useState } from 'react'
import { api } from '../api'
import './Login.css'

// Split-screen sign-in: brand panel with a live-looking backbone on the left, the form on the right.

const NODES: [number, number][] = [
  [90, 120], [250, 70], [430, 140], [530, 300], [420, 470], [240, 520], [80, 380], [270, 290], [380, 300], [160, 240],
]
const LINKS: [number, number][] = [
  [0, 1], [1, 2], [2, 3], [3, 4], [4, 5], [5, 6], [6, 0], [7, 8], [1, 7], [7, 5], [8, 2], [8, 4], [9, 0], [9, 7], [9, 6],
]

function Backbone() {
  return (
    <svg className="login-net" viewBox="0 0 600 600" aria-hidden="true">
      <defs>
        <radialGradient id="node-glow">
          <stop offset="0%" stopColor="#38bdf8" stopOpacity="0.9" />
          <stop offset="100%" stopColor="#38bdf8" stopOpacity="0" />
        </radialGradient>
      </defs>
      {LINKS.map(([a, b], i) => {
        const [x1, y1] = NODES[a]
        const [x2, y2] = NODES[b]
        return (
          <g key={i}>
            <line x1={x1} y1={y1} x2={x2} y2={y2} className="login-link" />
            <line x1={x1} y1={y1} x2={x2} y2={y2} className="login-flow" style={{ animationDelay: `${(i * 0.37) % 3}s` }} />
          </g>
        )
      })}
      {NODES.map(([x, y], i) => (
        <g key={i}>
          <circle cx={x} cy={y} r="22" fill="url(#node-glow)" className="login-pulse" style={{ animationDelay: `${(i * 0.53) % 4}s` }} />
          <circle cx={x} cy={y} r={i === 7 || i === 8 ? 7 : 5} className="login-node" />
        </g>
      ))}
    </svg>
  )
}

export function AuthLayout({ children }: { children: ReactNode }) {
  return (
    <div className="login-shell">
      <section className="login-brand">
        <Backbone />
        <div className="login-brand-content">
          <div className="login-logo"><span>N</span></div>
          <h1>NADIEN</h1>
          <p className="login-tagline">Network Access Design, Implementation &amp; Engineering Platform</p>
          <ul className="login-points">
            <li><b>Inventory &amp; topologi</b> dari Observium, LLDP, dan RANCID</li>
            <li><b>Capacity planning</b>, forecast, dan digital twin</li>
            <li><b>Change terkontrol</b>: MoP, config, approval, eksekusi, audit</li>
          </ul>
        </div>
        <div className="login-brand-foot">Akses tercatat di audit log · penggunaan tanpa izin dilarang</div>
      </section>
      <section className="login-panel">
        <div className="login-form-wrap">{children}</div>
      </section>
    </div>
  )
}

function PasswordInput({ value, onChange, autoComplete, label, id }: {
  value: string; onChange: (v: string) => void; autoComplete: string; label: string; id: string
}) {
  const [shown, setShown] = useState(false)
  const [caps, setCaps] = useState(false)
  const capsCheck = (e: KeyboardEvent<HTMLInputElement>) => setCaps(e.getModifierState?.('CapsLock') ?? false)
  return (
    <div className="login-field">
      <label htmlFor={id}>{label}</label>
      <div className="login-input-row">
        <input id={id} type={shown ? 'text' : 'password'} value={value} autoComplete={autoComplete} required
          maxLength={256} onChange={(e) => onChange(e.target.value)} onKeyUp={capsCheck} onKeyDown={capsCheck} />
        <button type="button" className="login-eye" onClick={() => setShown(!shown)}
          aria-label={shown ? 'Sembunyikan password' : 'Tampilkan password'}>{shown ? 'Sembunyikan' : 'Tampilkan'}</button>
      </div>
      {caps && <div className="login-caps">Caps Lock aktif</div>}
    </div>
  )
}

export function LoginPage({ sso, onDone }: { sso: { enabled: boolean; display_name: string }; onDone: () => void }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(() => new URLSearchParams(window.location.search).get('sso_error'))

  const submit = (e: FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError(null)
    api.post('/auth/login', { username, password })
      .then(() => {
        window.history.replaceState(null, '', window.location.pathname)
        onDone()
      })
      .catch((err: Error) => setError(err.message.replace(/^\d+ — /, '')))
      .finally(() => setBusy(false))
  }

  return (
    <AuthLayout>
      <form className="login-form" onSubmit={submit}>
        <h2>Selamat datang</h2>
        <p className="login-sub">Masuk dengan akun NADIEN Anda.</p>
        {error && <div className="login-alert" role="alert">{error}</div>}
        <div className="login-field">
          <label htmlFor="login-username">Username</label>
          <input id="login-username" value={username} autoComplete="username" autoFocus required maxLength={64}
            onChange={(e) => setUsername(e.target.value)} />
        </div>
        <PasswordInput id="login-password" label="Password" value={password} onChange={setPassword}
          autoComplete="current-password" />
        <button type="submit" className="login-submit" disabled={busy}>
          {busy ? <span className="login-spinner" aria-label="Memproses" /> : 'Masuk'}
        </button>
        {sso.enabled && <>
          <div className="login-divider"><span>atau</span></div>
          <a className="login-sso" href="/api/auth/sso/login">Masuk dengan {sso.display_name}</a>
        </>}
        <p className="login-help">Lupa password? Hubungi administrator NADIEN untuk reset.</p>
      </form>
    </AuthLayout>
  )
}

export function ChangePasswordPage({ username, minLength, onDone, onLogout }: {
  username: string; minLength: number; onDone: () => void; onLogout: () => void
}) {
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [repeat, setRepeat] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const checks = [
    { ok: next.length >= minLength, label: `Minimal ${minLength} karakter` },
    { ok: /[A-Za-z]/.test(next) && /\d/.test(next), label: 'Berisi huruf dan angka' },
    { ok: !!next && !next.toLowerCase().includes(username.toLowerCase()), label: 'Tidak memuat username' },
    { ok: !!next && next === repeat, label: 'Konfirmasi sama' },
  ]

  const submit = (e: FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError(null)
    api.post('/auth/password', { current_password: current, new_password: next })
      .then(onDone)
      .catch((err: Error) => setError(err.message.replace(/^\d+ — /, '')))
      .finally(() => setBusy(false))
  }

  return (
    <AuthLayout>
      <form className="login-form" onSubmit={submit}>
        <h2>Ganti password</h2>
        <p className="login-sub">Akun <b>{username}</b> wajib mengganti password sebelum melanjutkan.</p>
        {error && <div className="login-alert" role="alert">{error}</div>}
        <PasswordInput id="pw-current" label="Password saat ini" value={current} onChange={setCurrent} autoComplete="current-password" />
        <PasswordInput id="pw-new" label="Password baru" value={next} onChange={setNext} autoComplete="new-password" />
        <PasswordInput id="pw-repeat" label="Ulangi password baru" value={repeat} onChange={setRepeat} autoComplete="new-password" />
        <ul className="login-checks">
          {checks.map((c) => <li key={c.label} className={c.ok ? 'ok' : ''}>{c.label}</li>)}
        </ul>
        <button type="submit" className="login-submit" disabled={busy || !checks.every((c) => c.ok)}>
          {busy ? <span className="login-spinner" aria-label="Memproses" /> : 'Simpan password'}
        </button>
        <button type="button" className="login-link-button" onClick={onLogout}>Keluar</button>
      </form>
    </AuthLayout>
  )
}
