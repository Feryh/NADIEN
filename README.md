# NADIEN

**Network Access Design, Implementation & Engineering Platform.** Satu platform untuk desain topologi Metro, capacity planning, validasi engineering, customer path, MoP, dan config generation.

## Status roadmap

| Phase | Modul | Status |
|---|---|---|
| 1 | Inventory database (Node, Link, Device, Customer) | ✅ MVP |
| 2 | Topology builder (LLDP + config backup, drag & drop, warna kapasitas) | ✅ MVP |
| 3 | Capacity planning engine (forecast & prediksi exhaustion) | ✅ MVP |
| 4–10 | Customer path, validation, MoP, config gen, dashboard lanjutan, AI, digital twin | ⏳ Belum |

## Menjalankan

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
| Config dari RANCID Observium (`/opt/observium/rancid`, via SFTP atau mount), Oxidized, atau upload | LAG membership, IP & MTU, loopback, IS-IS NET, SRv6 locator | Interface & Router |

Parser config: **Cisco IOS-XR**, **Huawei VRP**, **Juniper Junos** (format kurung kurawal & `display set`), **Nokia SR OS** (classic & MD-CLI). Vendor terdeteksi otomatis.

Aturan penggabungan:
- **Observium** menang untuk state operasional (speed, status, ifAlias).
- **Config** menang untuk hal yang tidak terlihat SNMP (LAG, loopback, IS-IS, SRv6).
- **Field kurasi** (role device, POP device yang sudah ada) tidak pernah ditimpa.
- Pencocokan memakai hostname (sysName tanpa domain) dan nama interface. Ini kunci yang sama dengan LLDP, jadi data tidak terduplikasi.

Menjalankan (kredensial hanya lewat env / `backend/.env`, lihat `backend/env.example`):

```bash
python -m app.collectors.run observium                      # full sync
python -m app.collectors.run observium --parts utilization  # rate saja, jadwalkan tiap 5–15 menit
python -m app.collectors.run rancid                         # semua config dari arsip RANCID Observium
python -m app.collectors.run oxidized                       # alternatif: config dari Oxidized
python -m app.collectors.run config JKT-P-01 jkt-p-01.cfg   # satu file config
```

Bisa juga lewat UI di menu **Collectors**, atau API `POST /api/collectors/observium/sync` dan `POST /api/collectors/configs/{hostname}` (body berupa teks config).

Catatan:
- API Observium hanya memberi rate *saat ini*. Histori utilisasi terbentuk dari sync rutin, jadi tren growth makin akurat seiring waktu.
- API Observium tidak menyediakan konfigurasi device, sehingga config dibaca langsung dari arsip RANCID yang ditampilkan Observium. Semua folder `configs` di bawah base path (`var/<group>/configs/<host>`) dibaca. Nama file dicocokkan ke device lewat hostname, FQDN, atau IP management. Tipe parser diambil dari header `RANCID-CONTENT-TYPE`.
- Akses SFTP bersifat read-only dan **host key yang belum dikenal ditolak**. Daftarkan dulu host key server Observium di `known_hosts`.

### Data eksternal (DWDM, ODF, Patch Panel, Fiber, Wavelength)

Tabelnya sudah tersedia sebagai "wadah", dengan kolom `source_system` + `external_id` + `attributes` (JSON). Cara mengisi:

- **Push:** `POST /api/integrations/ingest/{fibers|dwdm_nodes|odfs|patch_panels|dwdm_wavelengths}?source_system=<nama>` berisi array record. Proses upsert berdasarkan `external_id`, jadi aman diulang.
- **Pull:** daftarkan sumbernya di *Integrations → API Sources*. Connector per sistem nantinya dipasang di `sync_source()` (`backend/app/routers/integrations.py`).

## Catatan MVP

- Tabel dibuat lewat `create_all` saat startup. Pakai Alembic sebelum ada data produksi.
- Belum ada autentikasi (rencana: Keycloak/OAuth2).
- Koordinat POP masih disimpan sebagai lat/lon biasa. Image Docker sudah PostGIS, jadi siap dimigrasi ke tipe geometry.
