import { useEffect, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { api } from '../api'
import { StatusBadge } from '../components/ValidationReport'
import { fmtG } from '../components/ui'

// Phase 7 — Config Generator: service configuration and rollback per endpoint, in each device's own dialect.

interface ServiceRow {
  service_id: number
  circuit_id: string
  service_type: string
  bandwidth_mbps: number
  service_status: string
  status: 'pass' | 'warn' | 'fail'
}

interface Endpoint {
  label: string
  device: string | null
  dialect: string | null
  dialect_label: string
  config: string[]
  rollback: string[]
  commit: string[]
  warnings: string[]
  notes: string[]
}

interface ConfigResult {
  circuit_id: string
  service_type: string
  endpoints: Record<string, Endpoint>
}

const INPUTS = [
  { key: 'a_ip', label: 'IP PE site A (CIDR)', placeholder: '10.10.10.1/30' },
  { key: 'b_ip', label: 'IP PE site B (CIDR)', placeholder: '10.10.10.5/30' },
  { key: 'vlans', label: 'VLAN VPLS (multi)', placeholder: 'mis. 1480-1485,1489' },
  { key: 'vc_id', label: 'VC / PW id', placeholder: 'default = VLAN' },
  { key: 'bgp_asn', label: 'ASN BGP (L3VPN)', placeholder: 'default dari RD' },
] as const

type Inputs = Record<(typeof INPUTS)[number]['key'], string>

export default function ConfigGenerator() {
  const [params, setParams] = useSearchParams()
  const selected = params.get('service') ? Number(params.get('service')) : null
  const [services, setServices] = useState<ServiceRow[] | null>(null)
  const [inputs, setInputs] = useState<Inputs>({ a_ip: '', b_ip: '', vlans: '', vc_id: '', bgp_asn: '' })
  const [policer, setPolicer] = useState(false)
  const [result, setResult] = useState<ConfigResult | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api.get<{ services: ServiceRow[] }>('/validation/services')
      .then((d) => setServices(d.services))
      .catch((e: Error) => setError(e.message))
  }, [])

  const query = useMemo(() => {
    const q = new URLSearchParams()
    for (const [k, v] of Object.entries(inputs)) if (v.trim()) q.set(k, v.trim())
    if (policer) q.set('policer', 'true')
    return q.toString()
  }, [inputs, policer])

  useEffect(() => {
    setResult(null)
    setError(null)
    if (selected == null) return
    // Debounced so a half-typed address does not fire a request (and a 422) on every key.
    const timer = window.setTimeout(() => {
      api.get<ConfigResult>(`/configs/services/${selected}?${query}`).then(setResult).catch((e: Error) => setError(e.message))
    }, 400)
    return () => window.clearTimeout(timer)
  }, [selected, query])

  return (
    <div className="capacity">
      <div className="card">
        <div className="toolbar">
          <h2>Config Generator</h2>
        </div>
        <p className="hint">
          Konfigurasi layanan dan rollback untuk kedua ujung, dalam sintaks perangkat masing-masing (Huawei VRP, ZTE,
          Cisco IOS-XR, FiberHome): sub-interface dot1q (policer opsional), pseudowire VC id = VLAN untuk L2VPN/IPLC, VRF +
          BGP untuk L3VPN, alamat publik untuk Internet. Hasil yang sama dipakai di bagian eksekusi &amp; rollback{' '}
          <Link to="/mop">MoP</Link>.
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
            <h2>{result ? `${result.circuit_id} — ${result.service_type.toUpperCase()}` : `Service #${selected}`}</h2>
            <div className="spacer" />
            <Link className="button" to={`/mop?service=${selected}`}>Buat MoP</Link>
            <button onClick={() => setParams({})}>Tutup</button>
          </div>
          <div className="mop-meta">
            {INPUTS.map((f) => (
              <label key={f.key}>
                <span>{f.label}</span>
                <input value={inputs[f.key]} placeholder={f.placeholder} maxLength={f.key === 'vlans' ? 256 : f.key === 'vc_id' || f.key === 'bgp_asn' ? 10 : 18}
                       onChange={(e) => setInputs({ ...inputs, [f.key]: e.target.value })} />
              </label>
            ))}
            <label className="check">
              <input type="checkbox" checked={policer} onChange={(e) => setPolicer(e.target.checked)} />
              <span>Policer sesuai bandwidth</span>
            </label>
          </div>
          {result ? (
            Object.values(result.endpoints).map((ep) => (
              <EndpointConfig key={ep.label} ep={ep} textUrl={(part) =>
                `/api/configs/services/${selected}/text?side=${ep.label}&part=${part}&${query}`} />
            ))
          ) : !error && <div className="muted">Menyusun konfigurasi…</div>}
        </div>
      )}
    </div>
  )
}

function EndpointConfig({ ep, textUrl }: { ep: Endpoint; textUrl: (part: 'config' | 'rollback') => string }) {
  return (
    <div className="config-endpoint">
      <h3>
        Site {ep.label}: {ep.device ?? '—'}{' '}
        <span className={`pill ${ep.dialect ? 'pill-muted' : 'pill-bad'}`}>{ep.dialect_label}</span>
      </h3>
      {ep.notes.map((n) => <div key={n} className="small">ℹ {n}</div>)}
      {ep.warnings.map((w) => <div key={w} className="small muted">⚠ {w}</div>)}
      {ep.config.length > 0 && (
        <div className="grid-2">
          <ConfigBlock title="Konfigurasi" lines={[...ep.config, ...ep.commit]} href={textUrl('config')} />
          <ConfigBlock title="Rollback" lines={[...ep.rollback, ...ep.commit]} href={textUrl('rollback')} />
        </div>
      )}
    </div>
  )
}

function ConfigBlock({ title, lines, href }: { title: string; lines: string[]; href: string }) {
  const [copied, setCopied] = useState(false)
  const copy = () => {
    navigator.clipboard?.writeText(lines.join('\n')).then(() => {
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    })
  }
  return (
    <div className="mop-step">
      <div className="toolbar">
        <span className="mop-step-title">{title}</span>
        <div className="spacer" />
        <button onClick={copy}>{copied ? 'Tersalin' : 'Salin'}</button>
        <a className="button" href={href}>.txt</a>
      </div>
      <pre>{lines.join('\n')}</pre>
    </div>
  )
}
