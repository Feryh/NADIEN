---
name: deploy
description: Deploy aplikasi ke staging atau production. Gunakan saat user meminta deploy, rilis, atau publish aplikasi.
---

# Deploy

Konfigurasi environment ada di [deploy-config.md](deploy-config.md) — baca dulu sebelum deploy.

## Langkah

1. Pastikan working tree bersih (`git status`).
2. Jalankan lint dan seluruh test; batalkan jika ada yang gagal.
3. Build aplikasi.
4. Deploy ke environment yang diminta (default: **staging**).
5. Jalankan smoke test pada URL environment tersebut.
6. Laporkan hasil, versi, dan URL.

## Aturan

- Deploy ke **production** wajib konfirmasi eksplisit dari user.
- Jangan deploy dari branch selain yang tercantum di deploy-config.md.
