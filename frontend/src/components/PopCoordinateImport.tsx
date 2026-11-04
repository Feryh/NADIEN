import { ChangeEvent, useMemo, useState } from 'react'
import { api } from '../api'

interface Placemark { name: string; lat: number; lon: number; folder: string | null; source: string | null }

type Confidence = 'high' | 'medium' | 'low'
type Target = 'pops' | 'devices'

/** One proposed coordinate, POP or device, in the shape the preview table needs. */
interface Row {
  id: number
  title: string
  subtitle: string
  area: string
  placemark: string
  detail: string
  source: string | null
  latitude: number
  longitude: number
  confidence: Confidence
}

interface PopProposal {
  pop_id: number; code: string; name: string; area: string; city: string; latitude: number; longitude: number
  placemark: string; source: string | null; confidence: Confidence; candidates: number; rejected_outside_area: number
}
interface DeviceProposal {
  device_id: number; hostname: string; pop: string; site: string; latitude: number; longitude: number
  placemark: string; source: string | null; confidence: Confidence; distance_km: number
}

const CONFIDENCE_LABEL: Record<Confidence, string> = { high: 'tinggi', medium: 'sedang', low: 'rendah' }
const LEVEL_CLASS: Record<Confidence, string> = { high: 'normal', medium: 'warning', low: 'critical' }

async function readPlacemarks(file: File): Promise<Placemark[]> {
  const res = await fetch(`/api/geo/placemarks?filename=${encodeURIComponent(file.name)}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/octet-stream' },
    body: await file.arrayBuffer(),
  })
  const data = await res.json().catch(() => ({}))
  if (!res.ok) throw new Error(`${file.name}: ${data.detail ?? res.statusText}`)
  return data.placemarks
}

async function match(target: Target, placemarks: Placemark[], onlyMissing: boolean): Promise<{ rows: Row[]; summary: string }> {
  if (target === 'pops') {
    const r = await api.post<{ pops_considered: number; proposals: PopProposal[]; unmatched: string[] }>(
      '/pops/coordinates/match', { placemarks, only_missing: onlyMissing })
    return {
      rows: r.proposals.map((p) => ({
        id: p.pop_id, title: p.code, subtitle: p.name !== p.code ? p.name : '', area: p.area, placemark: p.placemark,
        detail: `kota: ${p.city}${p.candidates > 1 ? ` · ${p.candidates} kandidat` : ''}${p.rejected_outside_area ? ` · ${p.rejected_outside_area} di luar area ditolak` : ''}`,
        source: p.source, latitude: p.latitude, longitude: p.longitude, confidence: p.confidence,
      })),
      summary: `${r.pops_considered} POP diperiksa · ${r.unmatched.length} tanpa placemark kota`,
    }
  }
  const r = await api.post<{ devices_considered: number; ambiguous: number; proposals: DeviceProposal[] }>(
    '/devices/coordinates/match', { placemarks, only_missing: onlyMissing })
  return {
    rows: r.proposals.map((p) => ({
      id: p.device_id, title: p.hostname, subtitle: `site ${p.site}`, area: p.pop, placemark: p.placemark,
      detail: `${p.distance_km} km dari POP`, source: p.source, latitude: p.latitude, longitude: p.longitude,
      confidence: p.confidence,
    })),
    summary: `${r.devices_considered} perangkat diperiksa · ${r.ambiguous} dilewati karena singkatan site cocok ke beberapa tempat`,
  }
}

/** Fill POP or device coordinates from KMZ/KML placemarks: preview, choose rows, then apply (backup first). */
export default function PopCoordinateImport({ onApplied }: { onApplied: () => void }) {
  const [target, setTarget] = useState<Target>('pops')
  const [files, setFiles] = useState<string[]>([])
  const [placemarkCount, setPlacemarkCount] = useState(0)
  const [rows, setRows] = useState<Row[] | null>(null)
  const [summary, setSummary] = useState('')
  const [chosen, setChosen] = useState<Set<number>>(new Set())
  const [onlyMissing, setOnlyMissing] = useState(true)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<{ text: string; error?: boolean } | null>(null)

  async function pick(e: ChangeEvent<HTMLInputElement>) {
    const list = [...(e.target.files ?? [])]
    if (!list.length) return
    setBusy(true)
    setMessage(null)
    setRows(null)
    try {
      const placemarks = (await Promise.all(list.map(readPlacemarks))).flat()
      setFiles(list.map((f) => f.name))
      setPlacemarkCount(placemarks.length)
      const r = await match(target, placemarks, onlyMissing)
      setRows(r.rows)
      setSummary(r.summary)
      // POPs: everything but "low". Devices: only "high" — a site abbreviation is easier to misread than a city.
      setChosen(new Set(r.rows.filter((row) => (target === 'pops' ? row.confidence !== 'low' : row.confidence === 'high')).map((row) => row.id)))
    } catch (err) {
      setMessage({ text: (err as Error).message, error: true })
    } finally {
      setBusy(false)
      e.target.value = ''
    }
  }

  async function apply() {
    if (!rows) return
    const selected = rows.filter((row) => chosen.has(row.id))
    const label = target === 'pops' ? 'POP' : 'perangkat'
    if (!confirm(`Simpan koordinat ${selected.length} ${label}? Database di-backup dulu.`)) return
    setBusy(true)
    try {
      const r = target === 'pops'
        ? await api.put<{ updated: number; backup: { name?: string } }>('/pops/coordinates',
            selected.map((row) => ({ pop_id: row.id, latitude: row.latitude, longitude: row.longitude })))
        : await api.put<{ updated: number; backup: { name?: string } }>('/devices/coordinates',
            selected.map((row) => ({ device_id: row.id, latitude: row.latitude, longitude: row.longitude, placemark: row.placemark, source: row.source })))
      setMessage({ text: `Koordinat ${r.updated} ${label} disimpan${r.backup?.name ? ` (backup ${r.backup.name})` : ''}.` })
      setRows(null)
      onApplied()
    } catch (err) {
      setMessage({ text: (err as Error).message, error: true })
    } finally {
      setBusy(false)
    }
  }

  const toggle = (id: number) =>
    setChosen((c) => {
      const next = new Set(c)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })

  const counts = useMemo(() => {
    const out: Record<Confidence, number> = { high: 0, medium: 0, low: 0 }
    rows?.forEach((row) => (out[row.confidence] += 1))
    return out
  }, [rows])

  return (
    <div className="card">
      <div className="toolbar">
        <h2>Import koordinat (KMZ/KML)</h2>
        <div className="segmented">
          {(['pops', 'devices'] as Target[]).map((t) => (
            <button key={t} className={target === t ? 'active' : ''} disabled={busy}
                    onClick={() => { setTarget(t); setRows(null); setMessage(null) }}>
              {t === 'pops' ? 'POP' : 'Perangkat (site)'}
            </button>
          ))}
        </div>
        <div className="spacer" />
        <label className="check">
          <input type="checkbox" checked={onlyMissing} onChange={(e) => setOnlyMissing(e.target.checked)} /> Hanya yang belum punya koordinat
        </label>
        <label className="button-like">
          {busy ? 'Memproses…' : 'Pilih file KMZ/KML'}
          <input type="file" accept=".kmz,.kml,.zip" multiple hidden onChange={pick} disabled={busy} />
        </label>
        {rows && rows.length > 0 && (
          <button className="primary" onClick={apply} disabled={busy || !chosen.size}>Simpan {chosen.size}</button>
        )}
      </div>
      <p className="hint">
        {target === 'pops'
          ? 'Placemark berupa nama lokasi ("NDC Palembang"), jadi POP dicocokkan lewat kotanya: kota dari kode POP atau nama/lokasinya. Titiknya harus di dalam area POP, dan situs utama (NDC/MTI/Branch/Data Centre) didahulukan.'
          : 'Token site di hostname (JMB-TLP-OLT1 → TLP) dicocokkan dengan nama placemark OLT/ODC dalam radius 80 km dari POP-nya: huruf per kata (LVU = Lippo Village Utara) paling kuat, lalu huruf mati berurutan (SKL = Syiah Kuala). Singkatan yang cocok ke beberapa tempat dilewati. Perangkat tanpa koordinat sendiri tetap tampil di lokasi POP-nya.'}
      </p>
      {message && <div className={message.error ? 'error' : 'notice'}>{message.text}</div>}
      {rows && (
        <>
          <div className="notice">
            {files.join(', ')}: {placemarkCount} placemark · {summary} · {rows.length} cocok (tinggi {counts.high}, sedang {counts.medium}, rendah {counts.low}).
          </div>
          {rows.length > 0 && (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr><th /><th>{target === 'pops' ? 'POP' : 'Perangkat'}</th><th>{target === 'pops' ? 'Area' : 'POP'}</th><th>Placemark</th><th className="num">Lat, Lon</th><th>Kecocokan</th></tr>
                </thead>
                <tbody>
                  {rows.map((row) => (
                    <tr key={row.id} className="clickable" onClick={() => toggle(row.id)}>
                      <td><input type="checkbox" checked={chosen.has(row.id)} readOnly /></td>
                      <td>{row.title}<div className="muted small">{row.subtitle}</div></td>
                      <td className="small">{row.area}</td>
                      <td>{row.placemark}<div className="muted small">{row.detail}</div></td>
                      <td className="num small">{row.latitude.toFixed(5)}, {row.longitude.toFixed(5)}</td>
                      <td><span className={`level level-${LEVEL_CLASS[row.confidence]}`}>{CONFIDENCE_LABEL[row.confidence]}</span></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </div>
  )
}
