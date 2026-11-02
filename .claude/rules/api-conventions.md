---
paths:
  - "backend/app/routers/**/*"
  - "backend/app/schemas.py"
---

# API Conventions

- Semua endpoint di bawah prefix `/api`; resource memakai kata benda jamak berbentuk kebab-case: `/device-models`, `/links/{id}`.
- Gunakan HTTP status yang tepat: 201 create, 204 delete, 404 tidak ditemukan, 409 constraint violation, 422 validasi.
- Error memakai format bawaan FastAPI: `{"detail": "..."}` (lewat `HTTPException`).
- Validasi input dengan schema Pydantic di `schemas.py`; nilai enum ditulis sebagai `Literal[...]`.
- Field JSON memakai `snake_case`, sama dengan nama kolom DB. Tanggal dalam ISO 8601 (UTC).
- Satuan ditulis di nama field: `_gbps`, `_mbps`, `_ms`, `_km`, `_pct`.
