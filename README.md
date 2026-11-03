# NADIEN

**Network Access Design, Implementation & Engineering Platform.** Satu platform untuk desain topologi Metro, capacity planning, validasi engineering, customer path, MoP, dan config generation.

## Status roadmap

| Phase | Modul | Status |
|---|---|---|
| 1 | Inventory database (Node, Link, Device, Customer) | ✅ MVP |
| 2 | Topology builder (LLDP + config backup, drag & drop, warna kapasitas) | ✅ MVP |
| 3 | Capacity planning engine (forecast & prediksi exhaustion) | ✅ MVP |
| 4 | Customer path engineering (jalur primary/backup, reservasi bandwidth) | ✅ MVP |
| 5 | Service validation (kapasitas & redundansi, MTU & port, VLAN/VRF/RD/RT, kesiapan perangkat) | ✅ MVP |
| 6 | MoP generator (PDF: pre-check, eksekusi, verifikasi, rollback per vendor) | ✅ MVP |
| 7 | Config generator (Huawei VRP, ZTE, Cisco IOS-XR, FiberHome; config + rollback) | ✅ MVP |
| 8 | Dashboard lanjutan (tren & forecast per area, layanan & validasi, kesehatan data, peta hotspot) | ✅ MVP |
| 9 | AI assistant (model lokal Ollama/vLLM, plus Claude, OpenAI, Azure OpenAI, Gemini, OpenAI-compatible) | ✅ MVP |
| 10 | Digital twin (skenario failure/what-if kapasitas, kesiapan migrasi IS-IS/SRv6) | ✅ MVP |

### Service validation (Phase 5)

Menu **Service Validation** memeriksa setiap layanan yang belum terminated. Desain baru bisa dicek sebelum reserve
lewat tombol **Validasi desain** di Customer Path. Empat kelompok cek, masing-masing berstatus lulus, peringatan,
gagal, atau info:

| Kelompok | Yang dicek |
|---|---|
| Kapasitas & redundansi | Utilisasi tiap link primary setelah layanan (warning ≥ 70%, critical ≥ 90%), link down, backup tanpa link/fiber (SRLG)/transit yang sama, single-homing |
| MTU & port | Port akses ada, admin up, bukan uplink; MTU jalur ≥ kebutuhan layanan (L2VPN/IPLC 1600, L3VPN/Internet 1530), anjuran jumbo 9000 |
| VLAN / VRF / RD / RT | VLAN wajib untuk L2VPN/IPLC dan tidak bentrok di port yang sama; L3VPN wajib VRF/RD/RT, format ASN:NN, RD tidak dipakai VRF lain |
| Kesiapan perangkat | Status aktif, loopback, IS-IS NET, SRv6 locator (info), versi software rekomendasi |

### Digital Twin (Phase 10)

Menu **Digital Twin** mensimulasikan perubahan pada salinan jaringan. Data produksi tidak pernah diubah.

**Skenario what-if** (`/api/twin/scenarios`, dijalankan dengan `POST …/{id}/run`, atau `POST /api/twin/simulate`
tanpa disimpan). Satu skenario berisi daftar perubahan:

| Perubahan | Arti |
|---|---|
| `fail_link` / `fail_device` / `fail_pop` / `fail_fiber` | Link, device, POP, atau fiber (SRLG: semua link di fiber itu) mati |
| `growth` | Trafik naik/turun N% di seluruh jaringan atau di link yang menyentuh satu area |
| `upgrade_link` | Kapasitas baru untuk link yang ada |
| `add_link` | Link virtual antara dua device |

Hasilnya mencakup:
- trafik yang dirutekan ulang dan yang hilang;
- jumlah link warning/kritis sebelum vs sesudah, dan link yang bebannya berubah;
- ringkasan per area;
- device yang terisolasi dari jaringan utama;
- dampak ke layanan lewat jalur engineered-nya: putus, pindah ke backup, atau kehilangan proteksi.

Model reroute adalah perkiraan, karena matriks trafik end-to-end tidak diketahui. Trafik link yang mati dipindah ke
jalur terpendek pengganti antar kedua ujungnya, dengan prioritas link yang paling lega. Trafik link yang menyentuh
device mati dianggap berakhir di device itu. Hasil terakhir disimpan bersama skenario, dan dihapus bila daftar
perubahannya diubah.

**Migrasi IS-IS/SRv6** (`/api/twin/migration/srv6`). Status kesiapan per router P/PE/agg/BNG/DCI:
- **siap**: loopback, IS-IS NET, dan SRv6 locator lengkap;
- **perlu konfigurasi**: belum ada locator;
- **terblokir**: tanpa loopback atau IS-IS NET.

Software yang bukan versi rekomendasi juga dihitung sebagai penghambat, tetapi hanya bila vendornya sudah punya
versi rekomendasi. Area diurutkan per wave, mulai dari area dengan router terblokir dan layanan paling sedikit. Di
dalam satu area, core (P/agg) dimigrasi sebelum edge. Layanan tetap di LDP sampai kedua PE-nya siap SRv6.

### Login, role & audit

Semua `/api` butuh login (cookie sesi HttpOnly, `NADIEN_SESSION_HOURS`, default 12 jam). Saat belum ada pengguna,
halaman pertama meminta pembuatan **admin pertama**; pengguna lain ditambah admin di menu **Pengguna & Audit**.

| Role | Hak |
|---|---|
| viewer | hanya membaca (termasuk simulasi Digital Twin dan AI chat) |
| engineer | mengubah data, mengajukan dan mengeksekusi change |
| approver | engineer + approve/tolak change milik orang lain |
| admin | semua, termasuk pengguna, Pengaturan Collector, provider AI, backup |

Setiap write tercatat di audit log (`/api/audit-logs`, admin), begitu juga login, approval, dan setiap eksekusi ke
perangkat. Sistem eksternal yang mengirim ke `/api/integrations` memakai `Authorization: Bearer <NADIEN_INGEST_TOKEN>`.
`NADIEN_AUTH_ENABLED=false` mematikan login (semua akses sebagai admin lokal), hanya untuk dev/test.

### Change & eksekusi ke perangkat

Dari **Config Generator** → **Ajukan change**: config, commit, rollback, pre-check dan post-check kedua site
dibekukan saat diajukan, jadi yang di-approve sama persis dengan yang dikirim. Alurnya:

1. **Approval** oleh approver/admin yang bukan pembuat change (atau ditolak dengan catatan).
2. **Pre-check** (perintah show) boleh sebelum atau sesudah approval.
3. **Push site A**, lalu **push site B**, masing-masing hanya setelah yang sebelumnya berhasil. Perintah dikirim satu
   per satu dan menunggu prompt. Bila perangkat menolak sebuah baris (`Error:`, `% Invalid`, `^`, …) atau meminta
   konfirmasi, script berhenti di baris itu.
4. **Post-check** setelah push site itu; **rollback** memakai script rollback yang sama-sama dibekukan.

Koneksi SSH atau telnet memakai username/password perangkat milik engineer yang menjalankan. Kredensial ini hanya
ada di memori selama sesi, tidak disimpan, dan disamarkan di transkrip. Transkrip lengkap tiap sesi disimpan per
eksekusi. Host key SSH dicatat saat koneksi pertama dan diwajibkan sesudahnya (`NADIEN_DEVICE_KNOWN_HOSTS`,
default di folder backup). Jump host opsional: `NADIEN_DEVICE_JUMP_HOST`/`_PORT`/`_USER`, dengan kredensial yang
sama kecuali diisi lain. Timeout per perintah: `NADIEN_DEVICE_TIMEOUT_S`.

### AI Assistant (Phase 9)

Menu **AI Assistant** menjawab pertanyaan tentang jaringan dengan model yang dipilih (`POST /api/ai/chat`).
Pada start pertama dibuat provider **Ollama lokal** sebagai default (`NADIEN_AI_OLLAMA_URL`,
`NADIEN_AI_DEFAULT_MODEL`, default `llama3.1:8b`), jadi data jaringan tidak keluar dari jaringan internal. Provider
lain ditambahkan dari tab **Provider AI** (`/api/ai/providers`):

| Jenis | Untuk |
|---|---|
| `ollama` | Ollama lokal |
| `openai` | Endpoint OpenAI-compatible: vLLM, LM Studio, OpenAI (ChatGPT), Gemini, OpenRouter, Groq, DeepSeek, GitHub Models, … |
| `azure_openai` | Azure OpenAI (jalur Microsoft / Copilot): endpoint resource, nama deployment, `api-version` |
| `anthropic` | Claude lewat SDK resmi `anthropic` (default `claude-opus-5-5`, fallback server-side bila permintaan ditolak) |

API key disimpan terenkripsi dengan kunci yang sama seperti secret collector dan tidak pernah dikembalikan oleh API.
Provider yang alamatnya di luar jaringan internal ditandai **eksternal**, dan halaman chat memperingatkan bahwa
ringkasan data jaringan ikut terkirim. Tombol **Tes** mengirim satu prompt pendek tanpa data jaringan.

Setiap pertanyaan dilampiri data yang relevan (`app/services/ai/context.py`): ringkasan jaringan, device/POP/
circuit/area yang disebut, serta bagian topik (hotspot, forecast, kesehatan data, layanan & validasi) bila
pertanyaannya menyinggung topik itu. Batas ukurannya `NADIEN_AI_CONTEXT_CHARS` (12.000 karakter). Cara ini tidak
memakai tool calling, jadi bekerja dengan model lokal yang kecil sekalipun. Data hanya dilampirkan ke pertanyaan
terakhir; riwayat percakapan (maksimal 12 pesan) dikirim apa adanya.

### Dashboard lanjutan (Phase 8)

Dashboard punya lima tab (`?tab=`), semuanya dibaca dari engine yang sudah ada dan di-cache:

| Tab | API | Isi |
|---|---|---|
| Ringkasan | `/api/dashboard/summary` | KPI, kesehatan kapasitas link, utilisasi POP |
| Tren & forecast | `/api/dashboard/trends` | Puncak harian seluruh jaringan dan per area, forecast ke ambang/penuh per area |
| Layanan & validasi | `/api/dashboard/services` | Layanan per tipe/status, hasil validasi, MoP yang masih DRAFT, temuan terbanyak |
| Kesehatan data | `/api/dashboard/data-health` | POP tanpa koordinat/area, router tanpa loopback/IS-IS, link tanpa LLDP/kapasitas, data utilisasi janggal, histori utilisasi, umur sync per collector |
| Peta hotspot | `/api/dashboard/areas` | Peta Indonesia: lingkaran per area (ukuran = kapasitas, warna = link terburuk ≥ 70% / ≥ 90%) dan titik per POP |

Posisi area adalah titik tengah POP-POP yang berkoordinat. Area tanpa POP berkoordinat memakai titik perkiraan
(lingkaran putus-putus), dan POP yang belum masuk area tidak digambar di peta. Grafik tren dan forecast butuh
histori harian dari RRD Observium; selama histori masih kurang dari 30 hari, tab Kesehatan data menampilkan
peringatan.

### MoP generator (Phase 6)

Menu **MoP Generator** menyusun Method of Procedure untuk satu layanan, dengan pratinjau di layar dan unduhan PDF
(`GET /api/mop/services/{id}/pdf`, versi JSON di `/api/mop/services/{id}`). Nomor MoP, nomor change, window, dan
nama pelaksana/reviewer/approver diisi di halaman itu lalu dikirim sebagai query parameter.

Isinya: tujuan, ringkasan layanan, jalur primary/backup dengan utilisasi, hasil validasi Phase 5, risiko & dampak,
persiapan (backup config), pre-check, eksekusi, verifikasi, rollback, dan tabel pelaksana & eskalasi. Bila validasi
masih **gagal**, dokumen ditandai **DRAFT**.

Perintah CLI ditulis per perangkat sesuai dialek vendornya (`app/services/vendors.py`): Huawei VRP, ZTE, Cisco
IOS-XR, dan FiberHome. Dialek dibaca dari vendor/OS di inventory; bila kosong, dari pola hostname. Perangkat yang
dialeknya tidak dikenali diberi catatan supaya perintahnya ditulis manual. Bagian eksekusi dan rollback memuat
konfigurasi dari Config Generator (Phase 7).

### Config generator (Phase 7)

Menu **Config Generator** (`GET /api/configs/services/{id}`) membuat konfigurasi dan rollback untuk kedua ujung
layanan dalam sintaks perangkatnya (`app/services/configgen.py`). Script siap tempel: tanpa baris komentar
(keluar dari view memakai `quit` / `$` / `exit` / `root`) dan disimpan dengan `commit` di semua vendor, bukan
`write`/`save`. Peringatan ditampilkan di samping script, bukan di dalamnya. File teks per site dan per bagian:
`/text?side=A|B&part=config|rollback`.

Template mengikuti konfigurasi layanan yang ada di backup RANCID jaringan ini (Metro, BNG, CSR), bukan contoh
umum vendor:

| Layanan | Huawei VRP | ZTE | FiberHome | Cisco IOS-XR |
|---|---|---|---|---|
| L2VPN / IPLC | `vsi … pwsignal ldp` (satu peer, control-word) + `l2 binding vsi` | `vpls` + `access-point` + `pseudo-wire … neighbour … vcid … cw-preferred`, VLAN di `vlan-configuration` | `vfi … vsi-id … peer … encapsulation raw`, sub-interface `l2transport` | `xconnect group … p2p`, `pw-class MPLS-CONTROL-WORD`, `rewrite ingress tag pop 1 symmetric` |
| L3VPN | `ip vpn-instance` + `bgp … ipv4-family vpn-instance` | `ip vrf` + `router bgp … address-family ipv4 vrf` | tidak dibuat (FiberHome metro hanya L2) | `vrf` + `router bgp … vrf` |
| Internet | sub-interface dengan alamat global | sama | tidak dibuat | sama |
| Policer (opsional, `policer=true`) | `qos car … green pass red discard` in/out | `qos` → `rate-limit input/output` | tidak dibuat (tidak ada contoh) | `policy-map … police rate` |

VC/VSI id default = VLAN, seperti praktik ZTE metro (bisa diganti lewat `vc_id`; layanan tanpa VLAN memakai
100000 + id layanan). Bila layanan lain di antara dua PE yang sama sudah memakai VLAN itu, muncul peringatan
supaya VC id diganti. Policer default tidak dibuat dan bisa diaktifkan per layanan.

VPLS bisa dibuat sekaligus untuk beberapa VLAN lewat `vlans` (mis. `1480-1485,1489`, maksimal 64). Setiap VLAN
mendapat sub-interface dan instance sendiri, dengan VC id = VLAN. Di ZTE, nama pw dialokasikan otomatis dari
running-config perangkat di backup RANCID (`app/services/running_config.py`, cache 5 menit). Nama yang dipakai
adalah `pwN` terkecil yang belum dideklarasikan (`pw pwN`) atau dirujuk (`pseudo-wire pwN`), lalu dipasangkan ke
access-point dan neighbour per VLAN. Akan muncul peringatan bila vcid ke neighbour yang sama atau sub-interface itu
sudah ada di config. Kalau config RANCID tidak bisa diambil, pw dinamai `pw<VC id>` dan muncul peringatan untuk
mengeceknya manual. MTU instance L2
2000 sesuai VPLS metro. Nama instance L2 mengikuti gaya jaringan: `<CUSTOMER>_<VLAN>`. Alamat IP sisi PE diisi
lewat `a_ip` / `b_ip` (CIDR), kalau kosong memakai placeholder `<ip> <mask>`. ASN BGP diisi lewat `bgp_asn`;
kalau kosong diambil dari RD, padahal di jaringan ini RD tidak selalu sama dengan AS perangkat (mis. RD
`10001:x` di AS 46038), jadi ada peringatannya. Blok VRF tidak dibuat ulang dan tidak dihapus saat rollback bila
layanan lain yang masih aktif sudah memakai VRF itu di perangkat yang sama.

## Menjalankan

Panduan lengkap untuk lokal (Docker, native, Windows + WSL) dan server production ada di [docs/LAUNCH.md](docs/LAUNCH.md).

### Opsi A — Docker (paling mudah)

```bash
docker compose up --build
```

- UI: http://localhost:8080
- API docs (Swagger): http://localhost:8000/docs
- Data contoh otomatis di-seed saat backend start pertama kali.

### Opsi B — Native (Python 3.12+, Node 20+, PostgreSQL 16)

```bash
# Backend
cd backend
python -m venv .venv
.venv\Scripts\activate          # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
copy env.example .env           # sesuaikan NADIEN_DATABASE_URL
python -m app.seed
uvicorn app.main:app --reload
```

```bash
# Frontend (terminal lain)
cd frontend
npm install
npm run dev                     # http://localhost:5173
```

Tanpa PostgreSQL, untuk coba cepat: set `NADIEN_DATABASE_URL=sqlite:///./nadien.db`.

Reset data contoh: `python -m app.seed --reset`.

## Arsitektur

```
backend/   FastAPI + SQLAlchemy 2 (PostgreSQL)
  app/models.py              skema engineering DB (Single Source of Truth)
  app/crud.py                generator CRUD endpoint
  app/routers/               inventory, integrations, topology, capacity, dashboard
  app/services/topology.py   discovery link dari LLDP (wajib) + deskripsi config (backup)
  app/services/capacity.py   engine kapasitas & forecast
  app/seed.py                data contoh jaringan Metro
  app/collectors/observium/  client + parser + sync Observium API
  app/collectors/configs/    parser config IOS-XR, VRP, Junos, SR OS + Oxidized
  tests/                     pytest dengan fixture Observium & config 4 vendor
frontend/  React + TypeScript + Vite, Cytoscape.js (peta), Recharts (grafik)
```

### Topology discovery

1. Collector (Nornir/Scrapli, nanti) atau NMS mengirim tabel LLDP neighbor ke `POST /api/topology/lldp`.
2. `POST /api/topology/discover` membangun ulang Link inventory:
   - **LLDP (wajib):** tiap adjacency jadi link. Kalau kedua sisi adalah member bundle, link dicatat di level bundle dengan capacity = jumlah speed member.
   - **Config (backup):** port yang tidak tercakup LLDP dipasangkan dari deskripsi interface yang saling menyebut hostname lawan (contoh `TO-BDG-AGG-01-100GE1/0/3`).
   - Link hasil discovery yang hilang **ditandai `down`**, tidak dihapus, supaya histori kapasitas dan referensi customer tetap aman.

### Capacity engine

```
available       = capacity − current − reserved
committed %     = (current + reserved) / capacity      → warna: hijau < 60%, kuning 60–80%, merah ≥ 80%
months_to_full  = (capacity − reserved − current) / growth
```

Contoh: link 400G, current 250G, growth 20G/bulan → **penuh dalam 7.5 bulan**.

Nilai growth diambil dari override manual per link. Kalau kosong, dihitung dari tren regresi linear sampel utilisasi 90 hari terakhir (`POST /api/capacity/samples`). Ambang warna dan planning bisa diatur lewat env `NADIEN_UTIL_*`.

### Collectors: Observium + config

Data valid diambil dari jaringan, bukan diisi manual:

| Sumber | Data | Masuk ke |
|---|---|---|
| Observium `/devices` | sysName, os, hardware, version, location (+lat/lon), status | POP (kode dari hostname), Vendor, Model, Software, Router |
| Observium `/ports` | ifName, ifType, ifHighSpeed, ifMtu, admin/oper status, ifAlias | Interface (physical / bundle / subinterface) |
| Observium `/address` | IPv4 + prefix | Interface IPv4 |
| Observium `/neighbours` | LLDP (CDP opsional) | Tabel LLDP → topology discovery |
| Observium `/ports` rate | ifIn/OutOctets_rate | 1 sampel utilisasi per link per sync, `current_gbps` |
| RRD Observium lewat `graph.php?format=json` (default), atau `rrdtool xport` via SSH | peak harian in/out 7–365 hari | histori utilisasi per link, tren 7/30/90 hari, `current_gbps` = peak hari penuh terakhir |
| Config dari RANCID Observium (`/opt/observium/rancid`, via SFTP atau mount), Oxidized, atau upload | LAG membership, IP & MTU, loopback, IS-IS NET, SRv6 locator | Interface & Router |
| Semua repo RANCID lewat gitweb (`NADIEN_GITWEB_URL`), otomatis tiap hari, hanya file yang berubah | Config sebagai cadangan Observium. Perangkat yang tidak ada di Observium (CSR) dibuat dari config | Router, Interface, role **CSR** |

Parser config: **Cisco IOS-XR**, **Cisco IOS/IOS-XE/NX-OS**, **Huawei VRP**, **Juniper Junos** (format kurung kurawal & `display set`), **Nokia SR OS** (classic & MD-CLI), **ZTE ZXCTN / ZXR10 M6000**, **FiberHome R8xx**. Vendor terdeteksi otomatis. Config OLT (GPON) dilaporkan sebagai tidak didukung.

Perangkat di repo `CSR-*` selalu ber-role `csr`, dengan POP = kota di hostname (`BAL-BA-BLI-0004-ME1-ZTE` → POP `BLI`, area dari kode provinsi `BA`). Di perangkat yang dimonitor Observium, config hanya melengkapi port yang sudah ada dan tidak menambah port baru.

Aturan penggabungan:
- **Observium** menang untuk state operasional (speed, status, ifAlias).
- **Config** menang untuk hal yang tidak terlihat SNMP (LAG, loopback, IS-IS, SRv6).
- **Field kurasi** (role device, POP device yang sudah ada) tidak pernah ditimpa.
- Pencocokan memakai hostname (sysName tanpa domain) dan nama interface. Ini kunci yang sama dengan LLDP, jadi data tidak terduplikasi.

Menjalankan (kredensial hanya lewat env / `backend/.env`, lihat `backend/env.example`):

```bash
python -m app.collectors.run observium                      # full sync
python -m app.collectors.run observium --parts utilization  # rate saja, jadwalkan tiap 5–15 menit
python -m app.collectors.run rrd --days 90                  # histori peak harian dari RRD Observium (harian cukup)
python -m app.collectors.run rancid                         # semua config dari arsip RANCID Observium
python -m app.collectors.run oxidized                       # alternatif: config dari Oxidized
python -m app.collectors.run config JKT-P-01 jkt-p-01.cfg   # satu file config
```

Bisa juga lewat UI di menu **Collectors**, atau API `POST /api/collectors/observium/sync` dan `POST /api/collectors/configs/{hostname}` (body berupa teks config).

Catatan:
- REST API Observium hanya memberi rate *saat ini*. Histori diambil dari RRD Observium, dengan dua pilihan sumber (`NADIEN_OBSERVIUM_RRD_SOURCE`):
  - `api` (default): `graph.php?type=port_bits&format=json`, memakai login yang sama dengan REST API, tanpa SSH. Endpoint ini tidak terdokumentasi resmi, tapi sudah dites di server produksi. rrdtool memilih satu resolusi untuk satu rentang, jadi 90 hari keluar sebagai rata-rata 2 jam. Karena itu 7 hari terakhir diambil ulang dengan data 5 menit, supaya peak-nya akurat dan sama dengan Max di Observium. Hari yang lebih lama memakai peak dari rata-rata 2 jam, sedikit di bawah peak 5 menit yang asli. Butuh 2 request per link, sekitar 0,3 detik per link dengan 6 request paralel (`NADIEN_OBSERVIUM_GRAPH_WORKERS`), atau sekitar 45 menit untuk 9.000 link.
  - `ssh` / `local`: peak harian asli dari archive MAX lewat `rrdtool xport` di server Observium (`NADIEN_OBSERVIUM_SSH_*`).
- Mapping link ke port Observium (port_id, ifIndex) disimpan saat Full sync, jadi **jalankan Full sync dulu** sebelum import histori. Halaman Capacity menampilkan tren growth 7, 30, dan 90 hari. Forecast memakai jendela `NADIEN_TREND_WINDOW_DAYS` (default 90).
- RRD port Observium dibuat dengan batas DS 12,5 GB/s (**100 Gbps**). Trafik di atas itu (mis. link 400G) disimpan RRD sebagai NaN sehingga hari itu hilang. Laporan import mencantumkan link yang kemungkinan terpotong (`possibly_clipped`). Solusinya di sisi Observium: naikkan max DS (`rrdtool tune ... --maximum INOCTETS:U`) untuk port tersebut.
- API Observium tidak menyediakan konfigurasi device, sehingga config dibaca langsung dari arsip RANCID yang ditampilkan Observium. Semua folder `configs` di bawah base path (`var/<group>/configs/<host>`) dibaca. Nama file dicocokkan ke device lewat hostname, FQDN, atau IP management. Tipe parser diambil dari header `RANCID-CONTENT-TYPE`.
- Akses SFTP bersifat read-only dan **host key yang belum dikenal ditolak**. Daftarkan dulu host key server Observium di `known_hosts`.

### Backup database

Setiap kali data inventory atau config diambil, database **di-backup lebih dulu**. Yang disimpan hanya **15 backup terakhir** (`NADIEN_BACKUP_KEEP`).

- **Pemicu backup:** Full sync Observium (inventory/neighbours), sync config RANCID/Oxidized, dan import config manual.
- **Tidak di-backup:** sync *utilisasi saja*. Sync ini hanya menambah sampel dan berjalan tiap beberapa menit; kalau ikut di-backup, 15 slot akan habis dalam hitungan jam.
- **Kalau backup gagal, sync tidak dijalankan.**
- **Format:**
  - SQLite: salinan online lewat sqlite3 backup API.
  - PostgreSQL: `pg_dump --format=custom`. Image Docker sudah memasang `postgresql-client-16`.
- **Lokasi:** `NADIEN_BACKUP_DIR`. Default-nya `nadien-backups/` di samping file SQLite, atau `./backups` di Docker.

```bash
python -m app.backup list
python -m app.backup create [alasan]
python -m app.backup restore nadien-20261102-075137-observium.db   # kondisi saat ini di-backup dulu (pre-restore)
```

Restore sengaja hanya tersedia lewat CLI, tidak lewat UI. Untuk PostgreSQL, hentikan API sebelum restore.

### Data eksternal (DWDM, ODF, Patch Panel, Fiber, Wavelength)

Tabelnya sudah tersedia sebagai "wadah", dengan kolom `source_system` + `external_id` + `attributes` (JSON). Cara mengisi:

- **Push:** `POST /api/integrations/ingest/{fibers|dwdm_nodes|odfs|patch_panels|dwdm_wavelengths}?source_system=<nama>` berisi array record. Proses upsert berdasarkan `external_id`, jadi aman diulang.
- **Pull:** daftarkan sumbernya di *Integrations → API Sources*. Connector per sistem nantinya dipasang di `sync_source()` (`backend/app/routers/integrations.py`).

## Catatan MVP

- Tabel dibuat lewat `create_all` saat startup. Pakai Alembic sebelum ada data produksi.
- Belum ada autentikasi (rencana: Keycloak/OAuth2).
- Koordinat POP masih disimpan sebagai lat/lon biasa. Image Docker sudah PostGIS, jadi siap dimigrasi ke tipe geometry.
