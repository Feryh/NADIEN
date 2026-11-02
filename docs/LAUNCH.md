# Panduan Launch NADIEN

Dua skenario:

1. [Local / development](#1-local--development): laptop atau PC untuk coding dan uji coba.
2. [Server production](#2-server-production): satu server Linux yang dipakai tim engineering.

Komponen yang selalu ada:

| Komponen | Isi | Port default |
|---|---|---|
| `db` | PostgreSQL 16 + PostGIS | 5432 |
| `backend` | FastAPI (`uvicorn app.main:app`) | 8000 |
| `frontend` | React build yang disajikan nginx. nginx juga mem-proxy `/api`, `/docs`, `/openapi.json` ke backend | 80 di container, 8080 di host |

Semua konfigurasi backend dibaca dari environment variable berprefix `NADIEN_`, atau dari `backend/.env`.
Daftar lengkapnya ada di [`backend/env.example`](../backend/env.example). **`backend/.env` tidak boleh di-commit**
(sudah di `.gitignore`). Kredensial Observium, RANCID, dan Oxidized hanya boleh ditaruh di file itu.

---

## 1. Local / development

### Opsi A: Docker Compose (paling mudah)

Syarat: Docker Desktop (Windows/macOS) atau Docker Engine + compose plugin (Linux).
Cek dengan `docker version`. Bagian **Server** harus muncul. Kalau hanya Client, daemon Docker belum jalan.

```bash
docker compose up --build
```

- UI: http://localhost:8080
- Swagger API: http://localhost:8000/docs
- Saat start pertama, backend mengisi **data contoh** ke database kosong. Start berikutnya tidak mengisi ulang.

Perintah yang sering dipakai:

```bash
docker compose logs -f backend                               # lihat log API
docker compose exec backend python -m app.seed --reset       # reset ke data contoh
docker compose exec backend python -m pytest -q              # (butuh requirements-dev.txt di image)
docker compose down                                          # stop, data tetap di volume pgdata
docker compose down -v                                       # stop + HAPUS database
```

### Opsi B: Native tanpa Docker

Syarat: Python 3.12+, Node 20+. PostgreSQL 16 opsional, karena SQLite cukup untuk coba-coba.

**Backend** (terminal 1):

```bash
cd backend
python -m venv .venv
source .venv/bin/activate            # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt -r requirements-dev.txt
cp env.example .env                  # Windows: copy env.example .env
```

Edit `backend/.env`, lalu pilih database:

```ini
# PostgreSQL lokal
NADIEN_DATABASE_URL=postgresql+psycopg://nadien:nadien@localhost:5432/nadien
# atau SQLite, tanpa install apa pun
NADIEN_DATABASE_URL=sqlite:///./nadien.db
```

```bash
python -m app.seed                   # isi data contoh (dilewati kalau DB sudah berisi)
uvicorn app.main:app --reload        # http://localhost:8000/docs
```

**Frontend** (terminal 2):

```bash
cd frontend
npm install
npm run dev                          # http://localhost:5173, /api di-proxy ke :8000
```

Buka http://localhost:5173. Dashboard harus menampilkan angka POP, link, dan port. Cek cepat API:

```bash
curl http://localhost:8000/api/health        # {"status":"ok"}
```

### Opsi C: Windows tanpa Docker (backend di WSL)

Ini setup yang dipakai di PC development saat ini.

1. **Backend di WSL Ubuntu.** Venv ada di `~/.nadien-venv`. Database SQLite di `/root/nadien.db`.
   Jangan taruh database di `/tmp`, karena `/tmp` di WSL ikut terhapus setiap kali distro berhenti.

   ```bash
   wsl -d Ubuntu -- bash -lc "cd '/mnt/d/Project Code/NADIEN/backend' && NADIEN_DATABASE_URL=sqlite:////root/nadien.db ~/.nadien-venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000"
   ```

   Kalau venv belum ada: install `python3-venv`, atau bootstrap pip dengan `get-pip.py`, lalu
   `~/.nadien-venv/bin/pip install -r requirements.txt -r requirements-dev.txt`.

2. **Frontend di Windows.** Node ada di `C:\Program Files\nodejs`. Kalau belum ada di PATH, tambahkan dulu:

   ```powershell
   $env:Path = "C:\Program Files\nodejs;$env:Path"
   cd frontend; npm install; npm run dev
   ```

   Di Claude Code desktop, frontend juga bisa dijalankan lewat konfigurasi `frontend` di `.claude/launch.json`.

3. **Git worktree** (`.claude/worktrees/...`) tidak membawa `frontend/node_modules`. Jalankan `npm install` di worktree,
   atau buat junction ke `node_modules` checkout utama kalau `package-lock.json`-nya sama.

### Menghubungkan ke Observium dari lokal

Isi `NADIEN_OBSERVIUM_URL` dan `NADIEN_OBSERVIUM_TOKEN` di `backend/.env`. Pakai token **read-only**. Setelah itu:

```bash
python -m app.collectors.run observium          # Full sync: device, port, LLDP, mapping port_id
python -m app.collectors.run rrd --days 90      # histori utilisasi (setelah Full sync)
```

Database di-backup otomatis sebelum setiap sync (lihat README, bagian *Backup database*).

---

## 2. Server production

> ⚠️ **Status MVP: baca sebelum go-live**
>
> - **Belum ada autentikasi.** Siapa pun yang bisa membuka URL bisa mengubah dan menghapus data. Batasi aksesnya
>   ke jaringan internal/VPN, atau pasang basic auth/SSO di reverse proxy (lihat langkah 5).
> - **Belum ada migrasi Alembic.** Tabel dibuat dengan `create_all` saat startup. Kolom baru di versi berikutnya
>   **tidak** otomatis ditambahkan ke tabel lama. Sebelum upgrade versi, backup dulu dan cek perubahan `models.py`.
> - `docker-compose.yml` bawaan memakai password database `nadien` dan membuka port 5432/8000 ke semua interface.
>   Itu hanya cocok untuk development. Langkah 3 menutupnya.
> - Command default backend menjalankan `app.seed`. Di database production yang masih kosong, ini akan mengisi
>   **data contoh**. Override di langkah 3 mematikannya.

### Spesifikasi server

Ukurannya disesuaikan dengan jaringan saat ini: sekitar 3.500 device, 300 ribu port, dan 9.000 link.

| | Minimum | Disarankan |
|---|---|---|
| OS | Ubuntu 22.04/24.04 LTS atau Debian 12 | sama |
| CPU / RAM | 4 vCPU / 8 GB | 8 vCPU / 16 GB |
| Disk | 100 GB SSD | 200 GB SSD (database + 15 backup + histori RRD) |
| Jaringan | Bisa mengakses Observium (HTTP/HTTPS, dan SSH kalau memakai RRD `ssh` atau RANCID SFTP) | sama |

### Langkah 1: Install Docker

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER        # logout/login sesudahnya
docker version && docker compose version
```

### Langkah 2: Ambil kode

```bash
sudo mkdir -p /opt/nadien && sudo chown $USER /opt/nadien
git clone <url-repo> /opt/nadien
cd /opt/nadien
git checkout main                    # atau tag rilis
```

### Langkah 3: Konfigurasi production

**a. Secret database.** Buat file `/opt/nadien/.env`, yang dibaca compose untuk substitusi `${...}`:

```ini
POSTGRES_PASSWORD=<password-acak-panjang>
```

Password bisa dibuat dengan `openssl rand -base64 32`. Pakai yang alfanumerik saja supaya aman dipakai di URL.

**b. Konfigurasi backend.** Salin lalu edit:

```bash
cp backend/env.example backend/.env
chmod 600 backend/.env .env
```

Yang wajib diisi atau dicek di `backend/.env`:

```ini
NADIEN_OBSERVIUM_URL=http://<observium>
NADIEN_OBSERVIUM_TOKEN=<token-read-only>
NADIEN_OBSERVIUM_POP_REGEX=^([A-Za-z]{3,4})[-_.]
NADIEN_BACKUP_KEEP=15
# Kalau UI diakses dari domain lain selain lewat nginx bawaan:
# NADIEN_CORS_ORIGINS=["https://nadien.example.co.id"]
```

`NADIEN_DATABASE_URL` **tidak** perlu diisi di sini, karena sudah diatur oleh file override di bawah.

**c. Override compose.** Buat `docker-compose.prod.yml`. Isinya menutup port, mengganti password, mematikan seed,
dan menambahkan restart otomatis:

```yaml
services:
  db:
    environment:
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?isi POSTGRES_PASSWORD di .env}
    ports: !reset []                     # DB tidak dibuka ke luar
    restart: unless-stopped

  backend:
    environment:
      NADIEN_DATABASE_URL: postgresql+psycopg://nadien:${POSTGRES_PASSWORD}@db:5432/nadien
      NADIEN_BACKUP_DIR: /backups
    # Tanpa app.seed. Satu worker saja: create_all saat startup bisa bentrok kalau dijalankan beberapa worker sekaligus.
    command: ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
    ports: !reset []                     # API hanya lewat nginx frontend
    volumes:
      - ./backups:/backups
      # Hanya kalau memakai RRD ssh / RANCID SFTP (key read-only + known_hosts):
      # - /opt/nadien/ssh:/root/.ssh:ro
    restart: unless-stopped

  frontend:
    ports: !reset ["127.0.0.1:8080:80"]  # di belakang reverse proxy HTTPS (langkah 5)
    restart: unless-stopped
```

Kalau tidak memakai reverse proxy dan servernya hanya bisa diakses dari jaringan internal, port frontend boleh
dibuat `"8080:80"` atau `"80:80"`.

> `!reset` butuh Docker Compose v2.24 atau lebih baru. Cek dengan `docker compose version`.

Supaya tidak perlu mengetik `-f` setiap kali, tambahkan baris ini ke `/opt/nadien/.env`:

```ini
COMPOSE_FILE=docker-compose.yml:docker-compose.prod.yml
```

### Langkah 4: Jalankan

```bash
docker compose up -d --build
docker compose ps                                   # semua harus "running" / db "healthy"
curl -s http://127.0.0.1:8080/api/health            # {"status":"ok"}
```

Database production mulai kosong. Isi dari jaringan dengan urutan berikut:

```bash
docker compose exec backend python -m app.collectors.run observium      # Full sync (wajib pertama)
docker compose exec backend python -m app.collectors.run rrd --days 90  # histori, sekitar 45 menit untuk 9.000 link
docker compose exec backend python -m app.collectors.run rancid         # config (opsional)
```

### Langkah 5: Reverse proxy HTTPS + pembatasan akses

Contoh dengan nginx di host. Install dengan `sudo apt install nginx`, lalu buat
`/etc/nginx/sites-available/nadien`:

```nginx
server {
    listen 443 ssl;
    server_name nadien.example.co.id;

    ssl_certificate     /etc/ssl/nadien/fullchain.pem;
    ssl_certificate_key /etc/ssl/nadien/privkey.pem;

    # Selama NADIEN belum punya login: batasi IP dan/atau basic auth
    allow 10.0.0.0/8;
    allow 172.16.0.0/12;
    deny  all;
    auth_basic           "NADIEN";
    auth_basic_user_file /etc/nginx/nadien.htpasswd;   # htpasswd -c /etc/nginx/nadien.htpasswd <user>

    client_max_body_size 50m;            # upload config / CSV
    proxy_read_timeout   3600s;          # sync manual dari UI bisa lama

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
server {
    listen 80;
    server_name nadien.example.co.id;
    return 301 https://$host$request_uri;
}
```

```bash
sudo ln -s /etc/nginx/sites-available/nadien /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

Untuk sertifikat, pakai sertifikat internal perusahaan. Kalau domainnya publik, bisa juga memakai
`certbot --nginx`. Buka firewall hanya untuk port 443 (dan 80 untuk redirect), misalnya
`sudo ufw allow 443/tcp`.

### Langkah 6: Jadwal collector (cron)

Jalankan semua collector lewat `app.collectors.run`. Jangan memanggil endpoint sync dari script lain.
Backup otomatis hanya dijalankan lewat jalur ini. Tambahkan dengan `crontab -e` di user yang menjalankan Docker:

```cron
# Utilisasi saat ini, setiap 10 menit (tanpa backup)
*/10 * * * * cd /opt/nadien && docker compose exec -T backend python -m app.collectors.run observium --parts utilization >> /var/log/nadien-util.log 2>&1
# Full sync inventory + LLDP, setiap hari 01:00 (backup otomatis dulu)
0 1 * * *    cd /opt/nadien && docker compose exec -T backend python -m app.collectors.run observium >> /var/log/nadien-sync.log 2>&1
# Histori RRD 7 hari terakhir, setiap hari 03:00
0 3 * * *    cd /opt/nadien && docker compose exec -T backend python -m app.collectors.run rrd --days 7 >> /var/log/nadien-rrd.log 2>&1
# Config RANCID, setiap hari 04:00
0 4 * * *    cd /opt/nadien && docker compose exec -T backend python -m app.collectors.run rancid >> /var/log/nadien-rancid.log 2>&1
```

Jangan menjadwalkan Full sync dan RRD pada jam yang sama. Keduanya membebani Observium.

### Langkah 7: Backup & restore

- Backup otomatis (`pg_dump`, 15 terakhir) dibuat sebelum setiap Full sync atau sync config. Hasilnya ada di
  `/opt/nadien/backups/`.
- Backup manual dan daftar backup:

  ```bash
  docker compose exec backend python -m app.backup create sebelum-upgrade
  docker compose exec backend python -m app.backup list
  ```

- Salin `/opt/nadien/backups/` ke storage lain, misalnya NAS atau server backup (`rsync` via cron).
  Backup yang hanya ada di server yang sama tidak menolong kalau disk server rusak.
- Restore (hentikan akses user dulu):

  ```bash
  docker compose exec backend python -m app.backup restore <nama-file>
  docker compose restart backend
  ```

### Langkah 8: Upgrade versi

```bash
cd /opt/nadien
docker compose exec backend python -m app.backup create sebelum-upgrade
git pull                                  # atau git checkout <tag-baru>
git diff HEAD@{1} -- backend/app/models.py  # ada perubahan skema? (lihat peringatan Alembic di atas)
docker compose up -d --build
curl -s http://127.0.0.1:8080/api/health
```

Kalau `models.py` menambah kolom atau tabel, tabel **baru** akan dibuat otomatis, tetapi kolom baru di tabel lama
**tidak**. Tambahkan dengan `ALTER TABLE` manual sampai migrasi Alembic tersedia.

### Troubleshooting

| Gejala | Cek |
|---|---|
| UI terbuka tapi angka kosong / "Memuat..." terus | `docker compose logs backend`, lalu `curl 127.0.0.1:8080/api/health` |
| `backend` restart terus | Password di `NADIEN_DATABASE_URL` tidak sama dengan `POSTGRES_PASSWORD`. Kalau volume `pgdata` dibuat dengan password lama, password **tidak** ikut berubah. Ubah lewat `ALTER USER` di psql. |
| Sync Observium gagal 401/403 | Token salah atau dibatasi ke IP lain. Cek dari server: `curl -H "Authorization: Bearer <token>" <url>/api/v0/devices?limit=1` |
| Topologi kosong (LLDP 0) | Cek apakah Observium memang punya data neighbours. `NADIEN_OBSERVIUM_INCLUDE_CDP=true` membantu untuk Cisco. |
| Histori 400G bolong | Batas DS RRD Observium 100 Gbps. Lihat `possibly_clipped` di laporan import. |
| Sync dari UI timeout | Naikkan `proxy_read_timeout` di reverse proxy, atau jalankan lewat CLI/cron. |
