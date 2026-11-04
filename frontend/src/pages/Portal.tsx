import { useEffect, useState } from 'react'
import { api } from '../api'
import { fmtG } from '../components/ui'

// Customer accounts: their own services, read-only.

interface PortalService {
  id: number
  circuit_id: string
  service_type: string
  bandwidth_mbps: number
  status: string
  protection: string
  vlan_id: number | null
  a_pop: string | null
  b_pop: string | null
}
interface PortalData { customer: { name: string; account_no: string }; services: PortalService[] }

const STATUS_PILL: Record<string, string> = { active: 'pill-ok', planned: 'pill-warn', reserved: 'pill-warn', terminated: 'pill-muted' }

export default function Portal() {
  const [data, setData] = useState<PortalData | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api.get<PortalData>('/portal/services').then(setData).catch((e: Error) => setError(e.message.replace(/^\d+ — /, '')))
  }, [])

  if (error) return <div className="card"><div className="error">{error}</div></div>
  if (!data) return <div className="card muted">Memuat…</div>
  const active = data.services.filter((s) => s.status === 'active')
  return (
    <div className="capacity">
      <div className="card">
        <div className="toolbar"><h2>Layanan {data.customer.name}</h2><span className="muted">No. akun {data.customer.account_no}</span></div>
        <div className="portal-stats">
          <div><b>{data.services.length}</b><span>Total layanan</span></div>
          <div><b>{active.length}</b><span>Aktif</span></div>
          <div><b>{fmtG(active.reduce((sum, s) => sum + s.bandwidth_mbps, 0) / 1000)}</b><span>Bandwidth aktif</span></div>
        </div>
        <div className="table-wrap">
          <table>
            <thead><tr><th>Circuit</th><th>Layanan</th><th className="num">Bandwidth</th><th>Rute</th><th>Proteksi</th><th>Status</th></tr></thead>
            <tbody>
              {data.services.map((s) => (
                <tr key={s.id}>
                  <td><b>{s.circuit_id}</b></td>
                  <td>{s.service_type.toUpperCase()}{s.vlan_id ? <span className="muted"> · VLAN {s.vlan_id}</span> : null}</td>
                  <td className="num">{fmtG(s.bandwidth_mbps / 1000)}</td>
                  <td>{s.a_pop ?? '-'} ↔ {s.b_pop ?? '-'}</td>
                  <td>{s.protection === 'none' ? 'Tanpa proteksi' : s.protection}</td>
                  <td><span className={`pill ${STATUS_PILL[s.status] ?? 'pill-muted'}`}>{s.status}</span></td>
                </tr>
              ))}
              {!data.services.length && <tr><td colSpan={6} className="empty">Belum ada layanan terdaftar.</td></tr>}
            </tbody>
          </table>
        </div>
        <p className="hint">Untuk perubahan atau gangguan layanan, hubungi account manager atau NOC kami.</p>
      </div>
    </div>
  )
}
