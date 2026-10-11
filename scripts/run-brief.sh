#!/usr/bin/env bash
# Headless Claude Code run for one GitHub issue, billed to the Max plan.
#
#   scripts/run-brief.sh <n>
#
# The script self-detaches: a plain launch forks into a new session and returns
# immediately, so the run survives the launching shell ending (a bare `nohup ... &`
# would still be killed when its process group is torn down; nohup only ignores SIGHUP).
#
# Creates worktree ~/tripwire-wt/issue-N on branch claude/issue-N (from origin/main),
# builds a venv there, and runs `claude -p` with the issue as the brief. Output goes to
# ~/tripwire-wt/logs/issue-N.log (stream-json). The repo's .claude/settings.json Stop hook
# writes .claude/last-run-done in the worktree when the run ends.
#
# Fails closed: refuses if ANTHROPIC_API_KEY is set (that would bill the API, not Max),
# if the issue was not opened by the repo owner, or if the issue number is not numeric.
# Never uses --bare or --dangerously-skip-permissions.
set -euo pipefail

# Validate the argument first, in the foreground, so a bad launch fails visibly
# (to stderr, exit 2) without detaching or creating anything.
if [ "$#" -lt 1 ] || [ -z "${1:-}" ]; then
  echo "usage: run-brief.sh <issue-number>" >&2
  exit 2
fi
N="$1"
[[ "$N" =~ ^[0-9]+$ ]] || { echo "issue number must be numeric: $N" >&2; exit 2; }

OWNER="Jott2121"
REPO_SLUG="$OWNER/tripwire"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
WT_ROOT="${TRIPWIRE_WT_ROOT:-$HOME/tripwire-wt}"
WT="$WT_ROOT/issue-$N"
BR="claude/issue-$N"
LOGS="$WT_ROOT/logs"
LOG="$LOGS/issue-$N.log"

# Self-detach: unless we are already the detached child, re-launch ourselves fully
# detached and exit 0 immediately. macOS has no `setsid` binary, so python3 does the
# double-detach: os.fork() (parent exits), then the child os.setsid()s, redirects
# stdin from /dev/null and stdout/stderr (appended) to the log, and os.execvp()s the
# re-run under nohup with TRIPWIRE_DETACHED=1.
if [ "${TRIPWIRE_DETACHED:-}" != "1" ]; then
  SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
  DETACH_PY='import os, sys; _log = sys.argv[1]; _cmd = sys.argv[2:]; (os._exit(0) if os.fork() > 0 else None); os.setsid(); _n = os.open("/dev/null", os.O_RDONLY); os.dup2(_n, 0); _f = os.open(_log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644); os.dup2(_f, 1); os.dup2(_f, 2); os.execvp(_cmd[0], _cmd)'
  CMD=(python3 -c "$DETACH_PY" "$LOG" nohup env TRIPWIRE_DETACHED=1 bash "$SELF" "$N")
  if [ "${TRIPWIRE_DETACH_DRYRUN:-}" = "1" ]; then
    printf 'would exec: %s\n' "${CMD[*]}"
    exit 0
  fi
  mkdir -p "$LOGS"
  "${CMD[@]}"
  echo "launched issue-$N; log: $LOG"
  exit 0
fi

mkdir -p "$LOGS"
exec >>"$LOG" 2>&1
echo "=== $(date -u +%FT%TZ) run-brief issue-$N start (pid $$)"
echo "$$" > "$LOGS/issue-$N.pid"

if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  echo "REFUSING: ANTHROPIC_API_KEY is set; this run would bill the API instead of the Max plan."
  exit 3
fi

author="$(gh issue view "$N" -R "$REPO_SLUG" --json author -q .author.login)"
if [ "$author" != "$OWNER" ]; then
  echo "REFUSING: issue #$N was opened by '$author', not $OWNER."
  exit 4
fi
BRIEF="$(gh issue view "$N" -R "$REPO_SLUG" --json title,body -q '"# Issue #'"$N"': " + .title + "\n\n" + .body')"

cd "$REPO_DIR"
git fetch -q origin main
if [ ! -d "$WT" ]; then
  if git show-ref --verify --quiet "refs/heads/$BR"; then
    git worktree add "$WT" "$BR"
  else
    git worktree add -b "$BR" "$WT" origin/main
  fi
fi
cd "$WT"

PY="${TRIPWIRE_PYTHON:-python3}"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || { echo "REFUSING: $PY is older than 3.11; set TRIPWIRE_PYTHON."; exit 5; }
[ -x .venv/bin/python ] || "$PY" -m venv .venv
.venv/bin/python -m pip install -q --upgrade pip
.venv/bin/python -m pip install -q -e ".[dev]"
export PATH="$WT/.venv/bin:$PATH"

PROMPT="You are working in a git worktree of $REPO_SLUG, already on branch $BR. Follow AGENTS.md.

Implement the GitHub issue below, and only that. Then:
1. Run: ruff check . && ruff format --check . && python -m pytest -q --cov=tripwire --cov-fail-under=90 && python scripts/scan_blocked_terms.py. All must pass. Fix and re-run until they do.
2. git add the changed files and git commit with a message of the form 'type(scope): summary (#$N)'.
3. Push with exactly: git push -u origin $BR
4. Open a PR with: gh pr create --base main --head $BR --title '<type(scope): summary>' --body '<body>'. The body must say 'Closes #$N' and have sections What / Why / How verified (paste the command output) / Risks / Not done.
Do not merge, force-push, tag, or touch main. If something you need is denied, stop and explain in your final message.

$BRIEF"

# caffeinate keeps the Mac awake for the run (macOS); plain run elsewhere.
CAFF="caffeinate -i"
command -v caffeinate >/dev/null || CAFF=""

set +e
$CAFF claude -p "$PROMPT" \
  --permission-mode acceptEdits --permission-prompts none \
  --allowedTools "Read,Edit,Write,Bash(pytest *),Bash(python -m pytest *),Bash(ruff *),Bash(python scripts/scan_blocked_terms.py*),Bash(git status*),Bash(git diff*),Bash(git log *),Bash(git add *),Bash(git commit *),Bash(git push -u origin claude/*),Bash(gh pr create *)" \
  --disallowedTools "Bash(git push --force*),Bash(git push -f*),Bash(curl *),Bash(wget *),Bash(rm -rf *),Bash(gh pr merge*)" \
  --output-format stream-json --verbose < /dev/null
rc=$?
set -e
echo "=== $(date -u +%FT%TZ) run-brief issue-$N claude exit $rc"
exit "$rc"
