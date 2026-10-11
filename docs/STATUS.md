# Tripwire build status

Updated 2026-10-11. One row per milestone; the assistant directing the build
reads this file first. Numbers never appear here unless a committed command
produced them.

## Milestones

| # | Milestone | State | PR |
|---|-----------|-------|----|
| — | SDR loader, synthetic rows, corruption injector + hash-chained ledger (issue #4) | open PR, awaiting review | #6 (`claude/issue-4`) |
| M0 | Measurement standards: `docs/MEASUREMENT_STANDARDS.md` + `tripwire/standards.py` + tests | **open PR**, CI green | #7 (`claude/m0-measurement-standards`) |
| M0b | `scripts/profile_sdr.py`: offline profiler that closes the M0 open items from a locally downloaded file | **open PR**, stacked on M0 | #8 (`claude/m0b-profile-sdr`) |
| M1 | Data contract + store: typed schemas carrying units/revision/provenance; fixed-seed synthetic lots, tests, work orders; spec limits from public standards | part 1 types **open PR** #9; part 2a generators + public spec rows **open PR** (`claude/m1-synth`); part 2b store next | #9, `claude/m1-synth` |
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

## Open questions

- See the Open items table in `docs/MEASUREMENT_STANDARDS.md` (keys mirrored in
  `standards.UNRESOLVED`). Column count (76) and the `SubmissionDate` zone
  (fixed -05:00) are resolved by the profile. The largest remaining: full SDRS
  code tables, and the exact NASA-STD-8739.4A Table 12-1 values needed for M1
  spec limits.
- Network: the cloud sandbox's egress policy blocks `sdrs.faa.gov`, `faa.gov`,
  `ecfr.gov`, `govinfo.gov` and `nasa.gov`; M0 was researched through search
  excerpts. Allow-listing those hosts (or running the profiler locally) would
  let the next pass promote MEDIUM items to HIGH.
- xAI model IDs for M5: current docs name `grok-4.7`, `grok-4.6`, `grok-4.5`
  as accepting `reasoning_effort`; the exact effort levels per model differ
  between sources and must be read from docs.x.ai when M5 starts.
