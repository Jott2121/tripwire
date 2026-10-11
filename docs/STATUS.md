# Tripwire build status

Updated 2026-10-11 (evening). One row per milestone; the assistant directing the build
reads this file first. Numbers never appear here unless a committed command
produced them.

## Milestones

| # | Milestone | State | PR |
|---|-----------|-------|----|
| — | SDR loader, synthetic rows, corruption injector + hash-chained ledger (issue #4) | **merged** 2026-10-11 (UTF-8 loader, observed conventions) | #6 |
| M0 | Measurement standards: `docs/MEASUREMENT_STANDARDS.md` + `tripwire/standards.py` + tests | **ready for review**, CI green; seven open items resolved from the committed profile receipt `reports/sdr_profile_2026.json`; main merged in | #7 (`claude/m0-measurement-standards`) |
| M0b | `scripts/profile_sdr.py`: offline profiler that closes the M0 open items from a locally downloaded file | **open PR**, stacked on M0 | #8 (`claude/m0b-profile-sdr`) |
| M1 | Data contract + store: typed schemas carrying units/revision/provenance; fixed-seed synthetic lots, tests, work orders; spec limits from public standards | part 1 types #9; part 2a generators + public spec rows #10; part 2b in-memory store **open PR** (`claude/m1-store`); all stacked, CI green | #9, #10, `claude/m1-store` |
| M2 | MCP server (Streamable HTTP, auth header) with `query_defects`, `get_spec`, `get_test_result`, `create_work_order`, `get_work_order`; in-memory store; contract tests | not started | — |
| M3 | Fault middleware F1-F6 + append-only hash-chained ledger with separately stored tail hash + deterministic adjudicator | not started (PR #6's injector and chain are the seed) | — |
| M4 | Task set (~30) with ground truth computed from the clean store | not started | — |
| M5 | Harness: xAI client (mocked in tests), server-side remote MCP and client-side function-calling paths, run records, cost tracking; `docs/PREREGISTRATION.md` committed before any scored run | not started | — |
| M6 | The gate: contract check + write read-back + fail-closed final report | not started | — |
| M7 | Live eval workflow (manual trigger, budget cap) and results report with CIs | not started | — |
| M8 | Demo: side-by-side ungated vs gated run, one-page results README, 60-second walkthrough | not started | — |

## Done in M0

- Researched the units, formats, and conventions of the FAA SDR annual CSV
  from FAA/CFR/NASA sources; recorded field by field with source URL and
  confidence in `docs/MEASUREMENT_STANDARDS.md`.
- `tripwire/standards.py`: units and exact conversion factors (fail closed
  across dimensions), the 76-column field registry with unit/kind/source/
  confidence, partial code lists with tri-state lookup, N-number (14 CFR
  47.15), JASC, control-number and date validators, and the `UNRESOLVED`
  registry. 43 tests, including a doc-sync test.
- PR #6 verdicts: crack length in inches and total time in hours are
  **confirmed**; four synthetic-field conventions are **contradicted**
  (submitter type codes, SDR type, stage-of-operation codes, control-number
  shape); SDR registration marks omit the leading `N`.

## Next

1. Merge #7 then #8; run `scripts/profile_sdr.py` on a downloaded
   `SDR-<year>.csv` and commit the JSON profile; then update the UNRESOLVED
   rows in `docs/MEASUREMENT_STANDARDS.md` from that receipt.
2. Fix the contradicted synthetic fields in PR #6 (or in M1 if PR #6 merges
   first) using `standards.py` code lists.
3. M1 data contract: every value carries `(value, unit, revision, provenance)`
   using `standards.Unit`; spec-limit records cite a source URL and revision.

## Reuse from sibling repos (proposed, pending owner OK)

Read-only review of `Jott2121/sabot`, `Jott2121/crucible`, `Jott2121/quorum-cal` on
2026-10-11. None of them states a consumption contract for Tripwire; the reuse below is
by adoption, with citation.

- **sabot** (`SPEC.md` v0.2.1, `seeds/`, `harness/sabot/{strict,uncertainty,score}.py`):
  adopt the DETECTED / REACTED / RECOVERED funnel and the hard-tier, anchor-substring
  adjudication for M3; frame H1 as a cross-lineage replication of sabot's O6 finding
  (silent write never flagged, 0 of 119); credit sabot for the H2 prompt; copy the
  exclusion codes (`RUN_ERROR > BASELINE_FAIL > INJECTION_UNVERIFIED`) and the
  "strict floor and published number quoted as a pair" rule into
  `docs/PREREGISTRATION.md`; use its replicate-cluster bootstrap for M7 CIs.
- **crucible** (`action.yml`, `experiments/PROTOCOL-B.md`, `experiments/DEVIATIONS.md`):
  add the model-free mutation diagnose to CI (`uses: Jott2121/crucible@v1`, `fail-under`)
  on `standards.py`, `contract.py`, `store.py`, later the adjudicator and the gate;
  copy the protocol skeleton (integrity gates, stopping rules, append-only deviations
  log, `assert_protocol_committed` refusal to run on an uncommitted protocol) into
  `docs/PREREGISTRATION.md`; copy the receipt layout (`meta.json`, `receipt.jsonl`,
  `result.json`) for M5 run records.
- **quorum-cal** (`docs/runtime-discipline.md`, `quorumcal/{judges,profile,monitor}.py`):
  no LLM judge in Tripwire, so the quorum math does not apply; borrow the per-call
  served-model provenance rule, the cache-and-resume runner, the quota-aware halt and
  the exit-code contract for M5; model the gate's data contract on the risk profile
  (expiry, provenance fingerprint, `profile check` exit codes) in M6; run its CUSUM
  monitor idea over the clean-run false-flag rate during M7; publish a different-
  lineage adversarial review of the results as a corrections section (its v1.0.1).

## Owner-directed changes, 2026-10-11 (Grok bot review)

- PR #7: six UNRESOLVED items closed from the maintainer profile of SDR-2026
  (`standards.RESOLVED`); SDR encoding is UTF-8; doc source corrections
  (crack length wording is from the Single Submission instructions; "whole
  hours" is stated for component times only; submitter code `M` is real).
  The owner then committed `reports/sdr_profile_2026.json` to the branch; it
  matches every relayed number, and the observed code values for every coded
  column (stage, how-discovered, corrosion, since-codes, sides, region,
  district office, nature, precautionary) are now in `standards.py` as partial
  lists. No SDR field is UNRESOLVED; `code_tables` stays open for meanings.
- PR #10: NASA-STD-8739.4A Table 12-1 replaced with the full 11-row table as
  read from the official PDF by the maintainer (AWG 26 = 36 N, AWG 28 = 22 N
  pinned by test), cited only to the PDF, HIGH; generated SubmissionDate at
  -05:00.
- PR #6: loader reads UTF-8 and fails closed on bad bytes; synthetic rows
  use observed code values, two-letter stage codes, designator+YYYYMMDD+seq
  control numbers, whole-hour times, N-less marks; DATA.md cites the units
  reference.
- PR #6 merged; main was merged into #7 and the stack merged forward
  (#7 → #8 → #9 → #10 → #11) with merge commits; no force-push, no new PRs.
  Build is paused pending owner go-ahead.

## Open questions

- `TestResult` carries `spec_id`, `parameter`, `spec_revision` but not `applies_to`, so a
  test result alone cannot name a spec row when one spec id covers several `applies_to`
  values (the NASA crimp rows). Synthetic specs are one id per part, so M1 works; decide
  in M2 whether `TestResult` gains `applies_to` (contract change) before the tools ship.

- See the Open items table in `docs/MEASUREMENT_STANDARDS.md` (keys mirrored in
  `standards.UNRESOLVED`). The largest: download CSV column count (76 vs 77),
  `SubmissionDate` zone semantics, full SDRS code tables, and the exact
  NASA-STD-8739.4A Table 12-1 values needed for M1 spec limits.
- Network: the cloud sandbox's egress policy blocks `sdrs.faa.gov`, `faa.gov`,
  `ecfr.gov`, `govinfo.gov` and `nasa.gov`; M0 was researched through search
  excerpts. Allow-listing those hosts (or running the profiler locally) would
  let the next pass promote MEDIUM items to HIGH.
- xAI model IDs for M5: current docs name `grok-4.7`, `grok-4.6`, `grok-4.5`
  as accepting `reasoning_effort`; the exact effort levels per model differ
  between sources and must be read from docs.x.ai when M5 starts.
