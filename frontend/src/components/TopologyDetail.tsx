import { Link as RouterLink } from 'react-router-dom'
import { bundleOf, clusterSummary, formatLastPoll, formatUptime, linkEnd } from './topology/cityDetail'
import { LAYER_LABEL, type CityFilter } from './topology/cityFilters'
import type { CityDeviceNode, CityLink, CityView } from './topology/types'
import { fmtG } from './ui'

const SEGMENT_NAME: Record<string, string> = { backbone: 'Backbone', distribution: 'Distribusi', last_mile: 'Last Mile' }

/** The node as it was when tapped; only identity is trusted, the details are looked up in the current view. */
export interface TappedNode {
  id: string
  kind: string
  label: string
}

export interface DrillAction {
  label: string
  run: () => void
}

interface Props {
  view: CityView
  node?: TappedNode
  linkId?: number
  drill: DrillAction | null
  onClose: () => void
}

function DeviceDetail({ device, view }: { device: CityDeviceNode; view: CityView }) {
  return (
    <dl>
      <dt>Hostname</dt><dd>{device.label}</dd>
      <dt>Mgmt IP</dt><dd>{device.mgmt_ip ?? '–'}</dd>
      <dt>Kota · POP</dt><dd>{[device.city, device.pop].filter(Boolean).join(' · ') || '–'}</dd>
      <dt>Role</dt><dd>{device.role.toUpperCase()}</dd>
      <dt>Vendor</dt><dd>{device.vendor ?? '–'}</dd>
      <dt>Reachability</dt><dd>{device.reachability}</dd>
      <dt>Uptime</dt><dd>{formatUptime(device.uptime_s)}</dd>
      <dt>Last poll</dt><dd>{formatLastPoll(device.last_polled_at)}</dd>
      <dt>SPOF</dt><dd>{device.spof ? 'Ya' : 'Tidak'} <span className="muted">(berdasarkan link yang diketahui)</span></dd>
      <dt>Cluster</dt><dd>{clusterSummary(view.clusters, device.cluster_id)}</dd>
    </dl>
  )
}

function BundleTable({ links, clickedId }: { links: CityLink[]; clickedId: number }) {
  return (
    <div className="table-wrap">
      <table className="bundle-table">
        <thead>
          <tr><th>A</th><th>B</th><th>Kapasitas</th><th>Trafik</th><th>Util %</th><th>Committed %</th><th>Status</th><th title="Berdasarkan link yang diketahui">SPOF</th></tr>
        </thead>
        <tbody>
          {links.map((l) => (
            <tr key={l.id} className={l.id === clickedId ? 'clicked' : ''} aria-current={l.id === clickedId ? 'true' : undefined}>
              <td>{linkEnd(l.a_device, l.a_interface)}</td>
              <td>{linkEnd(l.b_device, l.b_interface)}</td>
              <td>{l.capacity_gbps > 0 ? fmtG(l.capacity_gbps) : '?'}</td>
              <td>{fmtG(l.current_gbps)}</td>
              <td>{l.utilization_pct}</td>
              <td>{l.committed_pct}</td>
              <td>{l.status}</td>
              <td>{l.spof ? 'SPOF' : '–'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/** Detail panel of the city / neighbours levels; always derived from `view`, so live refreshes update it. */
export default function TopologyDetail({ view, node, linkId, drill, onClose }: Props) {
  const closeButton = <button className="panel-close" onClick={onClose} aria-label="Tutup">×</button>

  if (linkId != null) {
    const bundle = bundleOf(view.links, linkId)
    const first = bundle.find((l) => l.id === linkId) ?? bundle[0]
    const nameOf = (id: string) => view.nodes.find((n) => n.id === id)?.label ?? id
    return (
      <aside className="detail-panel wide card">
        {closeButton}
        <div className="detail">
          {first ? (
            <>
              <h3>{nameOf(first.source)} ↔ {nameOf(first.target)}</h3>
              <p className="small muted">{bundle.length} link · {SEGMENT_NAME[first.segment] ?? first.segment}</p>
              <BundleTable links={bundle} clickedId={linkId} />
              <RouterLink to="/capacity">Detail kapasitas & forecast →</RouterLink>
            </>
          ) : (
            <p className="small muted">Link ini sudah tidak ada di tampilan.</p>
          )}
        </div>
      </aside>
    )
  }

  if (!node) return null
  const current = view.nodes.find((n) => n.id === node.id)
  return (
    <aside className="detail-panel card">
      {closeButton}
      <div className="detail">
        <h3>{current?.label ?? node.label}</h3>
        {current?.kind === 'device' && <DeviceDetail device={current} view={view} />}
        {current?.kind === 'external_pop' && <dl><dt>POP</dt><dd>{current.code}</dd><dt>Perangkat</dt><dd>{current.devices}</dd></dl>}
        {current?.kind === 'summary' && <dl><dt>POP</dt><dd>{current.pop}</dd><dt>Perangkat</dt><dd>{current.devices}</dd></dl>}
        <div className="detail-actions">
          {drill && <button onClick={drill.run}>{drill.label} →</button>}
          {current?.kind === 'device' && <RouterLink to={`/ports?q=${encodeURIComponent(current.label)}`}>Lihat port perangkat →</RouterLink>}
        </div>
      </div>
    </aside>
  )
}

interface FilterBarProps {
  vendors: string[]
  filter: CityFilter
  onFilter: (next: CityFilter) => void
  highlightProblems: boolean
  highlightSpof: boolean
  onHighlightProblems: (on: boolean) => void
  onHighlightSpof: (on: boolean) => void
}

function toggled<T>(set: ReadonlySet<T>, value: T): Set<T> {
  const next = new Set(set)
  if (next.has(value)) next.delete(value)
  else next.add(value)
  return next
}

/** Filters and highlights of the city / neighbours levels. Everything here only restyles; it never re-lays out. */
export function CityFilterBar({ vendors, filter, onFilter, highlightProblems, highlightSpof, onHighlightProblems, onHighlightSpof }: FilterBarProps) {
  return (
    <div className="display-bar card city-filters" aria-label="Filter topologi kota">
      <div className="segment-chips" role="group" aria-label="Vendor">
        <span className="muted small">Vendor</span>
        {vendors.map((v) => (
          <button key={v} type="button" aria-pressed={filter.vendors.has(v)} className={`segment-chip ${filter.vendors.has(v) ? 'on' : ''}`}
            onClick={() => onFilter({ ...filter, vendors: toggled(filter.vendors, v) })}>{v}</button>
        ))}
      </div>
      <div className="segment-chips" role="group" aria-label="Layer">
        <span className="muted small">Layer</span>
        {LAYER_LABEL.map((label, tier) => (
          <button key={label} type="button" aria-pressed={filter.layers.has(tier)} className={`segment-chip ${filter.layers.has(tier) ? 'on' : ''}`}
            onClick={() => onFilter({ ...filter, layers: toggled(filter.layers, tier) })}>{label}</button>
        ))}
      </div>
      <label className="inline-field"><span>Status</span>
        <select value={filter.problemsOnly ? 'masalah' : 'semua'} onChange={(e) => onFilter({ ...filter, problemsOnly: e.target.value === 'masalah' })}>
          <option value="semua">Semua</option>
          <option value="masalah">Masalah saja</option>
        </select>
      </label>
      <label className="check" title="Terangkan perangkat down/kuning dan link down/kuning/merah; sisanya diredupkan">
        <input type="checkbox" checked={highlightProblems} onChange={(e) => onHighlightProblems(e.target.checked)} /> Sorot masalah
      </label>
      <label className="check" title="Tandai perangkat dan link single point of failure dengan garis oranye">
        <input type="checkbox" checked={highlightSpof} onChange={(e) => onHighlightSpof(e.target.checked)} /> SPOF
      </label>
    </div>
  )
}
