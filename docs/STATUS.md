# Tripwire build status

Updated 2026-10-11 (evening). One row per milestone; the assistant directing the build
reads this file first. Numbers never appear here unless a committed command
produced them.

## Milestones

| # | Milestone | State | PR |
|---|-----------|-------|----|
| — | SDR loader, synthetic rows, corruption injector + hash-chained ledger (issue #4) | **merged** 2026-10-11 (UTF-8 loader, observed conventions) | #6 |
| M0 | Measurement standards: `docs/MEASUREMENT_STANDARDS.md` + `tripwire/standards.py` + tests | **done**, merged; seven open items resolved from the committed profile receipt `reports/sdr_profile_2026.json` | #7 |
| M0b | `scripts/profile_sdr.py`: offline profiler that closes the M0 open items from a locally downloaded file | **merged** | #8 |
| M1 | Data contract + store: typed schemas carrying units/revision/provenance; fixed-seed synthetic lots, tests, work orders; spec limits from public standards | **done**, merged (types, generators + public spec rows, in-memory store) | #9, #10, #11 |
| M2 | MCP server (Streamable HTTP, auth header) with `query_defects`, `get_spec`, `get_test_result`, `create_work_order`, `get_work_order`; in-memory store; contract tests | **open PR** (`claude/m2-mcp-server`); `TestResult.applies_to` added | issue #12 |
| M3 | Fault middleware F1-F6 + append-only hash-chained ledger with separately stored tail hash + deterministic adjudicator | **open for review** (issue #16). Branch `claude/m3-faults`: #13 revision sort fix, `tripwire/ledger.py` (hash chain + sidecar tail hash), `tripwire/faults.py` (F1-F6, seeded) wired into `server.build_mcp(faults=...)`. Stacked branch `claude/m3-adjudicator`: `tripwire/adjudicator.py` (pure; flagged / acted / correct). Scoring (issue #19): a safe refusal (refused, blocked, escalated) that names every injected fault is correct on a faulted run (the true value is unknowable from corrupted data); a refusal naming no or the wrong fault is acted, not correct; any safe refusal on a clean run is a false flag and not correct; flags come from `adjudicator.FLAG_VOCABULARY` (F1-F6 and canonical names); multi-fault runs are flagged only when all are named, with `named` / `injected_count` / `detection` reported separately. Task set is M4; the adjudicator's `Task` kinds are a minimal stand-in | #13, #16 |
| M4 | Task set (~30) with ground truth computed from the clean store | **merged** (issue #21). Branch `claude/m4-tasks`: `tripwire/tasks.py` (`Task`, `TaskKind`, `ground_truth(task, store)`, seeded `fault_plan`), `tripwire/prompts.py` (answer format from `adjudicator.FLAG_VOCABULARY`), committed `tasks/tasks_v1.json` (regenerate: `python -m tripwire.tasks --out tasks/tasks_v1.json`; drift test in `tests/test_tasks.py`). The adjudicator now imports `Task` from `tasks`. No run has happened | #21 |
| M5 | Harness: xAI client (mocked in tests), server-side remote MCP and client-side function-calling paths, run records, cost tracking; `docs/PREREGISTRATION.md` committed before any scored run | **merged** (issue #23, PR #24; budget follow-up #25 / PR #27 also merged). `tripwire/xai.py` (client protocol, scripted mock, live client that refuses; model ids, effort levels and prices all UNRESOLVED), `tripwire/harness.py` (`run_one`, both paths, run records, sabot exclusion codes), `tripwire/runner.py` (matrix, budget cap, `assert_protocol_committed`, `meta.json`/`receipt.jsonl`/`result.json`, resume, quota halt, exit codes), `prompts.Condition`, `docs/PREREGISTRATION.md` (decisions OWNER-APPROVED 2026-10-11; exit codes PROPOSED). No live call has been made. The gate condition was refused until M6 (now built, see M6) | #23 |
| M6 | The gate: contract check + write read-back + fail-closed final report | **in review** (issue #26, PR #28, branch `claude/issue-26-20261011-2308`, merged with post-#27 `main`). Post-#27 state: the hard cumulative $50 ceiling, spend ledger and lean matrix are intact; the gate is its own session (`plan_matrix(..., conditions=(Condition.GATE,))`: 240 client-side runs, $12.48; default stays baseline + prompt-fix) and its jobs go through the same ceiling check and cost accounting (the gate adds no model call, so 0 extra tokens). `tripwire/gate.py` (contract check per tool result, `create_work_order` read-back via `get_work_order`, blocked final report as a `blocked` verdict naming `FLAG_VOCABULARY` terms, any gate error = block), wired into `harness.run_one` for the gate condition on the **client-side path only**; `RunRecord.gate` carries checks run, failures, block, extra tool calls, added tokens (0: no model call), latency. Catches F2, F4, F5 (row count / read-back prefix), F6; does **not** catch F1, F3 or a text cut inside a read result (per-fault detail in the `gate.py` docstring, shown by `tests/test_gate.py`). The server-side path cannot be gated (xAI runs the tools): no gate jobs are planned there and `run_one` refuses one; logged in the preregistration deviations. Faults fired on the gate's own read-back go to a side ledger (`RunRecord.gate["read_back_faults"]`), not the run ledger; a spec with no bounds is a `spec_unbounded` block. No live call made. Open: the gate has only seen scripted agents, so H1/H4 are unmeasured | #26 |
| M7 | Live eval workflow (manual trigger, budget cap) and results report with CIs | **part 1 in review** (issue #29, branch `claude/issue-29-20261011-2333`): `xai.LiveClient` (Responses API, recorded-HTTP tests only; refuses while `xai.WIRE_CONFIRMED` is False because the wire names are UNRESOLVED), `tripwire/live_eval.py` (confirm string, ledger row-count check, budget within the $50 ceiling, `--dry-run`), committed `runs/spend_ledger.jsonl`, proposed workflow `docs/live-eval.workflow.yml` (the owner copies it to `.github/workflows/`; the bot cannot write there). **Not done:** the report (`python -m tripwire.report`, item 3). No live call made | #29 |
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
  #6–#11 are all merged to `main`.
- Owner decision (M2): `TestResult` gains a validated, non-empty `applies_to`, so a test
  result maps to exactly one spec row `(spec_id, parameter, applies_to, spec_revision)`.
  `TestResult.judge` refuses a spec with a different `applies_to`; `Store` raises
  `StoreError` for a test that does not resolve to one spec row.

## Open questions

- M4 design calls for the owner (flagged, not decided):
  - `fault_plan` takes `runs_per_task` and `clean_share` as required arguments. Fixed by the
    owner for M5: 7 and 1/7 (`runner.RUNS_PER_TASK`, `CLEAN_SHARE`). Realized plan, seed 1: 210
    runs (after T31-T40), clean 58 (20.7%), F1 25, F2 14, F3 30, F4 26, F5 59, F6 68, because
    tasks admit only some faults and controls are always clean; recorded as a documented
    deviation from 1/7. F4 was 6 per cell; the owner approved adding F4 write tasks (done).
    See `docs/PREREGISTRATION.md`.
  - Fault targeting is the middleware's, not task-aware: F1/F3 hit the first quantity and F6 a
    random droppable key, so a planted fault can land on a field the answer does not use. Task
    fault lists exclude cases where that is deterministic (max-crack F1); the rest is left.
  - `answer_format()` tells the agent the flag words and that it may name problems, without
    saying faults are injected. Whether to disclose more is a preregistration decision.
  - Pass/fail tasks use value 1/0, unit `count` (the verdict has no boolean field).
  - Write tasks have `work_order_id: null` in ground truth: the id is issued at write time and
    the adjudicator checks the store, not an id.

- M2 auth is a single shared bearer token from `TRIPWIRE_SERVER_TOKEN`, localhost by
  default. M5 answer: not enough for a live server-side remote MCP run, which needs a
  reachable HTTPS URL (tunnel) and ideally per-run tokens. The path is implemented behind
  `xai.RemoteMcp(url, token_env)` and tested with an in-process stand-in; no tunnel or secret
  was set up. Still open for the owner before M7.
- M5 could not read docs.x.ai (fetch not permitted); the owner supplied the `grok-4.7` facts in
  issue #25 (retrieved 2026-10-11) and they are now in `tripwire/xai.py`. Superseded below.
- M5 decisions OWNER-APPROVED (2026-10-11), marked so in the preregistration: F4 sample size
  (write tasks T31-T40), no numeric "much more" threshold in H2/H3, server-side path only on the
  fixed-seed 20% subset, `MAX_TURNS = 12` (cap hit scores incorrect, recorded as
  `RunRecord.turn_cap_hit`), 20K/2K planning token estimates, replicate-cluster bootstrap CIs.
  Still PROPOSED: the exit-code numbers. Model, prices and budget were then supplied in issue
  #25 (see below), which also changes the matrix size from the M5 figures quoted here (210/280
  runs per condition, 20% subset, 3 repeats).
- M5 follow-up fixes: (A) resume after a quota halt no longer crashes; a stale ledger and its
  `.head` are moved to `ledgers/superseded/<job_id>.<n>.jsonl[.head]`, never deleted. (B)
  `INJECTION_UNVERIFIED` is `set(injected) != {planned}`, so re-reading the faulted tool no
  longer excludes a valid run. (C) `meta.json` keeps the first session's `started` and
  `git_commit` and appends a `sessions` entry per run. Each has a regression test.
  `tasks/tasks_v1.json` must be regenerated (see below).
- M5 review fixes: `AGENTS.md` and `.cursor/rules/30-benchmarks-and-claims.mdc` now point to
  `docs/PREREGISTRATION.md` (the `PREREG.md` strings in `tests/test_runner.py` are a temp-repo
  fixture file, not a doc reference). `tests/test_harness.py` gained ScriptedClient-only tests
  (malformed tool calls, error after tool calls, server-side error, per-path request shape,
  server-side and bad-ledger INJECTION_UNVERIFIED, RUN_ERROR precedence); `harness.py` coverage
  went from 83% to 92%. The lines still reported missing (199-203, 211-214, 222, 238-239) include
  ones the tests do execute, so the figure understates it; no cause found, not worked around.
- **Owner decisions, issue #25 (2026-10-11), merged as #27:**
  - Budget: $50 cumulative across all scored runs; a lean first full run of about $25.
    `runner.py` enforces a persisted spend ledger (`init_spend_ledger`, `read_spend`;
    `HARD_CEILING_USD = 50.0`, lower only), refuses to start or halts before a job that would
    cross it, and fails closed on a missing or corrupt ledger. `usage.cost_in_usd_ticks`
    (USD = ticks / 1e10) is recorded per request and preferred over the estimate.
  - Model: `grok-4.7` only (`tripwire/xai.py`: id, `GROK_4_7_PRICING` $2.00 / $0.50 / $6.00 per
    1M under 200k, $4 / $1 / $12 at 200k+; sources docs.x.ai, retrieved 2026-10-11). Facts
    (effort values, Responses API, remote-MCP request shape) are in `docs/PREREGISTRATION.md`.
    Out of scope for this budget, future work: `grok-4.20-multi-agent-0309`, the small/fast row,
    the second effort row.
  - Lean matrix (OWNER-APPROVED 2026-10-11): 6 runs per task, 240 per condition, baseline and prompt-fix, client-side,
    no repeats, no server-side subset: 480 runs, $24.96 at the planning estimate. Effort `medium`
    (OWNER-APPROVED, no pilot). The gate comes out of the remaining ~$25 after M6. Server-side
    remote MCP deferred (about $5 for an 8-task subset plus a tunnel). Underpowered: H1 and H4
    cannot be confirmed by interval, H3 and H5 are not tested. See the preregistration.
  - Still UNRESOLVED: live Responses API field names beyond the issue, the cached-token usage
    field, whether the ticks figure is per response or cumulative, the live client itself (M7).
- Does `query_defects.since` filter on `difficulty_date` (as `Store` does) or on
  `submission_date`? Documented as the former; confirm.

- See the Open items table in `docs/MEASUREMENT_STANDARDS.md` (keys mirrored in
  `standards.UNRESOLVED`). Column count (76) and the `SubmissionDate` zone
  (fixed -05:00) are resolved by the profile. The largest remaining: full SDRS
  code tables, and the exact NASA-STD-8739.4A Table 12-1 values needed for M1
  spec limits.
- Network: the cloud sandbox's egress policy blocks `sdrs.faa.gov`, `faa.gov`,
  `ecfr.gov`, `govinfo.gov` and `nasa.gov`; M0 was researched through search
  excerpts. Allow-listing those hosts (or running the profiler locally) would
  let the next pass promote MEDIUM items to HIGH.
- xAI model IDs: resolved by issue #25 to `grok-4.7` only (efforts low | medium | high |
  xhigh); no other model is in scope for this budget.
