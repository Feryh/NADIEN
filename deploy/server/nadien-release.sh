#!/usr/bin/env bash
# Releases of NADIEN on a single docker compose server. A release is staged first (source unpacked, images built,
# the live version keeps serving) and goes live at the next maintenance window (nadien-release.timer), or at once.
#
#   nadien-release.sh stage <commit>  < source.tar   unpack + build, queue for the next window
#   nadien-release.sh apply-pending                  what the timer runs: put the queued release live
#   nadien-release.sh apply <commit>                 put a staged release live now
#   nadien-release.sh status
#
# Layout under $NADIEN_ROOT (default /data/nadien):
#   config/.env, config/backend.env    secrets shared by every release (POSTGRES_PASSWORD, NADIEN_*)
#   releases/<commit>/                 one source tree per release, images tagged nadien-*:<commit>
#   current -> releases/<commit>       the live one
#   backups/                           the app's own backups and secret key (bind-mounted at /backups)
#   deploy/                            this script, the compose override, dumps/, release.log
set -euo pipefail
umask 077  # dumps and release .env files hold password hashes and the database password

ROOT=${NADIEN_ROOT:-/data/nadien}
RELEASES=$ROOT/releases
CONFIG=$ROOT/config
DEPLOY=$ROOT/deploy
DUMPS=$DEPLOY/dumps
PENDING=$DEPLOY/pending
LOG=$DEPLOY/release.log
CURRENT=$ROOT/current
PROJECT=nadien
HEALTH_URL=${NADIEN_HEALTH_URL:-http://127.0.0.1/api/health}
HEALTH_WAIT_S=${NADIEN_HEALTH_WAIT_S:-240}  # startup runs the Alembic migrations first
KEEP_RELEASES=3
KEEP_DUMPS=10

log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" | tee -a "$LOG" >&2; }
die() { log "ERROR: $*"; exit 1; }

in_release() {  # in_release <dir> <docker compose args...>
  local dir=$1
  shift
  (cd "$dir" && docker compose "$@")
}

live_release() { [[ -L $CURRENT ]] && basename "$(readlink "$CURRENT")" || true; }

db_exec() {
  local container
  container=$(docker ps -q -f "label=com.docker.compose.project=$PROJECT" -f "label=com.docker.compose.service=db")
  [[ -n $container ]] || die "database container is not running"
  docker exec -i "$container" "$@"
}

db_revision() { db_exec psql -U nadien -d nadien -tAc "select version_num from alembic_version" 2>/dev/null || true; }

check_commit() { [[ $1 =~ ^[0-9a-f]{7,40}$ ]] || die "not a commit sha: $1"; }

# ------------------------------------------------------------------------------------------------ stage

write_release_env() {  # the compose .env of one release: shared secrets + which images it runs
  local dir=$1 release=$2
  {
    cat "$CONFIG/.env"
    echo "COMPOSE_PROJECT_NAME=$PROJECT"
    echo "COMPOSE_FILE=docker-compose.yml:docker-compose.prod.yml:docker-compose.server.yml"
    echo "NADIEN_RELEASE=$release"
  } > "$dir/.env"
  install -m 600 "$CONFIG/backend.env" "$dir/backend/.env"
}

stage() {
  local release=$1
  check_commit "$release"
  [[ $(live_release) != "$release" ]] || die "stage $release: that release is live already"
  local dir=$RELEASES/$release
  rm -r -f "$dir.tmp"
  mkdir -p "$dir.tmp"
  tar -x -C "$dir.tmp"
  cp "$DEPLOY/docker-compose.server.yml" "$dir.tmp/"
  write_release_env "$dir.tmp" "$release"
  rm -r -f "$dir"
  mv "$dir.tmp" "$dir"
  log "stage $release: building images (live $(live_release) keeps serving)"
  in_release "$dir" build -q || die "stage $release: build failed, live version untouched"
  echo "$release" > "$PENDING"
  log "stage $release: ready, goes live at the next maintenance window"
}

# ------------------------------------------------------------------------------------------------ apply

backup_db() {  # prints the dump path
  local dump
  mkdir -p "$DUMPS"
  dump=$DUMPS/pre-$1-$(date -u +%Y%m%dT%H%M%SZ).dump
  db_exec pg_dump -U nadien -Fc nadien > "$dump" || die "pg_dump failed, nothing changed"
  ls -1t "$DUMPS"/*.dump | tail -n +$((KEEP_DUMPS + 1)) | xargs -r rm -f
  echo "$dump"
}

restore_db() {
  log "restoring database from $1"
  db_exec dropdb -U nadien --force nadien
  db_exec createdb -U nadien nadien
  # pg_restore exits non-zero on harmless notices (extension comments): judge by the health check afterwards.
  db_exec pg_restore -U nadien -d nadien --no-owner < "$1" || log "pg_restore reported errors (see above)"
}

switch_to() {
  ln -sfn "$RELEASES/$1" "$CURRENT.new"
  mv -T "$CURRENT.new" "$CURRENT"
  in_release "$CURRENT/" up -d --remove-orphans
}

healthy() {
  local waited=0
  while ((waited < HEALTH_WAIT_S)); do
    curl -fsS -m 5 "$HEALTH_URL" 2>/dev/null | grep -q '"ok"' && return 0
    sleep 5
    waited=$((waited + 5))
  done
  return 1
}

roll_back() {  # roll_back <failed> <previous> <dump> <revision before>
  local failed=$1 previous=$2 dump=$3 revision=$4
  [[ -n $previous ]] || die "apply $failed: unhealthy and there is no previous release to go back to"
  log "apply $failed: health check failed, rolling back to $previous"
  in_release "$CURRENT/" stop backend frontend || true
  # Only when the new release migrated the schema: otherwise the data written meanwhile is kept.
  [[ $(db_revision) == "$revision" ]] || restore_db "$dump"
  switch_to "$previous"
  healthy || die "rollback to $previous is unhealthy too: needs a person"
  rm -f "$PENDING"
  die "apply $failed: rolled back to $previous"
}

prune() {  # keep the live release and the newest others, with their images
  local live old
  live=$(live_release)
  for old in $(ls -1t "$RELEASES" | grep -v -x -e "$live" -e '.*\.tmp' | tail -n +"$KEEP_RELEASES"); do
    log "prune release $old"
    rm -r -f "${RELEASES:?}/$old"
    docker image rm "nadien-backend:$old" "nadien-frontend:$old" >/dev/null 2>&1 || true
  done
}

apply() {
  local release=$1
  check_commit "$release"
  [[ -d $RELEASES/$release ]] || die "apply $release: not staged"
  local previous dump revision
  previous=$(live_release)
  if [[ $previous == "$release" ]]; then
    log "apply $release: already live"
    rm -f "$PENDING"
    return
  fi
  dump=$(backup_db "$release")
  revision=$(db_revision)
  log "apply $release: switching from ${previous:-nothing} (db backup $dump)"
  switch_to "$release"
  healthy || roll_back "$release" "$previous" "$dump" "$revision"
  rm -f "$PENDING"
  log "apply $release: live"
  prune
}

apply_pending() {
  [[ -s $PENDING ]] || { log "maintenance window: no staged release"; return; }
  apply "$(cat "$PENDING")"
}

status() {
  echo "live:    $(live_release)"
  echo "pending: $(cat "$PENDING" 2>/dev/null || echo -)"
  systemctl list-timers nadien-release.timer --no-pager 2>/dev/null | sed -n 2p
  tail -n 10 "$LOG" 2>/dev/null || true
}

main() {
  mkdir -p "$RELEASES" "$DEPLOY"
  [[ ${1:-} == status ]] && { status; return; }
  exec 9>"$DEPLOY/.lock"
  flock -n 9 || die "another release run is active"
  case ${1:-} in
    stage) stage "${2:?commit}" ;;
    apply) apply "${2:?commit}" ;;
    apply-pending) apply_pending ;;
    *) echo "usage: $0 stage <commit> < source.tar | apply <commit> | apply-pending | status" >&2; exit 2 ;;
  esac
}

main "$@"
