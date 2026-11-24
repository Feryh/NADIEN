#!/usr/bin/env bash
# Run from a checkout (Git Bash / WSL / Linux). Ships a commit of main to the server, where it is built while the
# live version keeps serving, and goes live at the next maintenance window (23:00 WIB).
#
#   deploy/server/release.sh [ref]          stage ref (default: the main branch, NADIEN_MAIN)
#   deploy/server/release.sh [ref] --now    stage and put it live right away
#   deploy/server/release.sh --status
#   deploy/server/release.sh --install      copy the server script, compose override and timer (needs sudo there)
#
# NADIEN_SSH: ssh binary (on Windows the OpenSSH one, which sees the Windows ssh-agent:
#   NADIEN_SSH=/c/Windows/System32/OpenSSH/ssh.exe). NADIEN_HOST: ssh alias of the server.
set -euo pipefail
export MSYS_NO_PATHCONV=1  # Git Bash would turn the remote /data/... paths into C:/Program Files/Git/data/...

SSH=${NADIEN_SSH:-ssh}
HOST=${NADIEN_HOST:-nadien-server}
MAIN=${NADIEN_MAIN:-gitlab/main}
REMOTE=/data/nadien/deploy
HERE=$(cd "$(dirname "$0")" && pwd)

remote() { "$SSH" -o BatchMode=yes "$HOST" "$@"; }

install_files() {
  remote "mkdir -p $REMOTE"
  local file
  for file in nadien-release.sh docker-compose.server.yml nadien-release.service nadien-release.timer; do
    remote "cat > $REMOTE/$file" < "$HERE/$file"
  done
  remote "chmod 755 $REMOTE/nadien-release.sh && sudo -n install -m 644 $REMOTE/nadien-release.service \
    $REMOTE/nadien-release.timer /etc/systemd/system/ && sudo -n systemctl daemon-reload \
    && sudo -n systemctl enable --now nadien-release.timer && systemctl list-timers nadien-release.timer --no-pager"
}

stage() {
  local ref=$1 now=$2 commit
  git fetch -q "${MAIN%%/*}"
  commit=$(git rev-parse --short=12 "$ref^{commit}")
  git merge-base --is-ancestor "$commit" "$MAIN" || { echo "$ref ($commit) is not on $MAIN: refusing" >&2; exit 1; }
  echo "staging $commit ($(git log -1 --format=%s "$commit"))"
  git archive --format=tar "$commit" | remote "$REMOTE/nadien-release.sh stage $commit"
  if [[ $now == yes ]]; then
    remote "$REMOTE/nadien-release.sh apply $commit"
  fi
  remote "$REMOTE/nadien-release.sh status"
}

main() {
  local ref=$MAIN now=no arg
  for arg in "$@"; do
    case $arg in
      --install) install_files; return ;;
      --status) remote "$REMOTE/nadien-release.sh status"; return ;;
      --now) now=yes ;;
      -*) echo "unknown option $arg" >&2; exit 2 ;;
      *) ref=$arg ;;
    esac
  done
  stage "$ref" "$now"
}

main "$@"
