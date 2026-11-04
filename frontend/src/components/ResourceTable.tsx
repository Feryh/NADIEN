import { FormEvent, ReactNode, useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api'
import { ENTITY_BY_KEY, Entity, Field } from '../entities'
import { Pager, usePaged } from './Pager'

type Row = Record<string, unknown> & { id: number }

// Rows loaded per request (backend cap). Larger tables (300k+ interfaces) are searched server-side.
const FETCH_LIMIT = 10000

function StatusPill({ value }: { value: string }) {
  const tone = ['up', 'active', 'true'].includes(value)
    ? 'ok'
    : ['down', 'decommissioned', 'terminated'].includes(value)
      ? 'bad'
      : 'muted'
  return <span className={`pill pill-${tone}`}>{value}</span>
}

export default function ResourceTable({ entity }: { entity: Entity }) {
  const [rows, setRows] = useState<Row[]>([])
  const [refs, setRefs] = useState<Record<string, Row[]>>({})
  const [q, setQ] = useState('')
  const [editing, setEditing] = useState<Row | 'new' | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [serverQuery, setServerQuery] = useState('')

  const refKeys = useMemo(
    () => [...new Set(entity.fields.filter((f) => f.ref).map((f) => f.ref as string))],
    [entity],
  )
  const columns = entity.fields.filter((f) => !f.hideInTable)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const search = serverQuery ? `&q=${encodeURIComponent(serverQuery)}` : ''
      const fixed = entity.query ? `&${entity.query}` : ''
      const [main, ...refRows] = await Promise.all([
        api.get<Row[]>(`${entity.endpoint}?limit=${FETCH_LIMIT}${fixed}${search}`),
        ...refKeys.map((k) => api.get<Row[]>(`${ENTITY_BY_KEY[k].endpoint}?limit=${FETCH_LIMIT}`)),
      ])
      setRows(main)
      setRefs(Object.fromEntries(refKeys.map((k, i) => [k, refRows[i]])))
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setLoading(false)
    }
  }, [entity, refKeys, serverQuery])

  useEffect(() => {
    setEditing(null)
    load()
  }, [load])

  useEffect(() => {
    setQ('')
    setServerQuery('')
  }, [entity])

  const truncated = rows.length >= FETCH_LIMIT

  const refLabel = useCallback(
    (refKey: string, id: unknown) => {
      if (id === null || id === undefined) return ''
      const row = refs[refKey]?.find((r) => r.id === id)
      return row ? String(row[ENTITY_BY_KEY[refKey].display]) : `#${id}`
    },
    [refs],
  )

  const cellText = useCallback(
    (f: Field, value: unknown): string => {
      if (value === null || value === undefined) return ''
      if (f.type === 'ref' && f.ref) return refLabel(f.ref, value)
      if (f.type === 'datetime') return new Date(String(value)).toLocaleString()
      if (f.type === 'json') return JSON.stringify(value)
      return String(value)
    },
    [refLabel],
  )

  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase()
    if (!needle) return rows
    return rows.filter((r) => columns.some((f) => cellText(f, r[f.name]).toLowerCase().includes(needle)))
  }, [rows, q, columns, cellText])

  const paged = usePaged(filtered, `${entity.key}|${q}|${serverQuery}`)
  const pageRows = paged.pageRows

  async function remove(row: Row) {
    if (!confirm(`Hapus ${entity.label} "${row[entity.display]}"?`)) return
    try {
      await api.del(`${entity.endpoint}/${row.id}`)
      await load()
    } catch (e) {
      setError((e as Error).message)
    }
  }

  return (
    <div className="card">
      <div className="toolbar">
        <h2>{entity.label}</h2>
        {entity.external && (
          <span className="pill pill-muted" title="Data utama akan ditarik dari platform lain via API">
            external · API-fed
          </span>
        )}
        <div className="spacer" />
        <input
          className="search"
          placeholder={truncated || serverQuery ? 'Cari (Enter = cari di server)…' : 'Cari…'}
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') setServerQuery(q.trim())
          }}
        />
        <button onClick={load} disabled={loading}>
          {loading ? 'Memuat…' : 'Refresh'}
        </button>
        <button className="primary" onClick={() => setEditing('new')}>
          + Tambah
        </button>
      </div>

      {entity.external && (
        <p className="hint">
          Wadah untuk data dari platform eksternal. Untuk sekarang bisa diisi manual, atau di-push lewat{' '}
          <code>POST /api/integrations/ingest/&lt;entity&gt;?source_system=…</code> (upsert berdasarkan external_id).
        </p>
      )}
      {error && <div className="error">{error}</div>}
      {truncated && (
        <p className="hint">
          Menampilkan {FETCH_LIMIT.toLocaleString()} baris pertama
          {serverQuery ? ` untuk "${serverQuery}"` : ''}. Ketik kata kunci lalu tekan <b>Enter</b> untuk mencari di seluruh data
          {entity.key === 'interfaces' ? ' (nama port atau deskripsi)' : ''}.
        </p>
      )}
      {serverQuery && !truncated && (
        <p className="hint">
          Hasil pencarian server untuk "{serverQuery}" —{' '}
          <a href="#" onClick={(e) => { e.preventDefault(); setQ(''); setServerQuery('') }}>tampilkan semua</a>
        </p>
      )}

      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>ID</th>
              {columns.map((f) => (
                <th key={f.name}>{f.label}</th>
              ))}
              <th />
            </tr>
          </thead>
          <tbody>
            {pageRows.map((row) => (
              <tr key={row.id}>
                <td className="muted">{row.id}</td>
                {columns.map((f) => {
                  const value = row[f.name]
                  if (f.type === 'bool') return <td key={f.name}>{value ? '✓' : '–'}</td>
                  if (entity.endpoint === '/devices' && f.name === 'hostname')
                    return <td key={f.name}><Link to={`/devices/${row.id}`}>{String(value)}</Link></td>
                  if (f.name.endsWith('status') && typeof value === 'string' && f.type === 'select')
                    return (
                      <td key={f.name}>
                        <StatusPill value={value} />
                      </td>
                    )
                  return (
                    <td key={f.name} className={f.type === 'number' ? 'num' : undefined}>
                      {cellText(f, value)}
                    </td>
                  )
                })}
                <td className="actions">
                  <button onClick={() => setEditing(row)}>Edit</button>
                  <button className="danger" onClick={() => remove(row)}>
                    Hapus
                  </button>
                </td>
              </tr>
            ))}
            {!pageRows.length && (
              <tr>
                <td colSpan={columns.length + 2} className="empty">
                  {loading ? 'Memuat…' : 'Belum ada data'}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <Pager paged={paged} />

      {editing && (
        <FormModal
          entity={entity}
          row={editing === 'new' ? null : editing}
          refs={refs}
          onClose={() => setEditing(null)}
          onSaved={async () => {
            setEditing(null)
            await load()
          }}
        />
      )}
    </div>
  )
}

function toInput(f: Field, value: unknown): string | boolean {
  if (f.type === 'bool') return Boolean(value)
  if (value === null || value === undefined) return ''
  if (f.type === 'json') return JSON.stringify(value, null, 2)
  return String(value)
}

function fromInput(f: Field, value: string | boolean): unknown {
  if (f.type === 'bool') return Boolean(value)
  const text = String(value).trim()
  if (text === '') return null
  if (f.type === 'number' || f.type === 'ref') return Number(text)
  if (f.type === 'json') return JSON.parse(text)
  return text
}

function FormModal(props: {
  entity: Entity
  row: Row | null
  refs: Record<string, Row[]>
  onClose: () => void
  onSaved: () => void
}) {
  const { entity, row, refs, onClose, onSaved } = props
  const editable = entity.fields.filter((f) => !f.readonly)
  const [values, setValues] = useState<Record<string, string | boolean>>(() =>
    Object.fromEntries(
      editable.map((f) => [f.name, toInput(f, row ? row[f.name] : f.type === 'select' && !f.blankDefault ? f.options?.[0] : null)]),
    ),
  )
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  // POP code already taken by another POP: offer to merge this POP into it instead of renaming.
  const [mergeTarget, setMergeTarget] = useState<string | null>(null)

  async function merge() {
    if (!row || !mergeTarget) return
    if (!confirm(`Gabungkan POP ${row.code} ke ${mergeTarget}? Semua perangkat & referensi pindah ke ${mergeTarget}, ` +
                 `lalu POP ${row.code} dihapus. Data ${mergeTarget} yang sudah terisi tidak ditimpa.`)) return
    setSaving(true)
    try {
      await api.post(`/pops/${row.id}/merge`, { target_code: mergeTarget })
      onSaved()
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setSaving(false)
    }
  }

  async function submit(e: FormEvent) {
    e.preventDefault()
    setError(null)
    setMergeTarget(null)
    let payload: Record<string, unknown>
    try {
      payload = Object.fromEntries(editable.map((f) => [f.name, fromInput(f, values[f.name])]))
    } catch {
      setError('Field JSON tidak valid')
      return
    }
    setSaving(true)
    try {
      if (row) await api.patch(`${entity.endpoint}/${row.id}`, payload)
      else await api.post(entity.endpoint, payload)
      onSaved()
    } catch (err) {
      const message = (err as Error).message
      setError(message)
      const newCode = String(payload.code ?? '')
      if (entity.key === 'pops' && row && message.startsWith('409') && newCode && newCode !== row.code) setMergeTarget(newCode)
    } finally {
      setSaving(false)
    }
  }

  const set = (name: string, value: string | boolean) => setValues((v) => ({ ...v, [name]: value }))

  return (
    <div className="modal-backdrop" onMouseDown={onClose}>
      <form className="modal" onSubmit={submit} onMouseDown={(e) => e.stopPropagation()}>
        <h3>
          {row ? 'Edit' : 'Tambah'} {entity.label}
        </h3>
        <div className="form-grid">
          {editable.map((f) => {
            const value = values[f.name]
            let input: ReactNode
            if (f.type === 'bool') {
              input = <input type="checkbox" checked={Boolean(value)} onChange={(e) => set(f.name, e.target.checked)} />
            } else if (f.type === 'select') {
              input = (
                <select value={String(value)} onChange={(e) => set(f.name, e.target.value)} required={f.required}>
                  {!f.required && <option value="">—</option>}
                  {/* A stored value outside the list (custom area) stays selectable, so saving never wipes it. */}
                  {value && !f.options?.includes(String(value)) && <option value={String(value)}>{String(value)}</option>}
                  {f.options?.map((o) => (
                    <option key={o} value={o}>
                      {o}
                    </option>
                  ))}
                </select>
              )
            } else if (f.type === 'ref' && f.ref) {
              const display = ENTITY_BY_KEY[f.ref].display
              input = (
                <select value={String(value)} onChange={(e) => set(f.name, e.target.value)} required={f.required}>
                  <option value="">—</option>
                  {(refs[f.ref] ?? [])
                    .filter((r) => !row || f.ref !== entity.key || r.id !== row.id)
                    .map((r) => (
                      <option key={r.id} value={r.id}>
                        {String(r[display])}
                      </option>
                    ))}
                </select>
              )
            } else if (f.type === 'textarea' || f.type === 'json') {
              input = <textarea rows={f.type === 'json' ? 4 : 2} value={String(value)} onChange={(e) => set(f.name, e.target.value)} />
            } else {
              input = (
                <input
                  type={f.type === 'number' ? 'number' : 'text'}
                  step="any"
                  value={String(value)}
                  required={f.required}
                  onChange={(e) => set(f.name, e.target.value)}
                />
              )
            }
            return (
              <label key={f.name} className={f.type === 'textarea' || f.type === 'json' ? 'span-2' : undefined}>
                <span>
                  {f.label}
                  {f.required && ' *'}
                </span>
                {input}
              </label>
            )
          })}
        </div>
        {error && <div className="error">{error}</div>}
        {mergeTarget && row && (
          <div className="notice">
            POP {mergeTarget} sudah ada. Kalau {String(row.code)} sebenarnya POP yang sama,{' '}
            <button type="button" onClick={merge} disabled={saving}>Gabungkan {String(row.code)} ke {mergeTarget}</button>
          </div>
        )}
        <div className="modal-actions">
          <button type="button" onClick={onClose}>
            Batal
          </button>
          <button type="submit" className="primary" disabled={saving}>
            {saving ? 'Menyimpan…' : 'Simpan'}
          </button>
        </div>
      </form>
    </div>
  )
}
