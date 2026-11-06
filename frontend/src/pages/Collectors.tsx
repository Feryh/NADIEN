import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api'

interface BackupList {
  directory: string
  keep: number
  enabled: boolean
  supported: boolean
  backups: { name: string; size_bytes: number; created_at: string; reason: string }[]
}

interface SyncJob {
  id?: string
  kind: string
  status: 'running' | 'ok' | 'failed' | 'idle'
  phase: string
  started_at: string
  finished_at: string | null
  report: unknown
  error: string | null
}

const PHASES: Record<string, string> = {
  starting: 'memulai',
  backup: 'backup database',
  devices: 'mengambil device',
  ports: 'mengambil port (paling lama)',
  addresses: 'mengambil IP address',
  neighbours: 'mengambil LLDP neighbour',
  discovery: 'membangun topologi',
  utilization: 'menyimpan utilisasi',
  configs: 'membaca config',
  list: 'membaca daftar repo gitweb',
}

interface CollectorStatus {
  observium: { url: string; configured: boolean; auth: string | null; last_sync_at: string | null; last_status: string | null }
  observium_csr: { url: string; configured: boolean; last_sync_at: string | null; last_status: string | null }
  rancid: { source: string | null; configured: boolean }
  oxidized: { url: string | null; configured: boolean }
  gitweb: {
    url: string | null
    configured: boolean
    files: number
    devices_from_gitweb: number
    last_synced_at: string | null
    schedule: { enabled: boolean; daily_at: string | null; timezone: string }
  }
  rrd: { mode: 'api' | 'local' | 'ssh'; path: string; configured: boolean; mapped_ports: number }
  parts: string[]
}

const VENDORS = [
  { value: '', label: 'Auto-detect' },
  { value: 'iosxr', label: 'Cisco IOS-XR' },
  { value: 'vrp', label: 'Huawei VRP' },
  { value: 'junos', label: 'Juniper Junos' },
  { value: 'sros', label: 'Nokia SR OS' },
  { value: 'zte', label: 'ZTE ZXCTN / M6000' },
  { value: 'fiberhome', label: 'FiberHome R8xx' },
  { value: 'ios', label: 'Cisco IOS / NX-OS' },
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

  const [backups, setBackups] = useState<BackupList | null>(null)

  const [job, setJob] = useState<SyncJob | null>(null)
  const [now, setNow] = useState(Date.now())

  const loadStatus = useCallback(() => {
    api.get<CollectorStatus>('/collectors/status').then(setStatus).catch((e: Error) => setError(e.message))
    api.get<BackupList>('/backups').then(setBackups).catch(() => setBackups(null))
  }, [])

  useEffect(loadStatus, [loadStatus])

  // Long syncs run as background jobs on the server; poll until the current one finishes.
  const pollJob = useCallback(async () => {
    while (true) {
      const current = await api.get<SyncJob>('/collectors/jobs/current')
      setJob(current)
      if (current.status !== 'running') {
        if (current.status === 'ok') setResult(current.report)
        if (current.status === 'failed') setError(current.error)
        loadStatus()
        return
      }
      await new Promise((r) => setTimeout(r, 3000))
    }
  }, [loadStatus])

  useEffect(() => {
    api
      .get<SyncJob>('/collectors/jobs/current')
      .then((current) => {
        setJob(current)
        if (current.status === 'running') pollJob().catch((e: Error) => setError(e.message))
      })
      .catch(() => undefined)
  }, [pollJob])

  useEffect(() => {
    if (job?.status !== 'running') return
    const t = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(t)
  }, [job?.status])

  async function startJob(path: string) {
    setError(null)
    setResult(null)
    try {
      setJob(await api.post<SyncJob>(path))
      await pollJob()
    } catch (e) {
      setError((e as Error).message)
    }
  }

  const jobRunning = job?.status === 'running'
  const elapsed = job ? Math.round(((jobRunning ? now : Date.parse(job.finished_at ?? '') || now) - Date.parse(job.started_at)) / 1000) : 0

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
  const [rrdDays, setRrdDays] = useState('90')
  const rrdReady = !!status?.rrd.configured && (status?.rrd.mapped_ports ?? 0) > 0

  return (
    <div className="capacity">
      <div className="card">
        <div className="toolbar">
          <h2>Observium</h2>
          <span className={`pill ${status?.observium.configured ? 'pill-ok' : 'pill-bad'}`}>
            {status?.observium.configured ? `credentials: ${status.observium.auth}` : 'credentials belum di-set'}
          </span>
          <Link to="/settings" className="small">Pengaturan →</Link>
          <div className="spacer" />
          <button disabled={!!busy || jobRunning} onClick={() => startJob('/collectors/observium/sync?parts=utilization')}>
            Utilisasi saja
          </button>
          <button disabled={!!busy || jobRunning || !status?.observium_csr.configured}
            title="Perangkat CSR, port, dan trafiknya dari Observium CSR"
            onClick={() => startJob('/collectors/observium-csr/sync')}>
            Sync CSR
          </button>
          <button className="primary" disabled={!!busy || jobRunning} onClick={() => startJob('/collectors/observium/sync')}>
            Full sync
          </button>
        </div>
        {job && job.status !== 'idle' && (
          <div className={job.status === 'failed' ? 'error' : 'notice'}>
            {jobRunning ? '⏳ ' : job.status === 'ok' ? '✓ ' : '✗ '}
            Sync <b>{job.kind}</b> — {jobRunning ? `tahap: ${PHASES[job.phase] ?? job.phase}` : job.status === 'ok' ? 'selesai' : 'gagal'} ·{' '}
            {Math.floor(elapsed / 60)}m {elapsed % 60}s
            {!jobRunning && job.finished_at && ` · ${new Date(job.finished_at).toLocaleString()}`}
          </div>
        )}
        <p className="hint">
          {status?.observium.url} · device, port, IPv4, LLDP neighbour, dan rate port → inventory, topologi, sampel utilisasi.
          {status?.observium.last_sync_at && ` Terakhir: ${new Date(status.observium.last_sync_at).toLocaleString()} — ${status.observium.last_status}`}
        </p>
        {status?.observium_csr.url && (
          <p className="hint">
            CSR: {status.observium_csr.url} · perangkat CSR, port, dan rate port (tanpa LLDP).
            {status.observium_csr.last_sync_at
              ? ` Terakhir: ${new Date(status.observium_csr.last_sync_at).toLocaleString()} — ${status.observium_csr.last_status}`
              : ' Belum pernah di-sync.'}
          </p>
        )}
        {!status?.observium.configured && (
          <p className="hint">
            Isi URL dan token/login Observium di <Link to="/settings">Pengaturan Collector</Link> (tanpa restart), atau di{' '}
            <code>backend/.env</code>: <code>NADIEN_OBSERVIUM_TOKEN=…</code> atau <code>NADIEN_OBSERVIUM_USER</code> +{' '}
            <code>NADIEN_OBSERVIUM_PASSWORD</code>.
          </p>
        )}
      </div>

      <div className="card">
        <div className="toolbar">
          <h2>Histori utilisasi (RRD Observium)</h2>
          <span className={`pill ${status?.rrd.configured ? 'pill-ok' : 'pill-bad'}`}>
            {status?.rrd.configured
              ? status.rrd.mode === 'api'
                ? 'via API Observium'
                : `via ${status.rrd.mode}`
              : status?.rrd.mode === 'api'
                ? 'kredensial Observium belum di-set'
                : 'akses SSH belum di-set'}
          </span>
          <span className={`pill ${status?.rrd.mapped_ports ? 'pill-ok' : 'pill-muted'}`}>
            {status?.rrd.mapped_ports ?? 0} port ter-mapping
          </span>
          <div className="spacer" />
          <select value={rrdDays} onChange={(e) => setRrdDays(e.target.value)} disabled={jobRunning}>
            <option value="7">7 hari</option>
            <option value="30">30 hari</option>
            <option value="90">90 hari</option>
          </select>
          <button
            className="primary"
            disabled={!!busy || jobRunning || !rrdReady}
            onClick={() => startJob(`/collectors/observium/rrd?days=${rrdDays}`)}
          >
            Ambil histori RRD
          </button>
          <button
            disabled={!!busy || jobRunning || !status?.observium_csr.configured}
            title="Histori link yang hanya terlihat oleh Observium CSR; link dengan port di server utama tetap memakai server utama"
            onClick={() => startJob(`/collectors/observium-csr/rrd?days=${rrdDays}`)}
          >
            Histori CSR
          </button>
        </div>
        {status?.rrd.mode === 'api' ? (
          <p className="hint">
            Peak harian in/out tiap link diambil dari RRD Observium lewat <code>graph.php?format=json</code>, dengan login API yang
            sama (tanpa SSH). 7 hari terakhir memakai data 5 menit (peak akurat). Hari yang lebih lama memakai rata-rata 2 jam, jadi
            peak-nya sedikit lebih rendah dari aslinya. Satu link kira-kira 0,3 detik, jadi seluruh link sekitar 45 menit. Hasilnya
            menggantikan sampel di rentang itu dan dipakai untuk tren 7 / 30 / 90 hari di halaman Capacity. Tidak dibuat backup.
          </p>
        ) : (
          <p className="hint">
            Peak harian (RRA MAX) in/out tiap link dibaca langsung dari file RRD Observium (<code>{status?.rrd.path}</code>) dengan{' '}
            <code>rrdtool xport</code>, menggantikan sampel di rentang itu. Hasilnya dipakai untuk tren 7 / 30 / 90 hari di halaman
            Capacity. Tidak dibuat backup (hanya menulis sampel utilisasi).
          </p>
        )}
        {!status?.rrd.configured && status?.rrd.mode !== 'api' && (
          <p className="hint">
            Set di <code>backend/.env</code>: <code>NADIEN_OBSERVIUM_SSH_USER</code> + <code>NADIEN_OBSERVIUM_SSH_KEY_FILE</code>{' '}
            (atau <code>_PASSWORD</code>) — akun read-only ke server Observium yang bisa membaca{' '}
            <code>/opt/observium/rrd</code> dan menjalankan <code>rrdtool</code>. Host key server harus sudah ada di{' '}
            <code>known_hosts</code>. Lalu restart backend.
          </p>
        )}
        {status?.rrd.configured && !status.rrd.mapped_ports && (
          <p className="hint">Belum ada mapping port → file RRD (ifIndex). Jalankan Full sync Observium dulu.</p>
        )}
        <p className="hint">
          Catatan: RRD port Observium punya batas DS 100 Gbps; trafik di atas itu (link 400G) tersimpan sebagai NaN dan hari itu
          terlewat. Link yang kemungkinan terpotong dilaporkan di <code>possibly_clipped</code>.
        </p>
      </div>

      <div className="card">
        <div className="toolbar">
          <h2>RANCID gitweb (semua repo)</h2>
          <span className={`pill ${status?.gitweb.configured ? 'pill-ok' : 'pill-bad'}`}>
            {status?.gitweb.configured ? 'siap' : 'belum di-set'}
          </span>
          {status?.gitweb.schedule.enabled && (
            <span className="pill pill-ok">
              otomatis tiap hari {status.gitweb.schedule.daily_at} ({status.gitweb.schedule.timezone})
            </span>
          )}
          <div className="spacer" />
          <button
            disabled={!!busy || jobRunning || !status?.gitweb.configured}
            onClick={() => startJob('/collectors/configs/gitweb/sync?full=true')}
            title="Baca ulang semua file, bukan hanya yang berubah"
          >
            Sync penuh
          </button>
          <button
            className="primary"
            disabled={!!busy || jobRunning || !status?.gitweb.configured}
            onClick={() => startJob('/collectors/configs/gitweb/sync')}
          >
            Ambil perubahan terbaru
          </button>
        </div>
        <p className="hint">
          {status?.gitweb.url && <code>{status.gitweb.url}</code>} · semua repo RANCID (CSR, METRO, ROUTER, BNG, CGN, OLT, …). Hanya file
          yang berubah sejak sync terakhir yang diunduh. Perangkat yang tidak ada di Observium (mis. CSR) dibuat dari config-nya;
          perangkat di repo CSR-* selalu ber-role <b>CSR</b>. Config OLT dilaporkan sebagai tidak didukung.
          {status?.gitweb.configured &&
            ` ${status.gitweb.files.toLocaleString()} file tercatat · ${status.gitweb.devices_from_gitweb.toLocaleString()} perangkat hanya dari gitweb` +
              (status.gitweb.last_synced_at ? ` · terakhir ${new Date(status.gitweb.last_synced_at).toLocaleString()}` : '')}
        </p>
        {!status?.gitweb.configured && (
          <p className="hint">
            Isi URL gitweb (mis. <code>http://172.17.12.232/gitweb/</code>) dan jadwal harian di{' '}
            <Link to="/settings">Pengaturan Collector</Link>, atau <code>NADIEN_GITWEB_URL</code> di <code>backend/.env</code>.
          </p>
        )}
      </div>

      <div className="card">
        <div className="toolbar">
          <h2>Konfigurasi device</h2>
          {/* Device configs arrive through either RANCID path: the SSH/folder archive here or gitweb above. */}
          <span className={`pill ${status?.rancid.configured || status?.gitweb.configured ? 'pill-ok' : 'pill-bad'}`}
            title={!status?.rancid.configured && status?.gitweb.configured
              ? 'Config perangkat diambil lewat RANCID gitweb; akses SSH/folder RANCID tidak diperlukan' : undefined}>
            {status?.rancid.configured ? 'RANCID siap' : status?.gitweb.configured ? 'Lewat RANCID gitweb' : 'RANCID belum di-set'}
          </span>
          {status?.oxidized.configured && <span className="pill pill-ok">Oxidized siap</span>}
          <div className="spacer" />
          {status?.oxidized.configured && (
            <button disabled={!!busy || jobRunning} onClick={() => startJob('/collectors/configs/oxidized/sync')}>
              Sync dari Oxidized
            </button>
          )}
          {status?.rancid.configured || !status?.gitweb.configured ? (
            <button
              className="primary"
              disabled={!!busy || jobRunning || !status?.rancid.configured}
              onClick={() => startJob('/collectors/configs/rancid/sync')}
            >
              Sync semua dari RANCID
            </button>
          ) : (
            <button className="primary" disabled={!!busy || jobRunning} onClick={() => startJob('/collectors/configs/gitweb/sync')}
              title="Sama dengan “Ambil perubahan terbaru” di kartu RANCID gitweb">
              Sync dari RANCID gitweb
            </button>
          )}
        </div>
        <p className="hint">
          API Observium tidak menyediakan config, jadi config dibaca dari arsip RANCID yang dipakai Observium
          {status?.rancid.source ? <> (<code>{status.rancid.source}</code>)</> : null}. Config melengkapi: keanggotaan LAG, IP &amp; MTU
          interface, loopback, IS-IS NET, SRv6 locator.
        </p>
        {!status?.rancid.configured && status?.gitweb.configured && (
          <p className="hint">
            Config semua perangkat sudah diambil lewat <b>RANCID gitweb</b> (kartu di atas), jadi tidak ada yang perlu di-set di sini.
            Akses SSH/folder RANCID hanya alternatif bila gitweb tidak tersedia.
          </p>
        )}
        {!status?.rancid.configured && !status?.gitweb.configured && (
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
            disabled={!!busy || jobRunning || !configText.trim() || !hostname.trim()}
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

      <div className="card">
        <div className="toolbar">
          <h2>Backup database</h2>
          <span className={`pill ${backups?.enabled && backups.supported ? 'pill-ok' : 'pill-bad'}`}>
            {backups?.enabled ? `otomatis sebelum sync · simpan ${backups.keep} terakhir` : 'backup otomatis nonaktif'}
          </span>
          <div className="spacer" />
          <button disabled={!!busy || jobRunning || !backups?.supported} onClick={() => run('backup', () => api.post('/backups'))}>
            {busy === 'backup' ? 'Membuat backup…' : 'Backup sekarang'}
          </button>
        </div>
        <p className="hint">
          Dibuat sebelum setiap Full sync Observium, sync config RANCID/Oxidized, dan import config (sync utilisasi saja tidak
          di-backup). Lokasi: <code>{backups?.directory}</code>. Restore lewat terminal:{' '}
          <code>python -m app.backup restore &lt;nama file&gt;</code> (kondisi saat itu otomatis di-backup dulu sebagai pre-restore).
        </p>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>File</th>
                <th>Waktu</th>
                <th>Alasan</th>
                <th className="num">Ukuran</th>
              </tr>
            </thead>
            <tbody>
              {backups?.backups.map((b) => (
                <tr key={b.name}>
                  <td>
                    <code>{b.name}</code>
                  </td>
                  <td>{new Date(b.created_at).toLocaleString()}</td>
                  <td>{b.reason}</td>
                  <td className="num">{(b.size_bytes / 1024).toFixed(0)} KB</td>
                </tr>
              ))}
              {!backups?.backups.length && (
                <tr>
                  <td colSpan={4} className="empty">
                    Belum ada backup
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
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
