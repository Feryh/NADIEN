import { useEffect, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api, CapacityColor, HotspotLevel } from '../api'
import { Pager, usePaged } from '../components/Pager'
import { ColorBadge, fmtG, Kpi } from '../components/ui'

// One device: port usage per speed, every port with the link behind it, and that link's capacity and utilization.

type PortState = 'used' | 'idle' | 'disabled'

interface PortLink {
  link_id: number
  peer_id: number | null
  peer: string | null
  peer_pop: string | null
  peer_port: string | null
  port: string | null
  status: string | null
  capacity_gbps: number | null
  current_gbps: number | null
  reserved_gbps: number | null
  utilization_pct: number | null
  committed_pct: number | null
  color: CapacityColor | null
  level: HotspotLevel | null
}

interface Port {
  id: number
  name: string
  if_type: string
  speed_gbps: number | null
  speed: string
  admin_status: string
  oper_status: string
  state: PortState
  description: string | null
  optic_type: string | null
  pon: boolean
  onts_online: number | null
  pon_occupancy_pct: number | null
  pon_level: HotspotLevel | null
  in_gbps: number | null
  out_gbps: number | null
  /** Busier direction of the last synced rate against the port speed. */
  utilization_pct: number | null
  utilization_level: HotspotLevel | null
  link: PortLink | null
}

interface DeviceCapacity {
  device: {
    id: number
    hostname: string
    role: string
    pop: string | null
    vendor: string | null
    model: string | null
    mgmt_ip: string | null
    loopback_ipv4: string | null
    status: string
  }
  thresholds: { warning_pct: number; critical_pct: number }
  speeds: { speed: string; total: number; used: number; idle: number; disabled: number }[]
  pon: {
    total: number
    used: number
    onts_online: number
    max_onts: number
    busiest_onts: number
    busiest_pct: number | null
    hot_ports: number
    level: HotspotLevel | null
  } | null
  /** OLT front panel: uplink port ids, then PON port ids per slot in port order. */
  olt_layout: { uplinks: number[]; slots: { slot: number; label: string; ports: number[] }[] } | null
  links: PortLink[]
  ports: Port[]
}

const STATE_LABEL: Record<PortState, string> = { used: 'Terpakai', idle: 'Idle', disabled: 'Disabled' }
const STATE_PILL: Record<PortState, string> = { used: 'pill-ok', idle: 'pill-warn', disabled: 'pill-muted' }
const LEVEL_CLASS: Record<HotspotLevel, string> = { critical: 'pill-bad', warning: 'pill-warn', normal: 'pill-ok', suspect: 'pill-muted' }
const n = (v: number) => v.toLocaleString('id-ID')

export function Utilization({ link }: { link: Pick<PortLink, 'utilization_pct' | 'level' | 'capacity_gbps'> }) {
  if (link.utilization_pct == null || !link.capacity_gbps) return <span className="muted">–</span>
  const level = link.level ?? 'normal'
  return <span className={`pill ${LEVEL_CLASS[level]}`} title={level === 'suspect' ? 'Kapasitas link kemungkinan salah' : undefined}>
    {link.utilization_pct.toFixed(1)}%
  </span>
}

function Pct({ value, level, title }: { value: number | null; level: HotspotLevel | null; title?: string }) {
  if (value == null || !level) return <span className="muted" title={title}>–</span>
  return <span className={`pill ${LEVEL_CLASS[level]}`} title={title}>{value.toFixed(0)}%</span>
}

/** Traffic of a port: the link's current traffic when it has one, else the port's own synced rate. */
function traffic(p: Port): number | null {
  if (p.link?.current_gbps != null) return p.link.current_gbps
  return p.in_gbps == null && p.out_gbps == null ? null : Math.max(p.in_gbps ?? 0, p.out_gbps ?? 0)
}

function OltPanel({ layout, ports }: { layout: NonNullable<DeviceCapacity['olt_layout']>; ports: Port[] }) {
  const byId = new Map(ports.map((p) => [p.id, p]))
  const [allUplinks, setAllUplinks] = useState(false)
  const uplinks = layout.uplinks.map((id) => byId.get(id)).filter((p): p is Port => !!p)
  const shownUplinks = allUplinks ? uplinks : uplinks.filter((p) => p.link || p.state === 'used')
  return (
    <div className="card olt-panel">
      <div className="toolbar">
        <h2>Uplink card</h2>
        <span className="muted small">trafik / kapasitas port</span>
        <div className="spacer" />
        {uplinks.length > shownUplinks.length || allUplinks
          ? <label className="check"><input type="checkbox" checked={allUplinks} onChange={(e) => setAllUplinks(e.target.checked)} /> Tampilkan port uplink idle/disabled ({uplinks.length})</label>
          : null}
      </div>
      <div className="olt-uplinks">
        {shownUplinks.map((p) => (
          <div key={p.id} className={`olt-uplink ${p.state}`}>
            <span className="olt-uplink-name"><code>{p.name}</code> ({fmtG(traffic(p))}/{fmtG(p.link?.capacity_gbps ?? p.speed_gbps)})</span>
            <span className="olt-uplink-desc small">{p.link?.peer ? <>ke <Link to={`/devices/${p.link.peer_id}`}>{p.link.peer}</Link>{p.link.peer_port ? ` · ${p.link.peer_port}` : ''}</> : p.description ?? ''}</span>
            <span>{p.link ? <Utilization link={p.link} /> : <Pct value={p.utilization_pct} level={p.utilization_level} />}</span>
          </div>
        ))}
        {!shownUplinks.length && <span className="muted">Tidak ada port uplink yang aktif</span>}
      </div>

      <h2>Port GPON per slot</h2>
      <p className="hint">Tiap port: <b>PON</b> = ONT online terhadap kapasitas split · <b>BW</b> = trafik terhadap kecepatan port (dari sync utilisasi terakhir).</p>
      <div className="olt-slots">
        {layout.slots.map((slot) => (
          <div key={slot.slot} className="olt-slot">
            <div className="olt-slot-head">{slot.label}</div>
            {slot.ports.map((id) => {
              const p = byId.get(id)
              if (!p) return null
              // Slot1/Port10 and Dasan port10-FX-1000 both carry the port number after "port".
              const number = p.name.match(/port(\d+)/i)?.[1] ?? p.name
              return (
                <div key={id} className={`olt-pon ${p.state}`} title={`${p.name} · ${p.description ?? ''}`}>
                  <span className="olt-pon-name">GPON {number}</span>
                  <span className="olt-pon-metric"><span className="muted">PON</span> <Pct value={p.pon_occupancy_pct} level={p.pon_level} title={`${p.onts_online ?? 0} ONT online`} /></span>
                  <span className="olt-pon-metric"><span className="muted">BW</span> <Pct value={p.utilization_pct} level={p.utilization_level}
                    title={p.utilization_pct == null ? 'Belum ada data trafik: jalankan sync utilisasi Observium' : `${fmtG(traffic(p))} / ${fmtG(p.speed_gbps)}`} /></span>
                </div>
              )
            })}
          </div>
        ))}
        {!layout.slots.length && <span className="muted">Port PON tidak dikenali di perangkat ini</span>}
      </div>
    </div>
  )
}

export default function DeviceDetail() {
  const { deviceId } = useParams()
  const [data, setData] = useState<DeviceCapacity | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [state, setState] = useState<PortState | ''>('')
  const [speed, setSpeed] = useState('')
  const [onlyLinked, setOnlyLinked] = useState(false)
  const [q, setQ] = useState('')

  useEffect(() => {
    setData(null)
    setError(null)
    api.get<DeviceCapacity>(`/capacity/devices/${deviceId}`).then(setData).catch((e: Error) => setError(e.message))
  }, [deviceId])

  const ports = useMemo(() => {
    const needle = q.trim().toLowerCase()
    return (data?.ports ?? []).filter((p) => (!state || p.state === state) && (!speed || p.speed === speed)
      && (!onlyLinked || p.link) && (!needle || `${p.name} ${p.description ?? ''} ${p.link?.peer ?? ''}`.toLowerCase().includes(needle)))
  }, [data, state, speed, onlyLinked, q])
  const pagedPorts = usePaged(ports, `${deviceId}|${state}|${speed}|${onlyLinked}|${q}`)
  const pagedLinks = usePaged(data?.links ?? [], deviceId)

  if (error) return <div className="card"><div className="error">{error}</div><Link to="/ports">← Kapasitas Port</Link></div>
  if (!data) return <div className="card">Memuat…</div>
  const { device, pon, links } = data
  const totals = data.speeds.reduce((t, s) => ({ total: t.total + s.total, used: t.used + s.used, idle: t.idle + s.idle,
    disabled: t.disabled + s.disabled }), { total: 0, used: 0, idle: 0, disabled: 0 })
  const hotLinks = links.filter((l) => l.level === 'warning' || l.level === 'critical').length

  return (
    <div className="capacity device-detail">
      <div className="card">
        <div className="toolbar">
          <h2>{device.hostname}</h2>
          <span className="pill pill-muted">{device.role.toUpperCase()}</span>
          <span className="muted small">
            POP {device.pop ?? '–'} · {device.vendor ?? '–'}{device.model ? ` ${device.model}` : ''}
            {device.mgmt_ip ? ` · mgmt ${device.mgmt_ip}` : ''}{device.loopback_ipv4 ? ` · loopback ${device.loopback_ipv4}` : ''}
          </span>
          <div className="spacer" />
          <Link className="button" to={`/topology?level=neighbors&device_id=${device.id}`}>Lihat di topologi</Link>
        </div>
        <div className="kpis">
          <Kpi label="Port fisik" value={n(totals.total)} sub={`${n(totals.disabled)} disabled`} />
          <Kpi label="Terpakai" value={n(totals.used)} sub={totals.total ? `${(totals.used / totals.total * 100).toFixed(1)}%` : undefined} />
          <Kpi label="Idle" value={n(totals.idle)} sub="siap dipakai" />
          <Kpi label="Link" value={n(links.length)} sub={`${hotLinks} ≥ ${data.thresholds.warning_pct}% utilisasi`} />
          {pon && (pon.busiest_pct != null && pon.level
            ? <Kpi label="Port GPON terpadat" value={<span className={`pill ${LEVEL_CLASS[pon.level]}`}>{pon.busiest_pct.toFixed(1)}%</span>}
              sub={`${n(pon.busiest_onts)}/${n(pon.max_onts)} ONT · ${n(pon.hot_ports)} dari ${n(pon.total)} PON ≥ ${data.thresholds.warning_pct}% · ${n(pon.onts_online)} ONT online`} />
            : <Kpi label="Port GPON" value={n(pon.total)} sub="OLT ini tidak melaporkan jumlah ONT" />)}
        </div>
      </div>

      {data.olt_layout && <OltPanel layout={data.olt_layout} ports={data.ports} />}

      <div className="grid-2">
        <div className="card">
          <h2>Port per kapasitas</h2>
          <table>
            <thead><tr><th>Speed</th><th className="num">Total</th><th className="num">Terpakai</th><th className="num">Idle</th><th className="num">Disabled</th></tr></thead>
            <tbody>
              {data.speeds.map((s) => (
                <tr key={s.speed} className={data.olt_layout?.slots.length ? undefined : `clickable ${speed === s.speed ? 'selected' : ''}`}
                  onClick={data.olt_layout?.slots.length ? undefined : () => setSpeed(speed === s.speed ? '' : s.speed)}>
                  <td><b>{s.speed}</b></td><td className="num">{n(s.total)}</td><td className="num">{n(s.used)}</td>
                  <td className="num">{n(s.idle)}</td><td className="num muted">{n(s.disabled)}</td>
                </tr>
              ))}
              {!data.speeds.length && <tr><td colSpan={5} className="empty">Belum ada port fisik</td></tr>}
            </tbody>
          </table>
          {!data.olt_layout?.slots.length && <p className="hint">Klik baris speed untuk memfilter daftar port.</p>}
        </div>

        <div className="card">
          <h2>Link & utilisasi</h2>
          <div className="table-wrap">
            <table>
              <thead><tr><th>Port</th><th>Lawan</th><th className="num">Kapasitas</th><th className="num">Trafik</th><th>Utilisasi</th></tr></thead>
              <tbody>
                {pagedLinks.pageRows.map((l) => (
                  <tr key={l.link_id}>
                    <td><code>{l.port ?? '–'}</code>{l.status === 'down' && <> <span className="pill pill-bad">down</span></>}</td>
                    <td>{l.peer_id != null ? <Link to={`/devices/${l.peer_id}`}>{l.peer}</Link> : '–'}
                      {l.peer_port && <span className="muted small"> · {l.peer_port}</span>}</td>
                    <td className="num">{fmtG(l.capacity_gbps)}</td>
                    <td className="num">{fmtG(l.current_gbps)}</td>
                    <td><Utilization link={l} /> {l.color && <ColorBadge color={l.color}>{l.committed_pct?.toFixed(0)}% committed</ColorBadge>}</td>
                  </tr>
                ))}
                {!links.length && <tr><td colSpan={5} className="empty">Perangkat ini belum punya link</td></tr>}
              </tbody>
            </table>
          </div>
          <Pager paged={pagedLinks} unit="link" />
        </div>
      </div>

      {/* An OLT laid out per slot already shows every port: the flat list would only repeat it. */}
      {!data.olt_layout?.slots.length && <div className="card">
        <div className="toolbar">
          <h2>Port {speed && <span className="pill pill-muted">{speed}</span>}</h2>
          <span className="muted small">{n(ports.length)} dari {n(data.ports.length)} interface</span>
          <div className="spacer" />
          <input className="search" placeholder="Cari port, deskripsi, lawan…" value={q} onChange={(e) => setQ(e.target.value)} />
          <select value={state} onChange={(e) => setState(e.target.value as PortState | '')}>
            <option value="">Semua status</option>
            <option value="used">Terpakai</option>
            <option value="idle">Idle</option>
            <option value="disabled">Disabled</option>
          </select>
          <label className="check"><input type="checkbox" checked={onlyLinked} onChange={(e) => setOnlyLinked(e.target.checked)} /> Hanya port ber-link</label>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr><th>Port</th><th>Tipe</th><th>Speed</th><th>Status</th><th>Deskripsi</th><th>Lawan</th>
                <th className="num">Kapasitas link</th><th className="num">Trafik</th><th>Utilisasi</th></tr>
            </thead>
            <tbody>
              {pagedPorts.pageRows.map((p) => (
                <tr key={p.id}>
                  <td><code>{p.name}</code>{p.pon && <> <span className="pill pill-muted">PON</span></>}</td>
                  <td className="muted">{p.if_type}</td>
                  <td>{p.speed}</td>
                  <td><span className={`pill ${STATE_PILL[p.state]}`}>{STATE_LABEL[p.state]}</span></td>
                  <td className="small">{p.pon
                    ? <>{n(p.onts_online ?? 0)} ONT online{p.pon_occupancy_pct != null && p.pon_level && <> · <span className={`pill ${LEVEL_CLASS[p.pon_level]}`}>{p.pon_occupancy_pct.toFixed(0)}%</span></>}</>
                    : p.description ?? ''}</td>
                  <td>{p.link?.peer_id != null ? <Link to={`/devices/${p.link.peer_id}`}>{p.link.peer}</Link> : ''}
                    {p.link?.peer_port && <span className="muted small"> · {p.link.peer_port}</span>}</td>
                  <td className="num">{p.link ? fmtG(p.link.capacity_gbps) : ''}</td>
                  <td className="num">{p.link ? fmtG(p.link.current_gbps) : ''}</td>
                  <td>{p.link && <Utilization link={p.link} />}</td>
                </tr>
              ))}
              {!ports.length && <tr><td colSpan={9} className="empty">Tidak ada port untuk filter ini</td></tr>}
            </tbody>
          </table>
        </div>
        <Pager paged={pagedPorts} unit="interface" />
      </div>}
    </div>
  )
}
