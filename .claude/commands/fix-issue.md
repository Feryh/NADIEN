---
description: Analisis dan perbaiki issue GitHub berdasarkan nomornya
argument-hint: <issue-number>
---

Perbaiki issue GitHub #$ARGUMENTS.

1. Baca detail issue (via MCP GitHub atau `gh issue view $ARGUMENTS`).
2. Cari kode yang relevan dan pahami akar masalahnya.
3. Tulis test yang gagal untuk mereproduksi masalah.
4. Implementasikan perbaikan sampai test lulus.
5. Jalankan lint dan seluruh test terkait.
6. Ringkas perubahan dan siapkan pesan commit.
