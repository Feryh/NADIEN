#!/usr/bin/env bash
# PreToolUse hook: blokir perintah Bash yang berbahaya.
# Input JSON dari Claude Code masuk lewat stdin; exit 2 = blokir + pesan ke Claude.

input="$(cat)"

blocked_patterns=(
  'rm -rf /'
  'rm -rf ~'
  'rm -rf \*'
  'git push --force'
  'git push -f'
  'git reset --hard'
  'DROP TABLE'
  'DROP DATABASE'
  'mkfs'
  ':(){ :|:& };:'
)

for pattern in "${blocked_patterns[@]}"; do
  if printf '%s' "$input" | grep -qiF -- "$pattern"; then
    echo "Diblokir oleh validate-bash.sh: perintah mengandung '$pattern'." >&2
    exit 2
  fi
done

exit 0
