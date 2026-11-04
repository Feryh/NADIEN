import { FormEvent, useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api'
import { fmtG } from '../components/ui'
import ValidationReport, { ValidationResult } from '../components/ValidationReport'

// Phase 4 — Customer Path Engineering: Site A -> Site B, bandwidth, protection.

interface Hop {
  link_id: number
  from: string
  to: string
  from_pop: string | null
  to_pop: string | null
  capacity_gbps: number
  current_gbps: number
  reserved_gbps: number
  available_gbps: number
  after_pct: number | null
  latency_ms: number
  latency_estimated: boolean
}
interface PathInfo {
  device_ids: number[]
  link_ids: number[]
  devices: string[]
  pops: string[]
  hops: number
  latency_ms: number
  latency_estimated: boolean
  bottleneck_available_gbps: number | null
  max_after_pct: number
  hop_detail: Hop[]
}
interface Bottleneck {
  link_id: number
  from: string
  to: string
  capacity_gbps: number
  available_gbps: number
  after_pct: number | null
  shortfall_gbps: number
}
interface PathResult {
  a: string
  b: string
  bandwidth_gbps: number
  protection: string
  max_util_pct: number
  primary: PathInfo | null
  backup: PathInfo | null
  diversity: string | null
  warnings: string[]
  bottlenecks: Bottleneck[]
  unconstrained_path?: PathInfo
  service_id?: number
}
interface Engineered {
  service_id: number
  circuit_id: string
  status: string
  service_type: string
  bandwidth_mbps: number
  protection: string
  paths: { role: string; hops: number; latency_ms: number | null; latency_estimated: boolean; reserved_gbps: number; released: boolean; devices: string[] }[]
}

function AfterBar({ pct }: { pct: number | null }) {
  if (pct == null) return <span className="muted">–</span>
  const color = pct >= 80 ? '#ef4444' : pct >= 60 ? '#eab308' : '#22c55e'
  return (
    <div className="after-bar" title={`${pct}% setelah layanan ini`}>
      <div style={{ width: `${Math.min(pct, 100)}%`, background: color }} />
      <span>{pct}%</span>
    </div>
  )
}

function PathCard({ title, path, accent }: { title: string; path: PathInfo; accent: string }) {
  return (
    <div className="card path-card" style={{ borderTopColor: accent }}>
      <div className="toolbar">
        <h2>{title}</h2>
        <span className="pill pill-muted">{path.hops} hop</span>
        <span className="pill pill-muted">
          {path.latency_ms.toFixed(2)} ms{path.latency_estimated ? ' (estimasi)' : ''}
        </span>
        <span className="pill pill-muted">utilisasi maks {path.max_after_pct}%</span>
      </div>
      <div className="path-chain">
        {path.devices.map((d, i) => (
          <span key={d}>
            {i > 0 && <span className="arrow">→</span>}
            <b>{d}</b>
          </span>
        ))}
      </div>
      <p className="muted small">POP dilalui: {path.pops.join(' → ')}</p>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Hop</th>
              <th className="num">Kapasitas</th>
              <th className="num">Trafik</th>
              <th className="num">Reserved</th>
              <th className="num">Sisa</th>
              <th style={{ width: 160 }}>Setelah layanan</th>
              <th className="num">Latency</th>
            </tr>
          </thead>
          <tbody>
            {path.hop_detail.map((h) => (
              <tr key={h.link_id}>
                <td>
                  {h.from} → {h.to}
                  {h.from_pop !== h.to_pop && <div className="muted small">{h.from_pop} → {h.to_pop}</div>}
                </td>
                <td className="num">{fmtG(h.capacity_gbps)}</td>
                <td className="num">{fmtG(h.current_gbps)}</td>
                <td className="num">{fmtG(h.reserved_gbps)}</td>
                <td className="num">{fmtG(h.available_gbps)}</td>
                <td><AfterBar pct={h.after_pct} /></td>
                <td className="num">{h.latency_ms.toFixed(2)}{h.latency_estimated ? '*' : ''}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {path.latency_estimated && <p className="hint">* latency estimasi (jarak/koordinat POP), bukan hasil ukur.</p>}
    </div>
  )
}

export default function Paths() {
  const [devices, setDevices] = useState<{ id: number; hostname: string; status: string }[]>([])
  const [customers, setCustomers] = useState<{ id: number; name: string }[]>([])
  const [form, setForm] = useState({ a: '', b: '', bandwidth: '1000', protection: '1+1', metric: 'latency', maxUtil: '80' })
  const [result, setResult] = useState<PathResult | null>(null)
  const [lastRequest, setLastRequest] = useState<Record<string, unknown> | null>(null)
  const [reserve, setReserve] = useState({
    customer_id: '', circuit_id: '', service_type: 'l2vpn', vlan_id: '',
    a_interface: '', b_interface: '', vrf_name: '', route_distinguisher: '', route_target: '',
  })
  const [validation, setValidation] = useState<ValidationResult | null>(null)
  const [services, setServices] = useState<Engineered[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const loadServices = useCallback(() => {
    api.get<Engineered[]>('/paths/services').then(setServices).catch(() => undefined)
  }, [])

  useEffect(() => {
    api.get<{ id: number; hostname: string; status: string }[]>('/devices?limit=10000')
      .then((r) => setDevices(r.filter((d) => d.status !== 'decommissioned').sort((a, b) => a.hostname.localeCompare(b.hostname))))
      .catch((e: Error) => setError(e.message))
    api.get<{ id: number; name: string }[]>('/customers?limit=10000').then(setCustomers).catch(() => undefined)
    loadServices()
  }, [loadServices])

  const set = (k: keyof typeof form) => (e: { target: { value: string } }) => setForm({ ...form, [k]: e.target.value })

  async function submit(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError(null)
    setNotice(null)
    setResult(null)
    setValidation(null)
    try {
      const request = {
        a: { device_id: Number(form.a) },
        b: { device_id: Number(form.b) },
        bandwidth_mbps: Number(form.bandwidth),
        protection: form.protection,
        metric: form.metric,
        max_util_pct: Number(form.maxUtil) || undefined,
      }
      setLastRequest(request)
      setResult(await api.post<PathResult>('/paths/compute', request))
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy(false)
    }
  }

  // Service fields shared by the validation check and the reservation.
  const serviceFields = () => ({
    service_type: reserve.service_type,
    vlan_id: reserve.vlan_id ? Number(reserve.vlan_id) : null,
    a_interface: reserve.a_interface.trim() || null,
    b_interface: reserve.b_interface.trim() || null,
    vrf_name: reserve.vrf_name.trim() || null,
    route_distinguisher: reserve.route_distinguisher.trim() || null,
    route_target: reserve.route_target.trim() || null,
  })

  async function validateDesign() {
    if (!result?.primary || !lastRequest) return
    setBusy(true)
    setError(null)
    try {
      setValidation(await api.post<ValidationResult>('/validation/check', {
        ...serviceFields(),
        bandwidth_mbps: lastRequest.bandwidth_mbps,
        protection: lastRequest.protection,
        a_device_id: result.primary.device_ids[0],
        b_device_id: result.primary.device_ids[result.primary.device_ids.length - 1],
        primary_link_ids: result.primary.link_ids,
        backup_link_ids: result.backup?.link_ids ?? [],
      }))
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy(false)
    }
  }

  async function doReserve(e: FormEvent) {
    e.preventDefault()
    if (!lastRequest) return
    if (validation?.status === 'fail' && !confirm('Desain belum lolos validasi. Tetap reserve?')) return
    setBusy(true)
    setError(null)
    try {
      const r = await api.post<PathResult>('/paths/reserve', {
        ...lastRequest,
        ...serviceFields(),
        customer_id: Number(reserve.customer_id),
        circuit_id: reserve.circuit_id.trim(),
      })
      setNotice(`Service ${reserve.circuit_id} dibuat (status reserved, id ${r.service_id}); bandwidth ditambahkan ke reserved di setiap link.`)
      setResult(r)
      loadServices()
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy(false)
    }
  }

  async function release(s: Engineered) {
    if (!confirm(`Lepas reservasi ${s.circuit_id}? Bandwidth dikembalikan dan service ditandai terminated.`)) return
    try {
      await api.post(`/paths/services/${s.service_id}/release`)
      loadServices()
    } catch (err) {
      setError((err as Error).message)
    }
  }

  return (
    <div className="capacity">
      <div className="card">
        <h2>Customer Path Engineering</h2>
        <form className="calc-form" onSubmit={submit}>
          <label>
            <span>Site A (hostname)</span>
            <select value={form.a} onChange={set('a')} required>
              <option value="">Pilih perangkat Site A</option>
              {devices.map((d) => <option key={d.id} value={d.id}>{d.hostname}</option>)}
            </select>
          </label>
          <label>
            <span>Site B (hostname)</span>
            <select value={form.b} onChange={set('b')} required>
              <option value="">Pilih perangkat Site B</option>
              {devices.map((d) => <option key={d.id} value={d.id}>{d.hostname}</option>)}
            </select>
          </label>
          <label>
            <span>Bandwidth (Mbps)</span>
            <input type="number" min={1} value={form.bandwidth} onChange={set('bandwidth')} required />
          </label>
          <label>
            <span>Proteksi</span>
            <select value={form.protection} onChange={set('protection')}>
              <option value="none">Tanpa proteksi</option>
              <option value="1+1">1+1 (backup ikut di-reserve)</option>
              <option value="1:1">1:1 (backup dihitung, tidak di-reserve)</option>
            </select>
          </label>
          <label>
            <span>Optimasi</span>
            <select value={form.metric} onChange={set('metric')}>
              <option value="latency">Latency terendah</option>
              <option value="hops">Hop paling sedikit</option>
            </select>
          </label>
          <label>
            <span>Batas utilisasi (%)</span>
            <input type="number" min={1} max={100} value={form.maxUtil} onChange={set('maxUtil')} />
          </label>
          <button className="primary" type="submit" disabled={busy}>{busy ? 'Menghitung…' : 'Hitung jalur'}</button>
        </form>
        <p className="hint">
          Jalur dihitung dari hostname Site A ke hostname Site B yang dipilih. Link dipakai hanya jika status up dan utilisasi
          setelah layanan ≤ batas; jalur backup akan diusahakan memakai perangkat ujung yang berbeda bila tersedia.
        </p>
      </div>

      {error && <div className="error">{error}</div>}
      {notice && <div className="notice">{notice}</div>}

      {result && (
        <>
          <div className="card">
            <div className="toolbar">
              <h2>{result.a} → {result.b}</h2>
              <span className="pill pill-muted">{fmtG(result.bandwidth_gbps)}</span>
              <span className="pill pill-muted">proteksi {result.protection}</span>
              {result.diversity && <span className="pill pill-ok">backup: {result.diversity}</span>}
              {result.primary && <span className="pill pill-ok">kapasitas cukup</span>}
              {!result.primary && <span className="pill pill-bad">kapasitas tidak cukup</span>}
            </div>
            {result.warnings.map((w) => <div key={w} className="hint">⚠ {w}</div>)}
          </div>

          {result.primary && <PathCard title="Jalur utama" path={result.primary} accent="#38bdf8" />}
          {result.backup && <PathCard title="Jalur backup" path={result.backup} accent="#a78bfa" />}

          {!result.primary && result.bottlenecks.length > 0 && (
            <div className="card">
              <h2>Link yang perlu upgrade</h2>
              <p className="hint">Di jalur terpendek ({result.unconstrained_path?.devices.join(' → ')}):</p>
              <table>
                <thead>
                  <tr><th>Link</th><th className="num">Kapasitas</th><th className="num">Sisa</th><th className="num">Kurang</th><th>Setelah layanan</th></tr>
                </thead>
                <tbody>
                  {result.bottlenecks.map((b) => (
                    <tr key={b.link_id}>
                      <td>{b.from} → {b.to}</td>
                      <td className="num">{fmtG(b.capacity_gbps)}</td>
                      <td className="num">{fmtG(b.available_gbps)}</td>
                      <td className="num"><b>{fmtG(b.shortfall_gbps)}</b></td>
                      <td><AfterBar pct={b.after_pct} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {result.primary && !result.service_id && (
            <div className="card">
              <h2>Reservasi resource</h2>
              <form className="calc-form" onSubmit={doReserve}>
                <label>
                  <span>Customer</span>
                  <select value={reserve.customer_id} onChange={(e) => setReserve({ ...reserve, customer_id: e.target.value })} required>
                    <option value="">— pilih —</option>
                    {customers.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
                  </select>
                </label>
                <label>
                  <span>Circuit ID</span>
                  <input value={reserve.circuit_id} onChange={(e) => setReserve({ ...reserve, circuit_id: e.target.value })} required />
                </label>
                <label>
                  <span>Layanan</span>
                  <select value={reserve.service_type} onChange={(e) => setReserve({ ...reserve, service_type: e.target.value })}>
                    <option value="l2vpn">L2VPN</option>
                    <option value="l3vpn">L3VPN</option>
                    <option value="internet">Internet</option>
                    <option value="iplc">IPLC</option>
                  </select>
                </label>
                <label>
                  <span>VLAN</span>
                  <input type="number" min={1} max={4094} value={reserve.vlan_id} onChange={(e) => setReserve({ ...reserve, vlan_id: e.target.value })} />
                </label>
                <label>
                  <span>Port A ({result.primary.devices[0]})</span>
                  <input value={reserve.a_interface} placeholder="GigabitEthernet0/3/1" onChange={(e) => setReserve({ ...reserve, a_interface: e.target.value })} />
                </label>
                <label>
                  <span>Port B ({result.primary.devices[result.primary.devices.length - 1]})</span>
                  <input value={reserve.b_interface} onChange={(e) => setReserve({ ...reserve, b_interface: e.target.value })} />
                </label>
                {reserve.service_type === 'l3vpn' && (
                  <>
                    <label>
                      <span>VRF</span>
                      <input value={reserve.vrf_name} onChange={(e) => setReserve({ ...reserve, vrf_name: e.target.value })} />
                    </label>
                    <label>
                      <span>RD</span>
                      <input value={reserve.route_distinguisher} placeholder="65000:101" onChange={(e) => setReserve({ ...reserve, route_distinguisher: e.target.value })} />
                    </label>
                    <label>
                      <span>RT</span>
                      <input value={reserve.route_target} placeholder="65000:101" onChange={(e) => setReserve({ ...reserve, route_target: e.target.value })} />
                    </label>
                  </>
                )}
                <button type="button" onClick={validateDesign} disabled={busy}>Validasi desain</button>
                <button className="primary" type="submit" disabled={busy}>Reserve</button>
              </form>
              {validation && <ValidationReport report={validation} />}
              {!customers.length && (
                <p className="hint">Belum ada customer — tambahkan di <Link to="/inventory/customers">Inventory → Customer</Link>.</p>
              )}
              <p className="hint">Jalur dihitung ulang saat reserve, lalu bandwidth ditambahkan ke <i>reserved</i> setiap link sehingga langsung terlihat di Capacity Planning.</p>
            </div>
          )}
        </>
      )}

      <div className="card">
        <h2>Service dengan jalur terencana</h2>
        <div className="table-wrap">
          <table>
            <thead>
              <tr><th>Circuit</th><th>Layanan</th><th className="num">Bandwidth</th><th>Status</th><th>Jalur</th><th /></tr>
            </thead>
            <tbody>
              {services.map((s) => (
                <tr key={s.service_id}>
                  <td><b>{s.circuit_id}</b></td>
                  <td>{s.service_type.toUpperCase()} · {s.protection}</td>
                  <td className="num">{fmtG(s.bandwidth_mbps / 1000)}</td>
                  <td><span className={`pill ${s.status === 'reserved' ? 'pill-ok' : 'pill-muted'}`}>{s.status}</span></td>
                  <td className="small">
                    {s.paths.map((p) => (
                      <div key={p.role}>
                        <b>{p.role}</b>: {p.devices.join(' → ')} · {p.hops} hop
                        {p.reserved_gbps ? ` · reserved ${fmtG(p.reserved_gbps)}` : ''}{p.released ? ' · dilepas' : ''}
                      </div>
                    ))}
                  </td>
                  <td className="actions">
                    {s.status === 'reserved' && <button className="danger" onClick={() => release(s)}>Lepas</button>}
                  </td>
                </tr>
              ))}
              {!services.length && <tr><td colSpan={6} className="empty">Belum ada service yang di-reserve dari halaman ini</td></tr>}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}
