# Exploratory amendment A2: targeted live confirmation of the #45 gate (issue #46)

**Status:** post-hoc, exploratory. Prepared after gate run `38285916667` and the #45 gate change
(amendment [A1](A1-gate-store-crosscheck.md)). This is NOT part of the preregistered analysis in
`docs/PREREGISTRATION.md`, which is unchanged. Results from this run are labelled exploratory.

**2026-10-12, issue #46, targeted live confirmation run `gate-confirm-1` (OWNER-APPROVED budget
$4.00; prepared, NOT dispatched).** The #45 change (A1) added a store cross-check so the gate
catches F1 (unit swap) and F3 (wrong value) and downgrades an agent's unsupported value refusal on
a clean run. A1 validated this OFFLINE only, by replaying the improved gate over the recorded
receipts of runs `38253007798` and `38285916667` (`tripwire/gate_replay.py`, zero live spend). This
amendment adds a small, bounded LIVE confirmation that the improved gate behaves as replayed when
the model is actually in the loop, without paying for a second full gate matrix.

**What runs.** A committed, deterministic job list, `runs/targets/gate-confirm-1.json`, for
`condition_set=gate` (60 jobs). It is built by rule in `tripwire/targets.py`, never from
cherry-picked task ids:

* **54 skipped jobs** — the `Condition.GATE` jobs that run `38285916667` did not reach. That run
  hit `BUDGET_HALT` after 186 of the 240 planned gate jobs. The 54 it skipped are exactly the gate
  plan (`tripwire.runner.plan_matrix`) minus the gate job ids already recorded in the committed
  `runs/spend_ledger.jsonl`. Which 186 ran is a function of live cost, so the already-run set is
  read once from the committed ledger at authoring time and FROZEN into the file; verification
  (`--check`, the tests) never re-reads the mutable ledger.
* **6 top-up jobs** — a deterministic stratified sample of gate jobs whose planted fault is F1, F3,
  or clean (the strata the #45 change touches), drawn with `tripwire.runner.pilot_subset` so no
  fault is favoured, excluding anything already in the 54. This guarantees the confirmation
  exercises the changed F1/F3-catch and clean-downgrade paths specifically, even where the skipped
  tail is light on them.

Selection keys on the fault *definitions* and strata, not on task ids, and is reproducible from the
two committed inputs (the plan and the ledger). `python -m tripwire.targets --check
runs/targets/gate-confirm-1.json` verifies the file against the plan offline.

**Cost cap, fail-closed.** The target set's planning estimate is $3.12 (60 jobs × the
$0.052 per-run OWNER-APPROVED estimate). The driver (`tripwire/live_eval.py --targets`) refuses a
budget over the file's $4.00 cap, refuses if the planning estimate exceeds the budget, and refuses
on a stale list (any job id no longer in the plan). Every existing fail-closed ledger check still
applies: the committed-ledger row count must match `ledger_rows`, the prior paid run's ledger must
be contained in the committed one, and the cumulative $50 ceiling still bounds total spend.

**Workflow change (manual-only).** Running this needed one minimal, manual-only change to
`.github/workflows/live-eval.yml` (and its source `docs/live-eval.workflow.yml`): a new
`workflow_dispatch` `targets` choice input wired to `--targets runs/targets/<name>.json`, writing
its own `out/<name>-<id>` directory. No new trigger; still `workflow_dispatch` only. OWNER-approved
for this purpose (issue #46).

**Not dispatched.** This PR prepares the run only; it spends nothing and does not edit
`runs/spend_ledger.jsonl`. Ghost dispatches it after reviewing the PR, with the exact
`gh workflow run` command (including the current `ledger_rows`) given in the PR body.
