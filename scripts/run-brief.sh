#!/usr/bin/env bash
# Headless Claude Code run for one GitHub issue, billed to the Max plan.
#
#   nohup scripts/run-brief.sh <issue-number> >/dev/null 2>&1 &
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

N="${1:?usage: run-brief.sh <issue-number>}"
[[ "$N" =~ ^[0-9]+$ ]] || { echo "issue number must be numeric: $N" >&2; exit 2; }

OWNER="Jott2121"
REPO_SLUG="$OWNER/tripwire"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
WT_ROOT="${TRIPWIRE_WT_ROOT:-$HOME/tripwire-wt}"
WT="$WT_ROOT/issue-$N"
BR="claude/issue-$N"
LOGS="$WT_ROOT/logs"
LOG="$LOGS/issue-$N.log"
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
