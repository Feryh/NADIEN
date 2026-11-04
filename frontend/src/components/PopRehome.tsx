import { useState } from 'react'
import { api } from '../api'

interface RehomeResult {
  applied: boolean
  backup: { status: string; name?: string } | null
  devices_moved: number
  moves: { from: string; to: string; devices: number; examples: string[] }[]
  pops_created: string[]
  pops_deleted: string[]
  non_city_pops_left: { code: string; devices: number }[] | null
}

/** Review POPs against the hostname rule (first registered city code wins), preview first, then apply. */
export default function PopRehome({ onApplied }: { onApplied: () => void }) {
  const [result, setResult] = useState<RehomeResult | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function run(apply: boolean) {
    if (apply && !confirm(`Pindahkan ${result?.devices_moved} perangkat ke POP sesuai hostname dan hapus POP yang jadi kosong? Database di-backup dulu.`)) return
    setBusy(true)
    setError(null)
    try {
      const r = await api.post<RehomeResult>(`/pops/rehome?apply=${apply}`)
      setResult(r)
      if (apply) onApplied()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="card">
      <div className="toolbar">
        <h2>Rapikan POP dari hostname</h2>
        <div className="spacer" />
        <button onClick={() => run(false)} disabled={busy}>{busy && !result ? 'Memeriksa…' : 'Periksa'}</button>
        {result && !result.applied && result.devices_moved > 0 && (
          <button className="primary" onClick={() => run(true)} disabled={busy}>Terapkan ({result.devices_moved} perangkat)</button>
        )}
      </div>
      <p className="hint">
        POP diambil dari kode kota terdaftar pertama di hostname, dibaca dari kiri. Kata fungsi seperti NAP, CGN, NIX, MPLS
        dilewati, dan kode 4 huruf seperti JKTU/JKTS tetap dipakai (bagian dari JKT). Perangkat di POP yang bukan kode kota
        dipindah ke POP itu, lalu POP yang jadi kosong dihapus.
      </p>
      {error && <div className="error">{error}</div>}
      {result && (
        <>
          <div className="notice">
            {result.applied
              ? `${result.devices_moved} perangkat dipindah` +
                (result.pops_deleted.length ? ` · POP dihapus: ${result.pops_deleted.join(', ')}` : '') +
                (result.pops_created.length ? ` · POP baru: ${result.pops_created.join(', ')}` : '') +
                (result.backup?.name ? ` · backup ${result.backup.name}` : '')
              : result.devices_moved
                ? `Pratinjau: ${result.devices_moved} perangkat akan dipindah (belum ada yang diubah).`
                : 'Tidak ada perangkat yang perlu dipindah.'}
          </div>
          {result.moves.length > 0 && (
            <div className="table-wrap">
              <table>
                <thead><tr><th>Dari POP</th><th>Ke POP</th><th className="num">Perangkat</th><th>Contoh</th></tr></thead>
                <tbody>
                  {result.moves.map((mv) => (
                    <tr key={`${mv.from}>${mv.to}`}>
                      <td>{mv.from}</td><td>{mv.to}</td><td className="num">{mv.devices}</td>
                      <td className="small">{mv.examples.join(', ')}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {result.non_city_pops_left && result.non_city_pops_left.length > 0 && (
            <p className="hint">
              Masih ada {result.non_city_pops_left.length} POP bukan kode kota terdaftar yang hostname perangkatnya tidak menyebut kota
              lain. Cek manual (edit kode, atau gabungkan ke POP yang benar):{' '}
              {result.non_city_pops_left.slice(0, 40).map((p) => `${p.code} (${p.devices})`).join(', ')}
              {result.non_city_pops_left.length > 40 ? ', …' : ''}
            </p>
          )}
        </>
      )}
    </div>
  )
}
