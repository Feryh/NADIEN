---
description: Review perubahan kode yang belum di-commit
allowed-tools: Bash(git diff:*), Bash(git status)
---

## Context

- Status: !`git status --short`
- Diff: !`git diff HEAD`

## Task

Review perubahan di atas. Fokus pada:

1. Bug dan edge case
2. Keamanan (input validation, secret yang bocor)
3. Kepatuhan pada aturan di `.claude/rules/`
4. Test yang kurang

Laporkan temuan berurutan dari yang paling parah, sertakan `file:line`.
