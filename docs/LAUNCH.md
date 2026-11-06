# Panduan Launch NADIEN

Skenario:

1. [Local / development](#1-local--development): laptop atau PC untuk coding dan uji coba.
2. [Server production](#2-server-production): satu server Linux yang dipakai tim engineering.
3. [Harvester](#3-harvester-production): VM Harvester (compose) atau RKE2 guest cluster (Kubernetes).
4. [Kapasitas ±50 user](#4-kapasitas-50-user-bersamaan): tuning yang sudah disiapkan dan hasil uji bebannya.

Komponen yang selalu ada:

| Komponen | Isi | Port default |
|---|---|---|
| `db` | PostgreSQL 16 + PostGIS | 5432 |
| `backend` | FastAPI (`uvicorn app.main:app`) | 8000 |
| `frontend` | React build yang disajikan nginx (non-root). nginx juga mem-proxy `/api`, `/docs`, `/openapi.json` ke backend | 8080 di container dan di host |

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

3. **Start/restart manual.** `nadien.cmd` di root project menjalankan langkah 1 dan 2 di background
   (double-click, atau `nadien start | restart | stop | status`). Kalau Docker engine berjalan, skrip memakai
   `docker compose`. Log ada di `data\logs\`.

4. **Git worktree** (`.claude/worktrees/...`) tidak membawa `frontend/node_modules`. Jalankan `npm install` di worktree,
   atau buat junction ke `node_modules` checkout utama kalau `package-lock.json`-nya sama.

### Menghubungkan ke Observium dari lokal

Cara paling mudah: buka menu **Pengaturan Collector** di sidebar. Isi URL dan token Observium (pakai token
**read-only**), URL gitweb dan jadwal harian, lalu klik **Tes koneksi**. Nilainya langsung berlaku tanpa restart dan
menimpa `backend/.env`. Token dan password disimpan terenkripsi dan tidak pernah ditampilkan lagi.

Cara lain, isi `NADIEN_OBSERVIUM_URL` dan `NADIEN_OBSERVIUM_TOKEN` di `backend/.env`. Setelah itu:

```bash
python -m app.collectors.run observium          # Full sync: device, port, LLDP, mapping port_id
python -m app.collectors.run rrd --days 90      # histori utilisasi (setelah Full sync)
```

Database di-backup otomatis sebelum setiap sync (lihat README, bagian *Backup database*).

---

## 2. Server production

> ⚠️ **Baca sebelum go-live**
>
> - NADIEN punya login, role, dan SSO, tapi tetap batasi aksesnya ke jaringan internal/VPN (lihat langkah 5).
> - Isi `NADIEN_SECRET_KEY` (lihat [`deploy/k8s/README.md`](../deploy/k8s/README.md)). Kunci itu dipakai untuk sesi login
>   dan enkripsi secret.
> - Migrasi database berjalan otomatis saat backend start. Buat backup sebelum upgrade.
> - `docker-compose.yml` bawaan memakai password database `nadien` dan membuka port 5432/8000 ke semua interface.
>   Itu hanya cocok untuk development. Langkah 3 menutupnya.
> - `docker-compose.yml` (development) mengisi **data contoh** ke database kosong (`NADIEN_SEED_DEMO=true`).
>   `docker-compose.prod.yml` mematikannya.

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
# RANCID gitweb (semua repo; sumber satu-satunya untuk CSR) + jadwal harian
NADIEN_GITWEB_URL=http://172.17.12.232/gitweb/
NADIEN_GITWEB_DAILY_AT=02:30
# Kalau UI diakses dari domain lain selain lewat nginx bawaan:
# NADIEN_CORS_ORIGINS=["https://nadien.example.co.id"]
```

`NADIEN_DATABASE_URL` **tidak** perlu diisi di sini, karena sudah diatur oleh `docker-compose.prod.yml`.

**c. Override compose.** File [`docker-compose.prod.yml`](../docker-compose.prod.yml) sudah ada di repo. Isinya:

- menutup port DB dan API, sehingga keduanya hanya bisa diakses lewat nginx frontend;
- mengambil password DB dari `.env`;
- mematikan data contoh (`NADIEN_SEED_DEMO=false`);
- menjalankan **4 worker** uvicorn dengan pool koneksi yang disesuaikan untuk ±50 user (lihat bagian 4);
- menyetel PostgreSQL (`max_connections`, `shared_buffers`);
- menambahkan restart otomatis.

Frontend dibuka di `127.0.0.1:8080` untuk dipasang di belakang reverse proxy HTTPS (langkah 5). Kalau tidak memakai
reverse proxy dan servernya hanya bisa diakses dari jaringan internal, ganti menjadi `"80:8080"`.

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

    # NADIEN punya login sendiri; ini membatasi siapa yang bisa mencapainya
    allow 10.0.0.0/8;
    allow 172.16.0.0/12;
    deny  all;

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

**Update RANCID gitweb tidak perlu cron.** Backend menjalankannya sendiri setiap hari pada `NADIEN_GITWEB_DAILY_AT`
(default 02:30, zona `NADIEN_SCHEDULE_TIMEZONE`). Perilakunya:

- Hanya file yang berubah sejak sync terakhir yang diunduh.
- Backup dibuat hanya kalau memang ada perubahan.
- Jatah tiap hari diklaim satu kali di database, sehingga dengan 4 worker atau beberapa replica sync tetap jalan
  sekali saja.
- Kalau server mati pada jam itu, sync dijalankan begitu server hidup lagi.
- Sync manual tetap bisa dari UI (**Collectors → RANCID gitweb**) atau dari CLI:

```bash
docker compose exec -T backend python -m app.collectors.run gitweb          # perubahan terbaru
docker compose exec -T backend python -m app.collectors.run gitweb --full   # baca ulang semua file
```

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
docker compose up -d --build              # migrasi database berjalan saat backend start
curl -s http://127.0.0.1:8080/api/ready
```

---

## 3. Harvester (production)

Harvester (SUSE HCI) bisa menjalankan NADIEN dengan dua cara. Mulai dari Opsi A, lalu pindah ke Opsi B kalau butuh
high availability.

### Opsi A: VM Harvester + Docker Compose (disarankan untuk mulai)

1. Upload image cloud Ubuntu 22.04/24.04 di **Images**, misalnya
   `https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img`.
2. Buat VM di **Virtual Machines → Create**:
   - Spesifikasi: 8 vCPU, 16 GB RAM, disk 200 GB.
   - Network: VLAN yang bisa mengakses Observium (172.17.42.136) dan gitweb (172.17.12.232).
   - User data (Advanced Options): isi dengan [`deploy/harvester/cloud-init.yaml`](../deploy/harvester/cloud-init.yaml).
     Isi `REPO_URL` di file itu, atau salin kode ke `/opt/nadien` setelah VM menyala.
3. Login ke VM, lalu ikuti **bagian 2, langkah 3–8** (`.env`, `backend/.env`, `docker compose up -d`, reverse proxy,
   cron). `docker-compose.prod.yml` sudah memakai 4 worker dan tuning PostgreSQL untuk ±50 user.
4. Aktifkan backup VM di **Harvester → Backup & Snapshot** sebagai pelengkap backup `pg_dump` NADIEN. Simpan backup
   di target backup S3/NFS di luar cluster.

### Opsi B: Kubernetes (RKE2 guest cluster di Harvester)

Manifest dan langkah lengkapnya ada di [`deploy/k8s/README.md`](../deploy/k8s/README.md): secret yang wajib, deploy,
isi data pertama, backup, dan operasi harian.

---

## 4. Kapasitas ±50 user bersamaan

Yang sudah disiapkan:

- **Cache read-model** (`NADIEN_CACHE_TTL_S`, default 60 detik) untuk topologi, capacity, hotspot, dashboard, ringkasan
  port dan perangkat:
  - Satu view dihitung sekali per proses, lalu dipakai bersama oleh semua user.
  - Setiap perubahan data (edit, discovery, sync selesai) menaikkan versi cache di database. Semua worker dan replica
    membuang cache-nya dalam ±1 detik.
  - Kalau banyak request datang bersamaan untuk view yang sama, view itu dihitung sekali saja (single flight).
  - Saat TTL habis, satu request menghitung ulang dan yang lain tetap mendapat nilai lama, jadi tidak ada yang antre.
    Setelah ada perubahan data, nilai lama dibuang dan request menunggu nilai baru.
  - Login, logout, kalkulator, AI chat, simulasi, dan hitung jalur tidak membuang cache karena tidak mengubah data.
- **Proses:** beberapa worker uvicorn (`WEB_CONCURRENCY`). Status job sync dan jadwal harian disimpan di database,
  sehingga aman dijalankan multi-proses dan multi-replica.
- **Database:**
  - Pool koneksi per proses: `NADIEN_DB_POOL_SIZE` dan `NADIEN_DB_MAX_OVERFLOW`. Jaga agar
    worker × (pool + overflow) tetap di bawah `max_connections`.
  - Index baru untuk link per perangkat, perangkat per POP, dan sampel utilisasi per waktu.
  - Dashboard menghitung port di SQL, tidak lagi memuat 300 ribu interface.
- **Jaringan:** respons API di-gzip (backend dan nginx). Aset frontend di-cache permanen karena nama file-nya
  mengandung hash.

Hasil uji beban (2026-11-06): salinan data nyata di PostgreSQL 16 (4.547 perangkat, 349.731 interface, 8.456 link,
485.856 sampel utilisasi), lewat nginx dengan login. Backend 4 worker dibatasi 4 CPU / 6 GB, PostgreSQL 2 CPU / 5 GB.
k6 berjalan di mesin yang sama. Sync tidak berjalan selama pengujian.

| Skenario | Request/dtk | Gagal | Median | p95 | Maks |
|---|---|---|---|---|---|
| 50 user, jeda 3–8 dtk, 20 menit | 17,4 | 0,00% | 11 ms | 261 ms | 12,8 dtk |
| 50 user, jeda 0,5–2 dtk, 3 menit | 66,5 | 0,00% | 12 ms | 212 ms | 9,8 dtk |
| 100 user, jeda 3–8 dtk, 3 menit | 32,6 | 0,00% | 11 ms | 417 ms | 12,8 dtk |
| 50 user + 1 perubahan data tiap 30 dtk, 3 menit | 14,6 | 0,00% | 15 ms | 2,1 dtk | 11,6 dtk |

Yang perlu diketahui:

- **Halaman berat bisa beberapa detik sesekali.** Forecast per node p95 ±3 dtk; satu user yang kebetulan menghitung ulang
  menunggu sampai ±12 dtk saat server sibuk.
- **Perubahan data beruntun terasa.** Dengan satu perubahan tiap 30 dtk, p95 dashboard dan hotspot naik ke 8–10 dtk,
  karena setiap perubahan membuang semua cache di semua proses.
- **RAM backend** mendatar di ±3 GB untuk 4 worker pada uji 20 menit. CPU rata-rata 0,8 core, puncak 3,8 core.

Script uji beban ada di [`deploy/loadtest/k6-50users.js`](../deploy/loadtest/k6-50users.js). Ukur ulang di server
production sebelum go-live. Kalau user bertambah, naikkan `maxReplicas` HPA `nadien-api` (Opsi B) atau
`WEB_CONCURRENCY` (Opsi A), lalu cek `max_connections`.

### Troubleshooting

| Gejala | Cek |
|---|---|
| UI terbuka tapi angka kosong / "Memuat..." terus | `docker compose logs backend`, lalu `curl 127.0.0.1:8080/api/health` |
| `backend` restart terus | Password di `NADIEN_DATABASE_URL` tidak sama dengan `POSTGRES_PASSWORD`. Kalau volume `pgdata` dibuat dengan password lama, password **tidak** ikut berubah. Ubah lewat `ALTER USER` di psql. |
| Sync Observium gagal 401/403 | Token salah atau dibatasi ke IP lain. Cek dari server: `curl -H "Authorization: Bearer <token>" <url>/api/v0/devices?limit=1` |
| Topologi kosong (LLDP 0) | Cek apakah Observium memang punya data neighbours. `NADIEN_OBSERVIUM_INCLUDE_CDP=true` membantu untuk Cisco. |
| Histori 400G bolong | Batas DS RRD Observium 100 Gbps. Lihat `possibly_clipped` di laporan import. |
| Sync dari UI timeout | Naikkan `proxy_read_timeout` di reverse proxy, atau jalankan lewat CLI/cron. |
