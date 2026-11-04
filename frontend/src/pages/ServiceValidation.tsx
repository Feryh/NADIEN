import { useCallback, useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { api } from '../api'
import ValidationReport, { StatusBadge, ValidationResult } from '../components/ValidationReport'
import { fmtG } from '../components/ui'

// Phase 5 — Service Validation: every service that is not terminated, checked before activation.

interface ServiceRow {
  service_id: number
  circuit_id: string
  service_type: string
  bandwidth_mbps: number
  protection: string
  service_status: string
  vlan_id: number | null
  vrf_name: string | null
  status: 'pass' | 'warn' | 'fail'
  counts: Record<string, number>
  failed: string[]
}

type Detail = ValidationResult & ServiceRow

export default function ServiceValidation() {
  const [params, setParams] = useSearchParams()
  const selected = params.get('service') ? Number(params.get('service')) : null
  const [filter, setFilter] = useState<'' | 'fail' | 'warn' | 'pass'>('')
  const [data, setData] = useState<{ counts: Record<string, number>; services: ServiceRow[] } | null>(null)
  const [detail, setDetail] = useState<Detail | null>(null)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(() => {
    setError(null)
    api.get<{ counts: Record<string, number>; services: ServiceRow[] }>(`/validation/services${filter ? `?status=${filter}` : ''}`)
      .then(setData)
      .catch((e: Error) => setError(e.message))
  }, [filter])

  useEffect(load, [load])

  useEffect(() => {
    setDetail(null)
    if (selected == null) return
    api.get<Detail>(`/validation/services/${selected}`).then(setDetail).catch((e: Error) => setError(e.message))
  }, [selected])

  return (
    <div className="capacity">
      <div className="card">
        <div className="toolbar">
          <h2>Service Validation</h2>
          <div className="spacer" />
          <select value={filter} onChange={(e) => setFilter(e.target.value as typeof filter)}>
            <option value="">Semua hasil</option>
            <option value="fail">Gagal</option>
            <option value="warn">Peringatan</option>
            <option value="pass">Lulus</option>
          </select>
          <button onClick={load}>Validasi ulang</button>
        </div>
        <p className="hint">
          Setiap layanan yang belum terminated dicek sebelum diaktifkan: kapasitas &amp; redundansi (warning ≥ 70%, critical ≥ 90%,
          backup tanpa link/fiber/transit yang sama), MTU &amp; port akses, VLAN/VRF/RD/RT, dan kesiapan perangkat (status, loopback,
          IS-IS, software). Desain baru bisa divalidasi sebelum reserve di <Link to="/paths">Customer Path</Link>.
        </p>
        {error && <div className="error">{error}</div>}
        {data && (
          <div className="hot-counts">
            <StatusBadge status="fail">{data.counts.fail} gagal</StatusBadge>
            <StatusBadge status="warn">{data.counts.warn} peringatan</StatusBadge>
            <StatusBadge status="pass">{data.counts.pass} lulus</StatusBadge>
          </div>
        )}
        <div className="table-wrap">
          <table>
            <thead>
              <tr><th>Hasil</th><th>Circuit</th><th>Layanan</th><th className="num">Bandwidth</th><th>Status</th><th>Temuan utama</th></tr>
            </thead>
            <tbody>
              {data?.services.map((s) => (
                <tr key={s.service_id} className={`clickable ${selected === s.service_id ? 'selected' : ''}`}
                    onClick={() => setParams({ service: String(s.service_id) })}>
                  <td><StatusBadge status={s.status} /></td>
                  <td><b>{s.circuit_id}</b></td>
                  <td>{s.service_type.toUpperCase()} · {s.protection}{s.vlan_id ? ` · VLAN ${s.vlan_id}` : ''}{s.vrf_name ? ` · ${s.vrf_name}` : ''}</td>
                  <td className="num">{fmtG(s.bandwidth_mbps / 1000)}</td>
                  <td><span className="pill pill-muted">{s.service_status}</span></td>
                  <td className="small">{s.failed.join(' · ') || (s.counts.warn ? `${s.counts.warn} peringatan` : '—')}</td>
                </tr>
              ))}
              {data && !data.services.length && (
                <tr><td colSpan={6} className="empty">Belum ada layanan. Buat dari <Link to="/paths">Customer Path</Link> atau Inventory → Service.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      {selected != null && (
        <div className="card">
          <div className="toolbar">
            <h2>{detail ? `${detail.circuit_id} — ${detail.service_type.toUpperCase()} ${fmtG(detail.bandwidth_mbps / 1000)}` : `Service #${selected}`}</h2>
            <div className="spacer" />
            <button onClick={() => setParams({})}>Tutup</button>
          </div>
          {detail ? <ValidationReport report={detail} /> : <div className="muted">Memvalidasi…</div>}
        </div>
      )}
    </div>
  )
}
