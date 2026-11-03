import { FormEvent, useEffect, useMemo, useState } from 'react'
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

type Mode = 'l2vpn' | 'l3vpn' | 'igp' | 'bgp' | 'parameters'
interface DeviceOption { id: number; hostname: string; role: string }
interface ChangeResult {
  operation: string
  device: string
  dialect_label: string
  config: string[]
  rollback: string[]
  commit: string[]
  warnings: string[]
}
interface ChangeHints {
  device: string
  dialect: string
  interfaces: string[]
  ospf_process: string
  local_asn: string | null
  isis_net: string | null
}

const MODES: { key: Mode; label: string }[] = [
  { key: 'l2vpn', label: 'L2VPN' }, { key: 'l3vpn', label: 'L3VPN' },
  { key: 'igp', label: 'Migrasi IGP (OSPF → IS-IS)' }, { key: 'bgp', label: 'BGP Configuration' },
  { key: 'parameters', label: 'Parameter Changes' },
]

const INPUTS = [
  { key: 'a_ip', label: 'IP PE site A (CIDR)', placeholder: '10.10.10.1/30' },
  { key: 'b_ip', label: 'IP PE site B (CIDR)', placeholder: '10.10.10.5/30' },
  { key: 'vc_id', label: 'VC / PW id', placeholder: 'default = VLAN' },
  { key: 'bgp_asn', label: 'ASN BGP (L3VPN)', placeholder: 'default dari RD' },
] as const

type Inputs = Record<(typeof INPUTS)[number]['key'], string> & { vlan: string; vlans: string }
type VlanMode = 'standard' | 'multiple'

export default function ConfigGenerator() {
  const [params, setParams] = useSearchParams()
  const mode = (MODES.some((m) => m.key === params.get('mode')) ? params.get('mode') : 'l2vpn') as Mode
  const selected = params.get('service') ? Number(params.get('service')) : null
  const [services, setServices] = useState<ServiceRow[] | null>(null)
  const [devices, setDevices] = useState<DeviceOption[]>([])
  const [inputs, setInputs] = useState<Inputs>({ a_ip: '', b_ip: '', vlan: '', vlans: '', vc_id: '', bgp_asn: '' })
  const [vlanMode, setVlanMode] = useState<VlanMode>('standard')
  const [policer, setPolicer] = useState(false)
  const [result, setResult] = useState<ConfigResult | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api.get<{ services: ServiceRow[] }>('/validation/services')
      .then((d) => setServices(d.services))
      .catch((e: Error) => setError(e.message))
  }, [])

  useEffect(() => {
    api.get<DeviceOption[]>('/devices?limit=10000').then(setDevices).catch((e: Error) => setError(e.message))
  }, [])

  const query = useMemo(() => {
    const q = new URLSearchParams()
    for (const [k, v] of Object.entries(inputs)) {
      if (!v.trim() || k === 'vlan' || (mode === 'l2vpn' && k === 'vlans' && vlanMode !== 'multiple')) continue
      if (mode === 'l2vpn' && (k === 'bgp_asn' || (k === 'vc_id' && vlanMode === 'multiple'))) continue
      if (mode === 'l3vpn' && (k === 'vlans' || k === 'vc_id')) continue
      q.set(k, v.trim())
    }
    // Standard is a one-VLAN batch: the backend keeps the VC id override only when exactly one VLAN is sent.
    if (mode === 'l2vpn' && vlanMode === 'standard' && inputs.vlan.trim()) q.set('vlans', inputs.vlan.trim())
    if (policer) q.set('policer', 'true')
    return q.toString()
  }, [inputs, mode, vlanMode, policer])

  const modeServices = services?.filter((s) => mode === 'l2vpn'
    ? ['l2vpn', 'iplc'].includes(s.service_type.toLowerCase())
    : s.service_type.toLowerCase() === 'l3vpn')

  useEffect(() => {
    setResult(null)
    setError(null)
    if (selected == null) return
    if (mode === 'l2vpn' && vlanMode === 'multiple' && !inputs.vlans.trim()) {
      setError('Masukkan daftar atau rentang VLAN untuk mode Multi VLAN.')
      return
    }
    // Debounced so a half-typed address does not fire a request (and a 422) on every key.
    const timer = window.setTimeout(() => {
      api.get<ConfigResult>(`/configs/services/${selected}?${query}`).then(setResult).catch((e: Error) => setError(e.message))
    }, 400)
    return () => window.clearTimeout(timer)
  }, [selected, query, mode, vlanMode, inputs.vlans])

  return (
    <div className="capacity">
      <div className="card">
        <div className="toolbar">
          <h2>Config Generator</h2>
        </div>
        <div className="config-menu" role="tablist" aria-label="Jenis konfigurasi">
          {MODES.map((item) => <button key={item.key} type="button" role="tab" aria-selected={mode === item.key}
            className={mode === item.key ? 'active' : ''} onClick={() => setParams({ mode: item.key })}>{item.label}</button>)}
        </div>
        <p className="hint">
          {mode === 'l2vpn' || mode === 'l3vpn'
            ? <>Config layanan dibuat per ujung sesuai vendor (Huawei VRP, ZTE, Cisco IOS-XR, FiberHome) dan dapat diteruskan ke <Link to="/mop">MoP</Link>.</>
            : <>Pilih perangkat untuk membaca vendor dan konfigurasi RANCID sebagai referensi, lalu tinjau config dan rollback sebelum eksekusi.</>}
        </p>
        {error && <div className="error">{error}</div>}
        {(mode === 'l2vpn' || mode === 'l3vpn') && <div className="service-parameters">
          <h3>Parameter {mode.toUpperCase()}</h3>
          <div className="mop-meta">
            {mode === 'l2vpn' && <label>
              <span>Mode VLAN</span>
              <select value={vlanMode} onChange={(e) => setVlanMode(e.target.value as VlanMode)}>
                <option value="standard">Standard (1 VLAN)</option>
                <option value="multiple">Multi VLAN</option>
              </select>
            </label>}
            {mode === 'l2vpn' && vlanMode === 'standard' && <label>
              <span>VLAN</span>
              <input type="number" min={1} max={4094} value={inputs.vlan} placeholder="default = VLAN layanan"
                onChange={(e) => setInputs({ ...inputs, vlan: e.target.value })} />
            </label>}
            {mode === 'l2vpn' && vlanMode === 'multiple' && <label className="span-2">
              <span>Daftar VLAN / rentang VLAN (maks. 64)</span>
              <input value={inputs.vlans} placeholder="Contoh: 1480-1485,1489" maxLength={256}
                onChange={(e) => setInputs({ ...inputs, vlans: e.target.value })} />
              <small className="muted">Pisahkan VLAN dengan koma; gunakan tanda rentang untuk beberapa VLAN berurutan.</small>
            </label>}
            {INPUTS.filter((f) => mode === 'l2vpn'
              ? ['a_ip', 'b_ip', ...(vlanMode === 'standard' ? ['vc_id'] : [])].includes(f.key)
              : ['a_ip', 'b_ip', 'bgp_asn'].includes(f.key)).map((f) => (
                <label key={f.key}>
                  <span>{f.label}</span>
                  <input value={inputs[f.key]} placeholder={f.placeholder}
                    maxLength={f.key === 'vc_id' || f.key === 'bgp_asn' ? 10 : 18}
                    onChange={(e) => setInputs({ ...inputs, [f.key]: e.target.value })} />
                </label>
              ))}
            <label className="check">
              <input type="checkbox" checked={policer} onChange={(e) => setPolicer(e.target.checked)} />
              <span>Policer sesuai bandwidth layanan</span>
            </label>
          </div>
          <p className="hint">Parameter di atas diterapkan ke konfigurasi layanan yang dipilih di bawah. Untuk L2VPN, Standard menambah 1 VLAN (kosong = VLAN layanan, VC id bisa diisi); Multi VLAN menambah daftar/rentang VLAN sekaligus dengan VC id = VLAN masing-masing.</p>
        </div>}
        {mode === 'igp' || mode === 'bgp' || mode === 'parameters'
          ? <NetworkChangeForm key={mode} mode={mode} devices={devices} />
          : <>
        <div className="table-wrap mop-services">
          <table>
            <thead>
              <tr><th>Validasi</th><th>Circuit</th><th>Layanan</th><th className="num">Bandwidth</th><th>Status</th></tr>
            </thead>
            <tbody>
              {modeServices?.map((s) => (
                <tr key={s.service_id} className={`clickable ${selected === s.service_id ? 'selected' : ''}`}
                    onClick={() => setParams({ mode, service: String(s.service_id) })}>
                  <td><StatusBadge status={s.status} /></td>
                  <td><b>{s.circuit_id}</b></td>
                  <td>{s.service_type.toUpperCase()}</td>
                  <td className="num">{fmtG(s.bandwidth_mbps / 1000)}</td>
                  <td><span className="pill pill-muted">{s.service_status}</span></td>
                </tr>
              ))}
              {services && !modeServices?.length && (
                <tr><td colSpan={5} className="empty">Belum ada layanan {mode.toUpperCase()}. Buat dari <Link to="/paths">Customer Path</Link> atau Inventory → Service.</td></tr>
              )}
            </tbody>
          </table>
        </div>
        </>
        }
      </div>

      {(mode === 'l2vpn' || mode === 'l3vpn') && selected != null && (
        <div className="card">
          <div className="toolbar">
            <h2>{result ? `${result.circuit_id} — ${result.service_type.toUpperCase()}` : `Service #${selected}`}</h2>
            <div className="spacer" />
            <Link className="button" to={`/mop?service=${selected}`}>Buat MoP</Link>
            <button onClick={() => setParams({ mode })}>Tutup</button>
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

function NetworkChangeForm({ mode, devices }: { mode: Exclude<Mode, 'l2vpn' | 'l3vpn'>; devices: DeviceOption[] }) {
  const [values, setValues] = useState<Record<string, string>>({ device_id: '', interfaces: '' })
  const [result, setResult] = useState<ChangeResult | null>(null)
  const [hints, setHints] = useState<ChangeHints | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const fields = mode === 'igp'
    ? [
      { key: 'ospf_process', label: 'OSPF process', placeholder: '1' },
      { key: 'isis_process', label: 'IS-IS process/tag', placeholder: '1' },
      { key: 'net', label: 'IS-IS NET', placeholder: '49.0001.0100.0000.0001.00' },
      { key: 'interfaces', label: 'Interface (pisahkan dengan koma)', placeholder: 'LoopBack0, 10GE0/0/0' },
      { key: 'metric', label: 'Metric IS-IS', placeholder: '10' },
    ]
    : mode === 'bgp'
      ? [
        { key: 'local_asn', label: 'ASN lokal', placeholder: '65000' },
        { key: 'neighbor', label: 'Alamat neighbor', placeholder: '192.0.2.2' },
        { key: 'remote_asn', label: 'ASN remote', placeholder: '65001' },
        { key: 'address_family', label: 'Address family (ipv4 / ipv6)', placeholder: 'ipv4' },
      ]
      : [
        { key: 'interface', label: 'Interface', placeholder: '10GE0/0/0' },
        { key: 'mtu', label: 'MTU (opsional)', placeholder: '9216' },
        { key: 'description', label: 'Description (opsional)', placeholder: 'UPLINK TO ...' },
        { key: 'bandwidth_mbps', label: 'Bandwidth Mbps (opsional)', placeholder: '100000' },
      ]

  async function selectDevice(deviceId: string) {
    setValues({ device_id: deviceId, interfaces: '' })
    setHints(null); setResult(null); setError(null)
    if (!deviceId) return
    try {
      const next = await api.get<ChangeHints>(`/configs/network-change/hints?device_id=${deviceId}&operation=${mode}`)
      setHints(next)
      setValues((v) => ({ ...v, device_id: deviceId,
        ...(mode === 'igp' ? { ospf_process: next.ospf_process } : {}),
        ...(mode === 'bgp' && next.local_asn ? { local_asn: next.local_asn } : {}),
        ...(mode === 'igp' && next.isis_net ? { net: next.isis_net } : {}),
      }))
    } catch (e) { setError((e as Error).message) }
  }

  async function generateChange(e: FormEvent) {
    e.preventDefault()
    setError(null); setResult(null); setLoading(true)
    const body: Record<string, unknown> = { operation: mode, device_id: Number(values.device_id) }
    for (const key of ['interfaces']) body[key] = values[key].split(',').map((v) => v.trim()).filter(Boolean)
    for (const [key, value] of Object.entries(values)) {
      if (!value || key === 'device_id' || key === 'interfaces') continue
      body[key] = ['metric', 'local_asn', 'remote_asn', 'mtu', 'bandwidth_mbps'].includes(key) ? Number(value) : value
    }
    try { setResult(await api.post<ChangeResult>('/configs/network-change', body)) }
    catch (e) { setError((e as Error).message) }
    finally { setLoading(false) }
  }

  return <div className="config-change">
    <form className="mop-meta" onSubmit={generateChange}>
      <label><span>Perangkat</span><select required value={values.device_id} onChange={(e) => selectDevice(e.target.value)}>
        <option value="">Pilih perangkat</option>{devices.map((d) => <option key={d.id} value={d.id}>{d.hostname} · {d.role}</option>)}
      </select></label>
      {fields.map((f) => <label key={f.key}><span>{f.label}</span>
        {f.key === 'address_family' ? <select value={values[f.key] ?? 'ipv4'} onChange={(e) => setValues({ ...values, [f.key]: e.target.value })}><option value="ipv4">IPv4</option><option value="ipv6">IPv6</option></select>
          : f.key === 'interfaces' ? <textarea rows={2} required value={values[f.key] ?? ''} placeholder={f.placeholder} onChange={(e) => setValues({ ...values, [f.key]: e.target.value })} />
            : f.key === 'interface' && hints ? <select required value={values[f.key] ?? ''} onChange={(e) => setValues({ ...values, [f.key]: e.target.value })}><option value="">Pilih interface</option>{hints.interfaces.map((i) => <option key={i} value={i}>{i}</option>)}</select>
            : <input required={!['mtu', 'description', 'bandwidth_mbps'].includes(f.key)} value={values[f.key] ?? ''} placeholder={f.placeholder} onChange={(e) => setValues({ ...values, [f.key]: e.target.value })} />}
      </label>)}
      <button className="primary" disabled={loading || !devices.length}>{loading ? 'Menyusun…' : 'Generate config'}</button>
    </form>
    {hints && <p className="hint">Vendor: {hints.dialect}. Nilai OSPF/ASN dan pilihan interface dibaca dari config RANCID.</p>}
    {error && <div className="error">{error}</div>}
    {result && <div className="config-endpoint"><h3>{result.device} <span className="pill pill-muted">{result.dialect_label}</span></h3>
      {result.warnings.map((w) => <div key={w} className="small muted">⚠ {w}</div>)}
      <div className="grid-2">
        <ConfigBlock title="Konfigurasi" lines={[...result.config, ...result.commit]} />
        <ConfigBlock title="Rollback" lines={[...result.rollback, ...result.commit]} />
      </div>
    </div>}
  </div>
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

function ConfigBlock({ title, lines, href }: { title: string; lines: string[]; href?: string }) {
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
        {href && <a className="button" href={href}>.txt</a>}
      </div>
      <pre>{lines.join('\n')}</pre>
    </div>
  )
}
