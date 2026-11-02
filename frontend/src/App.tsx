import { NavLink, Navigate, Route, Routes } from 'react-router-dom'
import Capacity from './pages/Capacity'
import Dashboard from './pages/Dashboard'
import Inventory from './pages/Inventory'
import Topology from './pages/Topology'

const NAV = [
  { to: '/', label: 'Dashboard', phase: 8 },
  { to: '/inventory', label: 'Inventory', phase: 1 },
  { to: '/topology', label: 'Topology', phase: 2 },
  { to: '/capacity', label: 'Capacity Planning', phase: 3 },
]

// Shown in the sidebar so the roadmap stays visible; enabled as each sprint lands.
const ROADMAP = [
  { label: 'Customer Path', phase: 4 },
  { label: 'Service Validation', phase: 5 },
  { label: 'MoP Generator', phase: 6 },
  { label: 'Config Generator', phase: 7 },
  { label: 'AI Assistant', phase: 9 },
  { label: 'Digital Twin', phase: 10 },
]

export default function App() {
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
          {NAV.map((n) => (
            <NavLink key={n.to} to={n.to} end={n.to === '/'}>
              <span>{n.label}</span>
              <span className="phase">P{n.phase}</span>
            </NavLink>
          ))}
          <div className="nav-section">Roadmap</div>
          {ROADMAP.map((r) => (
            <span key={r.label} className="nav-disabled" title="Belum diimplementasi">
              <span>{r.label}</span>
              <span className="phase">P{r.phase}</span>
            </span>
          ))}
        </nav>
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
          <Route path="/capacity" element={<Capacity />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </div>
  )
}
