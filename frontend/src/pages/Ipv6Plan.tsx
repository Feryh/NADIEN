import { ChangeEvent, useCallback, useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { api } from '../api'
import { useAuth } from '../auth'
import {
  bandUse, CityCapacity, hexIndex, METHOD_LABEL, OamRow, PopRow, RouterRow, STATUS_LABEL, STATUS_PILL, Summary,
} from './ipv6Model'

// IPv6 / SRv6 plan: the backbone team's register applied to the NADIEN inventory.

type Tab = 'summary' | 'routers' | 'oam' | 'pops'
const TABS: { key: Tab; label: string }[] = [
  { key: 'summary', label: 'Ringkasan & kapasitas' }, { key: 'routers', label: 'Node locator' },
  { key: 'oam', label: 'OAM L2' }, { key: 'pops', label: 'Mapping POP' },
]

async function uploadRegister(file: File): Promise<{ cities: number; created: number; updated: number; not_in_file: string[] }> {
  const res = await fetch(`/api/ipv6-plan/cities/import?filename=${encodeURIComponent(file.name)}`, {
    method: 'POST', credentials: 'same-origin', body: await file.arrayBuffer(),
  })
  const data = await res.json()
  if (!res.ok) throw new Error(`${res.status} — ${typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail)}`)
  return data
}

function SummaryTab({ refresh, onChanged }: { refresh: number; onChanged: () => void }) {
  const { can } = useAuth()
  const [data, setData] = useState<{ summary: Summary; capacity: CityCapacity[] } | null>(null)
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null)
  const [onlyUsed, setOnlyUsed] = useState(true)
  useEffect(() => {
    api.get<{ summary: Summary; capacity: CityCapacity[] }>('/ipv6-plan/summary').then(setData)
      .catch((e: Error) => setMessage({ ok: false, text: e.message }))
  }, [refresh])
  async function onFile(e: ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0]
    e.target.value = ''
    if (!file) return
    try {
      const r = await uploadRegister(file)
      setMessage({ ok: true, text: `Register diimport: ${r.cities} kota (${r.created} baru, ${r.updated} diperbarui)${r.not_in_file.length ? `; tidak ada di file: ${r.not_in_file.join(', ')}` : ''}.` })
      onChanged()
    } catch (err) { setMessage({ ok: false, text: (err as Error).message }) }
  }
  async function save() {
    try {
      const r = await api.post<{ written: number; dropped: number; total: number }>('/ipv6-plan/save')
      setMessage({ ok: true, text: `Rencana disimpan: ${r.total} alokasi (${r.written} ditulis, ${r.dropped} dihapus).` })
      onChanged()
    } catch (err) { setMessage({ ok: false, text: (err as Error).message }) }
  }
  const s = data?.summary
  const rows = (data?.capacity ?? []).filter((c) => !onlyUsed || c.backbone + c.access + c.oam > 0)
  return (
    <>
      <div className="toolbar">
        {can('engineer') && (
          <label className="button">Import register (.xlsx)
            <input type="file" accept=".xlsx" hidden onChange={onFile} />
          </label>
        )}
        {can('engineer') && s && s.cities > 0 && <button className="primary" onClick={save} disabled={!s.unsaved}>Simpan rencana ({s.unsaved} baru)</button>}
        {s && s.cities > 0 && <a className="button" href="/api/ipv6-plan/export">Export Excel</a>}
      </div>
      {message && <div className={message.ok ? 'notice' : 'error'}>{message.text}</div>}
      {s && s.cities === 0 && (
        <p className="hint">Belum ada register. Import file register IPv6 dari tim backbone (sheet "City Register"), lalu NADIEN memetakan POP dan perangkat ke kota, node locator dan alamat OAM.</p>
      )}
      {s && s.cities > 0 && (
        <>
          <div className="kpis ipv6-kpis">
            <div><b>{s.routers.toLocaleString('id-ID')}</b><span>router dengan node locator ({s.backbone} backbone · {s.access} akses)</span></div>
            <div><b>{s.oam.toLocaleString('id-ID')}</b><span>perangkat L2 dengan alamat OAM</span></div>
            <div className={s.unmapped_devices ? 'warn' : ''}><b>{s.unmapped_devices.toLocaleString('id-ID')}</b><span>perangkat di {s.unmapped_pops} POP yang belum ada di register</span></div>
            <div className={s.overflow ? 'bad' : ''}><b>{s.overflow}</b><span>router tanpa locator (band penuh)</span></div>
            <div className={s.promote.length ? 'bad' : ''}><b>{s.promote.length}</b><span>kota perlu blok kedua (aturan 50%){s.promote.length ? `: ${s.promote.join(', ')}` : ''}</span></div>
            <div><b>{s.code_conflicts}</b><span>POP dengan kode kota berbeda dari register</span></div>
          </div>
          <div className="toolbar">
            <h3>Kapasitas per kota</h3>
            <div className="spacer" />
            <label className="check small"><input type="checkbox" checked={onlyUsed} onChange={(e) => setOnlyUsed(e.target.checked)} /> hanya kota yang punya perangkat</label>
          </div>
          <div className="table-wrap ipv6-table">
            <table>
              <thead><tr><th>Kota</th><th>Tier</th><th>Index</th><th>Blok infra</th><th>Backbone</th><th>Akses</th><th>Aturan 50%</th><th className="num">OAM</th></tr></thead>
              <tbody>
                {rows.map((c) => (
                  <tr key={c.code}>
                    <td><b>{c.code}</b> {c.name}<div className="small muted">{c.region}</div></td>
                    <td>{c.tier}</td>
                    <td>{c.city_index}</td>
                    <td className="small">{c.infra_block}</td>
                    <td className={c.backbone_fit !== 'OK' ? 'text-bad' : ''}>{bandUse(c.backbone, c.backbone_size)}</td>
                    <td>{bandUse(c.access, c.access_size)}</td>
                    <td>{c.fit_50 === 'OK' ? <span className="pill pill-ok">OK</span> : <span className="pill pill-bad">Perlu blok kedua</span>}</td>
                    <td className="num">{c.oam}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="hint">Node index dan alamat OAM baru berstatus "Baru" sampai disimpan; setelah disimpan, alokasi tidak bergeser saat perangkat baru masuk. Pembagian band backbone per fungsi dan layout OAM masih provisional (sheet 03A register belum ada).</p>
        </>
      )}
    </>
  )
}

function useCities(): { code: string; name: string }[] {
  const [cities, setCities] = useState<{ code: string; name: string }[]>([])
  useEffect(() => { api.get<{ cities: { code: string; name: string }[] }>('/ipv6-plan/cities').then((r) => setCities(r.cities)).catch(() => undefined) }, [])
  return cities
}

function Filters({ city, setCity, q, setQ, children }: {
  city: string; setCity: (v: string) => void; q: string; setQ: (v: string) => void; children?: React.ReactNode
}) {
  const cities = useCities()
  return (
    <div className="toolbar">
      <label className="small">Kota{' '}
        <select value={city} onChange={(e) => setCity(e.target.value)}>
          <option value="">Semua</option>
          {cities.map((c) => <option key={c.code} value={c.code}>{c.code} {c.name}</option>)}
        </select>
      </label>
      {children}
      <input value={q} placeholder="cari hostname" onChange={(e) => setQ(e.target.value)} />
    </div>
  )
}

function RoutersTab({ refresh }: { refresh: number }) {
  const { can } = useAuth()
  const [city, setCity] = useState('')
  const [band, setBand] = useState('')
  const [q, setQ] = useState('')
  const [rows, setRows] = useState<RouterRow[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const load = useCallback(() => {
    const p = new URLSearchParams()
    if (city) p.set('city', city)
    if (band) p.set('band', band)
    api.get<{ routers: RouterRow[] }>(`/ipv6-plan/routers?${p}`).then((r) => setRows(r.routers)).catch((e: Error) => setError(e.message))
  }, [city, band])
  useEffect(load, [load, refresh])
  const shown = useMemo(() => (rows ?? []).filter((r) => !q || r.hostname.toLowerCase().includes(q.toLowerCase())), [rows, q])
  async function confirm(r: RouterRow) {
    try { await api.patch(`/ipv6-plan/assignments/${r.device_id}`, { status: r.status === 'confirmed' ? 'proposed' : 'confirmed' }); load() }
    catch (e) { setError((e as Error).message) }
  }
  return (
    <>
      <Filters city={city} setCity={setCity} q={q} setQ={setQ}>
        <label className="small">Band{' '}
          <select value={band} onChange={(e) => setBand(e.target.value)}>
            <option value="">Semua</option><option value="backbone">Backbone</option><option value="access">Akses</option><option value="overflow">Tanpa locator</option>
          </select>
        </label>
      </Filters>
      {error && <div className="error">{error}</div>}
      <p className="small muted">{shown.length.toLocaleString('id-ID')} router</p>
      <div className="table-wrap ipv6-table">
        <table>
          <thead><tr><th>Hostname</th><th>Kota</th><th>Band</th><th>Index</th><th>Node locator</th><th>Loopback IPv6</th><th>End SID</th><th>IS-IS</th><th>Status</th><th /></tr></thead>
          <tbody>
            {shown.slice(0, 1000).map((r) => (
              <tr key={r.device_id} className={r.band === 'overflow' ? 'row-bad' : undefined}>
                <td><b>{r.hostname}</b><div className="small muted">{r.role} · {r.vendor ?? '-'} · {r.pop_code ?? '-'}</div></td>
                <td>{r.city}<div className="small muted">{r.how}</div></td>
                <td>{r.band}<div className="small muted">{r.group}</div></td>
                <td>{hexIndex(r.node_index)}</td>
                <td className="mono">{r.locator ?? '-'}</td>
                <td className="mono small">{r.loopback6 ?? '-'}</td>
                <td className="mono small">{r.end_sid ?? '-'}</td>
                <td className="small">{r.isis_area ?? '-'}</td>
                <td><span className={`pill ${STATUS_PILL[r.status]}`}>{STATUS_LABEL[r.status]}</span>{r.note && <div className="small text-bad">{r.note}</div>}</td>
                <td>{can('engineer') && r.status !== 'baru' && <button className="small" onClick={() => confirm(r)}>{r.status === 'confirmed' ? 'Batal konfirmasi' : 'Konfirmasi'}</button>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {shown.length > 1000 && <p className="hint">Menampilkan 1.000 pertama; persempit dengan filter kota, atau pakai Export Excel.</p>}
    </>
  )
}

function OamTab({ refresh }: { refresh: number }) {
  const [city, setCity] = useState('')
  const [q, setQ] = useState('')
  const [rows, setRows] = useState<OamRow[] | null>(null)
  useEffect(() => {
    api.get<{ oam: OamRow[] }>(`/ipv6-plan/oam${city ? `?city=${encodeURIComponent(city)}` : ''}`).then((r) => setRows(r.oam)).catch(() => setRows([]))
  }, [city, refresh])
  const shown = (rows ?? []).filter((r) => !q || r.hostname.toLowerCase().includes(q.toLowerCase()))
  return (
    <>
      <Filters city={city} setCity={setCity} q={q} setQ={setQ} />
      <p className="small muted">{shown.length.toLocaleString('id-ID')} perangkat</p>
      <div className="table-wrap ipv6-table">
        <table>
          <thead><tr><th>Hostname</th><th>Kota</th><th>Kelompok</th><th>Subnet /64</th><th>Alamat OAM</th><th>Status</th></tr></thead>
          <tbody>
            {shown.slice(0, 1000).map((r) => (
              <tr key={r.device_id}>
                <td><b>{r.hostname}</b><div className="small muted">{r.role} · {r.vendor ?? '-'}</div></td>
                <td>{r.city}</td><td>{r.group}</td><td className="mono small">{r.subnet}</td><td className="mono">{r.address}</td>
                <td><span className={`pill ${STATUS_PILL[r.status]}`}>{STATUS_LABEL[r.status]}</span></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="hint">Layout OAM /48 provisional: :1::/64 OLT, :2::/64 switch, :3::/64 DCI, :4::/64 router non-SRv6 / manajemen.</p>
    </>
  )
}

function PopsTab({ refresh, onChanged }: { refresh: number; onChanged: () => void }) {
  const { can } = useAuth()
  const cities = useCities()
  const [onlyUnmapped, setOnlyUnmapped] = useState(true)
  const [rows, setRows] = useState<PopRow[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const load = useCallback(() => {
    api.get<{ pops: PopRow[] }>(`/ipv6-plan/pops?only_unmapped=${onlyUnmapped}`).then((r) => setRows(r.pops)).catch((e: Error) => setError(e.message))
  }, [onlyUnmapped])
  useEffect(load, [load, refresh])
  async function decide(p: PopRow, value: string) {
    try {
      if (value === '') await api.del(`/ipv6-plan/pops/${p.pop_id}`)
      else if (value === 'ignore' || value === 'review') await api.put(`/ipv6-plan/pops/${p.pop_id}`, { decision: value })
      else await api.put(`/ipv6-plan/pops/${p.pop_id}`, { decision: 'parent', city_code: value })
      load(); onChanged()
    } catch (e) { setError((e as Error).message) }
  }
  return (
    <>
      <div className="toolbar">
        <label className="check small"><input type="checkbox" checked={onlyUnmapped} onChange={(e) => setOnlyUnmapped(e.target.checked)} /> hanya POP yang belum ada di register</label>
      </div>
      {error && <div className="error">{error}</div>}
      <div className="table-wrap ipv6-table">
        <table>
          <thead><tr><th>POP</th><th className="num">Perangkat</th><th>Kota register</th><th>Dasar</th><th>Usulan kota induk</th><th>Keputusan</th></tr></thead>
          <tbody>
            {rows?.map((p) => (
              <tr key={p.pop_id}>
                <td><b>{p.pop_code}</b> {p.pop_name !== p.pop_code ? p.pop_name : ''}<div className="small muted">{p.area ?? '-'} · {p.region ?? '?'}</div></td>
                <td className="num">{p.devices}</td>
                <td>{p.city ?? '-'}</td>
                <td className="small">{METHOD_LABEL[p.method] ?? p.method}{p.note && <div className="muted">{p.note}</div>}</td>
                <td>{p.suggested_city ? `${p.suggested_city} (${p.suggested_km} km)` : '-'}</td>
                <td>
                  {can('engineer') ? (
                    <select value={p.decision === 'ignore' || p.decision === 'review' ? p.decision : p.method === 'manual' ? p.city ?? '' : ''}
                      onChange={(e) => decide(p, e.target.value)}>
                      <option value="">Otomatis</option>
                      <option value="review">Perlu dicek</option>
                      <option value="ignore">Abaikan (bukan site)</option>
                      <optgroup label="Masuk kota register">
                        {cities.map((c) => <option key={c.code} value={c.code}>{c.code} {c.name}</option>)}
                      </optgroup>
                    </select>
                  ) : p.decision ?? '-'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="hint">Keputusan di sini menimpa pencocokan otomatis (kode sama, alias kode yang berbeda, atau nama kota di nama POP). Kota yang benar-benar baru tetap perlu ditambahkan tim backbone ke register lalu diimport ulang.</p>
    </>
  )
}

export default function Ipv6Plan() {
  const [params, setParams] = useSearchParams()
  const tab = (TABS.some((t) => t.key === params.get('tab')) ? params.get('tab') : 'summary') as Tab
  const [refresh, setRefresh] = useState(0)
  const bump = () => setRefresh((n) => n + 1)
  return (
    <div className="capacity">
      <div className="card ipv6-plan">
        <div className="toolbar"><h2>IPv6 / SRv6 Plan</h2></div>
        <p className="hint">Register alokasi IPv6 tim backbone diterapkan ke inventory NADIEN: kota per POP, node locator /48 per router (loopback, End SID, area IS-IS), alamat OAM untuk OLT dan switch, dan kapasitas tiap kota.</p>
        <div className="config-menu" role="tablist">
          {TABS.map((t) => <button key={t.key} type="button" role="tab" aria-selected={tab === t.key}
            className={tab === t.key ? 'active' : ''} onClick={() => setParams({ tab: t.key })}>{t.label}</button>)}
        </div>
        {tab === 'summary' && <SummaryTab refresh={refresh} onChanged={bump} />}
        {tab === 'routers' && <RoutersTab refresh={refresh} />}
        {tab === 'oam' && <OamTab refresh={refresh} />}
        {tab === 'pops' && <PopsTab refresh={refresh} onChanged={bump} />}
      </div>
    </div>
  )
}
