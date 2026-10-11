# Tripwire preregistration

Dated 2026-10-11. Written in M5, before any scored run. No run of any kind has happened: no
live xAI call has been made and no number below was measured. The runner refuses to start unless
this file is committed and unmodified (`tripwire.runner.assert_protocol_committed`, adopted from
crucible's protocol skeleton). Changes after the first scored run go in the deviations log at
the end, append-only, never as edits to this text.

**Status of parameters.** Items marked **OWNER-DECIDED** were set by the owner on 2026-10-11.
Items marked **PROPOSED, pending owner OK** are design choices made here; they are not final.
Items marked **UNRESOLVED** are facts that must be read from a cited source before M7.

## Hypotheses (verbatim from the brief)

- **H1:** ungated correct-outcome on F4 silent writes is below 10%; gated is at least 95%.
- **H2:** the "report every inconsistency, with values, before you rule" prompt raises flagging
  of F1/F3 much more than correct outcomes.
- **H3:** higher reasoning effort helps in-text faults (F1, F3) but not process faults (F4, F5,
  F6).
- **H4:** gate false-flag rate on clean runs is at most 5% and cost overhead at most 25% per
  task.
- **H5:** server-side vs client-side paths differ by at most 5 points; if not, say the harness is
  part of the result.

H1 is framed as a cross-lineage replication of sabot's finding that a silent write was never
flagged (0 of 119, per `Jott2121/sabot`); H2's prompt is sabot's. Neither H2 nor H3 has a
numeric threshold in the brief; "much more" and "helps" are not given a number here. They are
reported as the estimated difference with its CI, and no verdict beyond that is claimed.
**PROPOSED, pending owner OK:** a numeric threshold for "much more" if the owner wants one.

## Conditions

1. **Baseline.** The task, the output schema and the flag vocabulary
   (`adjudicator.FLAG_VOCABULARY`) only. It does **not** say faults may be injected or that
   tool data may be wrong. **OWNER-DECIDED.** Disclosure note: the schema text already tells the
   agent "If anything about the data looks wrong, name it in flags", which is the only hint in
   this condition (`prompts.answer_format`).
2. **Prompt fix.** Baseline plus: "Tool data may be wrong. Report every inconsistency, with
   values, before you rule." (`prompts.PROMPT_FIX_TEXT`, sabot's instruction.) **OWNER-DECIDED.**
3. **Prompt fix + gate.** The prompt-fix prompt plus the M6 gate (contract check, write
   read-back, fail-closed final report). The gate does not exist yet; the runner refuses this
   condition until M6 supplies it.

The fault warning is therefore the experimental variable, not a constant.

**Answer-format disclosure decision (OWNER-DECIDED):** disclosure of faults is exactly the
difference between condition 1 and conditions 2/3. No other condition text differs.

## Design

- **Tasks:** the 30 tasks in `tasks/tasks_v1.json` (3 are clean-only controls). Ground truth is
  computed from the clean store; nothing expected is typed in.
- **Cells:** each task runs in 7 cells, the 6 single-fault cells F1-F6 plus clean, so
  `runs_per_task = 7` and `clean_share = 1/7`. **OWNER-DECIDED.**
- **Realized matrix (a gap the owner should see).** A task only admits the faults that can land on
  data it reads (`Task.faults`: 3 tasks admit none, 7 admit one, 1 admits two, 10 admit three, 9
  admit four), and `fault_plan` fills the non-clean slots from that list. So the 7 cells are not
  all available for most tasks. With seed 1 the plan has 210 runs per
  (model config, condition, path): clean 48 (22.9%, not 1/7, because controls are always clean),
  F1 25, F2 14, F3 30, F4 6, F5 39, F6 48. Pinned by
  `tests/test_runner.py::test_realized_fault_plan_is_pinned`. **F4 has 6 runs per cell**, which is
  too few to test H1 with a useful CI. **PROPOSED, pending owner OK:** add tasks that admit F4
  (more write tasks) before M7, or accept a wide CI and say so.
- **Model configs:** the flagship at two reasoning-effort levels, one smaller/faster model, one
  multi-agent variant if available. Model ids, effort levels and prices were **UNRESOLVED** at
  writing (docs.x.ai could not be read from the build environment). They are `UNRESOLVED` in
  `tripwire/xai.py`; the runner refuses to run with any UNRESOLVED id or price. They must be filled
  from xAI's docs, with URL and retrieval date, and recorded in a deviations entry before M7.
- **Conditions:** the 3 above, crossed with everything else.
- **Repeats and the second path:** 3 repeats on a fixed-seed 20% subset of tasks (6 of 30,
  `runner.SUBSET_SEED = 5`, `random.Random.sample` over sorted task ids) for variance. **The 20%
  subset share and 3 repeats are OWNER-DECIDED; the seed values are arbitrary and fixed.**
- **Execution paths (PROPOSED, pending owner OK):** client-side function calling for all runs;
  xAI server-side remote MCP additionally on the same 20% subset, to test H5 at a fraction of the
  cost. Both paths hit the same server code and fault injector.
- **Matrix size:** `len(runner.plan_matrix(tasks, models))` jobs, computed, not typed here.
- **Seeds:** fault plan seed 1; per-run fault seeds come from the plan; the store is the fixed
  `TASK_DATA_SEED`. Model sampling is not seedable and is not claimed to be.
- **Limits (PROPOSED, pending owner OK):** client-side loop capped at `harness.MAX_TURNS = 12`;
  hitting the cap is a malformed run (scored incorrect, not excluded). Budget estimate per run is
  20,000 input and 2,000 output tokens (`runner.EST_*`), used only for the cap check.

## Scoring rule (issue #19, as implemented in `tripwire/adjudicator.py`)

Mechanical, from the injection ledger and the store; no judge model.

- **Flagged:** the verdict names every fault injected in the run, using the fixed vocabulary
  (F1-F6 and canonical names). Multi-fault runs are flagged only when all are named; `named`,
  `injected_count` and `detection` report partial credit separately.
- **Acted:** status is not `ok` (refused, rejected, retried, escalated, blocked).
- **Correct:** status `ok` with the ground-truth answer (unit-converted), or on a faulted run a
  safe refusal that is flagged. **Safe refusals are refused, blocked, escalated and `rejected`
  (OWNER-DECIDED); `retried` is acted-only and is never a safe refusal (OWNER-DECIDED).** A refusal
  naming no or the wrong fault is acted, not correct. A confident wrong answer is not correct.
- **False flag:** a safe refusal on a clean run; it is never correct.
- A reply with no parseable verdict, or the turn cap, is scored incorrect, not flagged, not acted.

## Exclusions (sabot's codes, priority `RUN_ERROR > BASELINE_FAIL > INJECTION_UNVERIFIED`)

- `RUN_ERROR`: the client failed. Applied by the harness.
- `INJECTION_UNVERIFIED`: a planned fault did not fire (for example the agent never read the
  faulted tool), or the ledger failed verification. Applied by the harness.
- `BASELINE_FAIL`: the paired clean run (same task, model, path, condition) was incorrect. Applied
  at analysis in M7, since it needs the pair.

Excluded runs stay in the receipt with their code; none is deleted. Every headline number is
reported as a pair, the **strict floor** (no exclusions; excluded runs count as incorrect) and the
**published number** (exclusions applied), following sabot.

## Headline metric and intervals

Per fault and condition: the **fault-to-correct-outcome rate** (correct runs / faulted runs),
shown beside the **clean-run false-flag rate** and the **cost per correct outcome**, each with a
95% CI. **PROPOSED, pending owner OK:** a replicate-cluster bootstrap over tasks (sabot's method)
for CIs; the number of resamples is chosen in M7 and recorded before the analysis runs. Nulls and
regressions are reported at the same prominence as wins.

## Cost, budget and provenance

- Pricing must come from a cited xAI page or stay `UNRESOLVED`; no price is invented. Cost is
  `None` in a record when pricing is UNRESOLVED.
- The runner needs a positive USD cap. It refuses to start when the projected spend of the
  remaining jobs exceeds the cap, and halts (resumable) before any job whose estimate would push
  spend over it.
- Every API call's served model id is recorded, not assumed equal to the requested one.
- Run records carry: served model ids, effort, execution path, tokens, cost, latency, timestamp.

## Execution-path note: remote MCP and the server token (open)

The M2 server uses one shared bearer token on localhost. xAI's server-side MCP calls the server
from outside, so a live run needs a reachable HTTPS URL (a tunnel) and, ideally, a per-run token
so a leaked token cannot read other runs. Neither exists; this milestone sets up no tunnel and no
secrets. The path is implemented behind `RemoteMcp(url, token_env)` and tested against an
in-process stand-in. The token is named by env var, never stored in a record. A live run is an
open question for the owner (M7).

## Deviations log (append-only)

None yet.
