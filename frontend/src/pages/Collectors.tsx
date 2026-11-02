import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'

interface CollectorStatus {
  observium: { url: string; configured: boolean; auth: string | null; last_sync_at: string | null; last_status: string | null }
  rancid: { source: string | null; configured: boolean }
  oxidized: { url: string | null; configured: boolean }
  parts: string[]
}

const VENDORS = [
  { value: '', label: 'Auto-detect' },
  { value: 'iosxr', label: 'Cisco IOS-XR' },
  { value: 'vrp', label: 'Huawei VRP' },
  { value: 'junos', label: 'Juniper Junos' },
  { value: 'sros', label: 'Nokia SR OS' },
]

async function postText<T>(path: string, text: string): Promise<T> {
  const res = await fetch(`/api${path}`, { method: 'POST', headers: { 'Content-Type': 'text/plain' }, body: text })
  const data = await res.json().catch(() => ({}))
  if (!res.ok) throw new Error(`${res.status} — ${typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail)}`)
  return data as T
}

export default function Collectors() {
  const [status, setStatus] = useState<CollectorStatus | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [result, setResult] = useState<unknown>(null)
  const [error, setError] = useState<string | null>(null)
  const [hostname, setHostname] = useState('')
  const [vendor, setVendor] = useState('')
  const [configText, setConfigText] = useState('')

  const loadStatus = useCallback(() => {
    api.get<CollectorStatus>('/collectors/status').then(setStatus).catch((e: Error) => setError(e.message))
  }, [])

  useEffect(loadStatus, [loadStatus])

  async function run(label: string, action: () => Promise<unknown>) {
    setBusy(label)
    setError(null)
    setResult(null)
    try {
      setResult(await action())
      loadStatus()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(null)
    }
  }

  const query = vendor ? `?vendor=${vendor}` : ''

  return (
    <div className="capacity">
      <div className="card">
        <div className="toolbar">
          <h2>Observium</h2>
          <span className={`pill ${status?.observium.configured ? 'pill-ok' : 'pill-bad'}`}>
            {status?.observium.configured ? `credentials: ${status.observium.auth}` : 'credentials belum di-set'}
          </span>
          <div className="spacer" />
          <button disabled={!!busy} onClick={() => run('util', () => api.post('/collectors/observium/sync?parts=utilization'))}>
            {busy === 'util' ? 'Menarik…' : 'Utilisasi saja'}
          </button>
          <button className="primary" disabled={!!busy} onClick={() => run('full', () => api.post('/collectors/observium/sync'))}>
            {busy === 'full' ? 'Sync berjalan…' : 'Full sync'}
          </button>
        </div>
        <p className="hint">
          {status?.observium.url} · device, port, IPv4, LLDP neighbour, dan rate port → inventory, topologi, sampel utilisasi.
          {status?.observium.last_sync_at && ` Terakhir: ${new Date(status.observium.last_sync_at).toLocaleString()} — ${status.observium.last_status}`}
        </p>
        {!status?.observium.configured && (
          <p className="hint">
            Set di <code>backend/.env</code>: <code>NADIEN_OBSERVIUM_TOKEN=…</code> (disarankan: token read-only, dibatasi IP) atau{' '}
            <code>NADIEN_OBSERVIUM_USER</code> + <code>NADIEN_OBSERVIUM_PASSWORD</code>, lalu restart backend.
          </p>
        )}
      </div>

      <div className="card">
        <div className="toolbar">
          <h2>Konfigurasi device</h2>
          <span className={`pill ${status?.rancid.configured ? 'pill-ok' : 'pill-bad'}`}>
            RANCID {status?.rancid.configured ? 'siap' : 'belum di-set'}
          </span>
          {status?.oxidized.configured && <span className="pill pill-ok">Oxidized siap</span>}
          <div className="spacer" />
          {status?.oxidized.configured && (
            <button disabled={!!busy} onClick={() => run('oxidized', () => api.post('/collectors/configs/oxidized/sync'))}>
              {busy === 'oxidized' ? 'Menarik config…' : 'Sync dari Oxidized'}
            </button>
          )}
          <button
            className="primary"
            disabled={!!busy || !status?.rancid.configured}
            onClick={() => run('rancid', () => api.post('/collectors/configs/rancid/sync'))}
          >
            {busy === 'rancid' ? 'Membaca RANCID…' : 'Sync semua dari RANCID'}
          </button>
        </div>
        <p className="hint">
          API Observium tidak menyediakan config, jadi config dibaca dari arsip RANCID yang dipakai Observium
          {status?.rancid.source ? <> (<code>{status.rancid.source}</code>)</> : null}. Config melengkapi: keanggotaan LAG, IP &amp; MTU
          interface, loopback, IS-IS NET, SRv6 locator.
        </p>
        {!status?.rancid.configured && (
          <p className="hint">
            Set di <code>backend/.env</code>: <code>NADIEN_RANCID_SSH_HOST</code> + <code>NADIEN_RANCID_SSH_USER</code> +{' '}
            <code>NADIEN_RANCID_SSH_KEY_FILE</code> (SFTP read-only ke server Observium, path default{' '}
            <code>/opt/observium/rancid</code>) atau <code>NADIEN_RANCID_PATH</code> kalau folder di-mount lokal.
          </p>
        )}
        <p className="hint">Atau tempel config satu device di bawah:</p>
        <div className="calc-form">
          <label>
            <span>Hostname (harus sudah ada di inventory)</span>
            <input value={hostname} onChange={(e) => setHostname(e.target.value)} placeholder="JKT-P-01" />
          </label>
          <label>
            <span>Vendor</span>
            <select value={vendor} onChange={(e) => setVendor(e.target.value)}>
              {VENDORS.map((v) => (
                <option key={v.value} value={v.value}>
                  {v.label}
                </option>
              ))}
            </select>
          </label>
          <button disabled={!!busy || !configText.trim()} onClick={() => run('parse', () => postText(`/collectors/configs/parse${query}`, configText))}>
            Preview parse
          </button>
          <button
            className="primary"
            disabled={!!busy || !configText.trim() || !hostname.trim()}
            onClick={() => run('apply', () => postText(`/collectors/configs/${encodeURIComponent(hostname.trim())}${query}`, configText))}
          >
            Import ke inventory
          </button>
        </div>
        <textarea
          className="config-input"
          rows={12}
          value={configText}
          onChange={(e) => setConfigText(e.target.value)}
          placeholder="Tempel running-config / current-configuration / show configuration / admin display-config di sini"
        />
      </div>

      {error && <div className="error">{error}</div>}
      {result !== null && (
        <div className="card">
          <h2>Hasil</h2>
          <pre className="result">{JSON.stringify(result, null, 2)}</pre>
        </div>
      )}
    </div>
  )
}
