import { useEffect, useState } from 'react'
import { api } from '../api'

interface MappingResult {
  total_pops: number
  area_updates: number
  city_updates: number
  unmapped_area_count: number
  unmapped_area_pops: string[]
  unmapped_city_count: number
  unmapped_city_pops: string[]
  area_conflict_count: number
  area_conflicts: { code: string; area: string; city: string; city_area: string }[]
  applied_area?: number
  applied_city?: number
}

export default function PopLocationMapping({ onApplied }: { onApplied: () => void }) {
  const [result, setResult] = useState<MappingResult | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function preview() {
    setBusy(true)
    setError(null)
    try {
      setResult(await api.get<MappingResult>('/areas/pop-location-mapping'))
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  useEffect(() => { void preview() }, [])

  async function apply() {
    setBusy(true)
    setError(null)
    try {
      setResult(await api.post<MappingResult>('/areas/pop-location-mapping'))
      onApplied()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="card pop-location-mapping">
      <div className="toolbar">
        <div>
          <h2>Pemetaan Area &amp; Kota POP</h2>
          <div className="muted small">Mengisi kolom kosong dari kode POP, nama, hostname perangkat, dan lokasi RANCID.</div>
        </div>
        <div className="spacer" />
        <button onClick={() => void preview()} disabled={busy}>Periksa lagi</button>
        <button className="primary" onClick={() => void apply()} disabled={busy || !result || result.area_updates + result.city_updates === 0}>
          {busy ? 'Memproses…' : 'Isi Area & Kota'}
        </button>
      </div>
      {error && <div className="error">{error}</div>}
      {result && (
        <>
          {result.applied_area !== undefined ? (
            <div className="notice">
              {result.applied_area} Area dan {result.applied_city} Kota POP diperbarui.
              {' '}Sisa belum terpetakan: {result.unmapped_area_count} Area, {result.unmapped_city_count} Kota.
            </div>
          ) : (
            <div className="notice">
              {result.total_pops} POP diperiksa. Bisa diisi: {result.area_updates} Area dan {result.city_updates} Kota.
              {' '}Belum ada dasar pemetaan: {result.unmapped_area_count} Area, {result.unmapped_city_count} Kota.
            </div>
          )}
          {(result.unmapped_area_pops.length > 0 || result.unmapped_city_pops.length > 0) && (
            <div className="muted small pop-location-unmapped">
              {result.unmapped_area_pops.length > 0 && <div>Area: {result.unmapped_area_pops.join(', ')}</div>}
              {result.unmapped_city_pops.length > 0 && <div>Kota: {result.unmapped_city_pops.join(', ')}</div>}
            </div>
          )}
          {result.area_conflict_count > 0 && (
            <div className="muted small pop-location-unmapped">
              Perlu cek konsistensi Area–Kota: {result.area_conflicts.map((item) => `${item.code} (${item.area} / ${item.city} → ${item.city_area})`).join(', ')}
            </div>
          )}
        </>
      )}
    </section>
  )
}
