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
COPY alembic.ini .
COPY app ./app

# Runs as an unprivileged user: Kubernetes "restricted" pods need it. Docker Compose bind-mounts ./backups, which
# belongs to the host user, so docker-compose.yml overrides this with root (see there).
RUN useradd --uid 10001 --no-create-home --home-dir /tmp --shell /usr/sbin/nologin nadien \
 && install -d -o 10001 -g 10001 /backups
USER 10001
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

EXPOSE 8000
# NADIEN_SEED_DEMO=true fills an empty database with demo data (development only).
# WEB_CONCURRENCY = uvicorn worker processes; job state and the daily schedule live in the DB, so
# workers and replicas never run the same sync twice.
# Keep-alive outlasts nginx's 60 s for idle upstream connections, so nginx is the side that closes them.
# One proxy: the frontend nginx. Add one for each proxy in front of it (NADIEN_TRUSTED_PROXY_HOPS in app/config.py).
ENV WEB_CONCURRENCY=1 NADIEN_SEED_DEMO=false NADIEN_TRUSTED_PROXY_HOPS=1
CMD ["sh", "-c", "if [ \"$NADIEN_SEED_DEMO\" = true ]; then python -m app.seed; fi; exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers ${WEB_CONCURRENCY} --proxy-headers --forwarded-allow-ips='*' --timeout-graceful-shutdown 30 --timeout-keep-alive 75"]
