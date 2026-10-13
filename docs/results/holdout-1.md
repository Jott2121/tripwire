# holdout-1: pre-registered holdout confirmation of the gate — results

**Status: confirmation per amendment [A4](../amendments/A4-holdout-confirmation.md)**, scored with
the criteria fixed in that amendment before the run. `docs/PREREGISTRATION.md` is unchanged. Every
number comes from `tripwire.report` over the downloaded run receipts; reproduce (zero spend) with
`python scripts/holdout_report.py <downloaded run dir> --out reports`. Reports:
[`reports/holdout_1.md`](../../reports/holdout_1.md),
[`reports/holdout_1_verdict.json`](../../reports/holdout_1_verdict.json).

## Run

| run | target set | jobs | exclusions | spend | cap | result |
| --- | --- | --- | --- | --- | --- | --- |
| `38434106473` | holdout-1 (72, `runs/targets/holdout-1.json`) | 72 / 72 | 0 | $4.1625 | $5.80 | success |

Run code commit `bbdb0ef`. Cumulative spend ledger: $39.32 → **$43.48 of the $50 ceiling**
(1000 → 1072 spend rows; the previously committed ledger is a byte-exact prefix of the run's
artifact ledger and `check_prior_ledger` passes). Model grok-4.7, medium effort, client-side
function calling; 3 conditions × 24 slots (6 faults × 3 + 6 clean). CIs: task-cluster bootstrap,
10,000 resamples, seed 1, 95%.

## A4 criteria

| criterion | observed | threshold | result |
| --- | --- | --- | --- |
| P1 gate strict − prompt_fix strict, all faults | 100.0% − 66.7% = **+33.3 pts** | ≥ +10 pts | pass |
| P2 gate published, all faults | **100.0%** (18/18) | ≥ 90% | pass |
| P3 gate clean false-flag (strict) | **0/6** (prompt_fix 1/6) | ≤ 1/6 and ≤ prompt_fix | pass |
| P4 gate $/correct ÷ prompt_fix $/correct | $0.0650 / $0.0998 = **0.65×** | ≤ 1.25× | pass |

**Verdict: PASS** (complete: 72/72 records, no exclusions).

Context: baseline all-faults 61.1% [36.8–88.2]; prompt_fix 66.7% [45.0–88.2]; gate 100% [100–100].
Gate cost over prompt_fix on 24 paired runs +4.1% [−9.6, +20.7].

## Caveats (read before citing)

* **Small n.** 3 runs per fault cell and 6 clean runs per condition; per-fault CIs span 0–100% for
  the non-gate conditions. The P1 margin is large, but this is one 72-job run.
* **Fresh draws, not fresh tasks.** Per A4, fault seeds/slots are new, but the tasks are the
  committed v1 tasks seen during development.
* **Single model/config** (grok-4.7, medium effort); nothing here generalises beyond it.
* P4 passes partly because prompt_fix's F5 cell was expensive ($0.4557/correct, 1 of 3 correct).

## Repo changes accompanying this result

* `runs/spend_ledger.jsonl` replaced with the run's artifact ledger (append-only superset).
* `artifacts/results_*` regenerated from the new ledger (`make demo`).
* Tests that asserted the holdout was still unspent now branch on whether it is spent and then
  assert the refusal paths (re-planning/re-authoring holdout-1 against the real ledger refuses).
* `tripwire/targets.ran_gate_job_ids` ignores `H1-` holdout ids, so gate-confirm-1 bookkeeping is
  not confused by holdout gate jobs. No workflow, threshold, or scoring change.
