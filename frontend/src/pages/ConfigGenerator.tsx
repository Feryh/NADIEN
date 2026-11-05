import { FormEvent, useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { api } from '../api'
import { useAuth } from '../auth'
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
  vc_ids?: VcIdRow[]
}

interface VcIdRow {
  vlan: number | null
  vc_id: number
  pw: string | null
  conflicts: string[]
}

interface VcIdView {
  vlan: number | null
  vc_id: number
  pws: string[]
  conflicts: string[]
}

/** One row per VLAN: the VC id both ends use, with each end's conflicts (DB services, RANCID running config). */
function mergeVcRows(result: ConfigResult): VcIdView[] {
  const rows = new Map<string, VcIdView>()
  for (const ep of Object.values(result.endpoints)) {
    for (const r of ep.vc_ids ?? []) {
      const key = String(r.vlan)
      const row = rows.get(key) ?? { vlan: r.vlan, vc_id: r.vc_id, pws: [], conflicts: [] }
      if (r.pw) row.pws.push(`${ep.label}: ${r.pw}`)
      for (const c of r.conflicts) {
        const text = `${ep.device ?? `Site ${ep.label}`}: ${c}`
        if (!row.conflicts.includes(text)) row.conflicts.push(text)
      }
      rows.set(key, row)
    }
  }
  return [...rows.values()]
}

interface ConfigResult {
  circuit_id: string
  service_type: string
  endpoints: Record<string, Endpoint>
}

type Mode = 'l2vpn' | 'l3vpn' | 'igp' | 'bgp' | 'parameters'
interface DeviceOption { id: number; hostname: string; role: string; loopback_ipv4?: string | null }
type L2Source = 'hostname' | 'service'
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
  // L2VPN is built from two hostnames by default; inventory services are the other source.
  const l2Source: L2Source = params.get('src') === 'service' || (mode === 'l2vpn' && selected != null) ? 'service' : 'hostname'
  const byHostname = mode === 'l2vpn' && l2Source === 'hostname'
  const [services, setServices] = useState<ServiceRow[] | null>(null)
  const [devices, setDevices] = useState<DeviceOption[]>([])
  const [inputs, setInputs] = useState<Inputs>({ a_ip: '', b_ip: '', vlan: '', vlans: '', vc_id: '', bgp_asn: '' })
  const [vlanMode, setVlanMode] = useState<VlanMode>('standard')
  const [vcIds, setVcIds] = useState<Record<number, string>>({})
  // Kept across refetches (result is cleared while a new one loads) so VC id inputs keep focus.
  const [vcRows, setVcRows] = useState<VcIdView[]>([])
  const [policer, setPolicer] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const { can } = useAuth()
  const navigate = useNavigate()
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
      // PE addresses are for L3 sub-interfaces; an L2VPN pseudowire only needs the neighbour loopback.
      if (mode === 'l2vpn' && (k === 'bgp_asn' || k === 'a_ip' || k === 'b_ip' || (k === 'vc_id' && vlanMode === 'multiple'))) continue
      if (mode === 'l3vpn' && (k === 'vlans' || k === 'vc_id')) continue
      q.set(k, v.trim())
    }
    // Standard is a one-VLAN batch: the backend keeps the VC id override only when exactly one VLAN is sent.
    if (mode === 'l2vpn' && vlanMode === 'standard' && inputs.vlan.trim()) q.set('vlans', inputs.vlan.trim())
    const overrides = Object.entries(vcIds).filter(([vlan, vc]) => vc.trim() && vc.trim() !== vlan)
    if (mode === 'l2vpn' && vlanMode === 'multiple' && overrides.length) {
      q.set('vc_ids', overrides.map(([vlan, vc]) => `${vlan}:${vc.trim()}`).join(','))
    }
    if (policer) q.set('policer', 'true')
    return q.toString()
  }, [inputs, mode, vlanMode, vcIds, policer])

  useEffect(() => {
    setVcRows([])
    setVcIds({})
  }, [selected, mode, vlanMode, l2Source])

  const standardPw = vlanMode === 'standard' ? (Number(inputs.vc_id) || Number(inputs.vlan) || null) : null

  const submitChange = () => {
    if (selected == null) return
    setSubmitting(true)
    setError(null)
    // Same query as the preview, so the frozen script is exactly what is shown here.
    api.post<{ id: number }>(`/changes?${query}`, { service_id: selected })
      .then((c) => navigate(`/changes?id=${c.id}`))
      .catch((e: Error) => setError(e.message))
      .finally(() => setSubmitting(false))
  }

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
      api.get<ConfigResult>(`/configs/services/${selected}?${query}`)
        .then((r) => { setResult(r); setVcRows(mergeVcRows(r)) })
        .catch((e: Error) => setError(e.message))
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
        {mode === 'l2vpn' && (
          <div className="segmented l2-source" role="group" aria-label="Sumber L2VPN">
            <button type="button" className={byHostname ? 'active' : ''} aria-pressed={byHostname}
              onClick={() => setParams({ mode })}>Dari hostname</button>
            <button type="button" className={!byHostname ? 'active' : ''} aria-pressed={!byHostname}
              onClick={() => setParams({ mode, src: 'service' })}>Layanan tercatat</button>
          </div>
        )}
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
              <input type="number" min={1} max={4094} value={inputs.vlan} placeholder={byHostname ? 'wajib, mis. 1480' : 'default = VLAN layanan'}
                onChange={(e) => setInputs({ ...inputs, vlan: e.target.value })} />
            </label>}
            {mode === 'l2vpn' && vlanMode === 'multiple' && <label className="span-2">
              <span>Daftar VLAN / rentang VLAN (maks. 64)</span>
              <input value={inputs.vlans} placeholder="Contoh: 1480-1485,1489" maxLength={256}
                onChange={(e) => { setInputs({ ...inputs, vlans: e.target.value }); setVcIds({}) }} />
              <small className="muted">Pisahkan VLAN dengan koma; gunakan tanda rentang untuk beberapa VLAN berurutan.</small>
            </label>}
            {INPUTS.filter((f) => mode === 'l2vpn'
              ? vlanMode === 'standard' && f.key === 'vc_id'
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
          {byHostname
            ? <>
                <L2vpnHostnameForm devices={devices} baseQuery={query} standardPw={standardPw} vlanMode={vlanMode}
                  vcIds={vcIds} onVcIdChange={(vlan, value) => setVcIds({ ...vcIds, [vlan]: value })} />
                <p className="hint">Standard: 1 VLAN, PW-id = VLAN kecuali VC / PW id diisi. Multi VLAN: satu pseudowire per VLAN dengan PW-id = VLAN masing-masing (bisa diganti di tabel).</p>
              </>
            : <p className="hint">Parameter di atas diterapkan ke konfigurasi layanan yang dipilih di bawah. Untuk L2VPN, Standard menambah 1 VLAN (kosong = VLAN layanan, VC id bisa diisi); Multi VLAN menambah daftar/rentang VLAN sekaligus dengan VC id = VLAN masing-masing.</p>}
        </div>}
        {mode === 'igp' || mode === 'bgp' || mode === 'parameters'
          ? <NetworkChangeForm key={mode} mode={mode} devices={devices} />
          : byHostname ? null : <>
        <div className="table-wrap mop-services">
          <table>
            <thead>
              <tr><th>Validasi</th><th>Circuit</th><th>Layanan</th><th className="num">Bandwidth</th><th>Status</th></tr>
            </thead>
            <tbody>
              {modeServices?.map((s) => (
                <tr key={s.service_id} className={`clickable ${selected === s.service_id ? 'selected' : ''}`}
                    onClick={() => setParams({ mode, ...(mode === 'l2vpn' ? { src: 'service' } : {}), service: String(s.service_id) })}>
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
            {can('engineer') && <button className="primary" disabled={!result || submitting} onClick={submitChange}>
              Ajukan change</button>}
            <button onClick={() => setParams(mode === 'l2vpn' ? { mode, src: 'service' } : { mode })}>Tutup</button>
          </div>
          {mode === 'l2vpn' && vcRows.length > 0 && (
            <VcIdTable rows={vcRows} editable={vlanMode === 'multiple'} values={vcIds}
              onChange={(vlan, value) => setVcIds({ ...vcIds, [vlan]: value })} />
          )}
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

interface Neighbour { ip: string; pw_ids: number[]; count: number; hostname: string | null }
interface NeighbourList {
  device_id: number
  hostname: string
  dialect: string | null
  loopback: string | null
  neighbours: Neighbour[]
  error: string | null
}

const MANUAL = '__manual__'

/** Existing L2VPN neighbours of a PE from its RANCID config (none while no device is chosen). */
function useNeighbours(deviceId: number | null): { list: NeighbourList | null; loading: boolean } {
  const [list, setList] = useState<NeighbourList | null>(null)
  const [loading, setLoading] = useState(false)
  useEffect(() => {
    setList(null)
    if (deviceId == null) return
    let alive = true
    setLoading(true)
    api.get<NeighbourList>(`/configs/l2vpn/neighbours?device_id=${deviceId}`)
      .then((l) => alive && setList(l))
      .catch((e: Error) => alive && setList({ device_id: deviceId, hostname: '', dialect: null, loopback: null, neighbours: [], error: e.message }))
      .finally(() => alive && setLoading(false))
    return () => { alive = false }
  }, [deviceId])
  return { list, loading }
}

/** Physical ports of a PE, offered while typing the access port. */
function usePorts(deviceId: number | null): string[] {
  const [ports, setPorts] = useState<string[]>([])
  useEffect(() => {
    setPorts([])
    if (deviceId == null) return
    let alive = true
    api.get<{ name: string }[]>(`/interfaces?device_id=${deviceId}&if_type=physical&limit=10000`)
      .then((rows) => alive && setPorts(rows.map((r) => r.name)))
      .catch(() => undefined)
    return () => { alive = false }
  }, [deviceId])
  return ports
}

interface PeerChoice { choice: string; manual: string }  // choice: '' = inventory loopback, an IP, or MANUAL

function peerValue(p: PeerChoice): string {
  return p.choice === MANUAL ? p.manual.trim() : p.choice
}

function PeerPicker({ side, list, loading, farHostname, farLoopback, peer, pwIds, onChange }: {
  side: 'A' | 'B'
  list: NeighbourList | null
  loading: boolean
  farHostname: string | null
  farLoopback: string | null
  peer: PeerChoice
  pwIds: number[]
  onChange: (peer: PeerChoice) => void
}) {
  const chosen = peerValue(peer) || farLoopback || ''
  const existing = list?.neighbours.find((n) => n.ip === chosen)
  const used = existing ? pwIds.filter((id) => existing.pw_ids.includes(id)) : []
  return (
    <label className="span-2">
      <span>Neighbour di site {side} (loopback PE {side === 'A' ? 'B' : 'A'})</span>
      <select value={peer.choice} onChange={(e) => onChange({ ...peer, choice: e.target.value })}>
        <option value="">
          {farLoopback ? `Loopback ${farHostname} dari inventory · ${farLoopback}` : 'Loopback PE tujuan (belum diketahui)'}
        </option>
        {list && list.neighbours.length > 0 && (
          <optgroup label={`Neighbour yang sudah ada di ${list.hostname} (${list.neighbours.length})`}>
            {list.neighbours.map((n) => (
              <option key={n.ip} value={n.ip}>
                {n.ip}{n.hostname ? ` · ${n.hostname}` : ''} · {n.count} PW
                {n.pw_ids.length ? ` (${n.pw_ids.slice(0, 8).join(', ')}${n.pw_ids.length > 8 ? ', …' : ''})` : ''}
              </option>
            ))}
          </optgroup>
        )}
        <option value={MANUAL}>Isi manual (tujuan baru)…</option>
      </select>
      {peer.choice === MANUAL && (
        <input value={peer.manual} placeholder="IP loopback tujuan, mis. 10.0.0.1" maxLength={15}
          onChange={(e) => onChange({ ...peer, manual: e.target.value })} />
      )}
      <small className="muted">
        {loading ? 'Membaca neighbour dari config RANCID…'
          : list?.error ? `Neighbour existing tidak terbaca: ${list.error}`
          : list ? `${list.neighbours.length} neighbour L2VPN di config ${list.hostname}.` : 'Pilih PE dulu untuk membaca neighbour existing.'}
        {used.length > 0 && <b className="text-bad"> PW-id {used.join(', ')} sudah dipakai ke neighbour ini.</b>}
      </small>
    </label>
  )
}

function DeviceField({ label, devices, text, onText, optional }: {
  label: string
  devices: DeviceOption[]
  text: string
  onText: (text: string) => void
  optional?: boolean
}) {
  const known = !text.trim() || devices.some((d) => d.hostname.toLowerCase() === text.trim().toLowerCase())
  return (
    <label>
      <span>{label}</span>
      <input list="l2vpn-devices" value={text} placeholder={optional ? 'opsional, ketik hostname' : 'ketik hostname'}
        onChange={(e) => onText(e.target.value)} />
      {!known && <small className="text-bad">Hostname tidak ada di inventory.</small>}
    </label>
  )
}

/**
 * L2VPN between two PEs picked by hostname, without an inventory service: neighbours come from the inventory
 * loopback, the PE's existing L2VPN neighbours (RANCID) or a typed address for a new destination.
 */
function L2vpnHostnameForm({ devices, baseQuery, standardPw, vlanMode, vcIds, onVcIdChange }: {
  devices: DeviceOption[]
  baseQuery: string
  standardPw: number | null
  vlanMode: VlanMode
  vcIds: Record<number, string>
  onVcIdChange: (vlan: number, value: string) => void
}) {
  const [aText, setAText] = useState('')
  const [bText, setBText] = useState('')
  const [aPort, setAPort] = useState('')
  const [bPort, setBPort] = useState('')
  const [name, setName] = useState('')
  const [bandwidth, setBandwidth] = useState('100')
  const [aPeer, setAPeer] = useState<PeerChoice>({ choice: '', manual: '' })
  const [bPeer, setBPeer] = useState<PeerChoice>({ choice: '', manual: '' })
  const [result, setResult] = useState<ConfigResult | null>(null)
  const [error, setError] = useState<string | null>(null)
  const find = (text: string) => devices.find((d) => d.hostname.toLowerCase() === text.trim().toLowerCase()) ?? null
  const a = find(aText)
  const b = find(bText)
  const aNeighbours = useNeighbours(a?.id ?? null)
  const bNeighbours = useNeighbours(b?.id ?? null)
  const aPorts = usePorts(a?.id ?? null)
  const bPorts = usePorts(b?.id ?? null)
  const hasVlan = new URLSearchParams(baseQuery).has('vlans')

  const query = useMemo(() => {
    if (!a || !aPort.trim() || !hasVlan) return null
    const q = new URLSearchParams(baseQuery)
    q.set('a_device_id', String(a.id))
    q.set('a_port', aPort.trim())
    if (b) q.set('b_device_id', String(b.id))
    if (b && bPort.trim()) q.set('b_port', bPort.trim())
    if (name.trim()) q.set('name', name.trim())
    if (Number(bandwidth) > 0) q.set('bandwidth_mbps', String(Math.round(Number(bandwidth))))
    if (peerValue(aPeer)) q.set('a_peer', peerValue(aPeer))
    if (b && peerValue(bPeer)) q.set('b_peer', peerValue(bPeer))
    return q.toString()
  }, [a, b, aPort, bPort, name, bandwidth, aPeer, bPeer, baseQuery, hasVlan])

  useEffect(() => {
    setResult(null)
    setError(null)
    if (!query) return
    const timer = window.setTimeout(() => {
      api.get<ConfigResult>(`/configs/l2vpn?${query}`).then(setResult).catch((e: Error) => setError(e.message))
    }, 400)
    return () => window.clearTimeout(timer)
  }, [query])

  const rows = result ? mergeVcRows(result) : []
  const pwIds = rows.length ? rows.map((r) => r.vc_id) : standardPw != null ? [standardPw] : []
  const missing = [!a && 'hostname PE A', !aPort.trim() && 'port PE A', !hasVlan && 'VLAN'].filter(Boolean)

  return (
    <div className="l2vpn-hostname">
      <datalist id="l2vpn-devices">{devices.map((d) => <option key={d.id} value={d.hostname}>{d.role}</option>)}</datalist>
      <datalist id="l2vpn-ports-a">{aPorts.map((p) => <option key={p} value={p} />)}</datalist>
      <datalist id="l2vpn-ports-b">{bPorts.map((p) => <option key={p} value={p} />)}</datalist>
      <div className="mop-meta">
        <DeviceField label="PE site A (sumber)" devices={devices} text={aText}
          onText={(t) => { setAText(t); setBPeer({ choice: '', manual: '' }) }} />
        <label>
          <span>Port akses PE A</span>
          <input list="l2vpn-ports-a" value={aPort} placeholder="mis. GigabitEthernet0/3/1" maxLength={64}
            onChange={(e) => setAPort(e.target.value)} />
        </label>
        <DeviceField label="PE site B (tujuan)" devices={devices} text={bText} optional
          onText={(t) => { setBText(t); setAPeer({ choice: '', manual: '' }) }} />
        <label>
          <span>Port akses PE B</span>
          <input list="l2vpn-ports-b" value={bPort} placeholder={b ? 'mis. GigabitEthernet0/3/2' : 'isi PE B dulu'}
            maxLength={64} disabled={!b} onChange={(e) => setBPort(e.target.value)} />
        </label>
        <label>
          <span>Nama instance / pelanggan</span>
          <input value={name} placeholder="L2VPN → L2VPN_<VLAN>" maxLength={40} onChange={(e) => setName(e.target.value)} />
        </label>
        <label>
          <span>Bandwidth (Mbps)</span>
          <input type="number" min={1} value={bandwidth} onChange={(e) => setBandwidth(e.target.value)} />
        </label>
        <PeerPicker side="A" list={aNeighbours.list} loading={aNeighbours.loading} farHostname={b?.hostname ?? null}
          farLoopback={b?.loopback_ipv4 ?? null} peer={aPeer} pwIds={pwIds} onChange={setAPeer} />
        {b && (
          <PeerPicker side="B" list={bNeighbours.list} loading={bNeighbours.loading} farHostname={a?.hostname ?? null}
            farLoopback={a?.loopback_ipv4 ?? null} peer={bPeer} pwIds={pwIds} onChange={setBPeer} />
        )}
      </div>
      <div className="pw-summary" aria-live="polite">
        <span className="muted">PW-id dipakai</span>
        <b>{pwIds.length ? pwIds.join(', ') : '—'}</b>
        <span className="muted small">
          {vlanMode === 'standard' ? '(default = VLAN; ganti lewat kolom VC / PW id di atas)' : '(per VLAN, bisa diganti di tabel)'}
        </span>
        {rows.some((r) => r.conflicts.length) && <span className="pill pill-bad">bentrok dengan config existing</span>}
      </div>
      {!b && <p className="hint">Tanpa PE B, hanya config site A yang dibuat; tujuan diisi lewat neighbour (pilih existing atau manual).</p>}
      {error && <div className="error">{error}</div>}
      {missing.length > 0 && <p className="muted">Lengkapi {missing.join(', ')} untuk menyusun konfigurasi.</p>}
      {result && (
        <>
          {rows.length > 0 && <VcIdTable rows={rows} editable={vlanMode === 'multiple'} values={vcIds} onChange={onVcIdChange} />}
          {Object.values(result.endpoints).filter((ep) => ep.label === 'A' || b).map((ep) => (
            <EndpointConfig key={ep.label} ep={ep} textUrl={(part) => `/api/configs/l2vpn/text?side=${ep.label}&part=${part}&${query}`} />
          ))}
          <p className="hint">Config ini belum tercatat sebagai layanan: untuk MoP dan pengajuan change, buat layanannya dari Customer Path atau Inventory → Service.</p>
        </>
      )}
      {query && !result && !error && <div className="muted">Menyusun konfigurasi…</div>}
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

function VcIdTable({ rows, editable, values, onChange }: {
  rows: VcIdView[]
  editable: boolean
  values: Record<number, string>
  onChange: (vlan: number, value: string) => void
}) {
  const clashing = rows.filter((r) => r.conflicts.length).length
  return (
    <div className="vc-ids">
      <h3>VC id per VLAN {clashing > 0 && <span className="pill pill-bad">{clashing} bentrok</span>}</h3>
      <div className="table-wrap">
        <table>
          <thead><tr><th className="num">VLAN</th><th>VC id</th><th>PW (ZTE)</th><th>Status</th></tr></thead>
          <tbody>
            {rows.map((r) => (
              <tr key={String(r.vlan)}>
                <td className="num">{r.vlan ?? '-'}</td>
                <td>
                  {editable && r.vlan != null
                    ? <input type="number" min={1} max={4294967295} value={values[r.vlan] ?? String(r.vc_id)}
                        aria-label={`VC id VLAN ${r.vlan}`} onChange={(e) => onChange(r.vlan as number, e.target.value)} />
                    : <b>{r.vc_id}</b>}
                </td>
                <td>{r.pws.join(', ') || '-'}</td>
                <td>{r.conflicts.length
                  ? <span className="pill pill-bad" title={r.conflicts.join('\n')}>Bentrok: {r.conflicts.join('; ')}</span>
                  : <span className="pill pill-ok">OK</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="hint">
        {editable
          ? 'Default VC id = VLAN. Ganti VC id pada VLAN yang bentrok; config dan rollback di bawah ikut memakai nilai baru.'
          : 'Default VC id = VLAN. Bila bentrok, isi VC / PW id di atas.'}
      </p>
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
