#!/usr/bin/env bash
# afterFileEdit (notification-only): format the edited Python file and warn on employer-data / secret patterns.
# It can't block the edit; CI's no-employer-data job is the real gate.
# Employer names are matched by hash (scripts/scan_blocked_terms.py); a scanner error also warns.
set -uo pipefail
f="$(python3 -c 'import sys,json; print(json.load(sys.stdin).get("file_path",""))')"
[ -f "$f" ] || exit 0
case "$f" in *.py) command -v ruff >/dev/null && ruff format -q "$f" && ruff check -q --fix "$f" ;; esac
root="$(cd "$(dirname "$0")/../.." && pwd)"
warn=0
if grep -EIiq '\bCUI\b|ITAR|EXPORT CONTROLLED|BEGIN (RSA|OPENSSH) PRIVATE KEY|xai-[A-Za-z0-9]{20,}' "$f"; then
  warn=1
fi
if ! python3 "$root/scripts/scan_blocked_terms.py" --root "$root" --paths "$f" >/dev/null 2>&1; then
  warn=1
fi
if [ "$warn" -eq 1 ]; then
  mkdir -p "$root/.cursor/hook-logs"
  echo "$(date -u +%FT%TZ) WARN employer/secret pattern in $f" >> "$root/.cursor/hook-logs/warnings.log"
fi
exit 0
