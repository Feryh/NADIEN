import { NavLink, Navigate, Route, Routes } from 'react-router-dom'
import AiAssistant from './pages/AiAssistant'
import Capacity from './pages/Capacity'
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

const NAV = [
  { to: '/', label: 'Dashboard', phase: 8 },
  { to: '/inventory', label: 'Inventory', phase: 1 },
  { to: '/ports', label: 'Kapasitas Port', phase: 1 },
  { to: '/topology', label: 'Topology', phase: 2 },
  { to: '/capacity', label: 'Capacity Planning', phase: 3 },
  { to: '/paths', label: 'Customer Path', phase: 4 },
  { to: '/validation', label: 'Service Validation', phase: 5 },
  { to: '/mop', label: 'MoP Generator', phase: 6 },
  { to: '/configs', label: 'Config Generator', phase: 7 },
  { to: '/ai', label: 'AI Assistant', phase: 9 },
  { to: '/twin', label: 'Digital Twin', phase: 10 },
  { to: '/collectors', label: 'Collectors', phase: 1 },
  { to: '/settings', label: 'Pengaturan Collector', phase: 1 },
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
          <Route path="/ports" element={<Ports />} />
          <Route path="/paths" element={<Paths />} />
          <Route path="/validation" element={<ServiceValidation />} />
          <Route path="/mop" element={<MopGenerator />} />
          <Route path="/configs" element={<ConfigGenerator />} />
          <Route path="/ai" element={<AiAssistant />} />
          <Route path="/twin" element={<DigitalTwin />} />
          <Route path="/capacity" element={<Capacity />} />
          <Route path="/collectors" element={<Collectors />} />
          <Route path="/settings" element={<CollectorSettings />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </div>
  )
}
