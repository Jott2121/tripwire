# .cursor/BUGBOT.md — review rules for tripwire
# (Bugbot does NOT read .cursor/rules/*.mdc; it reads this file.)

Flag as **blocking**:
1. Any guard, gate, validator, or refusal path that now fails OPEN (returns allow/True/answer on exception, timeout, empty, or malformed input).
2. Lowered thresholds: `--cov-fail-under`, mutation score floors, MIN_SCORE-style constants, retry/timeout bounds removed.
3. Tests that were deleted, skipped, xfailed, or had assertions weakened without a stated reason in the PR body.
4. README/docs numbers that changed without a reproducing command or artifact in the PR.
5. Employer or corporate data: the author's employer's name or abbreviations (CI checks them against the hashed list in `.github/blocked-terms.sha256`), its programs, products, or sites, internal acronyms, CUI/ITAR markings, real-looking employee records, internal URLs. A new employer mention anywhere is blocking.
6. Secrets, tokens, `.env` files, or absolute personal paths committed.
7. Unpinned GitHub Actions (must be full SHA) or widened workflow `permissions:`.
8. Live network / model calls added to the default test path.

Flag as **non-blocking**:
- New runtime dependency without justification.
- Non-deterministic code in eval paths (unseeded RNG, unordered iteration in hashes/output).
- Exceptions swallowed in non-decision code.

Do not comment on: formatting handled by ruff; naming bikesheds.

Also blocking: the blocked-term scan (`scripts/scan_blocked_terms.py`, `.github/blocked-terms.sha256`) passing on a missing, empty, or malformed hash file, digests removed, or paths excluded beyond generated junk. A plaintext blocked term added anywhere, including tests and docs, is blocking.
