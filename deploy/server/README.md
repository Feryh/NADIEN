# Rilis terjadwal ke satu server (docker compose)

Dipakai untuk server NADIEN 172.18.5.127 (`cloud`, lewat jump server). Kode dikirim dari laptop, jadi server tidak
butuh token GitLab. Rilis di-*stage* dulu: source dibongkar dan image dibuild selagi versi lama tetap melayani. Rilis
baru live di jendela maintenance berikutnya, **23:00 WIB**.

## Alur

```bash
# dari checkout yang sudah fetch main (Git Bash di Windows: pakai OpenSSH Windows yang membaca ssh-agent)
export NADIEN_SSH=/c/Windows/System32/OpenSSH/ssh.exe
deploy/server/release.sh                 # stage gitlab/main, live pukul 23:00 WIB
deploy/server/release.sh <commit> --now  # stage commit dari main dan langsung live
deploy/server/release.sh --status        # versi live, antrean, jadwal berikutnya, log terakhir
```

Hanya commit yang sudah ada di `gitlab/main` yang diterima. Stage berikutnya menggantikan antrean sebelumnya.

Saat apply (`nadien-release.sh apply`, dipanggil timer atau `--now`):

1. dump database (`pg_dump -Fc`) ke `/data/nadien/deploy/dumps/`, disimpan 10 terakhir;
2. `current` dipindah ke rilis baru, lalu `docker compose up -d` (downtime beberapa detik, migrasi Alembic jalan saat
   backend start);
3. `/api/health` ditunggu sampai 4 menit;
4. kalau gagal: versi sebelumnya dinyalakan lagi, dan database dikembalikan dari dump **hanya jika** revisi Alembic
   berubah.

Log ada di `/data/nadien/deploy/release.log` dan `journalctl -u nadien-release`.

## Layout server

| Path | Isi |
|------|-----|
| `/data/nadien/config/.env`, `backend.env` | `POSTGRES_PASSWORD` dan `NADIEN_*`, dipakai semua rilis |
| `/data/nadien/releases/<commit>/` | source per rilis (3 terakhir), image `nadien-*:<commit>` |
| `/data/nadien/current` | symlink ke rilis yang live; `docker compose` dijalankan dari sini |
| `/data/nadien/backups/` | backup aplikasi dan `nadien-secret.key` (kunci kredensial collector) |
| `/data/nadien/deploy/` | script ini, override compose, dumps, log |

Jam maintenance diubah di `nadien-release.timer`, lalu jalankan `deploy/server/release.sh --install` lagi.
