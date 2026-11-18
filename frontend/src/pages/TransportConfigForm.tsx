import { FormEvent, useState } from 'react'
import { api } from '../api'
import { ConfigBlock } from '../components/ConfigBlock'

export type TransportTech = 'sr-mpls' | 'srv6' | 'vxlan' | 'evpn'

interface DeviceOption { id: number; hostname: string; role: string }

interface TransportDevice {
  role: 'A' | 'B' | 'P'
  device: string
  device_id: number
  dialect_label: string
  config: string[]
  rollback: string[]
  commit: string[]
  warnings: string[]
  notes: string[]
}

interface TransportResult {
  technology: TransportTech
  label: string
  devices: TransportDevice[]
  warnings: string[]
}

type Values = Record<string, string>

const DEFAULTS: Values = {
  a: '', b: '', transit: '', isis_process: '', bgp_asn: '',
  srgb_start: '16000', srgb_end: '23999', a_index: '', b_index: '', color: '100',
  srv6_block: 'fcbb:bb00::/32', locator_name: 'NADIEN', a_ipv6: '', b_ipv6: '',
  vrf: '', route_distinguisher: '', route_target: '',
  vlan: '', vni: '', a_port: '', b_port: '', control_plane: 'evpn', evpn_type: 'vpws', evi: '',
}

const HINTS: Record<TransportTech, string> = {
  'sr-mpls': 'IS-IS SR-MPLS (SRGB, prefix-SID, TI-LFA) di semua node jalur dan SR-TE policy A ↔ B pada satu color. Prefix-SID yang sudah ada di RANCID dipertahankan; node transit menjadi segment list eksplisit.',
  srv6: 'Locator per node (block + ID perangkat), IS-IS SRv6, dan BGP VPNv4 over SRv6 antara A dan B. Isi VRF untuk layanan L3VPN di atas SRv6.',
  vxlan: 'L2 VNI antara dua VTEP pada sub-interface akses, dengan BGP EVPN atau replikasi statis (head-end, VRP). Node transit cukup underlay IP.',
  evpn: 'EVPN di atas underlay MPLS yang ada (LDP/SR): E-Line (VPWS, service id A=1, B=2) atau E-LAN (bridge domain ke EVI), dengan BGP l2vpn evpn antara A dan B. EVI default = VLAN.',
}

function deviceId(devices: DeviceOption[], hostname: string): number | null {
  const wanted = hostname.trim().toLowerCase()
  return devices.find((d) => d.hostname.toLowerCase() === wanted)?.id ?? null
}

function buildBody(tech: TransportTech, v: Values, devices: DeviceOption[]): Record<string, unknown> {
  const a = deviceId(devices, v.a)
  const b = deviceId(devices, v.b)
  if (a == null || b == null) throw new Error('PE A dan PE B harus hostname yang ada di inventory.')
  const transitNames = v.transit.split(',').map((t) => t.trim()).filter(Boolean)
  const transit = transitNames.map((name) => {
    const id = deviceId(devices, name)
    if (id == null) throw new Error(`Node transit ${name} tidak ada di inventory.`)
    return id
  })
  const body: Record<string, unknown> = { technology: tech, a_device_id: a, b_device_id: b, transit_device_ids: transit }
  const numeric = (key: string) => (v[key].trim() ? Number(v[key]) : undefined)
  const text = (key: string) => v[key].trim() || undefined
  Object.assign(body, { isis_process: text('isis_process'), bgp_asn: numeric('bgp_asn') })
  if (tech === 'sr-mpls') {
    const indexes: Record<number, number> = {}
    if (v.a_index.trim()) indexes[a] = Number(v.a_index)
    if (v.b_index.trim()) indexes[b] = Number(v.b_index)
    Object.assign(body, { srgb_start: numeric('srgb_start'), srgb_end: numeric('srgb_end'), color: numeric('color'),
      sid_indexes: indexes, ti_lfa: v.ti_lfa !== 'off', sr_policy: v.sr_policy !== 'off' })
  }
  if (tech === 'srv6') {
    const loopbacks: Record<number, string> = {}
    if (v.a_ipv6.trim()) loopbacks[a] = v.a_ipv6.trim()
    if (v.b_ipv6.trim()) loopbacks[b] = v.b_ipv6.trim()
    Object.assign(body, { srv6_block: text('srv6_block'), locator_name: text('locator_name'), ipv6_loopbacks: loopbacks })
  }
  if (tech === 'vxlan') {
    Object.assign(body, { vlan: numeric('vlan'), vni: numeric('vni'), a_port: text('a_port'), b_port: text('b_port'),
      control_plane: v.control_plane })
  }
  if (tech === 'evpn') {
    Object.assign(body, { vlan: numeric('vlan'), evi: numeric('evi'), a_port: text('a_port'), b_port: text('b_port'),
      evpn_type: v.evpn_type })
  }
  if (tech !== 'sr-mpls') {
    Object.assign(body, { vrf: tech === 'srv6' ? text('vrf') : undefined, route_distinguisher: text('route_distinguisher'),
      route_target: text('route_target') })
  }
  return body
}

/** End-to-end SR-MPLS / SRv6 / VXLAN between two PEs: config and rollback per device (A, transit P, B). */
export default function TransportConfigForm({ tech, devices }: { tech: TransportTech; devices: DeviceOption[] }) {
  const [values, setValues] = useState<Values>(DEFAULTS)
  const [result, setResult] = useState<TransportResult | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const set = (key: string) => (e: { target: { value: string } }) => setValues((v) => ({ ...v, [key]: e.target.value }))
  const field = (key: string, label: string, placeholder = '', required = false) => (
    <label key={key}><span>{label}</span>
      <input value={values[key] ?? ''} placeholder={placeholder} required={required} onChange={set(key)}
        list={['a', 'b'].includes(key) ? 'transport-devices' : undefined} />
    </label>
  )
  const toggle = (key: string, label: string) => (
    <label key={key} className="check">
      <input type="checkbox" checked={values[key] !== 'off'}
        onChange={(e) => setValues((v) => ({ ...v, [key]: e.target.checked ? 'on' : 'off' }))} />
      <span>{label}</span>
    </label>
  )

  async function generate(e: FormEvent) {
    e.preventDefault()
    setError(null); setResult(null); setLoading(true)
    try { setResult(await api.post<TransportResult>('/configs/transport', buildBody(tech, values, devices))) }
    catch (err) { setError((err as Error).message) }
    finally { setLoading(false) }
  }

  return <div className="config-change">
    <p className="hint">{HINTS[tech]}</p>
    <form className="mop-meta" onSubmit={generate}>
      {field('a', 'PE A (hostname)', 'ketik hostname', true)}
      {field('b', 'PE B (hostname)', 'ketik hostname', true)}
      {(tech === 'sr-mpls' || tech === 'srv6') && field('transit', 'Node transit P (opsional, urut, pisahkan koma)', 'P1, P2')}
      {(tech === 'sr-mpls' || tech === 'srv6') && field('isis_process', 'IS-IS process', 'default dari RANCID')}
      {field('bgp_asn', 'ASN BGP', 'default dari RANCID')}
      {tech === 'sr-mpls' && <>
        {field('srgb_start', 'SRGB awal', '16000', true)}
        {field('srgb_end', 'SRGB akhir', '23999', true)}
        {field('a_index', 'Prefix-SID index PE A', 'default RANCID / ID')}
        {field('b_index', 'Prefix-SID index PE B', 'default RANCID / ID')}
        {field('color', 'Color SR-TE', '100')}
        {toggle('ti_lfa', 'TI-LFA')}
        {toggle('sr_policy', 'SR-TE policy A ↔ B')}
      </>}
      {tech === 'srv6' && <>
        {field('srv6_block', 'Block SRv6', 'fcbb:bb00::/32', true)}
        {field('locator_name', 'Nama locator', 'NADIEN', true)}
        {field('a_ipv6', 'Loopback IPv6 PE A', 'default dari RANCID')}
        {field('b_ipv6', 'Loopback IPv6 PE B', 'default dari RANCID')}
        {field('vrf', 'VRF (opsional, L3VPN)', 'CUST-A')}
      </>}
      {tech === 'vxlan' && <>
        {field('vlan', 'VLAN akses', '1480', true)}
        {field('vni', 'VNI', 'default 10000 + VLAN')}
        {field('a_port', 'Port akses PE A', 'GigabitEthernet0/3/1')}
        {field('b_port', 'Port akses PE B', 'HundredGigE0/0/0/5')}
        <label><span>Control plane</span>
          <select value={values.control_plane} onChange={set('control_plane')}>
            <option value="evpn">BGP EVPN</option>
            <option value="static">Statis (head-end, VRP)</option>
          </select>
        </label>
      </>}
      {tech === 'evpn' && <>
        <label><span>Jenis EVPN</span>
          <select value={values.evpn_type} onChange={set('evpn_type')}>
            <option value="vpws">E-Line (VPWS)</option>
            <option value="elan">E-LAN (bridge domain)</option>
          </select>
        </label>
        {field('vlan', 'VLAN akses', '1480', true)}
        {field('evi', 'EVI', 'default = VLAN')}
        {field('a_port', 'Port akses PE A', 'GigabitEthernet0/3/1')}
        {field('b_port', 'Port akses PE B', 'HundredGigE0/0/0/5')}
      </>}
      {(tech === 'vxlan' || tech === 'evpn' || (tech === 'srv6' && values.vrf.trim())) && <>
        {field('route_distinguisher', 'RD', 'default ASN:ID')}
        {field('route_target', 'RT', tech === 'vxlan' ? 'default ASN:VNI' : tech === 'evpn' ? 'default ASN:EVI' : 'default ASN:100')}
      </>}
      <button className="primary" disabled={loading || !devices.length}>{loading ? 'Menyusun…' : 'Generate config'}</button>
      <datalist id="transport-devices">{devices.map((d) => <option key={d.id} value={d.hostname}>{d.role}</option>)}</datalist>
    </form>
    {error && <div className="error">{error}</div>}
    {result && <>
      {result.warnings.map((w) => <div key={w} className="small muted">⚠ {w}</div>)}
      {result.devices.map((d) => (
        <div key={d.device_id} className="config-endpoint">
          <h3>{d.role === 'P' ? 'Transit' : `PE ${d.role}`}: {d.device} <span className="pill pill-muted">{d.dialect_label}</span></h3>
          {d.notes.map((n) => <div key={n} className="small">ℹ {n}</div>)}
          {d.warnings.map((w) => <div key={w} className="small muted">⚠ {w}</div>)}
          {d.config.length > 0 && <div className="grid-2">
            <ConfigBlock title="Konfigurasi" lines={[...d.config, ...d.commit]} />
            <ConfigBlock title="Rollback" lines={[...d.rollback, ...d.commit]} />
          </div>}
        </div>
      ))}
    </>}
  </div>
}
