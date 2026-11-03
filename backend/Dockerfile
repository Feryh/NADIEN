FROM python:3.12-slim-bookworm

# pg_dump/pg_restore for pre-sync database backups. The client must match the server major
# version (postgis/postgis:16), so it comes from the PostgreSQL apt repository.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && install -d /usr/share/postgresql-common/pgdg \
 && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
 && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
    > /etc/apt/sources.list.d/pgdg.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends postgresql-client-16 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

EXPOSE 8000
# NADIEN_SEED_DEMO=true fills an empty database with demo data (development only).
# WEB_CONCURRENCY = uvicorn worker processes; job state and the daily schedule live in the DB, so
# workers and replicas never run the same sync twice.
ENV WEB_CONCURRENCY=1 NADIEN_SEED_DEMO=false
CMD ["sh", "-c", "if [ \"$NADIEN_SEED_DEMO\" = true ]; then python -m app.seed; fi; exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers ${WEB_CONCURRENCY} --proxy-headers --forwarded-allow-ips='*'"]
