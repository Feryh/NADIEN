import { FormEvent, KeyboardEvent, useEffect, useMemo, useRef, useState } from 'react'
import { api, TopologySearch } from '../api'

export type SearchHit =
  | { kind: 'pop'; code: string; label: string; meta: string }
  | { kind: 'device'; deviceId: number; pop: string; label: string; meta: string }

/** Hostname / POP search with suggestions as you type (↑/↓ to choose, Enter to open). */
export function TopologySearchBox({ onPick }: { onPick: (hit: SearchHit) => void }) {
  const [term, setTerm] = useState('')
  const [result, setResult] = useState<TopologySearch | null>(null)
  const [open, setOpen] = useState(false)
  const [active, setActive] = useState(0)
  const [error, setError] = useState<string | null>(null)
  const box = useRef<HTMLFormElement>(null)

  useEffect(() => {
    const q = term.trim()
    if (q.length < 2) {
      setResult(null)
      return
    }
    // Debounced: one request after typing pauses, not one per key.
    const t = setTimeout(() => {
      api
        .get<TopologySearch>(`/topology/search?q=${encodeURIComponent(q)}`)
        .then((r) => {
          setResult(r)
          setActive(0)
          setError(null)
        })
        .catch((e: Error) => setError(e.message))
    }, 200)
    return () => clearTimeout(t)
  }, [term])

  useEffect(() => {
    const close = (e: MouseEvent) => box.current && !box.current.contains(e.target as Node) && setOpen(false)
    document.addEventListener('mousedown', close)
    return () => document.removeEventListener('mousedown', close)
  }, [])

  const needle = term.trim()
  const hits = useMemo<SearchHit[]>(() => {
    if (!result) return []
    return [
      ...result.pops.map((p): SearchHit => ({ kind: 'pop', code: p.code, label: p.code, meta: `${p.name} · ${p.area} · ${p.devices} perangkat` })),
      ...result.devices.map((d): SearchHit => ({
        // The management IP is shown only when it is what matched, so a hostname hit stays as short as before.
        kind: 'device', deviceId: d.device_id, pop: d.pop, label: d.hostname,
        meta: `${d.role.toUpperCase()} · ${d.pop}${d.mgmt_ip && d.mgmt_ip.startsWith(needle) ? ` · ${d.mgmt_ip}` : ''}`,
      })),
    ]
  }, [result, needle])

  function pick(hit: SearchHit | undefined) {
    if (!hit) return
    setOpen(false)
    setTerm(hit.label)
    onPick(hit)
  }

  function onKey(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key === 'ArrowDown') setActive((a) => Math.min(a + 1, hits.length - 1))
    else if (e.key === 'ArrowUp') setActive((a) => Math.max(a - 1, 0))
    else if (e.key === 'Escape') setOpen(false)
    else return
    e.preventDefault()
    setOpen(true)
  }

  function submit(e: FormEvent) {
    e.preventDefault()
    pick(hits[active])
  }

  const pops = hits.filter((h) => h.kind === 'pop')
  const devices = hits.filter((h) => h.kind === 'device')
  const row = (hit: SearchHit) => {
    const index = hits.indexOf(hit)
    return (
      <button type="button" key={`${hit.kind}:${hit.label}`} className={index === active ? 'active' : ''}
              onMouseEnter={() => setActive(index)} onClick={() => pick(hit)}>
        <span>{hit.label}</span>
        <span className="meta">{hit.meta}</span>
      </button>
    )
  }

  return (
    <form onSubmit={submit} className="inline search-box" ref={box}>
      <input
        className="search"
        placeholder="Cari hostname / IP / kode POP…"
        value={term}
        onChange={(e) => {
          setTerm(e.target.value)
          setOpen(true)
        }}
        onFocus={() => setOpen(true)}
        onKeyDown={onKey}
      />
      <button type="submit" disabled={!hits.length}>Buka</button>
      {open && term.trim().length >= 2 && (
        <div className="search-results">
          {error && <div className="group">{error}</div>}
          {!error && result && !hits.length && <div className="group">Tidak ditemukan</div>}
          {pops.length > 0 && <div className="group">POP</div>}
          {pops.map(row)}
          {devices.length > 0 && <div className="group">Perangkat</div>}
          {devices.map(row)}
        </div>
      )}
    </form>
  )
}

let areaOptions: Promise<string[]> | null = null

/** Change the area a POP belongs to; empty = back to automatic mapping. */
export function PopAreaEditor({ popId, region, onSaved }: { popId: number; region: string | null | undefined; onSaved: (area: string) => void }) {
  const [areas, setAreas] = useState<string[]>([])
  const [value, setValue] = useState(region ?? '')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    areaOptions = areaOptions ?? api.get<{ areas: string[] }>('/areas/options').then((r) => r.areas)
    areaOptions.then(setAreas).catch((e: Error) => setError(e.message))
  }, [])
  useEffect(() => setValue(region ?? ''), [region, popId])

  async function save() {
    setSaving(true)
    setError(null)
    try {
      const r = await api.put<{ area: string }>(`/pops/${popId}/area`, { area: value || null })
      areaOptions = null  // a new custom area joins the list
      onSaved(r.area)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setSaving(false)
    }
  }

  const options = value && !areas.includes(value) ? [value, ...areas] : areas
  return (
    <>
      <div className="area-edit">
        <select value={value} onChange={(e) => setValue(e.target.value)} title="Area POP ini (kosong = otomatis dari kode/nama)">
          <option value="">Otomatis</option>
          {options.map((a) => <option key={a} value={a}>{a}</option>)}
        </select>
        <button onClick={save} disabled={saving || value === (region ?? '')}>{saving ? '…' : 'Simpan'}</button>
      </div>
      {error && <div className="error small">{error}</div>}
    </>
  )
}
