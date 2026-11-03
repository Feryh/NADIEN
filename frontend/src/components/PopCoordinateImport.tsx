import { ChangeEvent, useMemo, useState } from 'react'
import { api } from '../api'

interface Placemark { name: string; lat: number; lon: number; folder: string | null; source: string | null }

interface Proposal {
  pop_id: number
  code: string
  name: string
  area: string
  city: string
  current_latitude: number | null
  current_longitude: number | null
  latitude: number
  longitude: number
  placemark: string
  source: string | null
  confidence: 'high' | 'medium' | 'low'
  candidates: number
  rejected_outside_area: number
}

interface MatchResult { pops_considered: number; proposals: Proposal[]; unmatched: string[] }

const CONFIDENCE_LABEL = { high: 'tinggi', medium: 'sedang', low: 'rendah' }

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

/** Fill POP lat/lon from KMZ/KML placemarks: preview per POP (by city, inside its area), then apply. */
export default function PopCoordinateImport({ onApplied }: { onApplied: () => void }) {
  const [files, setFiles] = useState<string[]>([])
  const [placemarkCount, setPlacemarkCount] = useState(0)
  const [result, setResult] = useState<MatchResult | null>(null)
  const [chosen, setChosen] = useState<Set<number>>(new Set())
  const [onlyMissing, setOnlyMissing] = useState(true)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<{ text: string; error?: boolean } | null>(null)

  async function pick(e: ChangeEvent<HTMLInputElement>) {
    const list = [...(e.target.files ?? [])]
    if (!list.length) return
    setBusy(true)
    setMessage(null)
    setResult(null)
    try {
      const placemarks = (await Promise.all(list.map(readPlacemarks))).flat()
      setFiles(list.map((f) => f.name))
      setPlacemarkCount(placemarks.length)
      const r = await api.post<MatchResult>('/pops/coordinates/match', { placemarks, only_missing: onlyMissing })
      setResult(r)
      // Low-confidence matches (city word after another word, e.g. "Gunung Pati") start unticked.
      setChosen(new Set(r.proposals.filter((p) => p.confidence !== 'low').map((p) => p.pop_id)))
    } catch (err) {
      setMessage({ text: (err as Error).message, error: true })
    } finally {
      setBusy(false)
      e.target.value = ''
    }
  }

  async function apply() {
    if (!result) return
    const rows = result.proposals.filter((p) => chosen.has(p.pop_id)).map((p) => ({ pop_id: p.pop_id, latitude: p.latitude, longitude: p.longitude }))
    if (!confirm(`Simpan koordinat ${rows.length} POP? Database di-backup dulu.`)) return
    setBusy(true)
    try {
      const r = await api.put<{ updated: number; backup: { name?: string } }>('/pops/coordinates', rows)
      setMessage({ text: `Koordinat ${r.updated} POP disimpan${r.backup?.name ? ` (backup ${r.backup.name})` : ''}.` })
      setResult(null)
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
    const out = { high: 0, medium: 0, low: 0 }
    result?.proposals.forEach((p) => (out[p.confidence] += 1))
    return out
  }, [result])

  return (
    <div className="card">
      <div className="toolbar">
        <h2>Import koordinat POP (KMZ/KML)</h2>
        <div className="spacer" />
        <label className="check">
          <input type="checkbox" checked={onlyMissing} onChange={(e) => setOnlyMissing(e.target.checked)} /> Hanya POP yang belum punya koordinat
        </label>
        <label className="button-like">
          {busy ? 'Memproses…' : 'Pilih file KMZ/KML'}
          <input type="file" accept=".kmz,.kml,.zip" multiple hidden onChange={pick} disabled={busy} />
        </label>
        {result && result.proposals.length > 0 && (
          <button className="primary" onClick={apply} disabled={busy || !chosen.size}>Simpan {chosen.size} POP</button>
        )}
      </div>
      <p className="hint">
        Nama placemark berupa nama lokasi ("NDC Palembang", "MTI Kisaran"), jadi POP dicocokkan lewat kotanya: kota dari kode
        POP atau dari nama/lokasi POP. Titiknya harus berada di dalam area POP itu, dan situs utama (NDC/MTI/Branch/Data
        Centre) didahulukan. Kecocokan "rendah" (kata kota setelah kata lain, mis. "Gunung Pati") tidak dicentang otomatis.
      </p>
      {message && <div className={message.error ? 'error' : 'notice'}>{message.text}</div>}
      {result && (
        <>
          <div className="notice">
            {files.join(', ')}: {placemarkCount} placemark · {result.pops_considered} POP diperiksa · {result.proposals.length} cocok
            (tinggi {counts.high}, sedang {counts.medium}, rendah {counts.low}) · {result.unmatched.length} tanpa placemark kota.
          </div>
          {result.proposals.length > 0 && (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr><th /><th>POP</th><th>Area</th><th>Placemark</th><th className="num">Lat, Lon</th><th>Kecocokan</th></tr>
                </thead>
                <tbody>
                  {result.proposals.map((p) => (
                    <tr key={p.pop_id} className="clickable" onClick={() => toggle(p.pop_id)}>
                      <td><input type="checkbox" checked={chosen.has(p.pop_id)} readOnly /></td>
                      <td>{p.code}<div className="muted small">{p.name !== p.code ? p.name : ''}</div></td>
                      <td className="small">{p.area}</td>
                      <td>{p.placemark}<div className="muted small">kota: {p.city}{p.candidates > 1 ? ` · ${p.candidates} kandidat` : ''}{p.rejected_outside_area ? ` · ${p.rejected_outside_area} di luar area ditolak` : ''}</div></td>
                      <td className="num small">{p.latitude.toFixed(5)}, {p.longitude.toFixed(5)}</td>
                      <td><span className={`level level-${p.confidence === 'high' ? 'normal' : p.confidence === 'medium' ? 'warning' : 'critical'}`}>{CONFIDENCE_LABEL[p.confidence]}</span></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {result.unmatched.length > 0 && (
            <p className="hint">Tanpa placemark untuk kotanya: {result.unmatched.slice(0, 60).join(', ')}{result.unmatched.length > 60 ? ', …' : ''}</p>
          )}
        </>
      )}
    </div>
  )
}
