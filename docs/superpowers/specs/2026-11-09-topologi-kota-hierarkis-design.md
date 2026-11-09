# Topologi Kota Hierarkis untuk Monitoring NOC — Desain

Tanggal: 2026-11-09 · Status: disetujui per bagian di chat, menunggu review spec

## 1. Latar belakang & tujuan

Halaman Topologi sulit dibaca di level perangkat: link full/semi-mesh antar baris role digambar sebagai
garis bezier lurus yang menembus node lain dan saling menumpuk. Link paralel digabung per pasangan node, jadi
identitas tiap link (interface A/B) tidak terlihat.

Tujuan: skematik hierarkis **per Kab/Kota** yang bisa dibaca NOC sekilas — apa yang down, apa yang padat, di
mana redundansi dan single point of failure — dengan data LLDP apa adanya.

Pengguna: operator NOC (monitoring) dan engineer (investigasi).

### Keputusan pengguna (brainstorming)

| Topik | Keputusan |
|---|---|
| Tampilan yang paling bermasalah | Perangkat dalam satu POP/kota, dan tetangga 1–2 hop |
| Ring/cluster | Dihitung dari graf LLDP (komponen biconnected), label mengikuti bentuk aktual |
| Status perangkat | Diambil dari Observium (kolom baru) |
| Pendekatan layout | ELK (elkjs) untuk layout + Cytoscape untuk render |
| Unit tampilan | Per Kab/Kota, POP sebagai swimlane |
| Drag/simpan posisi | Dimatikan di tampilan kota & tetangga |

### Constraint keras

- Tidak ada link yang diubah, ditambah, digabung, atau dihapus di data. Link paralel/redundan tetap satu
  record per link; "bundel" hanya cara menggambar (garis sejajar rapat).
- Tidak mengasumsikan ring: label `ring` hanya untuk siklus sederhana yang benar-benar ada.
- Hostname, IP, interface, dan mapping link ditampilkan persis dari DB.
- Rumus kapasitas/warna utilisasi hanya di `backend/app/services/capacity.py`; frontend memakai `color` dari API.
- LLDP tetap sumber wajib topologi (tidak ada perubahan pada `services/topology.py` discovery).

## 2. Lingkup & fase

**Fase 1 (MVP, spec ini):**
1. Level tampilan baru `city`: kotak kota → swimlane per POP → baris layer → cluster ring/mesh → perangkat.
2. Layout ELK di Web Worker: atas-ke-bawah, orthogonal, menghindari node, satu edge per link nyata. Dipakai
   juga untuk level `neighbors`.
3. Deteksi struktur graf: cluster ring/mesh/pair, link SPOF (bridge), perangkat SPOF (articulation point).
4. Status perangkat dari Observium: `reachability`, `uptime_s`, `last_polled_at`.
5. Monitoring: warna status node, warna utilisasi link, "Sorot masalah", "SPOF", panel detail per link.
6. Filter vendor/status/layer; pencarian hostname dan mgmt IP.
7. Pengaman skala (> 600 perangkat): layer Akses per POP diringkas.

**Fase 2:** overview multi-kota (kota sebagai node + trunk antar kota), jalur alternatif/redundansi dari
perangkat terpilih ke core (k jalur disjoint), ringkasan congestion per kota.

**Fase 3:** availability % historis, layer customer/service access, pengelompokan teknologi
(Metro-E/MPLS/DWDM/IP Core — datanya belum ada).

**Di luar lingkup:** perubahan overview area/provinsi/regency, mode peta (tetap seperti sekarang), level POP list
dan level `device` per POP (tetap ada, tetap bisa disimpan tata letaknya).

## 3. Backend

### 3.1 Status perangkat dari Observium

Saat ini `collectors/observium/parsers.py::parse_device` memetakan Observium `status=0` ke
`Device.status="maintenance"`, yang mencampur status operasional dengan lifecycle. Perilaku itu dipertahankan
(layar lain bergantung padanya); ditambah kolom terpisah di `devices`:

| Kolom | Tipe | Default | Sumber Observium |
|---|---|---|---|
| `reachability` | `String(16)` | `server_default='unknown'` | `status` (`1` → `up`, `0` → `down`; `disabled=1` → `unknown`) |
| `uptime_s` | `Integer \| None` | null | `uptime` (detik) |
| `last_polled_at` | `DateTime(tz) \| None` | null | `last_polled` (UTC) |

- `DeviceRecord` diperluas, sync menulis ketiga kolom. Perangkat yang tidak ada di Observium (mis. CSR dari
  RANCID) tetap `unknown`.
- Migrasi Alembic (batch untuk SQLite); `alembic check` harus bersih. Panjang string dicek terhadap PostgreSQL.
- Schema Pydantic perangkat memakai `Literal["up", "down", "unknown"]`.

### 3.2 Struktur graf — `backend/app/services/topology_structure.py`

Fungsi murni tanpa DB:

```python
@dataclass(frozen=True)
class Cluster:
    id: str                     # "c:<id perangkat terkecil>"
    kind: Literal["ring", "mesh", "pair"]
    device_ids: frozenset[int]

@dataclass(frozen=True)
class Structure:
    clusters: list[Cluster]
    cluster_of: dict[int, str]  # device_id -> cluster id (hanya perangkat di cluster)
    spof_links: frozenset[int]  # link_id yang merupakan bridge
    spof_devices: frozenset[int]  # articulation point

def analyse(links: Iterable[tuple[int, int, int]]) -> Structure:  # (link_id, a_device_id, b_device_id)
```

Aturan:
- Graf sederhana atas pasangan perangkat unik; self-loop diabaikan.
- Komponen biconnected dicari dengan Tarjan **iteratif** (tanpa rekursi; graf nyata ±3.5k node, ±9k link), O(V+E).
- Komponen dengan ≥ 3 node: `ring` bila jumlah pasangan unik = jumlah node, `mesh` bila lebih.
- Komponen 2 node (satu pasangan) dengan ≥ 2 link paralel: `pair` (redundan, bukan SPOF).
- Komponen 2 node dengan 1 link: bukan cluster; link itu bridge → `spof_links`.
- Articulation point → `spof_devices`.
- Satu perangkat bisa ada di beberapa komponen biconnected (articulation point). `cluster_of` memilih
  komponen terbesar, lalu id terkecil (deterministik); keanggotaan lengkap tetap ada di `Cluster.device_ids`.
- Dihitung atas **seluruh** graf link (bukan hanya yang tampak), supaya pemotongan tampilan tidak membuat SPOF
  palsu. Hasil disimpan di read-model cache (`services/cache.py`) dan ikut invalidasi versi `app_state`.

### 3.3 Endpoint

`GET /api/topology/view?level=city&area=…&province=…&regency=…` (ketiganya wajib, 422 bila kurang; 404 bila
regency tidak ada). Bentuk respons = `View` yang ada, ditambah:

- `nodes[]` perangkat (`kind="device"`): field yang ada + `mgmt_ip`, `city`, `reachability`, `uptime_s`,
  `last_polled_at`, `cluster_id` (atau null), `spof` (bool).
- `nodes[]` `kind="external_pop"`: POP di kota lain yang terhubung (sama seperti level `device`).
- `pops[]`: `{code, name, city, device_count}` untuk swimlane.
- `clusters[]`: `{id, kind, pop, device_ids}`, hanya cluster yang semua anggotanya di kota ini dan berada di
  layer Metro/Akses (tier 2–3). Cluster yang anggotanya tersebar di beberapa POP digambar di baris layer tanpa
  dibungkus swimlane POP. Anggota di kota lain tidak dibungkus.
- `links[]`: **satu entri per record `links`** yang punya ujung di kota ini:
  `{id, source, target, a_device, a_interface, b_device, b_interface, capacity_gbps, current_gbps,
  utilization_pct, committed_pct, color, status, segment, spof}`. `source`/`target` = id node yang digambar
  (`dev:<id>` atau `pop:<code>` untuk ujung di kota lain). `color` dihitung lewat `capacity.py`.
- `edges[]` agregat dan `segments` tetap ada (statistik, kompatibilitas).
- `layout_key = "city:<area>|<province>|<regency>"` (tidak dipakai untuk menyimpan posisi di level ini).
- `summarized`: daftar `{node_id, pop, count, device_ids}` bila pengaman skala aktif (§4.6).

`level=neighbors` mendapat field perangkat, `links[]`, `clusters[]`, dan `spof` yang sama.

`GET /api/topology/search` juga mencocokkan `mgmt_ip` (exact dulu, lalu prefix).

Kode level kota ditaruh di modul baru `backend/app/services/topology_city.py` (fungsi pembangun view), dipanggil
dari `routers/analysis.py::_topology_view`. Router tidak bertambah logika.

### 3.4 Test backend

- `tests/test_topology_structure.py`:
  - should label ring when 4 devices form a single cycle
  - should label mesh when a component has more pairs than devices
  - should mark link as spof when it is the only path between two parts
  - should not mark parallel links as spof when two links join the same pair
  - should mark device as spof when removing it disconnects the graph
  - should handle large graph without recursion error (≥ 5k node rantai)
- `tests/test_topology_city.py` (seed): should return every link record once when level=city;
  should keep parallel links as separate entries; should return 422 when regency missing.
- Parser Observium (fixture): should map status 0 to reachability down; should map disabled to unknown.

## 4. Frontend

### 4.1 Modul

| File | Tanggung jawab |
|---|---|
| `components/topology/elkGraph.ts` | `View` → graf ELK berhierarki (murni) |
| `components/topology/elk.worker.ts` | menjalankan elkjs di Web Worker; timeout 20 s; error dikirim balik |
| `components/topology/elkLayout.ts` | API promise ke worker (`layoutCity(view)`), batalkan layout lama |
| `components/topology/elkToCytoscape.ts` | hasil ELK → posisi node + `segments` edge Cytoscape (murni) |
| `components/TopologyDetail.tsx` | panel detail node/link (dipindah dari Topology.tsx dan diperluas) |
| `pages/Topology.tsx` | orkestrasi: level `city`, toolbar, filter, toggle |

Dependensi baru: `elkjs` (versi stabil terbaru, dipin), di-bundle Vite sebagai worker (`new Worker(new URL(...),
{ type: 'module' })`).

### 4.2 Graf ELK

Hierarki: root → kota (compound) → swimlane POP (compound, berurutan menurut kode POP) → perangkat.
Baris layer lewat `org.eclipse.elk.partitioning` (partisi = tier: 0 Core, 1 PE/BNG, 2 Metro/Agg, 3 Akses,
4 Lainnya) sehingga Core selalu di atas dan Akses di bawah di setiap swimlane. Cluster ring/mesh = compound
di dalam swimlane POP-nya (atau langsung di kota bila lintas POP). Node `external_pop` di partisi -1 (atas).

Level `neighbors` memakai graf yang sama tanpa kotak kota: satu swimlane per POP yang muncul, perangkat pusat
ditandai `center` seperti sekarang.

Opsi utama: `elk.algorithm=layered`, `elk.direction=DOWN`, `elk.edgeRouting=ORTHOGONAL`,
`elk.hierarchyHandling=INCLUDE_CHILDREN`, `spacing.nodeNode=30`, `layered.spacing.nodeNodeBetweenLayers=50`,
`spacing.edgeEdge=6`, `layered.spacing.edgeEdgeBetweenLayers=6`, `layered.crossingMinimization.strategy=LAYER_SWEEP`.
Ukuran node dari panjang label (seperti `w` sekarang). Satu edge ELK per entri `links[]`.

### 4.3 Konversi ke Cytoscape

ELK memberi posisi absolut node dan titik belok tiap edge. `elkToCytoscape` menghasilkan:
- posisi node (pusat), dan untuk compound ukuran ditentukan Cytoscape dari anak + padding yang sama dengan ELK;
- per edge: `curve-style: segments`, `segment-weights`/`segment-distances` (proyeksi tiap titik belok ke garis
  source→target, `edge-distances: node-position`), `source-endpoint`/`target-endpoint` = titik awal/akhir ELK
  relatif ke pusat node. Edge tanpa titik belok → `straight`.
- Kasus source/target berimpit (jarak 0) dijaga agar tidak membagi nol.

### 4.4 Gaya

- Perangkat: kotak bulat, label hostname (ringkas/lengkap mengikuti pengaturan yang ada), isi warna status:
  hijau `up`, merah `down`, kuning (up tetapi ada link down atau link committed > 80% — dari `color` API),
  abu `unknown`. Warna tepi = role (`ROLE_COLOR` yang ada).
- Swimlane POP: compound berlabel "POP <kode> · <nama>". Cluster: compound putus-putus berlabel
  "Ring · 5 perangkat" / "Mesh · 4" / "Redundan · 2".
- Link: warna `color` dari API (`CAPACITY_HEX`), abu bila kapasitas tidak diketahui, merah putus-putus bila down;
  tebal sesuai kapasitas (`edgeWidth`). Label interface (`source-label`/`target-label`) hanya saat zoom ≥ 1.2.
- SPOF: tepi/underlay oranye saat toggle SPOF aktif.

### 4.5 Interaksi

- Toolbar level kota: filter vendor (multi), status (semua / masalah saja), layer (multi); toggle
  **Sorot masalah** (device down + link merah/kuning terang, sisanya `.faded`) dan **SPOF**. Filter memakai
  `display:none`, tanpa layout ulang.
- Klik node/link: sorot tetangga (seperti sekarang) dan buka `TopologyDetail`.
  - Link: tabel semua link nyata di antara dua node itu — A host:interface ↔ B host:interface, kapasitas,
    trafik, utilisasi %, committed %, status, SPOF.
  - Perangkat: hostname, mgmt IP, kota · POP, role, vendor, reachability, uptime, last poll, SPOF, cluster.
- Double-click perangkat → `neighbors` 1 hop; double-click swimlane POP → level `device` POP itu.
- Navigasi: double-click/“Buka” Kab/Kota membuka level `city` (default baru); level `pop` tetap bisa dibuka
  dari breadcrumb. Breadcrumb mendapat langkah kota.
- Drag dimatikan (`autoungrabify`) di level `city` dan `neighbors`; tombol simpan/atur ulang tata letak
  disembunyikan di level itu.
- Live: bila himpunan node/link sama, hanya data gaya (status, warna, label) yang diperbarui di tempat; bila
  berubah, layout dijalankan ulang di worker, viewport dipertahankan, elemen baru diberi kelas `fresh`.
- Selama layout berjalan tampil "Menyusun tata letak…"; error worker tampil sebagai pesan error, tanpa crash.

### 4.6 Pengaman skala

Dilakukan di backend: bila perangkat kota > 600, perangkat tier 3 (Akses) per POP diganti satu node ringkasan
`summary:<pop>` ("N akses"). Link ke perangkat yang diringkas tetap satu entri per link nyata di `links[]`;
hanya `source`/`target`-nya yang menunjuk id ringkasan, sedangkan `a_device`/`a_interface`/`b_device`/
`b_interface` tetap asli. Klik node ringkasan membuka level `device` POP itu.
Statistik di status bar tetap menghitung semua link.

### 4.7 Verifikasi

- `npm run typecheck` dan `npm run build` lulus; `npm test` (vitest, dev dependency baru) untuk `elkGraph` dan
  `elkToCytoscape`.
- `python -m pytest -q` lulus (test baru + lama).
- Browser (preview worktree, port non-5173, DB nyata):
  - kota kecil dan kota terbesar: tidak ada garis yang menembus node (dicek visual + `javascript_tool`:
    jumlah edge Cytoscape = `links.length`), waktu layout kota terbesar dicatat;
  - tetangga 1 dan 2 hop;
  - klik link paralel → panel menampilkan tiap link dengan interface yang benar;
  - toggle Sorot masalah dan SPOF, filter vendor/status/layer, pencarian mgmt IP.

## 5. Risiko

| Risiko | Mitigasi |
|---|---|
| ELK lambat di kota besar | Worker + pengaman skala §4.6; waktu diukur saat verifikasi |
| Kolom baru gagal di PostgreSQL (tidak terlihat di SQLite) | Panjang/tipe kolom eksplisit, migrasi dicek |
| SPOF palsu karena link ke perangkat di luar Observium | Dihitung atas seluruh graf; panel menyebut "berdasarkan link yang diketahui" |
| Operator kehilangan susunan manual | Hanya level `city`/`neighbors` yang tanpa drag; level `device` per POP tetap |
