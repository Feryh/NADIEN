import { useState } from 'react'
import { NavLink, Navigate, Route, Routes } from 'react-router-dom'
import { AuthGate, Role, ROLE_LABELS, useAuth } from './auth'
import Access from './pages/Access'
import AiAssistant from './pages/AiAssistant'
import Capacity from './pages/Capacity'
import Changes from './pages/Changes'
import CollectorSettings from './pages/CollectorSettings'
import Collectors from './pages/Collectors'
import ConfigGenerator from './pages/ConfigGenerator'
import Dashboard from './pages/Dashboard'
import DeviceDetail from './pages/DeviceDetail'
import DigitalTwin from './pages/DigitalTwin'
import Inventory from './pages/Inventory'
import MopGenerator from './pages/MopGenerator'
import Paths from './pages/Paths'
import Portal from './pages/Portal'
import Ports from './pages/Ports'
import ServiceValidation from './pages/ServiceValidation'
import Topology from './pages/Topology'

/** mark: two letters shown for the item when the sidebar is collapsed. */
type NavItem = { to: string; label: string; mark: string; tag: string; role?: Role }

const NAV: NavItem[] = [
  { to: '/', label: 'Dashboard', mark: 'DB', tag: 'P8' },
  { to: '/inventory', label: 'Inventory', mark: 'IN', tag: 'P1' },
  { to: '/ports', label: 'Kapasitas Port', mark: 'KP', tag: 'P1' },
  { to: '/topology', label: 'Topology', mark: 'TP', tag: 'P2' },
  { to: '/capacity', label: 'Capacity Planning', mark: 'CP', tag: 'P3' },
  { to: '/paths', label: 'Customer Path', mark: 'PA', tag: 'P4' },
  { to: '/validation', label: 'Service Validation', mark: 'SV', tag: 'P5' },
  { to: '/mop', label: 'MoP Generator', mark: 'MG', tag: 'P6' },
  { to: '/configs', label: 'Config Generator', mark: 'CG', tag: 'P7' },
  { to: '/changes', label: 'Change & Eksekusi', mark: 'CE', tag: 'P7' },
  { to: '/ai', label: 'AI Assistant', mark: 'AI', tag: 'P9' },
  { to: '/twin', label: 'Digital Twin', mark: 'DT', tag: 'P10' },
  { to: '/collectors', label: 'Collectors', mark: 'CO', tag: 'P1' },
  { to: '/settings', label: 'Pengaturan Collector', mark: 'PC', tag: 'P1', role: 'admin' },
  { to: '/access', label: 'Pengaturan Akses', mark: 'AK', tag: 'Akses' },
]
const CUSTOMER_NAV: NavItem[] = [
  { to: '/', label: 'Layanan Saya', mark: 'LS', tag: 'Portal' },
  { to: '/access', label: 'Akun Saya', mark: 'AK', tag: 'Akses' },
]

export default function App() {
  return (
    <AuthGate>
      <Shell />
    </AuthGate>
  )
}

const COLLAPSED_KEY = 'nadien.sidebar.collapsed'

function storedCollapsed(): boolean {
  try {
    return localStorage.getItem(COLLAPSED_KEY) === '1'
  } catch {
    return false
  }
}

function Shell() {
  const { user, authEnabled, can, logout } = useAuth()
  const customer = user.role === 'customer'
  const nav = customer ? CUSTOMER_NAV : NAV.filter((n) => !n.role || can(n.role))
  const [collapsed, setCollapsed] = useState(storedCollapsed)

  function toggle() {
    setCollapsed(!collapsed)
    try {
      localStorage.setItem(COLLAPSED_KEY, collapsed ? '0' : '1')
    } catch {
      /* storage blocked: the sidebar just opens expanded next time */
    }
  }

  return (
    <div className={`app ${collapsed ? 'sidebar-collapsed' : ''}`}>
      <aside className="sidebar">
        <div className="brand">
          <div className="logo">N</div>
          <div className="brand-text">
            <div className="brand-name">NADIEN</div>
            <div className="brand-sub">Network Access Design, Implementation &amp; Engineering</div>
          </div>
          <button type="button" className="sidebar-toggle" onClick={toggle} aria-expanded={!collapsed}
            title={collapsed ? 'Buka menu' : 'Ciutkan menu'} aria-label={collapsed ? 'Buka menu' : 'Ciutkan menu'}>
            {collapsed ? '»' : '«'}
          </button>
        </div>
        <nav>
          {nav.map((n) => (
            <NavLink key={n.to} to={n.to} end={n.to === '/'} title={collapsed ? n.label : undefined}>
              <span className="nav-mark" aria-hidden="true">{n.mark}</span>
              <span className="nav-label">{n.label}</span>
              <span className="phase">{n.tag}</span>
            </NavLink>
          ))}
        </nav>
        <div className="user-box">
          <div className="user-avatar">{(user.full_name || user.username).slice(0, 1).toUpperCase()}</div>
          <div className="user-meta">
            <b>{user.full_name || user.username}</b>
            <div className="muted">{user.username} · {ROLE_LABELS[user.role]}</div>
          </div>
          {authEnabled && <button onClick={logout} title="Keluar">Keluar</button>}
        </div>
        {!customer && <a className="api-link" href="/docs" target="_blank" rel="noreferrer">API docs ↗</a>}
      </aside>
      <main className="content">
        {customer ? (
          <Routes>
            <Route path="/" element={<Portal />} />
            <Route path="/access" element={<Access />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        ) : (
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/inventory" element={<Inventory />} />
            <Route path="/inventory/:entityKey" element={<Inventory />} />
            <Route path="/topology" element={<Topology />} />
            <Route path="/ports" element={<Ports />} />
            <Route path="/devices/:deviceId" element={<DeviceDetail />} />
            <Route path="/paths" element={<Paths />} />
            <Route path="/validation" element={<ServiceValidation />} />
            <Route path="/mop" element={<MopGenerator />} />
            <Route path="/configs" element={<ConfigGenerator />} />
            <Route path="/changes" element={<Changes />} />
            <Route path="/ai" element={<AiAssistant />} />
            <Route path="/twin" element={<DigitalTwin />} />
            <Route path="/capacity" element={<Capacity />} />
            <Route path="/collectors" element={<Collectors />} />
            <Route path="/settings" element={<CollectorSettings />} />
            <Route path="/access" element={<Access />} />
            <Route path="/users" element={<Navigate to="/access?tab=users" replace />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        )}
      </main>
    </div>
  )
}
