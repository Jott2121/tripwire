# Tripwire docs map

The numbers in the [top-level README](../README.md) come from the files below. This page says
what each one is and how much weight it carries, so you can go straight to the evidence. Start
with the row that matches your question.

## Where to start

| If you want | Read |
|---|---|
| The results in two minutes | [README](../README.md), top section and Results |
| Phase two on real FAA records, with its limits | [results/phase2-real-data.md](results/phase2-real-data.md) |
| v1's method, the design decisions and the honest limits | [WRITEUP.md](../WRITEUP.md) |
| One run you can watch, with full transcripts | [results/demo-1.md](results/demo-1.md), then [WALKTHROUGH.md](WALKTHROUGH.md) (a 60-second script) |
| What was decided before any scored run | the preregistrations below |
| Where the build stands, milestone by milestone | [STATUS.md](STATUS.md) |

## Preregistrations and amendments

Each protocol was committed before the runs it governs; later changes are numbered amendments or
logged deviations, never silent edits.

| File | Covers | State |
|---|---|---|
| [PREREGISTRATION.md](PREREGISTRATION.md) | v1: planted faults F1 to F6, hypotheses H1 to H5, decision rules, exclusions, bootstrap | run; H1 refuted, H4 not confirmable at this n, H3 and H5 untested |
| [amendments/A1-gate-store-crosscheck.md](amendments/A1-gate-store-crosscheck.md) | gate extended to catch F1 and F3 by a store cross-check | exploratory |
| [amendments/A2-targeted-gate-confirmation.md](amendments/A2-targeted-gate-confirmation.md) | live check of the extended gate on 60 targeted slots | exploratory |
| [amendments/A3-turn-cap-blocked-report.md](amendments/A3-turn-cap-blocked-report.md) | one scoring rule, decided after seeing results | exploratory |
| [amendments/A4-holdout-confirmation.md](amendments/A4-holdout-confirmation.md) | the 72-job holdout, pass marks P1 to P4 fixed in advance | **confirmatory; passed** |
| [PREREGISTRATION-phase2-real-data.md](PREREGISTRATION-phase2-real-data.md) | phase two: real SDR vs registry disagreements, frozen mining rules, item draw, $65 cap, deviations log | **frozen 2026-11-01; run 2026-11-02** |
| [PREREGISTRATION-v2-multimodel.md](PREREGISTRATION-v2-multimodel.md) | a four-model planted-fault run | shelved before any spend, with the reason |

## Results

| File | What it reports | Status of the result |
|---|---|---|
| [results/phase2-real-data.md](results/phase2-real-data.md) | 800 runs, two models, real records | pre-registered |
| [results/holdout-1.md](results/holdout-1.md) | v1 gate vs prompt vs plain agent, 72 jobs | pre-registered (A4) |
| [results/gate-confirm-1.md](results/gate-confirm-1.md) | v1 gate live on 60 slots | exploratory (A2, A3) |
| [results/gate-determined.md](results/gate-determined.md) and [its note](results/gate-determined-NOTE.md) | why the gate's catch rate follows from its design (2,016 scripted runs, no model) | offline, zero spend |
| [results/demo-1.md](results/demo-1.md) | one task, three conditions, three repeats, full transcripts | demo, not a result |

The machine-readable sources sit in [`reports/`](../reports/): `phase2_results.json` (phase two
numbers), `holdout_1.json` and `holdout_1_verdict.json`, `gate_240_mixed*` (the pre-registered H2
run, with a [note on row accounting](../reports/gate_240_mixed.NOTE.md)) and
`gate_confirm_1_same_slots*`. Every live request in v1 is booked to
[`runs/spend_ledger.jsonl`](../runs/spend_ledger.jsonl).

## Data and measurement

| File | Covers |
|---|---|
| [DATA.md](DATA.md) | the FAA files used, how to fetch them, and why no real row is committed |
| [MEASUREMENT_STANDARDS.md](MEASUREMENT_STANDARDS.md) | units, formats and code lists of the SDR file, each with its public source and a confidence |

## Regenerate the figures

`make demo` rebuilds every committed figure from committed data, with no API call:
[`artifacts/phase2_noticed_vs_acted.svg`](../artifacts/phase2_noticed_vs_acted.svg) from
`reports/phase2_results.json`, and [`artifacts/results_cost.svg`](../artifacts/results_cost.svg)
from the spend ledger. Golden tests fail CI if a committed figure drifts from its source.
