import { FormEvent, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api'

interface SettingField {
  key: string
  group: 'observium' | 'gitweb'
  label: string
  kind: 'text' | 'secret' | 'bool' | 'int' | 'select'
  options: string[]
  help: string
  source: 'ui' | 'env' | 'default'
  value?: string | number | boolean | null
  is_set?: boolean
}

interface SettingsResponse {
  editable: boolean
  fields: SettingField[]
}

const GROUPS: { key: SettingField['group']; title: string; test: string; hint: string; extraTest?: { target: string; label: string } }[] = [
  {
    key: 'observium', title: 'Observium', test: 'observium', extraTest: { target: 'observium_csr', label: 'Tes Observium CSR' },
    hint: 'Satu login untuk semua data collector: perangkat, port, IP, LLDP/CDP, utilisasi, dan histori RRD (graph.php). Login yang sama dipakai untuk Observium CSR.',
  },
  {
    key: 'gitweb', title: 'RANCID gitweb', test: 'gitweb',
    hint: 'Config semua repo RANCID: cadangan untuk Observium dan satu-satunya sumber perangkat CSR.',
  },
]

const SOURCE_LABEL = { ui: 'disimpan di UI', env: 'dari backend/.env', default: 'default' }

type Draft = Record<string, string | boolean>

function draftFrom(fields: SettingField[]): Draft {
  return Object.fromEntries(fields.map((f) => [f.key, f.kind === 'secret' ? '' : f.kind === 'bool' ? Boolean(f.value) : f.value == null ? '' : String(f.value)]))
}

/** Connection settings for the collectors; saved values override backend/.env. Secrets are write-only. */
export default function CollectorSettings() {
  const [data, setData] = useState<SettingsResponse | null>(null)
  const [draft, setDraft] = useState<Draft>({})
  const [clear, setClear] = useState<Set<string>>(new Set())
  const [message, setMessage] = useState<{ text: string; error?: boolean } | null>(null)
  const [saving, setSaving] = useState(false)
  const [testing, setTesting] = useState<string | null>(null)

  function load(next: SettingsResponse) {
    setData(next)
    setDraft(draftFrom(next.fields))
    setClear(new Set())
  }

  useEffect(() => {
    api.get<SettingsResponse>('/collectors/settings').then(load).catch((e: Error) => setMessage({ text: e.message, error: true }))
  }, [])

  const original = useMemo(() => (data ? draftFrom(data.fields) : {}), [data])
  const changed = Object.keys(draft).filter((k) => draft[k] !== original[k])

  async function save(e: FormEvent) {
    e.preventDefault()
    setSaving(true)
    setMessage(null)
    try {
      const values = Object.fromEntries(changed.map((k) => [k, draft[k]]))
      load(await api.put<SettingsResponse>('/collectors/settings', { values, clear: [...clear] }))
      setMessage({ text: 'Tersimpan. Berlaku untuk sync berikutnya, tanpa restart.' })
    } catch (err) {
      setMessage({ text: (err as Error).message, error: true })
    } finally {
      setSaving(false)
    }
  }

  async function test(target: string) {
    setTesting(target)
    setMessage(null)
    try {
      const r = await api.post<{ ok: boolean; detail: string }>(`/collectors/settings/test?target=${target}`)
      setMessage({ text: `${r.ok ? '✓' : '✗'} ${r.detail}`, error: !r.ok })
    } catch (err) {
      setMessage({ text: (err as Error).message, error: true })
    } finally {
      setTesting(null)
    }
  }

  const toggleClear = (key: string) =>
    setClear((c) => {
      const next = new Set(c)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })

  if (!data) return <div className="card">{message ? <div className="error">{message.text}</div> : 'Memuat…'}</div>
  const readOnly = !data.editable

  return (
    <form className="capacity" onSubmit={save}>
      <div className="card">
        <div className="toolbar">
          <h2>Pengaturan Collector</h2>
          <div className="spacer" />
          <Link to="/collectors">← Kembali ke Collectors</Link>
          <button className="primary" type="submit" disabled={readOnly || saving || (!changed.length && !clear.size)}>
            {saving ? 'Menyimpan…' : `Simpan${changed.length + clear.size ? ` (${changed.length + clear.size})` : ''}`}
          </button>
        </div>
        <p className="hint">
          Nilai yang disimpan di sini menimpa <code>backend/.env</code> dan langsung dipakai semua worker. Kosongkan lalu centang
          "pakai .env" untuk kembali ke nilai dari environment. Token &amp; password disimpan terenkripsi dan tidak pernah
          ditampilkan lagi. Biarkan kosong untuk mempertahankan yang tersimpan. NADIEN belum punya login, jadi batasi
          akses aplikasi ke jaringan internal.
        </p>
        {readOnly && (
          <div className="notice">
            Pengaturan lewat UI dimatikan (<code>NADIEN_SETTINGS_UI_ENABLED=false</code>). Ubah lewat <code>backend/.env</code>.
          </div>
        )}
        {message && <div className={message.error ? 'error' : 'notice'}>{message.text}</div>}
      </div>

      {GROUPS.map((group) => (
        <div className="card" key={group.key}>
          <div className="toolbar">
            <h2>{group.title}</h2>
            <div className="spacer" />
            <button type="button" onClick={() => test(group.test)} disabled={!!testing || changed.length > 0}
                    title={changed.length ? 'Simpan dulu perubahan, lalu tes' : 'Tes dengan pengaturan yang tersimpan'}>
              {testing === group.test ? 'Mengetes…' : 'Tes koneksi'}
            </button>
            {group.extraTest && (
              <button type="button" onClick={() => test(group.extraTest!.target)} disabled={!!testing || changed.length > 0}>
                {testing === group.extraTest.target ? 'Mengetes…' : group.extraTest.label}
              </button>
            )}
          </div>
          <p className="hint">{group.hint}</p>
          <div className="settings-grid">
            {data.fields.filter((f) => f.group === group.key).map((f) => (
              <SettingRow key={f.key} field={f} value={draft[f.key]} disabled={readOnly} clearing={clear.has(f.key)}
                          onChange={(v) => setDraft((d) => ({ ...d, [f.key]: v }))} onToggleClear={() => toggleClear(f.key)} />
            ))}
          </div>
        </div>
      ))}
    </form>
  )
}

function SettingRow(props: {
  field: SettingField
  value: string | boolean | undefined
  disabled: boolean
  clearing: boolean
  onChange: (v: string | boolean) => void
  onToggleClear: () => void
}) {
  const { field: f, value, disabled, clearing, onChange, onToggleClear } = props
  let input
  if (f.kind === 'bool') {
    input = <input type="checkbox" checked={Boolean(value)} disabled={disabled} onChange={(e) => onChange(e.target.checked)} />
  } else if (f.kind === 'select') {
    input = (
      <select value={String(value ?? '')} disabled={disabled} onChange={(e) => onChange(e.target.value)}>
        {f.options.map((o) => <option key={o} value={o}>{o}</option>)}
      </select>
    )
  } else {
    input = (
      <input
        type={f.kind === 'secret' ? 'password' : f.kind === 'int' ? 'number' : 'text'}
        autoComplete={f.kind === 'secret' ? 'new-password' : 'off'}
        value={String(value ?? '')}
        disabled={disabled}
        placeholder={f.kind === 'secret' ? (f.is_set ? '•••••• (tersimpan, kosongkan untuk tetap)' : 'belum di-set') : ''}
        onChange={(e) => onChange(e.target.value)}
      />
    )
  }
  return (
    <label className="setting-row">
      <span className="setting-label">
        {f.label}
        <span className={`pill ${f.source === 'ui' ? 'pill-ok' : 'pill-muted'}`}>{SOURCE_LABEL[f.source]}</span>
      </span>
      {input}
      <span className="small muted">
        {f.help}
        {f.source === 'ui' && !disabled && (
          <label className="check inline-check">
            <input type="checkbox" checked={clearing} onChange={onToggleClear} /> pakai .env
          </label>
        )}
      </span>
    </label>
  )
}
