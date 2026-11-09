# Topologi Kota Hierarkis — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Level topologi `city` (per Kab/Kota, POP sebagai swimlane) dengan layout ELK orthogonal tanpa garis menumpuk, status perangkat dari Observium, dan sorotan ring/mesh/SPOF untuk monitoring NOC; level `neighbors` memakai layout yang sama.

**Architecture:** Backend menghitung struktur graf (biconnected/bridge/articulation) atas seluruh link dan membangun view `city` berisi satu entri per link nyata. Frontend mengubah view menjadi graf ELK berhierarki, menjalankan elkjs di Web Worker, lalu menggambar hasilnya di Cytoscape dengan `curve-style: segments`.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.0, Alembic, pytest · React 18, TypeScript strict, Vite 8, Cytoscape.js 3.30, elkjs (baru), vitest (baru, dev).

**Spec:** `docs/superpowers/specs/2026-11-09-topologi-kota-hierarkis-design.md`

## Global Constraints

- Tidak ada link yang diubah/ditambah/digabung/dihapus di data; `links[]` = satu entri per record `links`.
- Label `ring` hanya untuk komponen biconnected dengan jumlah pasangan unik = jumlah node ≥ 3; tidak ada asumsi ring.
- Hostname, mgmt IP, nama interface ditampilkan persis dari DB.
- Warna kapasitas hanya dari `backend/app/services/capacity.py::capacity_color`; frontend memakai field `color`.
- `services/topology.py` (discovery LLDP) tidak diubah.
- Semua endpoint di `/api`; 422 parameter kurang, 404 tidak ditemukan; enum di schema sebagai `Literal[...]`; satuan di nama field (`_gbps`, `_pct`, `_s`).
- Nama test: `test_should_<hasil>_when_<kondisi>`.
- Python: type hints di semua signature; fungsi > ~50 baris dipecah.
- TypeScript strict, tanpa `any` (kecuali tipe library, dengan komentar).
- Ambang pengaman skala: 600 perangkat. Ambang link "tinggi": `color` `yellow`/`red` dari API (committed > 80% mengikuti `capacity.py`).
- Node tidak ada di PATH Windows: jalankan npm via `C:\Program Files\nodejs`. Test backend di WSL: `~/.nadien-venv/bin/python -m pytest -q` di `backend/`.
- Commit hanya bila pengguna sudah mengizinkan (aturan proyek); langkah "Commit" di bawah dilewati bila belum ada izin. Jangan push.

## Review Focus

1. **Perangkat tanpa link / kota tanpa link** — view tetap tampil (node tanpa edge, ELK tidak error), bukan layar kosong atau crash. → test Task 4 (`test_should_return_isolated_devices_when_city_has_no_links`) dan Task 6 (`elkGraph` dengan `links: []`).
2. **Link yang kedua ujungnya perangkat yang sama, atau ujung yang perangkatnya hilang** — diabaikan di struktur dan tidak membuat edge ELK ke node yang tidak ada. → Task 3 (`self-loop`), Task 6 (`should_skip_link_when_endpoint_not_in_nodes`).
3. **Titik belok ELK segaris dengan source→target atau source/target berimpit** — konversi ke segments tidak menghasilkan `NaN`. → Task 6 (`toSegments` kasus jarak 0).
4. **Observium `last_polled` kosong/format aneh, `uptime` bukan angka** — kolom jadi null, sync tidak gagal. → Task 2 (`test_should_leave_last_polled_empty_when_value_invalid`).
5. **Live refresh saat layout sebelumnya masih berjalan** — layout lama dibatalkan, hasil lama tidak menimpa view baru. → Task 7 (`layoutCity` membatalkan request sebelumnya; diverifikasi di Task 10).

---

### Task 1: Pindahkan read-model topologi ke service

`_topology_view` dan pembantunya ada di `backend/app/routers/analysis.py`; builder kota baru di service tidak boleh mengimpor router.

**Files:**
- Create: `backend/app/services/topology_readmodel.py`
- Modify: `backend/app/routers/analysis.py` (hapus definisi yang dipindah, impor dari service)

**Interfaces:**
- Produces (pindah apa adanya, nama dan perilaku tetap): `_Ctx`, `_context(db) -> _Ctx`, `_Agg`, `_links(db)`, `_edges(aggs)`, `_segment_of(ctx, link) -> str`, `_segments(totals)`, `_pair(a, b)`, `_device_node(ctx, d, center=False) -> dict`, `_place_devices(ctx, nodes) -> None`, `_path_fields(path) -> dict`, `_pop_labels(ctx, pop_ids) -> dict[int, str]`, beserta konstanta yang mereka pakai. Nama diekspor tanpa garis bawah: `Ctx`, `context`, `Agg`, `load_links`, `edges_of`, `segment_of`, `segments_of`, `pair`, `device_node`, `place_devices`, `path_fields`, `pop_labels`; router mengimpor dengan alias nama lama supaya diff router kecil.

- [ ] **Step 1:** Pindahkan definisi; jangan ubah logika.
- [ ] **Step 2:** Run `~/.nadien-venv/bin/python -m pytest -q` di `backend/`. Expected: semua lulus, jumlah sama dengan sebelum perubahan.
- [ ] **Step 3:** Commit `refactor: move topology read model helpers to services`.

---

### Task 2: Reachability perangkat dari Observium

**Files:**
- Modify: `backend/app/models.py` (class `Device`)
- Create: `backend/app/migrations/versions/0014_device_reachability.py`
- Modify: `backend/app/collectors/observium/parsers.py` (`DeviceRecord`, `parse_device`)
- Modify: `backend/app/collectors/observium/sync.py:~176` (tulis kolom baru)
- Modify: `backend/app/schemas.py` (`DeviceRead`)
- Test: `backend/tests/test_observium_parsers.py`, `backend/tests/test_observium_sync.py`

**Interfaces:**
- Produces: `Device.reachability: Mapped[str]` (`String(16)`, `default="unknown"`, `server_default="unknown"`), `Device.uptime_s: Mapped[int | None]` (`BigInteger`), `Device.last_polled_at: Mapped[datetime | None]` (`DateTime(timezone=True)`); `DeviceRecord.reachability: str`, `.uptime_s: int | None`, `.last_polled_at: datetime | None` (default `"unknown"`, `None`, `None`); `DeviceRead.reachability: Literal["up", "down", "unknown"]`, `uptime_s: int | None`, `last_polled_at: datetime | None`.

- [ ] **Step 1: Failing tests** di `test_observium_parsers.py`:
  - `test_should_map_status_0_to_reachability_down_when_device_enabled`: row `{"device_id": 1, "hostname": "JKT-P1", "status": "0", "disabled": "0", "uptime": "3600", "last_polled": "2026-11-09 10:00:00"}` → `reachability == "down"`, `uptime_s == 3600`, `last_polled_at == datetime(2026, 11, 9, 3, 0, tzinfo=timezone.utc)` (Observium memakai waktu lokal server = `settings.schedule_timezone` Asia/Jakarta).
  - `test_should_map_disabled_to_reachability_unknown_when_disabled`: `disabled: "1"` → `"unknown"`.
  - `test_should_leave_last_polled_empty_when_value_invalid`: `last_polled: ""` dan `"0000-00-00 00:00:00"`, `uptime: "abc"` → `None`, `None`, tanpa exception.
  - `test_should_keep_status_maintenance_when_observium_down` (perilaku lama `status` tetap).
- [ ] **Step 2:** Run `pytest tests/test_observium_parsers.py -q`. Expected: FAIL (atribut tidak ada).
- [ ] **Step 3:** Implement kolom model, field `DeviceRecord`, parsing di `parse_device` (helper `_observium_time(value: Any) -> datetime | None` memakai `zoneinfo.ZoneInfo(settings.schedule_timezone)`), dan penulisan di sync di sebelah `device.status = rec.status`.
- [ ] **Step 4:** Migrasi `0014` dengan `op.batch_alter_table("devices")`, `down_revision = "0013_if_index_bigint"` (cek id revisi persis di file 0013). Run `alembic upgrade head` lalu `alembic check` pada SQLite sementara (`NADIEN_DATABASE_URL=sqlite:////tmp/nadien-plan.db`). Expected: "No new upgrade operations detected."
- [ ] **Step 5:** Test sync di `test_observium_sync.py`: `test_should_store_reachability_when_device_synced` (pakai `observium_transport`; tambahkan `status`/`uptime`/`last_polled` ke satu device di `tests/fixtures/observium/api.json`). Run seluruh `pytest -q`. Expected: PASS.
- [ ] **Step 6:** Commit `feat: store Observium device reachability, uptime and last poll`.

---

### Task 3: Struktur graf (ring/mesh/pair, SPOF)

**Files:**
- Create: `backend/app/services/topology_structure.py`
- Test: `backend/tests/test_topology_structure.py`

**Interfaces:**
- Produces: `Cluster(id: str, kind: Literal["ring", "mesh", "pair"], device_ids: frozenset[int])`, `Structure(clusters: list[Cluster], cluster_of: dict[int, str], spof_links: frozenset[int], spof_devices: frozenset[int])`, `analyse(links: Iterable[tuple[int, int, int]]) -> Structure` — persis seperti spec §3.2 (dataclass frozen; `Cluster.id = "c:<terkecil>-<terkecil kedua>"`).

- [ ] **Step 1: Failing tests** (link = `(link_id, a, b)`):
  - `test_should_label_ring_when_four_devices_form_one_cycle`: `[(1,1,2),(2,2,3),(3,3,4),(4,4,1)]` → satu cluster `kind="ring"`, `device_ids={1,2,3,4}`, `spof_links == frozenset()`, `spof_devices == frozenset()`.
  - `test_should_label_mesh_when_component_has_more_pairs_than_devices`: ring di atas + `(5,1,3)` → `kind="mesh"`.
  - `test_should_mark_link_as_spof_when_it_is_the_only_path`: ring + `(6,4,5)` → `spof_links == {6}`, `spof_devices == {4}`, device 5 tidak di `cluster_of`.
  - `test_should_label_pair_when_two_parallel_links_join_same_devices`: `[(1,1,2),(2,1,2)]` → cluster `pair` `{1,2}`, `spof_links == frozenset()`.
  - `test_should_ignore_self_loop_when_both_ends_same_device`: `[(1,1,1)]` → tanpa cluster, tanpa SPOF.
  - `test_should_assign_articulation_device_to_largest_cluster_when_in_two`: ring 1-2-3-4 + ring 4-5-6 (`(5,4,5),(6,5,6),(7,6,4)`) → `cluster_of[4] == "c:1-2"`.
  - `test_should_handle_long_chain_without_recursion_error`: rantai 6000 node → `len(spof_links) == 5999`.
- [ ] **Step 2:** Run `pytest tests/test_topology_structure.py -q`. Expected: FAIL (modul tidak ada).
- [ ] **Step 3:** Implement `analyse`: graf sederhana atas pasangan unik (hitung multiplisitas per pasangan), Tarjan biconnected **iteratif** dengan stack edge; bridge = komponen 2 node dengan multiplisitas 1; klasifikasi sesuai spec.
- [ ] **Step 4:** Run lagi. Expected: PASS.
- [ ] **Step 5:** Commit `feat: detect rings, meshes and single points of failure from links`.

---

### Task 4: View `level=city`

**Files:**
- Create: `backend/app/services/topology_city.py`
- Modify: `backend/app/routers/analysis.py` (`topology_view`: pola level menerima `city`; `_topology_view` memanggil builder; `layout_key` untuk `city`; `topology_search` mencocokkan `mgmt_ip`)
- Modify: `backend/app/services/topology_readmodel.py` (`context` ikut membaca `mgmt_ip`, `reachability`, `uptime_s`, `last_polled_at`; `device_node` menambah field tersebut)
- Test: `backend/tests/test_topology_city.py`

**Interfaces:**
- Consumes: Task 1 (`context`, `Agg`, `edges_of`, `segments_of`, `device_node`, `pop_labels`, `path_fields`, `load_links`), Task 3 (`analyse`, `Structure`).
- Produces:
  - `network_structure(db: Session) -> Structure` — `cache.cached(("topology-structure",), ...)` atas seluruh link.
  - `city_view(db: Session, ctx: Ctx, area: str, province: str, regency: str) -> dict` — bentuk persis spec §3.3: `level="city"`, `area`, `province`, `regency`, `nodes`, `pops`, `clusters`, `links`, `edges`, `segments`, `summarized`.
  - `link_entries(db: Session, ctx: Ctx, link_ids: set[int], node_of: Callable[[int], str], structure: Structure) -> list[dict]` — satu dict per link: `id, source, target, a_device, a_interface, b_device, b_interface, capacity_gbps, current_gbps, utilization_pct, committed_pct, color, status, segment, spof`; nama interface di-join dari `interfaces` (null bila tidak ada); `color` via `capacity_color`.
  - Node perangkat mendapat: `mgmt_ip, city, reachability, uptime_s, last_polled_at` (ISO 8601 UTC), `cluster_id`, `spof`.
  - `SUMMARY_THRESHOLD = 600`; node ringkasan `{"id": f"summary:{pop}", "kind": "summary", "pop": pop, "label": f"{n} akses", "devices": n, "tier": 3}`.
  - `layout_key` untuk city: `f"city:{area}|{province}|{regency}"`.

- [ ] **Step 1: Failing tests** (fixture seperti `network` di `test_topology_regions.py`: dua POP di satu regency, satu POP di regency lain; perangkat P/PE/AGG/OLT; link paralel 2× antara dua PE; ring AGG 3 perangkat; satu link ke POP regency lain):
  - `test_should_return_every_link_once_when_level_city`: `len(view["links"]) == ` jumlah record `Link` yang punya ujung di kota itu; `id` unik.
  - `test_should_keep_parallel_links_separate_when_two_links_join_same_devices`: dua entri dengan `a_interface` berbeda, `source`/`target` sama.
  - `test_should_point_to_external_pop_when_link_leaves_city`: `target == "pop:<kode>"`, `b_device` = hostname asli.
  - `test_should_group_agg_ring_when_three_agg_devices_form_cycle`: `clusters` berisi satu `kind="ring"` dengan 3 `device_ids`, node AGG `cluster_id` sama.
  - `test_should_return_422_when_regency_missing` dan `test_should_return_404_when_regency_unknown`.
  - `test_should_return_isolated_devices_when_city_has_no_links`: kota dengan perangkat tanpa link → `links == []`, node tetap ada.
  - `test_should_summarize_access_devices_when_city_exceeds_threshold`: monkeypatch `SUMMARY_THRESHOLD = 3` → node `summary:<pop>` ada, entri `links` ke OLT punya `target == "summary:<pop>"` dan `b_device` tetap hostname OLT, jumlah `links` tidak berubah.
  - `test_should_find_device_by_mgmt_ip_when_searching`: `/api/topology/search?q=10.0.0.5` mengembalikan perangkat itu di urutan pertama.
- [ ] **Step 2:** Run `pytest tests/test_topology_city.py -q`. Expected: FAIL.
- [ ] **Step 3:** Implement builder: perangkat kota = POP dengan `ctx.path_of_pop(pid) == (area, province, regency)`; ujung di luar kota → node `external_pop` (pola `_device_level`); `pops[]` diurutkan kode; `clusters[]` hanya cluster yang semua anggotanya di kota dan tier 2–3, `pop` = POP bersama atau `null` bila lintas POP.
- [ ] **Step 4:** Run `pytest -q`. Expected: semua PASS (termasuk `test_topology_*` lama).
- [ ] **Step 5:** Commit `feat: city topology view with per-link entries, clusters and SPOF`.

---

### Task 5: Level `neighbors` dengan field yang sama

**Files:**
- Modify: `backend/app/routers/analysis.py` (`_neighbor_level`) atau pindahkan ke `topology_city.py` bila lebih rapi
- Test: `backend/tests/test_topology_city.py`

**Interfaces:**
- Consumes: Task 4 `link_entries`, `network_structure`.
- Produces: respons `neighbors` menambah `links`, `clusters` (cluster yang semua anggotanya ada di view), `pops`, dan field perangkat Task 4.

- [ ] **Step 1: Failing test** `test_should_return_links_and_spof_when_level_neighbors`: depth 1 dari PE → `links` berisi kedua link paralel; perangkat ujung SPOF `spof is True`.
- [ ] **Step 2:** Run, FAIL. **Step 3:** Implement. **Step 4:** `pytest -q` PASS.
- [ ] **Step 5:** Commit `feat: neighbours view carries per-link entries and SPOF`.

---

### Task 6: Graf ELK dan konversi ke Cytoscape (murni, dengan vitest)

**Files:**
- Modify: `frontend/package.json` (dependensi `elkjs` versi stabil terbaru dipin persis; devDependency `vitest` versi yang kompatibel dengan Vite 8; script `"test": "vitest run"`)
- Create: `frontend/src/components/topology/types.ts`
- Create: `frontend/src/components/topology/elkGraph.ts`
- Create: `frontend/src/components/topology/elkToCytoscape.ts`
- Test: `frontend/src/components/topology/elkGraph.test.ts`, `frontend/src/components/topology/elkToCytoscape.test.ts`

**Interfaces:**
- Consumes: respons Task 4/5.
- Produces:
  - `types.ts`: `CityLink`, `CityCluster`, `CityPop`, `CityNode` (field persis respons Task 4), `CityView` (`level: 'city' | 'neighbors'` + field Task 4).
  - `buildElkGraph(view: CityView, sizeOf: (n: CityNode) => { width: number; height: number }): ElkNode` — hierarki spec §4.2: root → `city` (hanya level city) → `pop:<code>` → (`cluster:<id>`) → perangkat; partisi `elk.partitioning.partition` = tier (external_pop = -1 → dinormalisasi jadi 0, tier n → n+1); satu `ElkExtendedEdge` per `links[]` dengan `id = "link:<id>"`; link yang ujungnya tidak ada di `nodes` dilewati. Opsi persis spec §4.2 di konstanta `ELK_OPTIONS`.
  - `toCytoscape(result: ElkNode): { positions: Record<string, Pos>; edges: Record<string, EdgeRoute> }` dengan `EdgeRoute = { curve: 'segments' | 'straight'; weights: number[]; distances: number[]; sourceEndpoint: string; targetEndpoint: string }`; posisi = pusat absolut (jumlah offset parent ELK); `sourceEndpoint`/`targetEndpoint` dalam `"<dx>px <dy>px"` relatif pusat node.
  - `toSegments(source: Pos, target: Pos, bends: Pos[]): { weights: number[]; distances: number[] }`.

- [ ] **Step 1:** Install: `& "C:\Program Files\nodejs\npm.cmd" install --save-exact elkjs` dan `--save-dev --save-exact vitest` di `frontend/` (worktree: bila `node_modules` junction ke checkout utama, buat `node_modules` lokal dengan `npm ci` terlebih dulu supaya checkout utama tidak berubah).
- [ ] **Step 2: Failing tests**:
  - `elkGraph`: `should put every link as its own edge when links are parallel` (2 link sama pasangan → 2 edge, id `link:1`, `link:2`); `should nest device in pop and cluster when device has cluster_id`; `should place cluster under city when cluster pop is null` (cluster lintas POP); `should skip link when endpoint not in nodes`; `should build graph without edges when links empty`; `should put external pop in top partition when node is external_pop` (partition 0, perangkat tier 0 → 1).
  - `elkToCytoscape`: `toSegments({x:0,y:0},{x:100,y:0},[{x:0,y:50},{x:100,y:50}])` → `weights ≈ [0, 1]`, `Math.abs(distances[i]) ≈ 50` dan kedua distance bertanda sama (arah tanda dicek visual di Task 10 Step 2: garis harus menempel pada titik belok ELK); `should return finite values when source equals target`; `should report straight curve when edge has no bends`; `should sum parent offsets when node is nested`.
- [ ] **Step 3:** Run `npm test`. Expected: FAIL.
- [ ] **Step 4:** Implement. `toSegments`: proyeksi titik ke vektor `T−S` (`w = ((P−S)·(T−S))/|T−S|²`, `d` = jarak tegak lurus bertanda); bila `|T−S| = 0` pakai `w = 0`, `d` = jarak ke S.
- [ ] **Step 5:** `npm test` PASS, `npm run typecheck` PASS.
- [ ] **Step 6:** Commit `feat: ELK graph and Cytoscape route conversion for city topology`.

---

### Task 7: Worker ELK

**Files:**
- Create: `frontend/src/components/topology/elk.worker.ts`
- Create: `frontend/src/components/topology/elkLayout.ts`

**Interfaces:**
- Consumes: Task 6 `buildElkGraph`, `toCytoscape`.
- Produces: `layoutCity(view: CityView, sizeOf: (n: CityNode) => { width: number; height: number }): Promise<{ positions: Record<string, Pos>; edges: Record<string, EdgeRoute>; ms: number }>`; satu worker dipakai ulang; panggilan baru membatalkan yang lama (promise lama ditolak dengan `LayoutCancelled`); timeout `LAYOUT_TIMEOUT_MS = 20_000` → reject `Error('Tata letak terlalu lama …')` dan worker di-restart.

- [ ] **Step 1:** Worker: `import ELK from 'elkjs/lib/elk.bundled.js'`, terima `{ requestId, graph }`, balas `{ requestId, result }` atau `{ requestId, error }`. Dibuat dengan `new Worker(new URL('./elk.worker.ts', import.meta.url), { type: 'module' })`.
- [ ] **Step 2:** `elkLayout.ts` dengan `requestId` naik; hasil dengan `requestId` lama diabaikan.
- [ ] **Step 3:** `npm run typecheck` dan `npm run build` PASS (build menghasilkan chunk worker terpisah).
- [ ] **Step 4:** Commit `feat: run ELK layout in a web worker`.

---

### Task 8: Level `city` di halaman Topologi

**Files:**
- Modify: `frontend/src/pages/Topology.tsx`
- Create: `frontend/src/components/topology/cityStyle.ts` (stylesheet Cytoscape untuk city/neighbors)
- Modify: `frontend/src/styles.css` (atau file css halaman topologi yang dipakai sekarang) untuk toolbar baru

**Interfaces:**
- Consumes: Task 6 `CityView`, Task 7 `layoutCity`.
- Produces: `Level` menambah `'city'`; `drillTarget` untuk `regency`/`external_regency` → `{ level: 'city', area, province, regency }`; untuk `pop` node di city → `{ level: 'device', pop }`; `summary` → `{ level: 'device', pop }`. `nodeStatus(n: CityNode, links: CityLink[]): 'up' | 'down' | 'warn' | 'unknown'` (warn = up dan ada link miliknya `status === 'down'` atau `color` `yellow`/`red`).

- [ ] **Step 1:** Fetch `level=city` lewat `viewPath` yang ada (tambah `city`); breadcrumb langkah kota; level `pop` tetap dapat dibuka dari breadcrumb.
- [ ] **Step 2:** Untuk `city` dan `neighbors`: elemen dibangun dari `links[]` (satu edge Cytoscape per link, id `link:<id>`), compound `city`/`pop:<code>`/`cluster:<id>`; posisi dan rute dari `layoutCity`; `curve-style` per edge dari `EdgeRoute`. Tampilkan "Menyusun tata letak…" selama layout; error worker → `setMessage({ error: true })`.
- [ ] **Step 3:** Gaya `cityStyle.ts` sesuai spec §4.4 (warna status node, tepi = `ROLE_COLOR`, link `CAPACITY_HEX[color]`/abu bila kapasitas 0/merah putus-putus bila down, `source-label`/`target-label` = interface dengan `min-zoomed-font-size` setara zoom 1.2, compound POP "POP <kode> · <nama>", cluster "Ring · n perangkat"/"Mesh · n"/"Redundan · n").
- [ ] **Step 4:** Drag dimatikan (`autoungrabify(true)`) dan tombol simpan/atur ulang disembunyikan di `city`/`neighbors`; mode peta tetap memakai `mapLayout` lama.
- [ ] **Step 5:** Live: bila himpunan id node dan link sama dengan view sebelumnya, perbarui `data()` (status, warna, label) di tempat tanpa layout; bila berbeda, layout ulang dengan viewport dipertahankan dan kelas `fresh` untuk elemen baru.
- [ ] **Step 6:** `npm run typecheck`, `npm run build` PASS.
- [ ] **Step 7:** Commit `feat: hierarchical city topology with orthogonal ELK routing`.

---

### Task 9: Panel detail, filter, sorotan, pencarian IP

**Files:**
- Create: `frontend/src/components/TopologyDetail.tsx`
- Modify: `frontend/src/pages/Topology.tsx` (panel lama dipindah ke `TopologyDetail`; toolbar city)
- Modify: `frontend/src/components/TopologyTools.tsx` (hasil pencarian menampilkan mgmt IP bila cocok)

**Interfaces:**
- Consumes: Task 8 `nodeStatus`, `CityView`.
- Produces: `<TopologyDetail view node edgeLinks onDrill />` — untuk edge di city/neighbors: tabel semua `CityLink` dengan pasangan `source`/`target` yang sama (kolom: A host:interface, B host:interface, kapasitas, trafik, utilisasi %, committed %, status, SPOF); untuk perangkat: hostname, mgmt IP, kota · POP, role, vendor, reachability, uptime (format `x h y m`), last poll (waktu lokal `id-ID`), SPOF, cluster. Level lain merender panel lama tanpa perubahan perilaku.

- [ ] **Step 1:** Filter vendor (multi), status (`semua`/`masalah`), layer (multi) dengan `style('display', …)`, tanpa layout ulang.
- [ ] **Step 2:** Toggle **Sorot masalah** (node `down`/`warn` dan link down/kuning/merah tanpa `.faded`, sisanya `.faded`) dan **SPOF** (kelas `spof` pada node `spof` dan link `spof`: underlay oranye `#f97316`). Pilihan disimpan dengan `remember()`.
- [ ] **Step 3:** Pencarian mgmt IP memakai endpoint Task 4; hit membuka `neighbors` seperti hostname.
- [ ] **Step 4:** `npm run typecheck`, `npm run build`, `npm test` PASS.
- [ ] **Step 5:** Commit `feat: per-link detail, problem highlight, SPOF and filters on city topology`.

---

### Task 10: Verifikasi end-to-end di browser

- [ ] **Step 1:** Backend worktree di port 8010 (DB nyata, scheduler off) — perintah di memory `dev-environment`; jalankan `alembic upgrade head` hanya pada **salinan** DB (`/root/nadien.db.pre-0014` dibuat dulu; minta pengguna bila akses DB diblokir). Frontend preview dengan `NADIEN_API_URL=http://localhost:8010` di port 5180 lewat `.claude/launch.json`.
- [ ] **Step 2:** Kota kecil dan kota dengan perangkat terbanyak: `javascript_tool` → jumlah edge Cytoscape = `view.links.length`; tidak ada titik rute yang berada di dalam bounding box node lain (cek otomatis atas semua segmen); catat `ms` layout.
- [ ] **Step 3:** Neighbors 1 dan 2 hop; link paralel → panel menampilkan tiap link dengan interface benar (bandingkan dengan `/api/links`).
- [ ] **Step 4:** Sorot masalah, SPOF, filter vendor/status/layer, pencarian mgmt IP, live refresh (ubah status satu link di salinan DB → warna berubah tanpa layout ulang).
- [ ] **Step 5:** `pytest -q`, `npm run typecheck`, `npm run build`, `npm test` semua PASS; laporkan hasil + waktu layout + screenshot ke pengguna.

---

### Task 11: Geser dan simpan posisi perangkat di mode Peta (permintaan pengguna 2026-11-09)

**Files:**
- Modify: `frontend/src/pages/Topology.tsx` (`mapLayout`, `runLayout` map branch, drag/save wiring), `backend/app/routers/analysis.py` (`_with_layout` untuk key peta) dan/atau `backend/app/routers/topology.py` (PUT/DELETE `/api/topology/layouts` sudah ada)
- Test: `backend/tests/test_topology_layouts.py`, unit test vitest untuk konversi model↔lat/lon

**Interfaces:**
- Produces: view_key peta = `map:<layout_key>` (mis. `map:device:JKT`, `map:city:<area>|<province>|<regency>`); `positions` berisi `{ "<node id>": { "lat": number, "lon": number } }`; respons view menambah `map_layout` (dict yang sama, `{}` bila tidak ada).
- Fungsi murni `unproject(pos: Pos, pxPerDegree: number): { lat: number; lon: number }` sebagai kebalikan `project` di `components/basemap.ts`.

- [ ] **Step 1: Failing tests**: backend `test_should_return_map_layout_when_saved_for_view` (PUT `map:device:JKT` lalu GET `level=device&pop=JKT` → `map_layout` berisi posisi; `layout` skematik tidak berubah); `test_should_keep_device_coordinates_when_map_layout_saved` (`DeviceLocation`/`Pop` lat/lon tidak berubah). Frontend: `unproject(project(lat, lon, k), k)` ≈ `(lat, lon)`.
- [ ] **Step 2:** Implement: di mode Peta, drag diizinkan untuk node perangkat (bukan basemap/kotak grup); posisi awal = override bila ada, lalu declutter seperti sekarang; `countMoved`/bar "Simpan perubahan"/Ctrl+S dipakai ulang; simpan → PUT `/api/topology/layouts` dengan `view_key = map:<layout_key>` dan lat/lon hasil `unproject`; "Atur ulang tata letak" di peta → DELETE key peta. Marker scale (`applyMarkerScale`) tetap berjalan saat drag.
- [ ] **Step 3:** `pytest -q`, `npm test`, `npm run typecheck`, `npm run build` PASS.
- [ ] **Step 4:** Commit `feat: arrange and save device positions on the topology map`.
