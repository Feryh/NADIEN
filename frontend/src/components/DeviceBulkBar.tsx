import { useEffect, useState } from 'react'
import { api } from '../api'

interface PopOption {
  id: number
  code: string
  name?: string | null
}

interface BulkResult {
  devices: number
  devices_moved: number
  pops_area_changed: string[]
  devices_in_changed_pops: number
  skipped_pops: string[]
}

/**
 * Ticked devices in Inventory > Router: move them all to one POP (locked there), or put their POPs in one area.
 * The area belongs to the POP, so the confirmation says how many POPs change and that their other devices follow.
 */
export default function DeviceBulkBar({ ids, pops, total, onSelectAll, onClear, onDone }: {
  ids: number[]
  pops: PopOption[]
  /** Rows the current search shows, for "select all of them". */
  total: number
  onSelectAll: () => void
  onClear: () => void
  onDone: (message: string) => void
}) {
  const [popId, setPopId] = useState('')
  const [area, setArea] = useState('')
  const [areas, setAreas] = useState<string[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api.get<{ areas: string[] }>('/areas/options').then((r) => setAreas(r.areas)).catch(() => setAreas([]))
  }, [])

  async function apply(body: Record<string, unknown>, question: string) {
    if (!window.confirm(question)) return
    setBusy(true)
    setError(null)
    try {
      const r = await api.patch<BulkResult>('/devices', { device_ids: ids, ...body })
      const parts = []
      if ('pop_id' in body) parts.push(`${r.devices_moved} perangkat dipindah ke POP baru dan dikunci`)
      if (r.pops_area_changed.length) {
        parts.push(`area ${r.pops_area_changed.length} POP diubah (${r.devices_in_changed_pops} perangkat mengikuti)`)
      }
      if (r.skipped_pops.length) parts.push(`POP ${r.skipped_pops.join(', ')} dilewati: tidak bisa diberi area`)
      onDone(`${r.devices} perangkat diproses: ${parts.join('; ')}.`)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const pop = pops.find((p) => String(p.id) === popId)
  return (
    <div className="bulk-bar">
      <div className="bulk-bar-row">
        <b>{ids.length} perangkat dipilih</b>
        {ids.length < total && <button className="link-button" onClick={onSelectAll}>Pilih semua hasil ({total})</button>}
        <button className="link-button" onClick={onClear}>Batal pilih</button>
      </div>
      <div className="bulk-bar-row">
        <label>
          <span className="muted small">Pindah ke POP</span>
          <select value={popId} onChange={(e) => setPopId(e.target.value)}>
            <option value="">— pilih POP —</option>
            {pops.map((p) => <option key={p.id} value={p.id}>{p.code}{p.name && p.name !== p.code ? ` · ${p.name}` : ''}</option>)}
          </select>
        </label>
        <button className="primary" disabled={busy || !pop}
          onClick={() => pop && apply({ pop_id: pop.id },
            `Pindahkan ${ids.length} perangkat ke POP ${pop.code}? POP-nya dikunci, sehingga sync tidak memindahkannya lagi.`)}>
          Pindahkan
        </button>
        <label>
          <span className="muted small">Area POP-nya</span>
          <select value={area} onChange={(e) => setArea(e.target.value)}>
            <option value="">— pilih area —</option>
            {areas.map((a) => <option key={a} value={a}>{a}</option>)}
          </select>
        </label>
        <button disabled={busy || !area}
          onClick={() => apply({ area },
            `Set area "${area}" untuk POP dari ${ids.length} perangkat ini? Area melekat ke POP: semua perangkat lain di POP tersebut ikut berpindah area.`)}>
          Set area
        </button>
        <button disabled={busy} title="Kembalikan area POP-nya ke penentuan otomatis (kode kota / hostname)"
          onClick={() => apply({ clear_area: true }, `Kembalikan area POP dari ${ids.length} perangkat ini ke otomatis?`)}>
          Area otomatis
        </button>
      </div>
      {error && <div className="error">{error}</div>}
    </div>
  )
}
