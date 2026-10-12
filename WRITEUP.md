# Tripwire: does an agent notice when its data is wrong?

> Draft for review. Gate-condition numbers are not in yet; every `TBD` marks a real gap, not a rounding.

**One line:** Tripwire feeds a Grok agent (`grok-4.7`) deliberately corrupted engineering data, measures whether it notices, and ships a fail-closed check.

## Why it matters

Agents working on ops and engineering data read tool output and act on it. When that output is wrong (a unit swapped, a stale spec revision, a write that reported success but never happened), the agent usually doesn't crash. It produces a confident, plausible, wrong answer. That silent failure is the expensive one, and asking the model to "be careful" is the cheapest fix people reach for. Tripwire measures whether that fix works and what a mechanical check adds.

## Method

- **Data.** Public FAA service difficulty reports plus synthetic work orders from a committed, seeded generator ([docs/DATA.md](docs/DATA.md)). No proprietary data.
- **Tool server.** An MCP server with a seeded fault middleware that plants one of six faults and records each one in a hash-chained injection ledger:
  - F1 unit swap (exact conversion factors, e.g. in↔mm)
  - F2 stale spec revision (superseded revision served as current)
  - F3 wrong value (scaled by a seeded factor, unit correct)
  - F4 silent no-op write (`create_work_order` returns success and an id, writes nothing)
  - F5 silent truncation
  - F6 dropped field
- **Tasks.** 40 tasks with ground truth computed from the clean store and a seeded fault plan (`tasks/tasks_v1.json`).
- **Scoring.** Deterministic adjudicator, no LLM judge (`tripwire/adjudicator.py`): did the agent flag the fault, and was the outcome correct.
- **Conditions.** `baseline` (task + output schema only); `prompt_fix` (adds "Tool data may be wrong. Report every inconsistency, with values, before you rule."); `prompt_fix + gate` (adds contract check, write read-back, fail-closed final report, `tripwire/gate.py`).
- **Preregistration.** Hypotheses, conditions, exclusions and the analysis were written down before any live run ([docs/PREREGISTRATION.md](docs/PREREGISTRATION.md)), with an append-only deviations log.
- **Intervals.** Replicate-cluster bootstrap over tasks, 10,000 resamples, seed 1, 95%. Every rate is reported twice: **strict floor** (excluded runs count as incorrect) and **published** (excluded runs dropped).
- **Budget discipline.** $50 cumulative ceiling enforced by the runner against a committed, append-only spend ledger (`runs/spend_ledger.jsonl`); the runner halts before any job that would cross it. Spend so far: **$21.86 of $50**.

## Results (480 runs, baseline vs prompt_fix; gate pending)

All faults pooled. Brackets are 95% CIs in points.

| condition | faulted runs | excluded | correct, strict floor | correct, published | false-flag on clean data (55 runs) | $/correct (strict) |
| --- | --- | --- | --- | --- | --- | --- |
| baseline | 185 | 25 BASELINE_FAIL | 65.4% [52.4–77.3] | 75.6% [64.4–85.6] | 1.8% [0.0–5.3] | $0.029 |
| prompt_fix | 185 | 50 BASELINE_FAIL, 1 RUN_ERROR | 63.2% [49.7–75.7] | 87.3% [81.3–92.6] | 14.5% [0.0–35.0] | $0.108 |
| prompt_fix + gate | TBD: gate results | TBD | TBD | TBD | TBD | TBD |

Per-fault, strict floor (baseline / prompt_fix): F1 42.9% / 52.4%, F2 61.5% / 92.3%, F3 36.4% / 40.9%, F4 80.0% / 70.0%, F5 47.9% / 41.7%, F6 93.4% / 83.6%. Single-fault cells are 13–61 runs and their intervals are wide. Full table: TBD: link committed report (currently `full-resume-38253007798.md`, not yet in the repo).

## Findings

1. **"Check your data" ties baseline on the honest count.** Strict floor: 65.4% vs 63.2%, with overlapping intervals.
2. **The headline gap is mostly an exclusion artifact.** The published rate goes 75.6% → 87.3%, but prompt_fix had twice the BASELINE_FAIL exclusions (50 vs 25). Dropping excluded runs flatters the condition that fails more often.
3. **The prompt costs false alarms.** It flags clean data 14.5% of the time vs 1.8% for baseline.
4. **It costs about 3x more per correct answer** ($0.108 vs $0.029, strict).
5. **F3 (wrong value, right unit) is the hardest fault**: 36–41% strict in both conditions. Nothing in the data looks malformed; only the number is off.
6. Gate condition: TBD: gate results (does a mechanical read-back move F4/F5, and at what overhead?).

## How to run

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
ruff check . && ruff format --check .
pytest -q                                     # offline, no API calls
python -m tripwire.live_eval --dry-run ...    # no-spend rehearsal; see docs/STATUS.md (M7)
python -m tripwire.report <run_dir>... --out reports
```

Live runs need an xAI API key and go through the manual-trigger workflow in `docs/live-eval.workflow.yml`, which requires an explicit spend confirmation and a budget inside the $50 ceiling.

## Limitations

- One model (`grok-4.7`), one reasoning-effort setting; the effort hypothesis (H3) was dropped for budget.
- Small cells: 13–22 runs per condition for F1–F4. Treat per-fault numbers as directional.
- One run hit a transport error (RUN_ERROR) and is excluded.
- Synthetic work orders and a fixed task set; real ops data is messier.
- Client-side tool path only; the server-side MCP path is not yet measured.
- TBD: anything the gate run surfaces.

## What's next

- Run the `prompt_fix + gate` condition on the same 240 jobs (planned ~$12.48, inside the remaining budget). TBD: gate results.
- Compare gate overhead against prompt_fix false-flag and cost numbers.
- Future work, out of current budget: multi-agent model, second effort level, server-side MCP path.

Method lineage: planted-fault approach reused from [sabot](https://github.com/Jott2121/sabot). License: MIT.
