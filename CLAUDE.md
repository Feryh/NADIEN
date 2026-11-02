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

## Architecture

- `backend/app/models.py` — seluruh skema DB. Tabel external (DWDM, ODF, patch panel, fiber,
  wavelength) memakai `ExternalRefMixin` dan diisi via `/api/integrations/ingest/{entity}`.
- `backend/app/crud.py` — factory CRUD; endpoint inventory baru cukup ditambah di `routers/inventory.py`.
- `backend/app/services/topology.py` — LLDP adalah sumber wajib, deskripsi config hanya backup.
- `backend/app/services/capacity.py` — semua rumus kapasitas; warna dari committed % (current + reserved).
- `frontend/src/entities.ts` — definisi field yang menggerakkan semua layar inventory.

## Coding Conventions

Aturan detail dipecah per topik di `.claude/rules/`:

- @.claude/rules/code-style.md
- @.claude/rules/testing.md
- @.claude/rules/api-conventions.md

## Local Overrides

Preferensi pribadi (tidak di-commit) ditaruh di `CLAUDE.local.md`.
