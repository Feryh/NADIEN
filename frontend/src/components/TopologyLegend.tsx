import { CAPACITY_HEX } from './ui'

// Legend of the topology map: what colours, line styles and markers mean.

const ROLES: [string, string][] = [
  ['P / DCI', '#6366f1'], ['PE', '#0ea5e9'], ['BNG', '#e879f9'], ['Metro / Agg', '#14b8a6'], ['Switch (DS)', '#22d3ee'],
  ['CSR', '#fb7185'], ['OLT', '#a3e635'], ['Switch akses', '#64748b'],
]
const SEGMENTS: [string, string, string][] = [
  ['backbone', 'Backbone', 'P/DCI/PE/BNG ↔ P/DCI/PE/BNG'],
  ['distribution', 'Distribusi', 'Metro/Agg ↔ Agg/PE/BNG/P'],
  ['last_mile', 'Last Mile', 'ke OLT, CSR, switch akses'],
]

export default function TopologyLegend({ open, colorMode, level, onToggle }: {
  open: boolean
  colorMode: 'utilization' | 'segment'
  level: string
  onToggle: () => void
}) {
  if (!open) {
    return <button type="button" className="legend-toggle" onClick={onToggle}>Legenda</button>
  }
  return (
    <aside className="topology-legend" aria-label="Legenda peta">
      <div className="legend-head">
        <b>Legenda</b>
        <button type="button" onClick={onToggle} aria-label="Tutup legenda">×</button>
      </div>
      {(level === 'device' || level === 'neighbors') && (
        <section>
          <h4>Role perangkat</h4>
          <ul className="legend-grid">
            {ROLES.map(([label, color]) => <li key={label}><span className="legend-dot" style={{ background: color }} />{label}</li>)}
          </ul>
        </section>
      )}
      <section>
        <h4>Fungsi link</h4>
        <ul>
          {SEGMENTS.map(([key, label, hint]) => (
            <li key={key}><span className={`segment-line ${key}`} /><span><b>{label}</b> <span className="muted">{hint}</span></span></li>
          ))}
        </ul>
      </section>
      <section>
        <h4>{colorMode === 'segment' ? 'Warna = fungsi link' : 'Warna = committed (trafik + reserved)'}</h4>
        {colorMode === 'utilization' && (
          <ul className="legend-grid">
            <li><span className="legend-bar" style={{ background: CAPACITY_HEX.green }} />Aman</li>
            <li><span className="legend-bar" style={{ background: CAPACITY_HEX.yellow }} />Waspada</li>
            <li><span className="legend-bar" style={{ background: CAPACITY_HEX.red }} />Kritis</li>
            <li><span className="legend-bar" style={{ background: '#94a3b8' }} />Kapasitas belum diketahui</li>
          </ul>
        )}
        <ul>
          <li><span className="legend-bar dashed" />Semua link di jalur ini down</li>
          <li>Tebal garis = kapasitas</li>
        </ul>
      </section>
      <section>
        <h4>Penanda</h4>
        <ul>
          <li><span className="legend-dot fresh" />Baru muncul (Live, 10 menit)</li>
          <li><span className="legend-bar went-down" />Link baru saja down</li>
          <li><span className="legend-dot estimated" />Posisi perkiraan (tanpa koordinat)</li>
        </ul>
      </section>
    </aside>
  )
}
