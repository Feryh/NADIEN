import { FormEvent, useCallback, useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { api } from '../api'
import { useAuth } from '../auth'
import {
  IpPool, KIND_LABELS, parseNumberList, Reservation, reservationTarget, ResourceKind, SOURCE_LABELS, STATUS_LABELS,
  STATUS_PILLS, Usage,
} from './registryModel'

// Resource registry: VLANs per device, VC ids per PE pair, IPv4 pools, and the reservations that hold them.

interface DeviceOption { id: number; hostname: string; role: string }
type Tab = 'reservations' | 'vlan' | 'vc' | 'ip'
const TABS: { key: Tab; label: string }[] = [
  { key: 'reservations', label: 'Reservasi' }, { key: 'vlan', label: 'VLAN per perangkat' },
  { key: 'vc', label: 'VC id antar PE' }, { key: 'ip', label: 'Pool IPv4' },
]

function when(iso: string | null): string {
  return iso ? new Date(iso).toLocaleDateString('id-ID', { dateStyle: 'medium' }) : '-'
}

function UsageList({ usage }: { usage: Usage[] }) {
  return <>{usage.map((u, i) => (
    <div key={i} className="small"><span className="muted">{SOURCE_LABELS[u.source]}:</span> {u.detail}</div>
  ))}</>
}

function HostField({ label, devices, value, onChange }: {
  label: string; devices: DeviceOption[]; value: string; onChange: (text: string) => void
}) {
  const known = !value.trim() || devices.some((d) => d.hostname.toLowerCase() === value.trim().toLowerCase())
  return (
    <label>
      <span>{label}</span>
      <input list="registry-devices" value={value} placeholder="ketik hostname" onChange={(e) => onChange(e.target.value)} />
      {!known && <small className="text-bad">Hostname tidak ada di inventory.</small>}
    </label>
  )
}

function findDevice(devices: DeviceOption[], text: string): DeviceOption | null {
  return devices.find((d) => d.hostname.toLowerCase() === text.trim().toLowerCase()) ?? null
}

/** Reserve values with a label; shows the 409 text when something is taken. */
function ReserveForm({ build, hint, onDone }: {
  build: () => { kind: ResourceKind; device_id?: number; peer_device_id?: number; value?: number; prefix?: string }[] | string
  hint: string
  onDone: (text: string) => void
}) {
  const { can } = useAuth()
  const [label, setLabel] = useState('')
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null)
  if (!can('engineer')) return null
  async function submit(e: FormEvent) {
    e.preventDefault()
    const items = build()
    if (typeof items === 'string') { setMessage({ ok: false, text: items }); return }
    try {
      const r = await api.post<{ reservations: Reservation[] }>('/resources/reservations', { label, items })
      setMessage(null)
      onDone(`${r.reservations.length} nilai direservasi untuk ${label}.`)
    } catch (err) {
      setMessage({ ok: false, text: (err as Error).message })
    }
  }
  return (
    <form className="mop-meta reserve-form" onSubmit={submit}>
      <label className="span-2">
        <span>Reservasi untuk (pelanggan / circuit / keperluan)</span>
        <input required maxLength={120} value={label} onChange={(e) => setLabel(e.target.value)} placeholder="mis. PT CONTOH L2VPN BJM-BJN" />
        <small className="muted">{hint}</small>
      </label>
      <button className="primary" type="submit">Reservasi</button>
      {message && <div className={message.ok ? 'notice span-2' : 'error span-2'}>{message.text}</div>}
    </form>
  )
}

function ReservationsTab({ refresh }: { refresh: number }) {
  const { can } = useAuth()
  const [rows, setRows] = useState<Reservation[] | null>(null)
  const [kind, setKind] = useState('')
  const [status, setStatus] = useState('reserved')
  const [error, setError] = useState<string | null>(null)
  const load = useCallback(() => {
    const q = new URLSearchParams()
    if (kind) q.set('kind', kind)
    if (status) q.set('status', status)
    api.get<{ reservations: Reservation[] }>(`/resources/reservations?${q}`).then((r) => setRows(r.reservations))
      .catch((e: Error) => setError(e.message))
  }, [kind, status])
  useEffect(load, [load, refresh])
  async function release(id: number) {
    try { await api.post(`/resources/reservations/${id}/release`); load() } catch (e) { setError((e as Error).message) }
  }
  return (
    <>
      <div className="toolbar">
        <label className="small">Jenis{' '}
          <select value={kind} onChange={(e) => setKind(e.target.value)}>
            <option value="">Semua</option>
            {(Object.keys(KIND_LABELS) as ResourceKind[]).map((k) => <option key={k} value={k}>{KIND_LABELS[k]}</option>)}
          </select>
        </label>
        <label className="small">Status{' '}
          <select value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="">Semua</option>
            {Object.entries(STATUS_LABELS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </label>
      </div>
      {error && <div className="error">{error}</div>}
      <div className="table-wrap">
        <table>
          <thead><tr><th>Resource</th><th>Untuk</th><th>Status</th><th>Oleh</th><th>Berlaku s/d</th><th /></tr></thead>
          <tbody>
            {rows?.map((r) => (
              <tr key={r.id}>
                <td><b>{reservationTarget(r)}</b></td>
                <td>{r.label}{r.note && <div className="small muted">{r.note}</div>}</td>
                <td><span className={`pill ${STATUS_PILLS[r.status]}`}>{STATUS_LABELS[r.status]}</span></td>
                <td className="small">{r.created_by}<div className="muted">{when(r.created_at)} · {r.source}</div></td>
                <td className="small">{r.status === 'reserved' ? when(r.expires_at) : '-'}</td>
                <td>{r.status === 'reserved' && can('engineer') && <button className="small" onClick={() => release(r.id)}>Lepas</button>}</td>
              </tr>
            ))}
            {rows && !rows.length && <tr><td colSpan={6} className="empty">Tidak ada reservasi.</td></tr>}
          </tbody>
        </table>
      </div>
      <p className="hint">Reservasi berhenti menahan nilai saat dilepas, kedaluwarsa, atau nilainya sudah muncul di inventory/config RANCID (status "Sudah di config").</p>
    </>
  )
}

function VlanTab({ devices, onReserved }: { devices: DeviceOption[]; onReserved: () => void }) {
  const [host, setHost] = useState('')
  const [data, setData] = useState<{ hostname: string; vlans: { vlan: number; used_by: Usage[] }[] } | null>(null)
  const [next, setNext] = useState<number[]>([])
  const [wanted, setWanted] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const device = findDevice(devices, host)
  const load = useCallback(() => {
    setData(null); setNext([]); setError(null)
    if (!device) return
    api.get<typeof data>(`/resources/vlans?device_id=${device.id}`).then(setData).catch((e: Error) => setError(e.message))
    api.get<{ vlans: number[] }>(`/resources/vlans/next?device_id=${device.id}&count=10`).then((r) => setNext(r.vlans)).catch(() => undefined)
  }, [device?.id])
  useEffect(load, [load])
  return (
    <>
      <div className="mop-meta"><HostField label="Perangkat" devices={devices} value={host} onChange={(t) => { setHost(t); setNotice(null) }} /></div>
      {error && <div className="error">{error}</div>}
      {notice && <div className="notice">{notice}</div>}
      {data && (
        <>
          <p><b>{data.vlans.length}</b> VLAN terpakai di {data.hostname}. VLAN bebas terendah: <b>{next.join(', ') || '-'}</b></p>
          {device && (
            <>
              <label className="small">VLAN yang mau direservasi{' '}
                <input value={wanted} onChange={(e) => setWanted(e.target.value)} placeholder="mis. 1480-1482,1490" />
              </label>
              <ReserveForm hint={`Reservasi VLAN di ${data.hostname}.`} onDone={(text) => { setNotice(text); load(); onReserved() }}
                build={() => {
                  const vlans = parseNumberList(wanted)
                  return vlans ? vlans.map((value) => ({ kind: 'vlan' as const, device_id: device.id, value })) : 'Isi daftar VLAN yang valid (2–4094).'
                }} />
            </>
          )}
          <div className="table-wrap registry-list">
            <table>
              <thead><tr><th className="num">VLAN</th><th>Dipakai oleh</th></tr></thead>
              <tbody>{data.vlans.map((v) => <tr key={v.vlan}><td className="num">{v.vlan}</td><td><UsageList usage={v.used_by} /></td></tr>)}</tbody>
            </table>
          </div>
        </>
      )}
    </>
  )
}

interface VcData {
  device: string; peer_device: string; loopbacks: Record<string, string | null>
  vc_ids: { vc_id: number; used_by: Usage[] }[]; suggested: { wanted: number; vc_id: number }[]
}

function VcTab({ devices, onReserved }: { devices: DeviceOption[]; onReserved: () => void }) {
  const [hostA, setHostA] = useState('')
  const [hostB, setHostB] = useState('')
  const [wanted, setWanted] = useState('')
  const [data, setData] = useState<VcData | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const a = findDevice(devices, hostA)
  const b = findDevice(devices, hostB)
  const wantedList = parseNumberList(wanted, 4294967295) ?? []
  const load = useCallback(() => {
    setData(null); setError(null)
    if (!a || !b) return
    const q = new URLSearchParams({ device_id: String(a.id), peer_device_id: String(b.id) })
    for (const w of wantedList) q.append('wanted', String(w))
    api.get<VcData>(`/resources/vc-ids?${q}`).then(setData).catch((e: Error) => setError(e.message))
  }, [a?.id, b?.id, wantedList.join(',')])
  useEffect(load, [load])
  return (
    <>
      <div className="mop-meta">
        <HostField label="PE A" devices={devices} value={hostA} onChange={(t) => { setHostA(t); setNotice(null) }} />
        <HostField label="PE B" devices={devices} value={hostB} onChange={(t) => { setHostB(t); setNotice(null) }} />
        <label><span>VLAN / VC id yang diinginkan</span>
          <input value={wanted} onChange={(e) => setWanted(e.target.value)} placeholder="mis. 1480-1482" />
        </label>
      </div>
      {error && <div className="error">{error}</div>}
      {notice && <div className="notice">{notice}</div>}
      {data && (
        <>
          <p>
            <b>{data.vc_ids.length}</b> VC id terpakai antara {data.device} dan {data.peer_device}
            {' '}(loopback {Object.entries(data.loopbacks).map(([h, ip]) => `${h} ${ip ?? '?'}`).join(', ')}).
          </p>
          {data.suggested.length > 0 && (
            <p>Saran: {data.suggested.map((s) => s.wanted === s.vc_id ? `${s.vc_id}` : `${s.wanted} → ${s.vc_id}`).join(', ')}</p>
          )}
          {a && b && data.suggested.length > 0 && (
            <ReserveForm hint="Reservasi VC id hasil saran untuk pasangan PE ini." onDone={(text) => { setNotice(text); load(); onReserved() }}
              build={() => data.suggested.map((s) => ({ kind: 'vc_id' as const, device_id: a.id, peer_device_id: b.id, value: s.vc_id }))} />
          )}
          <div className="table-wrap registry-list">
            <table>
              <thead><tr><th className="num">VC id</th><th>Dipakai oleh</th></tr></thead>
              <tbody>{data.vc_ids.map((v) => <tr key={v.vc_id}><td className="num">{v.vc_id}</td><td><UsageList usage={v.used_by} /></td></tr>)}</tbody>
              {!data.vc_ids.length && <tbody><tr><td colSpan={2} className="empty">Belum ada VC id di config maupun reservasi untuk pasangan ini.</td></tr></tbody>}
            </table>
          </div>
          <p className="hint">VC id dari config dibaca saat sync gitweb; jalankan sync gitweb penuh sekali agar semua config terbaca.</p>
        </>
      )}
    </>
  )
}

function IpTab({ onReserved }: { onReserved: () => void }) {
  const { can } = useAuth()
  const [pools, setPools] = useState<IpPool[] | null>(null)
  const [open, setOpen] = useState<IpPool | null>(null)
  const [form, setForm] = useState({ name: '', cidr: '', purpose: 'p2p', default_prefix_len: '30' })
  const [alloc, setAlloc] = useState({ label: '', prefix_len: '' })
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null)
  const load = useCallback(() => {
    api.get<{ pools: IpPool[] }>('/resources/ip-pools').then((r) => setPools(r.pools)).catch((e: Error) => setMessage({ ok: false, text: e.message }))
  }, [])
  useEffect(load, [load])
  const openPool = (id: number) => api.get<IpPool>(`/resources/ip-pools/${id}`).then(setOpen).catch((e: Error) => setMessage({ ok: false, text: e.message }))
  async function createPool(e: FormEvent) {
    e.preventDefault()
    try {
      await api.post('/resources/ip-pools', { ...form, default_prefix_len: form.default_prefix_len ? Number(form.default_prefix_len) : null })
      setForm({ name: '', cidr: '', purpose: 'p2p', default_prefix_len: '30' }); setMessage({ ok: true, text: 'Pool dibuat.' }); load()
    } catch (err) { setMessage({ ok: false, text: (err as Error).message }) }
  }
  async function allocate(e: FormEvent) {
    e.preventDefault()
    if (!open) return
    try {
      const r = await api.post<Reservation>(`/resources/ip-pools/${open.id}/allocate`, {
        label: alloc.label, prefix_len: alloc.prefix_len ? Number(alloc.prefix_len) : null })
      setMessage({ ok: true, text: `${r.prefix} direservasi untuk ${r.label}.` }); setAlloc({ label: '', prefix_len: '' })
      openPool(open.id); load(); onReserved()
    } catch (err) { setMessage({ ok: false, text: (err as Error).message }) }
  }
  return (
    <>
      {message && <div className={message.ok ? 'notice' : 'error'}>{message.text}</div>}
      <div className="table-wrap">
        <table>
          <thead><tr><th>Pool</th><th>Blok</th><th>Fungsi</th><th className="num">Terpakai</th></tr></thead>
          <tbody>
            {pools?.map((p) => (
              <tr key={p.id} className={`clickable ${open?.id === p.id ? 'selected' : ''}`} onClick={() => openPool(p.id)}>
                <td><b>{p.name}</b>{p.note && <div className="small muted">{p.note}</div>}</td>
                <td>{p.cidr}{p.default_prefix_len && <span className="muted"> · /{p.default_prefix_len}</span>}</td>
                <td>{p.purpose}</td>
                <td className="num">{p.used_pct}%<div className="small muted">{p.used_addresses.toLocaleString('id-ID')} / {p.size.toLocaleString('id-ID')}</div></td>
              </tr>
            ))}
            {pools && !pools.length && <tr><td colSpan={4} className="empty">Belum ada pool. Tambahkan blok IP di bawah.</td></tr>}
          </tbody>
        </table>
      </div>
      {open && (
        <div className="card-inner">
          <h3>{open.name} — {open.cidr}</h3>
          {can('engineer') && (
            <form className="mop-meta" onSubmit={allocate}>
              <label className="span-2"><span>Alokasi prefix berikutnya untuk</span>
                <input required maxLength={120} value={alloc.label} onChange={(e) => setAlloc({ ...alloc, label: e.target.value })} placeholder="mis. link BJM-GBT ↔ BJN-BNG1" />
              </label>
              <label><span>Panjang prefix</span>
                <input type="number" min={8} max={32} value={alloc.prefix_len} placeholder={open.default_prefix_len ? `/${open.default_prefix_len}` : 'wajib'}
                  onChange={(e) => setAlloc({ ...alloc, prefix_len: e.target.value })} />
              </label>
              <button className="primary" type="submit">Alokasikan</button>
            </form>
          )}
          <div className="table-wrap registry-list">
            <table>
              <thead><tr><th>Prefix</th><th>Alamat</th><th>Sumber</th><th>Keterangan</th></tr></thead>
              <tbody>
                {open.usage?.map((u, i) => (
                  <tr key={i}><td>{u.prefix}</td><td>{u.address ?? '-'}</td><td>{SOURCE_LABELS[u.source as Usage['source']] ?? u.source}</td><td className="small">{u.detail}</td></tr>
                ))}
                {!open.usage?.length && <tr><td colSpan={4} className="empty">Belum ada alamat terpakai di pool ini.</td></tr>}
              </tbody>
            </table>
          </div>
        </div>
      )}
      {can('engineer') && (
        <form className="mop-meta" onSubmit={createPool}>
          <h3 className="span-2">Tambah pool</h3>
          <label><span>Nama</span><input required maxLength={120} value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="P2P Kalimantan" /></label>
          <label><span>Blok (CIDR)</span><input required value={form.cidr} onChange={(e) => setForm({ ...form, cidr: e.target.value })} placeholder="10.20.0.0/22" /></label>
          <label><span>Fungsi</span>
            <select value={form.purpose} onChange={(e) => setForm({ ...form, purpose: e.target.value })}>
              <option value="p2p">P2P link</option><option value="loopback">Loopback</option>
              <option value="customer">Pelanggan</option><option value="other">Lainnya</option>
            </select>
          </label>
          <label><span>Prefix default</span><input type="number" min={8} max={32} value={form.default_prefix_len} onChange={(e) => setForm({ ...form, default_prefix_len: e.target.value })} /></label>
          <button className="primary" type="submit">Simpan pool</button>
        </form>
      )}
      <p className="hint">Alamat terpakai dibaca dari interface dan loopback di inventory (Observium dan config RANCID) ditambah reservasi aktif. Alokasi memberi prefix bebas pertama yang tidak bertumpuk dengan semuanya.</p>
    </>
  )
}

export default function ResourceRegistry() {
  const [params, setParams] = useSearchParams()
  const tab = (TABS.some((t) => t.key === params.get('tab')) ? params.get('tab') : 'reservations') as Tab
  const [devices, setDevices] = useState<DeviceOption[]>([])
  const [refresh, setRefresh] = useState(0)
  useEffect(() => { api.get<DeviceOption[]>('/devices?limit=10000').then(setDevices).catch(() => undefined) }, [])
  const bump = () => setRefresh((n) => n + 1)
  return (
    <div className="capacity">
      <div className="card registry">
        <div className="toolbar"><h2>Resource Registry</h2></div>
        <p className="hint">Satu tempat untuk VLAN, VC id dan IPv4: yang sudah terpakai di jaringan (inventory dan config RANCID) dan yang direservasi untuk pekerjaan yang direncanakan.</p>
        <datalist id="registry-devices">{devices.map((d) => <option key={d.id} value={d.hostname}>{d.role}</option>)}</datalist>
        <div className="config-menu" role="tablist">
          {TABS.map((t) => <button key={t.key} type="button" role="tab" aria-selected={tab === t.key}
            className={tab === t.key ? 'active' : ''} onClick={() => setParams({ tab: t.key })}>{t.label}</button>)}
        </div>
        {tab === 'reservations' && <ReservationsTab refresh={refresh} />}
        {tab === 'vlan' && <VlanTab devices={devices} onReserved={bump} />}
        {tab === 'vc' && <VcTab devices={devices} onReserved={bump} />}
        {tab === 'ip' && <IpTab onReserved={bump} />}
      </div>
    </div>
  )
}
