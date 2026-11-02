---
paths:
  - "backend/**/*.py"
  - "frontend/src/**/*"
---

# Code Style

- Gunakan nama yang deskriptif; hindari singkatan yang tidak umum.
- Satu fungsi = satu tanggung jawab. Pecah fungsi yang lebih dari ~50 baris.
- Ikuti formatter/linter project; jangan format manual yang bertentangan.
- Komentar menjelaskan *kenapa*, bukan *apa*.
- Python: type hints di semua signature, SQLAlchemy 2.0 `Mapped[...]`.
- TypeScript: `strict` aktif; hindari `any` (kecuali untuk tipe library yang tidak cocok, dan beri komentar).
- Rumus kapasitas hanya ada di `backend/app/services/capacity.py`; jangan diduplikasi di frontend.
