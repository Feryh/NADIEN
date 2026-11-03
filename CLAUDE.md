# NADIEN

Dimuat otomatis oleh Claude Code di awal setiap sesi.

## Project Overview

NADIEN — Network Access Design, Implementation & Engineering Platform untuk ISP besar
(Internet, IPLC, L2VPN, L3VPN, Metro-E, DWDM, migrasi IS-IS/SRv6). Roadmap 10 phase;
Phase 1–3 (inventory, topology, capacity) sudah MVP. Lihat README.md untuk detail.

## Tech Stack

- Backend: Python 3.12, FastAPI, SQLAlchemy 2.0 (Mapped style), Pydantic v2, PostgreSQL (+PostGIS image)
- Frontend: React 18 + TypeScript + Vite, Cytoscape.js, Recharts, react-router v6
- Deploy: docker compose (db, backend, frontend/nginx)

## Commands

- `docker compose up --build` — jalankan semua (UI :8080, API :8000/docs)
- `cd backend && uvicorn app.main:app --reload` — backend dev
- `cd backend && python -m app.seed --reset` — reset data contoh
- `cd frontend && npm run dev` — frontend dev (:5173, proxy /api ke :8000)
- `cd frontend && npm run typecheck` — cek TypeScript
- `cd backend && python -m pytest -q` — test backend (butuh `requirements-dev.txt`)
- `cd backend && python -m app.collectors.run observium` — sync dari Observium (kredensial via env)
- `cd backend && python -m app.collectors.run rrd --days 90` — histori utilisasi dari RRD Observium
- `cd backend && python -m app.collectors.run gitweb [--full]` — config semua repo RANCID via gitweb (juga otomatis tiap hari)

## Architecture

- `backend/app/models.py` — seluruh skema DB. Tabel external (DWDM, ODF, patch panel, fiber,
  wavelength) memakai `ExternalRefMixin` dan diisi via `/api/integrations/ingest/{entity}`.
- `backend/app/crud.py` — factory CRUD; endpoint inventory baru cukup ditambah di `routers/inventory.py`.
- `backend/app/services/topology.py` — LLDP adalah sumber wajib, deskripsi config hanya backup.
- `backend/app/services/capacity.py` — semua rumus kapasitas; warna dari committed % (current + reserved).
- `frontend/src/entities.ts` — definisi field yang menggerakkan semua layar inventory.
- `backend/app/collectors/` — Observium (inventory, LLDP, utilisasi), RANCID gitweb (`configs/gitweb.py`,
  semua repo, inkremental per blob hash, membuat perangkat CSR) dan parser config 7 OS (IOS-XR, IOS/NX-OS,
  VRP, Junos, SR OS, ZTE, FiberHome). Parser bersifat murni (tanpa DB) dan diuji dengan fixture di
  `backend/tests/fixtures/`.
- `backend/app/collectors/runner.py` + `scheduler.py` — status job dan jadwal harian disimpan di DB
  (`collector_jobs`, `scheduled_runs`) supaya aman untuk banyak worker/replica.
- `backend/app/services/cache.py` — cache read-model; versi di tabel `app_state` dinaikkan setiap write.
  Kredensial Observium/Oxidized hanya lewat env; jangan pernah ditulis ke file yang di-commit.
- `backend/app/collectors/jobs.py` — satu-satunya pintu untuk menjalankan sync: backup DB dulu
  (`app/backup.py`, retensi 15), baru sync. Router dan CLI wajib lewat sini, jangan panggil
  `sync_*` langsung dari endpoint baru.
- `backend/app/collectors/observium/rrd.py` — histori utilisasi (peak harian) dari RRD Observium.
  Sumber default `graph_api.py` (`graph.php?format=json`, login REST API); alternatif `rrdtool xport`
  lewat SSH/lokal. Butuh mapping `ObserviumPort` (port_id, ifIndex) dari Full sync.
  Batas DS port Observium 100 Gbps: nilai di atasnya jadi NaN dan harinya terlewat.

## Coding Conventions

Aturan detail dipecah per topik di `.claude/rules/`:

- @.claude/rules/code-style.md
- @.claude/rules/testing.md
- @.claude/rules/api-conventions.md

## Local Overrides

Preferensi pribadi (tidak di-commit) ditaruh di `CLAUDE.local.md`.
