import { FormEvent, KeyboardEvent, useCallback, useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { api } from '../api'

// Phase 9 — AI Assistant: questions about the network answered by a pluggable model (local Ollama/vLLM by
// default, Claude / OpenAI / Azure OpenAI / any OpenAI-compatible endpoint added by hand).

type Kind = 'ollama' | 'openai' | 'azure_openai' | 'anthropic'

interface Provider {
  id: number
  name: string
  kind: Kind
  base_url: string | null
  model: string
  api_version: string | null
  has_api_key: boolean
  extra_headers: Record<string, string>
  temperature: number | null
  max_tokens: number | null
  timeout_s: number
  enabled: boolean
  is_default: boolean
  external: boolean
}

interface Preset {
  key: string
  label: string
  kind: Kind
  base_url: string
  model: string
  model_hint?: string
  api_version?: string
  needs_key: boolean
}

interface ChatTurn {
  role: 'user' | 'assistant'
  content: string
  meta?: { model: string; provider: string; duration_ms: number; sources: string[]; external: boolean }
  error?: boolean
}

const KIND_LABEL: Record<Kind, string> = {
  ollama: 'Ollama',
  openai: 'OpenAI-compatible',
  azure_openai: 'Azure OpenAI',
  anthropic: 'Claude (Anthropic)',
}

export default function AiAssistant() {
  const [params, setParams] = useSearchParams()
  const tab = params.get('tab') === 'providers' ? 'providers' : 'chat'
  const [data, setData] = useState<{ editable: boolean; providers: Provider[]; presets: Preset[] } | null>(null)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(() => {
    api.get<{ editable: boolean; providers: Provider[]; presets: Preset[] }>('/ai/providers')
      .then(setData)
      .catch((e: Error) => setError(e.message))
  }, [])
  useEffect(load, [load])

  return (
    <div className="capacity">
      <div className="tabs">
        <button className={tab === 'chat' ? 'active' : ''} onClick={() => setParams({})}>Chat</button>
        <button className={tab === 'providers' ? 'active' : ''} onClick={() => setParams({ tab: 'providers' })}>Provider AI</button>
      </div>
      {error && <div className="error">{error}</div>}
      {data && tab === 'chat' && <Chat providers={data.providers.filter((p) => p.enabled)} onConfigure={() => setParams({ tab: 'providers' })} />}
      {data && tab === 'providers' && <Providers data={data} reload={load} />}
    </div>
  )
}

// --------------------------------------------------------------------------- chat

const EXAMPLES = [
  'Link mana saja yang kritis di Jabodetabek?',
  'Area mana yang paling cepat habis kapasitasnya?',
  'Apa saja kekurangan data inventory yang paling penting dibereskan?',
  'Jelaskan beda VPLS dan VPWS untuk layanan L2VPN.',
]

function Chat({ providers, onConfigure }: { providers: Provider[]; onConfigure: () => void }) {
  const [providerId, setProviderId] = useState<number | null>(null)
  const [turns, setTurns] = useState<ChatTurn[]>([])
  const [input, setInput] = useState('')
  const [withContext, setWithContext] = useState(true)
  const [busy, setBusy] = useState(false)
  const bottom = useRef<HTMLDivElement>(null)
  const provider = providers.find((p) => p.id === providerId) ?? providers.find((p) => p.is_default) ?? providers[0]

  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: 'smooth' })
  }, [turns, busy])

  const send = (text: string) => {
    const question = text.trim()
    if (!question || busy || !provider) return
    const history = [...turns.filter((t) => !t.error), { role: 'user' as const, content: question }]
    setTurns([...turns, { role: 'user', content: question }])
    setInput('')
    setBusy(true)
    api.post<{ reply: string; model: string; duration_ms: number; provider: Provider; context: { sources: string[] } }>('/ai/chat', {
      provider_id: provider.id,
      include_network_context: withContext,
      messages: history.map(({ role, content }) => ({ role, content })),
    })
      .then((r) => setTurns((prev) => [...prev, {
        role: 'assistant', content: r.reply,
        meta: { model: r.model, provider: r.provider.name, duration_ms: r.duration_ms, sources: r.context.sources, external: r.provider.external },
      }]))
      .catch((e: Error) => setTurns((prev) => [...prev, { role: 'assistant', content: e.message, error: true }]))
      .finally(() => setBusy(false))
  }

  const onSubmit = (e: FormEvent) => {
    e.preventDefault()
    send(input)
  }
  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      send(input)
    }
  }

  if (!providers.length) {
    return (
      <div className="card">
        <h2>AI Assistant</h2>
        <p className="hint">Belum ada provider AI yang aktif.</p>
        <button className="primary" onClick={onConfigure}>Tambah provider</button>
      </div>
    )
  }

  return (
    <div className="card ai-chat">
      <div className="toolbar">
        <h2>AI Assistant</h2>
        <div className="spacer" />
        <label className="small"><input type="checkbox" checked={withContext} onChange={(e) => setWithContext(e.target.checked)} /> Sertakan data jaringan</label>
        <select value={provider?.id ?? ''} onChange={(e) => setProviderId(Number(e.target.value))}>
          {providers.map((p) => <option key={p.id} value={p.id}>{p.name} — {p.model || KIND_LABEL[p.kind]}</option>)}
        </select>
        {turns.length > 0 && <button onClick={() => setTurns([])}>Percakapan baru</button>}
      </div>
      {provider?.external && withContext && (
        <div className="ai-warning small">
          {provider.name} berada di luar jaringan internal: ringkasan data jaringan yang relevan dengan pertanyaan
          (hostname, utilisasi, layanan) ikut dikirim ke penyedia ini.
        </div>
      )}
      <div className="ai-messages">
        {!turns.length && (
          <div className="ai-empty">
            <p className="muted">Tanyakan kondisi jaringan, kapasitas, layanan, atau konsep jaringan. Sebut hostname, kode POP, circuit ID, atau nama area agar datanya ikut dikirim.</p>
            <div className="ai-examples">
              {EXAMPLES.map((q) => <button key={q} onClick={() => send(q)}>{q}</button>)}
            </div>
          </div>
        )}
        {turns.map((t, i) => (
          <div key={i} className={`ai-turn ${t.role} ${t.error ? 'error' : ''}`}>
            <div className="ai-bubble"><Reply text={t.content} /></div>
            {t.meta && (
              <div className="ai-meta small muted">
                {t.meta.provider} · {t.meta.model} · {(t.meta.duration_ms / 1000).toFixed(1)} dtk
                {t.meta.sources.length > 0 && <> · data: {t.meta.sources.join(', ')}</>}
              </div>
            )}
          </div>
        ))}
        {busy && <div className="ai-turn assistant"><div className="ai-bubble muted">Menyusun jawaban…</div></div>}
        <div ref={bottom} />
      </div>
      <form className="ai-input" onSubmit={onSubmit}>
        <textarea value={input} onChange={(e) => setInput(e.target.value)} onKeyDown={onKey} rows={2} maxLength={20000}
                  placeholder="Tulis pertanyaan… (Enter kirim, Shift+Enter baris baru)" />
        <button className="primary" disabled={busy || !input.trim()}>Kirim</button>
      </form>
    </div>
  )
}

/** Plain text with ``` fenced blocks shown as code; nothing is rendered as HTML. */
function Reply({ text }: { text: string }) {
  const parts = text.split('```')
  return (
    <>
      {parts.map((part, i) => i % 2 === 1
        ? <pre key={i}>{part.replace(/^[a-zA-Z0-9_-]*\n/, '')}</pre>
        : <div key={i} className="ai-text">{part}</div>)}
    </>
  )
}

// --------------------------------------------------------------------------- providers

interface Draft {
  id?: number
  name: string
  kind: Kind
  base_url: string
  model: string
  api_key: string
  clear_key: boolean
  api_version: string
  temperature: string
  max_tokens: string
  timeout_s: string
  enabled: boolean
  is_default: boolean
  has_api_key: boolean
  model_hint?: string
}

function draftFrom(p: Provider): Draft {
  return {
    id: p.id, name: p.name, kind: p.kind, base_url: p.base_url ?? '', model: p.model, api_key: '', clear_key: false,
    api_version: p.api_version ?? '', temperature: p.temperature?.toString() ?? '', max_tokens: p.max_tokens?.toString() ?? '',
    timeout_s: String(p.timeout_s), enabled: p.enabled, is_default: p.is_default, has_api_key: p.has_api_key,
  }
}

function draftFromPreset(preset: Preset): Draft {
  return {
    name: preset.label.split(' (')[0], kind: preset.kind, base_url: preset.base_url, model: preset.model, api_key: '',
    clear_key: false, api_version: preset.api_version ?? '', temperature: '', max_tokens: '', timeout_s: '120',
    enabled: true, is_default: false, has_api_key: false, model_hint: preset.model_hint,
  }
}

function Providers({ data, reload }: { data: { editable: boolean; providers: Provider[]; presets: Preset[] }; reload: () => void }) {
  const [draft, setDraft] = useState<Draft | null>(null)
  const [message, setMessage] = useState<{ text: string; error?: boolean } | null>(null)
  const [testing, setTesting] = useState<number | null>(null)

  const test = (p: Provider) => {
    setTesting(p.id)
    setMessage(null)
    api.post<{ reply: string; model: string; duration_ms: number }>(`/ai/providers/${p.id}/test`)
      .then((r) => setMessage({ text: `${p.name} OK (${r.model}, ${(r.duration_ms / 1000).toFixed(1)} dtk): “${r.reply}”` }))
      .catch((e: Error) => setMessage({ text: `${p.name}: ${e.message}`, error: true }))
      .finally(() => setTesting(null))
  }
  const patch = (p: Provider, body: Partial<Provider>) =>
    api.patch(`/ai/providers/${p.id}`, body).then(reload).catch((e: Error) => setMessage({ text: e.message, error: true }))
  const remove = (p: Provider) => {
    if (!window.confirm(`Hapus provider ${p.name}?`)) return
    api.del(`/ai/providers/${p.id}`).then(reload).catch((e: Error) => setMessage({ text: e.message, error: true }))
  }

  return (
    <>
      <div className="card">
        <div className="toolbar">
          <h2>Provider AI</h2>
          <div className="spacer" />
          {data.editable && (
            <select value="" onChange={(e) => {
              const preset = data.presets.find((p) => p.key === e.target.value)
              if (preset) setDraft(draftFromPreset(preset))
            }}>
              <option value="">+ Tambah provider…</option>
              {data.presets.map((p) => <option key={p.key} value={p.key}>{p.label}</option>)}
            </select>
          )}
        </div>
        <p className="hint">
          Default memakai model lokal (Ollama/vLLM) sehingga data jaringan tidak keluar dari jaringan internal. Provider
          lain (Claude, ChatGPT, Azure OpenAI, Gemini, atau endpoint OpenAI-compatible apa pun) bisa ditambahkan dengan
          API key-nya; key disimpan terenkripsi dan tidak pernah ditampilkan lagi.
          {!data.editable && ' Pengaturan lewat UI dimatikan di server (NADIEN_SETTINGS_UI_ENABLED=false).'}
        </p>
        {message && <div className={message.error ? 'error' : 'notice'}>{message.text}</div>}
        <div className="table-wrap">
          <table>
            <thead>
              <tr><th>Nama</th><th>Jenis</th><th>Model</th><th>Endpoint</th><th>API key</th><th>Lokasi</th><th>Status</th><th /></tr>
            </thead>
            <tbody>
              {data.providers.map((p) => (
                <tr key={p.id}>
                  <td><b>{p.name}</b>{p.is_default && <span className="pill pill-ok">default</span>}</td>
                  <td>{KIND_LABEL[p.kind]}</td>
                  <td>{p.model || '—'}</td>
                  <td className="small">{p.base_url || (p.kind === 'anthropic' ? 'api.anthropic.com' : '—')}</td>
                  <td>{p.has_api_key ? 'tersimpan' : '—'}</td>
                  <td>{p.external ? <span className="pill pill-bad">eksternal</span> : <span className="pill pill-muted">internal</span>}</td>
                  <td>{p.enabled ? 'aktif' : 'nonaktif'}</td>
                  <td className="actions">
                    <button onClick={() => test(p)} disabled={testing === p.id}>{testing === p.id ? 'Menguji…' : 'Tes'}</button>
                    {data.editable && (
                      <>
                        {!p.is_default && <button onClick={() => patch(p, { is_default: true })}>Jadikan default</button>}
                        <button onClick={() => setDraft(draftFrom(p))}>Ubah</button>
                        <button className="danger" onClick={() => remove(p)}>Hapus</button>
                      </>
                    )}
                  </td>
                </tr>
              ))}
              {!data.providers.length && <tr><td colSpan={8} className="empty">Belum ada provider.</td></tr>}
            </tbody>
          </table>
        </div>
      </div>
      {draft && <ProviderForm draft={draft} setDraft={setDraft} onSaved={(text) => { setDraft(null); setMessage({ text }); reload() }} />}
    </>
  )
}

function ProviderForm({ draft, setDraft, onSaved }: { draft: Draft; setDraft: (d: Draft | null) => void; onSaved: (text: string) => void }) {
  const [error, setError] = useState<string | null>(null)
  const set = (patch: Partial<Draft>) => setDraft({ ...draft, ...patch })
  const submit = (e: FormEvent) => {
    e.preventDefault()
    setError(null)
    const body: Record<string, unknown> = {
      name: draft.name, kind: draft.kind, base_url: draft.base_url.trim() || null, model: draft.model.trim(),
      api_version: draft.api_version.trim() || null,
      temperature: draft.temperature === '' ? null : Number(draft.temperature),
      max_tokens: draft.max_tokens === '' ? null : Number(draft.max_tokens),
      timeout_s: Number(draft.timeout_s) || 120, enabled: draft.enabled, is_default: draft.is_default,
    }
    if (draft.api_key) body.api_key = draft.api_key
    else if (draft.clear_key) body.api_key = ''
    const request = draft.id ? api.patch(`/ai/providers/${draft.id}`, body) : api.post('/ai/providers', body)
    request.then(() => onSaved(`Provider ${draft.name} disimpan.`)).catch((err: Error) => setError(err.message))
  }
  return (
    <form className="card" onSubmit={submit}>
      <div className="toolbar">
        <h2>{draft.id ? `Ubah ${draft.name}` : 'Provider baru'}</h2>
        <div className="spacer" />
        <button type="button" onClick={() => setDraft(null)}>Batal</button>
        <button className="primary">Simpan</button>
      </div>
      {error && <div className="error">{error}</div>}
      <div className="mop-meta">
        <label><span>Nama</span><input value={draft.name} onChange={(e) => set({ name: e.target.value })} required maxLength={64} /></label>
        <label><span>Jenis</span>
          <select value={draft.kind} onChange={(e) => set({ kind: e.target.value as Kind })}>
            {(Object.keys(KIND_LABEL) as Kind[]).map((k) => <option key={k} value={k}>{KIND_LABEL[k]}</option>)}
          </select>
        </label>
        <label><span>Endpoint (base URL)</span>
          <input value={draft.base_url} onChange={(e) => set({ base_url: e.target.value })} maxLength={255}
                 placeholder={draft.kind === 'anthropic' ? 'kosong = api.anthropic.com' : 'http://host:port'} />
        </label>
        <label><span>{draft.kind === 'azure_openai' ? 'Deployment' : 'Model'}</span>
          <input value={draft.model} onChange={(e) => set({ model: e.target.value })} maxLength={128} placeholder={draft.model_hint ?? ''} />
        </label>
        <label><span>API key {draft.has_api_key && '(tersimpan — kosongkan untuk tetap)'}</span>
          <input type="password" value={draft.api_key} onChange={(e) => set({ api_key: e.target.value })} maxLength={512} autoComplete="new-password" />
        </label>
        {draft.kind === 'azure_openai' && (
          <label><span>API version</span><input value={draft.api_version} onChange={(e) => set({ api_version: e.target.value })} maxLength={32} /></label>
        )}
        <label><span>Temperature (opsional)</span><input value={draft.temperature} onChange={(e) => set({ temperature: e.target.value })} inputMode="decimal" placeholder="default model" /></label>
        <label><span>Max token jawaban (opsional)</span><input value={draft.max_tokens} onChange={(e) => set({ max_tokens: e.target.value })} inputMode="numeric" placeholder="default" /></label>
        <label><span>Timeout (detik)</span><input value={draft.timeout_s} onChange={(e) => set({ timeout_s: e.target.value })} inputMode="numeric" /></label>
        <label className="check"><input type="checkbox" checked={draft.enabled} onChange={(e) => set({ enabled: e.target.checked })} /><span>Aktif</span></label>
        <label className="check"><input type="checkbox" checked={draft.is_default} onChange={(e) => set({ is_default: e.target.checked })} /><span>Jadikan default</span></label>
        {draft.has_api_key && (
          <label className="check"><input type="checkbox" checked={draft.clear_key} onChange={(e) => set({ clear_key: e.target.checked })} /><span>Hapus API key</span></label>
        )}
      </div>
    </form>
  )
}
