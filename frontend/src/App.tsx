import { NavLink, Navigate, Route, Routes } from 'react-router-dom'
import { AuthGate, Role, ROLE_LABELS, useAuth } from './auth'
import AiAssistant from './pages/AiAssistant'
import Capacity from './pages/Capacity'
import Changes from './pages/Changes'
import CollectorSettings from './pages/CollectorSettings'
import Collectors from './pages/Collectors'
import ConfigGenerator from './pages/ConfigGenerator'
import Dashboard from './pages/Dashboard'
import DigitalTwin from './pages/DigitalTwin'
import Inventory from './pages/Inventory'
import MopGenerator from './pages/MopGenerator'
import Paths from './pages/Paths'
import Ports from './pages/Ports'
import ServiceValidation from './pages/ServiceValidation'
import Topology from './pages/Topology'
import Users from './pages/Users'

const NAV: { to: string; label: string; tag: string; role?: Role }[] = [
  { to: '/', label: 'Dashboard', tag: 'P8' },
  { to: '/inventory', label: 'Inventory', tag: 'P1' },
  { to: '/ports', label: 'Kapasitas Port', tag: 'P1' },
  { to: '/topology', label: 'Topology', tag: 'P2' },
  { to: '/capacity', label: 'Capacity Planning', tag: 'P3' },
  { to: '/paths', label: 'Customer Path', tag: 'P4' },
  { to: '/validation', label: 'Service Validation', tag: 'P5' },
  { to: '/mop', label: 'MoP Generator', tag: 'P6' },
  { to: '/configs', label: 'Config Generator', tag: 'P7' },
  { to: '/changes', label: 'Change & Eksekusi', tag: 'P7' },
  { to: '/ai', label: 'AI Assistant', tag: 'P9' },
  { to: '/twin', label: 'Digital Twin', tag: 'P10' },
  { to: '/collectors', label: 'Collectors', tag: 'P1' },
  { to: '/settings', label: 'Pengaturan Collector', tag: 'P1', role: 'admin' },
  { to: '/users', label: 'Pengguna & Audit', tag: 'Admin', role: 'admin' },
]

export default function App() {
  return (
    <AuthGate>
      <Shell />
    </AuthGate>
  )
}

function Shell() {
  const { user, authEnabled, can, logout } = useAuth()
  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <div className="logo">N</div>
          <div>
            <div className="brand-name">NADIEN</div>
            <div className="brand-sub">Network Access Design, Implementation &amp; Engineering</div>
          </div>
        </div>
        <nav>
          {NAV.filter((n) => !n.role || can(n.role)).map((n) => (
            <NavLink key={n.to} to={n.to} end={n.to === '/'}>
              <span>{n.label}</span>
              <span className="phase">{n.tag}</span>
            </NavLink>
          ))}
        </nav>
        <div className="user-box">
          <div>
            <b>{user.full_name || user.username}</b>
            <div className="muted">{user.username} · {ROLE_LABELS[user.role]}</div>
          </div>
          {authEnabled && <button onClick={logout}>Logout</button>}
        </div>
        <a className="api-link" href="/docs" target="_blank" rel="noreferrer">
          API docs ↗
        </a>
      </aside>
      <main className="content">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/inventory" element={<Inventory />} />
          <Route path="/inventory/:entityKey" element={<Inventory />} />
          <Route path="/topology" element={<Topology />} />
          <Route path="/ports" element={<Ports />} />
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
          <Route path="/users" element={<Users />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </div>
  )
}
