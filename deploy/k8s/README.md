# NADIEN di Kubernetes

Manifest untuk RKE2 di Harvester (ingress-nginx, storage class `harvester`). Di cluster lain, ganti `storageClassName`
di `postgres.yaml` dan `backend.yaml`, `ingressClassName` di `ingress.yaml`, dan daftar namespace ingress controller di
`networkpolicy.yaml`.

| Komponen | Isi |
|---|---|
| `nadien-frontend` | 2 replica nginx. `/api/collectors`, `/api/backups`, `/api/changes` ke worker, sisanya ke api |
| `nadien-api` | 2–6 replica (HPA) × 2 proses, tanpa state dan tanpa scheduler |
| `nadien-worker` | 1 pod: sync, backup `pg_dump`, push ke perangkat, jadwal gitweb harian. Punya PVC `nadien-backups` |
| `nadien-db` | StatefulSet PostGIS 16, 1 replica, PVC 100 Gi. **Satu titik gagal**: lihat bagian Backup |
| CronJob | Utilisasi tiap 10 menit, Full sync 01:00, Sync CSR 01:30, RRD 04:30, RRD CSR 05:30 |

Syarat cluster: Kubernetes ≥ 1.27 (`timeZone` CronJob), metrics-server (HPA), CNI yang menegakkan NetworkPolicy,
ingress-nginx, dan registry image yang bisa dijangkau.

## 1. Build dan push image

```bash
docker build -t registry.example.co.id/neteng/nadien-backend:0.1.0 backend
docker build -t registry.example.co.id/neteng/nadien-frontend:0.1.0 frontend
docker push registry.example.co.id/neteng/nadien-backend:0.1.0
docker push registry.example.co.id/neteng/nadien-frontend:0.1.0
```

Naikkan tag di setiap rilis dan sesuaikan `images:` di `kustomization.yaml`. Jangan memakai `latest`.

## 2. Secret (jangan di-commit)

Tiga nilai wajib dibuat. Tanpa `NADIEN_SECRET_KEY` dan `NADIEN_JOB_TOKEN`, pod berhenti di
`CreateContainerConfigError`, dan itu memang disengaja.

```bash
kubectl apply -f deploy/k8s/namespace.yaml
```

```bash
kubectl -n nadien create secret generic nadien-db --from-literal=POSTGRES_PASSWORD="$(openssl rand -hex 24)"
```

Isi `nadien.env` di luar repo, lalu buat secret-nya:

```ini
# Kunci sesi login dan enkripsi secret. Buat sekali, simpan aman. Mengubahnya membuat semua user logout
# dan secret di Pengaturan Collector tidak terbaca lagi:
#   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
NADIEN_SECRET_KEY=
# Token CronJob untuk memulai sync: openssl rand -hex 32
NADIEN_JOB_TOKEN=
# Password awal admin, wajib diganti saat login pertama.
# Tanpa ini, buat dengan: kubectl -n nadien exec deploy/nadien-worker -- python -m app.reset_password
NADIEN_ADMIN_PASSWORD=
NADIEN_OBSERVIUM_URL=http://172.17.42.136
NADIEN_OBSERVIUM_CSR_URL=http://172.17.12.112
NADIEN_OBSERVIUM_USER=
NADIEN_OBSERVIUM_PASSWORD=
NADIEN_GITWEB_URL=http://172.17.12.232/gitweb/
```

```bash
kubectl -n nadien create secret generic nadien-env --from-env-file=nadien.env
```

```bash
kubectl -n nadien create secret tls nadien-tls --cert=fullchain.pem --key=privkey.pem
```

## 3. Deploy

Ganti `nadien.example.co.id` di `ingress.yaml` dan `kustomization.yaml`, dan nama registry di `kustomization.yaml`.

```bash
kubectl apply -k deploy/k8s
```

```bash
kubectl -n nadien rollout status statefulset/nadien-db
```

```bash
kubectl -n nadien rollout status deploy/nadien-worker deploy/nadien-api deploy/nadien-frontend
```

Migrasi database berjalan sendiri saat pod backend start, dan advisory lock membuatnya aman untuk banyak replica.

## 4. Isi data pertama kali

```bash
kubectl -n nadien exec deploy/nadien-worker -- python -m app.collectors.run observium
```

```bash
kubectl -n nadien exec deploy/nadien-worker -- python -m app.collectors.run observium-csr
```

```bash
kubectl -n nadien exec deploy/nadien-worker -- python -m app.collectors.run rrd --days 90
```

```bash
kubectl -n nadien exec deploy/nadien-worker -- python -m app.collectors.run rrd-csr --days 90
```

## Pindah data dari server yang sudah berjalan

Di server PostgreSQL lama:

```bash
pg_dump --format=custom --no-owner -f nadien.dump "$DATABASE_URL"
```

```bash
kubectl -n nadien cp nadien.dump nadien-db-0:/tmp/nadien.dump
```

```bash
kubectl -n nadien exec nadien-db-0 -- pg_restore --clean --if-exists --no-owner -U nadien -d nadien /tmp/nadien.dump
```

Data di SQLite (PC development) tidak bisa dipindah dengan cara ini. Jalankan langkah 4 untuk mengisinya dari
Observium. Pengaturan buatan tangan seperti POP dan kunci POP perlu dipindahkan terpisah.

## Backup

- `nadien-worker` membuat `pg_dump` sebelum setiap Full sync (15 terakhir disimpan di PVC `nadien-backups`).
- **PVC berada di cluster yang sama dengan database.** Salin ke luar cluster: aktifkan Harvester Backup ke target
  S3/NFS untuk volume `nadien-backups` dan `data-nadien-db-0`.
- Coba restore secara berkala. Backup yang belum pernah dipulihkan belum terbukti.

```bash
kubectl -n nadien exec deploy/nadien-worker -- python -m app.backup restore <nama-file>
```

## Operasi

| Kebutuhan | Perintah |
|---|---|
| Status | `kubectl -n nadien get pods,hpa,pvc,cronjob` |
| Log API | `kubectl -n nadien logs deploy/nadien-api --tail=100` |
| Jalankan sync sekarang | `kubectl -n nadien create job --from=cronjob/nadien-utilization sync-manual` |
| Rollback rilis | `kubectl -n nadien rollout undo deploy/nadien-api deploy/nadien-worker deploy/nadien-frontend` |
| Uji beban | `deploy/loadtest/k6-50users.js` |

`rollout undo` tidak membatalkan migrasi. Sebelum rilis yang mengubah skema, buat backup dulu.
