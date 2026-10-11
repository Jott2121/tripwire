---
name: verify-receipts
description: Use before opening or updating a PR, or when README/docs numbers change. Re-runs the repo's lint, tests, and eval checks, cross-checks every published number against its reproducing command, and writes a receipts block for the PR body.
---
# Verify receipts

1. Read AGENTS.md "Commands". Run setup, lint, tests (with coverage gate), and the offline eval/`--check` scripts. Capture exit codes and the last ~20 lines of each.
2. `git diff origin/main -- README.md docs/ WRITEUP.md` — list every number that changed. For each, find the command that produces it and re-run it. If you can't reproduce it, mark it UNVERIFIED and stop; do not open the PR as ready.
3. Grep the diff for employer-data and secret patterns (see .cursor/rules/40-security-and-data.mdc). Any hit = stop and report.
4. Confirm no thresholds were lowered (`--cov-fail-under`, mutation floors, gate constants) and no tests were skipped/removed.
5. Output a Markdown block:
   - Commands run + pass/fail
   - Numbers: before → after → command → reproduced? (Y/N)
   - Not verified / known gaps
6. Paste that block into the PR's "How verified" section. Never claim a check passed that you didn't run.
