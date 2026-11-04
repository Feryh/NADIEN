import { useEffect, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { api } from '../api'
import { StatusBadge } from '../components/ValidationReport'
import { fmtG } from '../components/ui'

// Phase 6 — MoP Generator: Method of Procedure per service, previewed here and exported as PDF.

interface ServiceRow {
  service_id: number
  circuit_id: string
  service_type: string
  bandwidth_mbps: number
  service_status: string
  status: 'pass' | 'warn' | 'fail'
}

interface MopStep {
  device: string
  dialect: string | null
  dialect_label: string
  title: string
  commands: string[]
  note: string
}

interface MopSection {
  title: string
  paragraphs: string[]
  tables: { caption?: string; headers: string[]; rows: string[][] }[]
  steps: MopStep[]
}

interface Mop {
  title: string
  subtitle: string
  draft: boolean
  validation_status: 'pass' | 'warn' | 'fail'
  sections: MopSection[]
}

const META_FIELDS = [
  { key: 'number', label: 'Nomor MoP', placeholder: 'MOP-2026-001' },
  { key: 'change_ticket', label: 'Nomor change', placeholder: 'CHG-1234' },
  { key: 'window_start', label: 'Window mulai', placeholder: '2026-11-10 00:00' },
  { key: 'window_end', label: 'Window selesai', placeholder: '2026-11-10 04:00' },
  { key: 'executor', label: 'Pelaksana', placeholder: '' },
  { key: 'reviewer', label: 'Reviewer', placeholder: '' },
  { key: 'approver', label: 'Approver', placeholder: '' },
  { key: 'a_ip', label: 'IP PE site A (CIDR)', placeholder: '10.10.10.1/30' },
  { key: 'b_ip', label: 'IP PE site B (CIDR)', placeholder: '10.10.10.5/30' },
  { key: 'vlans', label: 'VLAN VPLS (multi)', placeholder: 'mis. 1480-1485,1489' },
  { key: 'vc_id', label: 'VC / PW id', placeholder: 'default = VLAN' },
  { key: 'bgp_asn', label: 'ASN BGP (L3VPN)', placeholder: 'default dari RD' },
] as const

type Meta = Record<(typeof META_FIELDS)[number]['key'], string>
const EMPTY_META = Object.fromEntries(META_FIELDS.map((f) => [f.key, ''])) as Meta

export default function MopGenerator() {
  const [params, setParams] = useSearchParams()
  const selected = params.get('service') ? Number(params.get('service')) : null
  const [services, setServices] = useState<ServiceRow[] | null>(null)
  const [meta, setMeta] = useState<Meta>(EMPTY_META)
  const [policer, setPolicer] = useState(false)
  const [mop, setMop] = useState<Mop | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api.get<{ services: ServiceRow[] }>('/validation/services')
      .then((d) => setServices(d.services))
      .catch((e: Error) => setError(e.message))
  }, [])

  const query = useMemo(() => {
    const q = new URLSearchParams()
    for (const [k, v] of Object.entries(meta)) if (v.trim()) q.set(k, v.trim())
    if (policer) q.set('policer', 'true')
    const s = q.toString()
    return s ? `?${s}` : ''
  }, [meta, policer])

  useEffect(() => {
    setMop(null)
    setError(null)
    if (selected == null) return
    // Debounced so typing in the header fields does not rebuild the preview on every key.
    const timer = window.setTimeout(() => {
      api.get<Mop>(`/mop/services/${selected}${query}`).then(setMop).catch((e: Error) => setError(e.message))
    }, 300)
    return () => window.clearTimeout(timer)
  }, [selected, query])

  return (
    <div className="capacity">
      <div className="card">
        <div className="toolbar">
          <h2>MoP Generator</h2>
        </div>
        <p className="hint">
          Pilih layanan untuk membuat Method of Procedure: ringkasan, jalur, hasil{' '}
          <Link to="/validation">validasi</Link>, risiko, pre-check, eksekusi, verifikasi, dan rollback per perangkat dalam
          sintaks vendor masing-masing (Huawei VRP, ZTE, Cisco IOS-XR, FiberHome). MoP dari layanan yang validasinya masih
          gagal diberi tanda DRAFT.
        </p>
        {error && <div className="error">{error}</div>}
        <div className="table-wrap mop-services">
          <table>
            <thead>
              <tr><th>Validasi</th><th>Circuit</th><th>Layanan</th><th className="num">Bandwidth</th><th>Status</th></tr>
            </thead>
            <tbody>
              {services?.map((s) => (
                <tr key={s.service_id} className={`clickable ${selected === s.service_id ? 'selected' : ''}`}
                    onClick={() => setParams({ service: String(s.service_id) })}>
                  <td><StatusBadge status={s.status} /></td>
                  <td><b>{s.circuit_id}</b></td>
                  <td>{s.service_type.toUpperCase()}</td>
                  <td className="num">{fmtG(s.bandwidth_mbps / 1000)}</td>
                  <td><span className="pill pill-muted">{s.service_status}</span></td>
                </tr>
              ))}
              {services && !services.length && (
                <tr><td colSpan={5} className="empty">Belum ada layanan. Buat dari <Link to="/paths">Customer Path</Link> atau Inventory → Service.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      {selected != null && (
        <div className="card">
          <div className="toolbar">
            <h2>{mop ? mop.title : `Service #${selected}`}</h2>
            <div className="spacer" />
            <a className="button primary" href={`/api/mop/services/${selected}/pdf${query}`}>Download PDF</a>
            <button onClick={() => setParams({})}>Tutup</button>
          </div>
          <div className="mop-meta">
            {META_FIELDS.map((f) => (
              <label key={f.key}>
                <span>{f.label}</span>
                <input value={meta[f.key]} placeholder={f.placeholder} maxLength={f.key === 'vlans' ? 256 : f.key.startsWith('window') ? 32 : 64}
                       onChange={(e) => setMeta({ ...meta, [f.key]: e.target.value })} />
              </label>
            ))}
            <label className="check">
              <input type="checkbox" checked={policer} onChange={(e) => setPolicer(e.target.checked)} />
              <span>Policer sesuai bandwidth</span>
            </label>
          </div>
          {mop ? <MopPreview mop={mop} /> : <div className="muted">Menyusun MoP…</div>}
        </div>
      )}
    </div>
  )
}

function MopPreview({ mop }: { mop: Mop }) {
  return (
    <div className="mop-doc">
      <div className="mop-subtitle">{mop.subtitle}</div>
      {mop.draft && <div className="error">DRAFT — validasi desain masih gagal. Jangan dieksekusi sebelum temuan diselesaikan.</div>}
      {mop.sections.map((s) => (
        <section key={s.title}>
          <h3>{s.title}</h3>
          {s.paragraphs.map((p) => <p key={p}>{p}</p>)}
          {s.tables.map((t, i) => (
            <div key={i} className="table-wrap">
              {t.caption && <div className="mop-caption">{t.caption}</div>}
              <table>
                <thead><tr>{t.headers.map((h, j) => <th key={j}>{h}</th>)}</tr></thead>
                <tbody>{t.rows.map((r, j) => <tr key={j}>{r.map((c, k) => <td key={k}>{c}</td>)}</tr>)}</tbody>
              </table>
            </div>
          ))}
          {s.steps.map((step, i) => (
            <div key={i} className="mop-step">
              <div className="mop-step-title">
                {step.title} <span className={`pill ${step.dialect ? 'pill-muted' : 'pill-bad'}`}>{step.dialect_label}</span>
              </div>
              {step.commands.length > 0 && <pre>{step.commands.join('\n')}</pre>}
              {step.note && <div className="small muted">{step.note}</div>}
            </div>
          ))}
        </section>
      ))}
      <div className="validation-summary"><StatusBadge status={mop.validation_status} /> status validasi</div>
    </div>
  )
}
