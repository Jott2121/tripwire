# Tripwire preregistration

Dated 2026-10-11. Written in M5, before any scored run. No run of any kind has happened: no
live xAI call has been made and no number below was measured. The runner refuses to start unless
this file is committed and unmodified (`tripwire.runner.assert_protocol_committed`, adopted from
crucible's protocol skeleton). Changes after the first scored run go in the deviations log at
the end, append-only, never as edits to this text.

**Status of parameters.** Items marked **OWNER-DECIDED** were set by the owner on 2026-10-11.
Items marked **OWNER-APPROVED (2026-10-11)** were proposed here and approved by the owner.
Items marked **PROPOSED, pending owner OK** are design choices made here; they are not final
(only the exit-code numbers remain in this state).
Items marked **UNRESOLVED** are facts that must be read from a cited source before M7.
Items marked **OWNER-DECIDED (issue #25, 2026-10-11)** are the budget and model decisions in
that issue: $50 cumulative, a lean first run of about $25, `grok-4.7` only. The matrix below was
redesigned to fit them; the redesign is a change from the M5 design and is logged under
"Deviations log". The reasoning effort (`medium`), 6 runs per task and the remote MCP deferral
were **OWNER-APPROVED (2026-10-11)**; only the exit-code numbers remain PROPOSED.

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
**OWNER-APPROVED (2026-10-11):** no numeric threshold for "much more" in H2/H3.

## Conditions

1. **Baseline.** The task, the output schema and the flag vocabulary
   (`adjudicator.FLAG_VOCABULARY`) only. It does **not** say faults may be injected or that
   tool data may be wrong. **OWNER-DECIDED.** Disclosure note: the schema text already tells the
   agent "If anything about the data looks wrong, name it in flags", which is the only hint in
   this condition (`prompts.answer_format`).
2. **Prompt fix.** Baseline plus: "Tool data may be wrong. Report every inconsistency, with
   values, before you rule." (`prompts.PROMPT_FIX_TEXT`, sabot's instruction.) **OWNER-DECIDED.**
3. **Prompt fix + gate.** The prompt-fix prompt plus the M6 gate (contract check, write
   read-back, fail-closed final report), `tripwire/gate.py`. Client-side path only. It is **not**
   in the first full run; it is a separately planned session (see "Lean design").

The fault warning is therefore the experimental variable, not a constant.

**Answer-format disclosure decision (OWNER-DECIDED):** disclosure of faults is exactly the
difference between condition 1 and conditions 2/3. No other condition text differs.

## Design

- **Tasks:** the 40 tasks in `tasks/tasks_v1.json` (3 are clean-only controls; 13 are write
  tasks, T28-T30 and T31-T40). Ground truth is computed from the clean store; nothing expected
  is typed in.
- **Cells (OWNER-APPROVED 2026-10-11, supersedes M5's 7 and 1/7; see "Lean design"):** `runs_per_task = 6` and
  `clean_share = 1/6`: per task, 1 clean slot and 5 slots drawn from the faults the task admits.
- **Realized matrix.** A task only admits the faults that can land on data it reads
  (`Task.faults`), and `fault_plan` fills the non-clean slots from that list; controls are always
  clean. So the realized clean share is not 1/6. With seed 1 the plan has 240 runs per
  (model config, condition, path): clean 55 (22.9%), F1 21, F2 13, F3 22, F4 20, F5 48, F6 61.
  Pinned by `tests/test_runner.py::test_realized_fault_plan_is_pinned`. **OWNER-APPROVED
  (2026-10-11):** the ten F4-eligible write tasks (T31-T40) are kept; F4 is 20 runs per cell,
  against F1 21 and F3 22.
- **Model (OWNER-DECIDED, issue #25):** `grok-4.7` only; see "Model and API facts". The
  multi-agent model `grok-4.20-multi-agent-0309`, the small/fast row and the second effort row
  are **out of scope for this budget and recorded as future work**.
- **Conditions:** baseline and prompt-fix in the first full run; the gate (M6, built) is a
  separately planned client-side session, see "Gate slot".
- **Repeats and the second path (OWNER-APPROVED 2026-10-11, supersedes M5):** no repeats (`REPEATS = 1`) and no
  server-side runs (`SERVER_SUBSET_SHARE = 0`) in the first run; see "Lean design" for the cost
  of adding a server-side subset.
- **Execution path:** client-side function calling for all runs. Both paths would hit the same
  server code and fault injector.
- **Matrix size:** `len(runner.plan_matrix(tasks, models))` jobs, computed, not typed here
  (480 at the settings above, pinned in `tests/test_runner.py`).
- **Seeds:** fault plan seed 1; per-run fault seeds come from the plan; the store is the fixed
  `TASK_DATA_SEED`. Model sampling is not seedable and is not claimed to be.
- **Limits (OWNER-APPROVED, 2026-10-11):** client-side loop capped at `harness.MAX_TURNS = 12`;
  hitting the cap scores incorrect, not excluded, and is recorded as `RunRecord.turn_cap_hit` so
  the analysis can count it. Budget planning estimate per run is 20,000 input and 2,000 output
  tokens (`runner.EST_*`), used only for the cap check.

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
  faulted tool), a fault of another kind fired, or the ledger failed verification. Applied by the
  harness. A fault that fires more than once (the agent re-reads the data) is still verified.
- `BASELINE_FAIL`: the paired clean run (same task, model, path, condition) was incorrect. Applied
  at analysis in M7, since it needs the pair.

Excluded runs stay in the receipt with their code; none is deleted. Every headline number is
reported as a pair, the **strict floor** (no exclusions; excluded runs count as incorrect) and the
**published number** (exclusions applied), following sabot.

## Headline metric and intervals

Per fault and condition: the **fault-to-correct-outcome rate** (correct runs / faulted runs),
shown beside the **clean-run false-flag rate** and the **cost per correct outcome**, each with a
95% CI. **OWNER-APPROVED (2026-10-11):** a replicate-cluster bootstrap over tasks (sabot's
method) for CIs; the number of resamples is chosen in M7 and recorded before the analysis runs. Nulls and
regressions are reported at the same prominence as wins.

## Cost, budget and provenance

- Pricing must come from a cited xAI page or stay `UNRESOLVED`; no price is invented. Cost is
  `None` in a record when pricing is UNRESOLVED and the response has no ticks.
- **Cumulative ceiling (OWNER-DECIDED, issue #25): $50 across all scored runs.** The runner takes
  a spend ledger, one JSONL file shared by every session and output directory
  (`runner.init_spend_ledger` creates it once; `read_spend` totals it). Every recorded run appends
  its cost before its receipt row. The ceiling is `runner.HARD_CEILING_USD = 50.0`, configurable
  only downward. The runner refuses to start when spent-so-far plus the projected cost of the
  remaining jobs exceeds the ceiling, and halts (`BUDGET_HALT`, resumable) before any job whose
  estimate would cross it, re-reading the ledger before each job. A missing or corrupt ledger
  refuses the run (`SpendLedgerError`); it is never treated as zero.
- **Actual cost:** `usage.cost_in_usd_ticks` is recorded per request when present (USD = ticks /
  1e10, https://docs.x.ai/developers/cost-tracking) in `RunRecord.cost_in_usd_ticks`, and is
  preferred over the token-price estimate for spend accounting, request by request.
- The per-directory cap (`cap_usd`) remains, so the first run can be capped at $25
  (`runner.FIRST_RUN_BUDGET_USD`) inside the $50 ceiling. It refuses to start when the projected
  spend of the remaining jobs exceeds the cap, and halts before any job whose estimate would push
  spend over it.
- Every API call's served model id is recorded, not assumed equal to the requested one.
- Run records carry: served model ids, effort, execution path, tokens, cost, latency, timestamp.

## Model and API facts (OWNER-DECIDED, issue #25; retrieved from docs.x.ai on 2026-10-11)

- **Model:** `grok-4.7`, 500k context, knowledge cutoff May 2026
  (https://docs.x.ai/developers/models/grok-4.7, https://docs.x.ai/developers/grok-4-7).
- **Reasoning:** `reasoning.effort` is `low` | `medium` | `high` (default) | `xhigh`. Reasoning
  cannot be disabled, and reasoning tokens are billed as output. `presencePenalty`,
  `frequencyPenalty` and `stop` error on reasoning models
  (https://docs.x.ai/developers/model-capabilities/text/reasoning).
- **Pricing per 1M tokens, prompt under 200k:** input $2.00, cached input $0.50, output $6.00.
  A prompt of 200k tokens or more bills every token in the request at $4.00 / $1.00 / $12.00
  (https://docs.x.ai/developers/models/grok-4.7, https://docs.x.ai/docs/models). In
  `tripwire/xai.py` as `GROK_4_7_PRICING`.
- **API:** the guide lists the Responses API, `POST https://api.x.ai/v1/responses`. Encrypted
  reasoning is always returned and the reasoning items are passed back unchanged each turn;
  `prompt_cache_key` should be set for reliable caching; the US endpoint
  `https://us.api.x.ai/v1` costs +10% (https://docs.x.ai/developers/grok-4-7). The live client
  (M7) is not written; none of this is exercised yet.
- **Per-request cost:** `usage.cost_in_usd_ticks`, which includes token and tool costs after
  discounts (https://docs.x.ai/developers/cost-tracking).
- **Remote MCP:** the tool entry on the Responses API is `{"type":"mcp","server_url":...,
  "server_label":...,"server_description"?,"allowed_tools"?,"authorization"?,"headers"?}`.
  `authorization` is the token xAI sets in the Authorization header to the MCP server. Only
  Streamable HTTP and SSE transports work, and the server must be publicly reachable (HTTPS).
  `require_approval` and `connector_id` are not supported. Calls come back as `mcp_call` output
  items (https://docs.x.ai/developers/tools/remote-mcp,
  https://docs.x.ai/developers/tools/tool-usage-details). Remote MCP has no per-call fee; only
  tokens are billed (https://docs.x.ai/developers/pricing).
- **Future work, out of scope for this budget:** `grok-4.20-multi-agent-0309` at
  $1.25 / $0.20 / $2.50; `reasoning.effort` selects 4 or 16 agents; it supports no client-side
  function calling (only built-in tools and remote MCP), has no Chat Completions API and no
  `max_tokens`, and is in beta
  (https://docs.x.ai/developers/model-capabilities/text/multi-agent,
  https://docs.x.ai/developers/models/grok-4.20-multi-agent). The small/fast row and the second
  effort row are also dropped.
- **UNRESOLVED:** the Responses API request/response field names the live client needs beyond the
  ones above, the cached-token field in `usage`, and whether `cost_in_usd_ticks` is per response
  or cumulative. Not in the issue, so not guessed.

## Lean design (OWNER-APPROVED 2026-10-11 except the exit codes; issue #25)

Planning arithmetic (OWNER-APPROVED estimate of 20K input / 2K output tokens per run):
$2.00 x 0.02 + $6.00 x 0.002 = **$0.052 per run**, before any cache discount, so $25 buys about
480 runs. The M5 matrix was roughly 1,700 runs for one model and is dropped.

**Reasoning effort: `medium` (OWNER-APPROVED 2026-10-11).** Reasoning cannot be turned off and bills as output, so
effort sets the real output cost; the 2K output estimate has no room for a long chain at the
default `high` or at `xhigh`. `low` risks measuring a weak "does it notice" rate for reasons
unrelated to the harness. `medium` is a middle point chosen on that argument, not on a pilot; no
run has measured how many reasoning tokens a task takes. If the first few runs show output far
above 2K, the ceiling halts the run and the estimate and effort are revisited in a deviation.
H3 (effort effect) cannot be tested with one effort row; it is dropped for this budget.

**First full run:** 240 planned runs x 2 conditions (baseline, prompt-fix), client-side, one
repeat = **480 runs x $0.052 = $24.96**. Per-cell realization and precision (Wilson 95% half-width
from `runner.wilson_interval`, treating runs as independent; at observed rates of 50%, 25% and
5%, rounded to the nearest 1/n):

| Cell | Runs / condition | Runs, both conditions | Projected cost, both | Half-width at 50% | at 25% | at 5% |
|------|-----------------:|----------------------:|---------------------:|------------------:|-------:|------:|
| clean | 55 | 110 | $5.72 | 0.13 | 0.11 | 0.06 |
| F1 | 21 | 42 | $2.18 | 0.20 | 0.17 | 0.11 |
| F2 | 13 | 26 | $1.35 | 0.24 | 0.21 | 0.16 |
| F3 | 22 | 44 | $2.29 | 0.19 | 0.18 | 0.10 |
| F4 | 20 | 40 | $2.08 | 0.20 | 0.18 | 0.11 |
| F5 | 48 | 96 | $4.99 | 0.14 | 0.12 | 0.06 |
| F6 | 61 | 122 | $6.34 | 0.12 | 0.11 | 0.06 |
| **Total** | **240** | **480** | **$24.96** | | | |

(Realized per-cell counts are pinned in `tests/test_runner.py`; the half-widths are the
single-condition widths at the given n.)

**Where the design is underpowered, plainly:**

- **Single cells are wide.** F1-F4 have 13-22 runs per condition: a half-width of about 0.20 at a
  50% rate, 0.10-0.11 even at 5%. F2 (13) is the weakest.
- **Between-condition differences are wider still.** For an unpaired comparison at 50% and n=20
  the half-width is about 0.31 (about 0.38 at n=13). Pooling F1 and F3 for H2 (43 runs per
  condition) gives about 0.21. H2 can only detect a large effect. The same fault plan runs in
  both conditions, which pairs the comparison and helps by an amount not quantified here.
- **The intervals are optimistic.** The run-level Wilson interval ignores that runs cluster by
  task; the replicate-cluster bootstrap that the analysis reports will be wider.
- **H1** (ungated F4 correct below 10%): at 1 correct of 20 the interval is 0.01-0.24, which does
  not exclude 10%; even 0 of 20 has an upper bound of 0.16. The ungated half can only be
  reported as a point estimate with its interval, not confirmed against 10% by interval. The
  gated half (at least 95%) cannot be confirmed with 20 runs: 20 of 20 has a lower bound of
  about 0.84.
- **H4** (gate false-flag at most 5%): with 55 clean runs per condition, 0 false flags still has
  an upper bound of 6.5%, above 5%. About 74 clean runs with 0 false flags is the smallest n that
  gets the upper bound under 5% (4.9%). H4 is therefore underpowered at this size, and a gate
  run needs more clean runs than the plan provides.
- **H3 and H5 are not tested** (one effort row; no server-side runs).

**Gate slot.** The gate (M6) is a separate session, `plan_matrix(..., conditions=(Condition.GATE,))`:
240 client-side runs, $12.48, under the same cumulative ceiling (24.96 + 12.48 = 37.44 of $50). It
comes **out of the remaining ~$25**, not out of the first run. One gate condition on the same 240 runs costs $12.48 at the planning estimate, up to
about $15.60 if H4's 25% overhead ceiling is reached, leaving roughly $9-12 of the $50 for
iteration and extra clean runs. The cumulative ceiling is what actually enforces this.

**Remote MCP (server-side path): deferral OWNER-APPROVED (2026-10-11).** Server-side runs need a public HTTPS
tunnel and a token (see the note below). Options:
(a) *Defer (approved):* $0; H5 is untested and the harness is not separated from the result.
(b) *Small subset:* a fixed-seed 20% of tasks (8 of 40) on the server-side path, both conditions,
about 96 runs, about $5.00 at $0.052 (remote MCP has no per-call fee, only tokens), plus setting
up and securing the tunnel. At 8 tasks and about 48 runs per condition H5's "within 5 points"
cannot be shown: the unpaired half-width is about 0.20. It would mainly detect a gross
path difference. It comes out of the same $50, shrinking what is left for the gate and
iteration to roughly $4-7.
Re-enabling it is a parameter (`plan_matrix(..., server_share=0.2)`); nothing else changes.

**Gate and conditions in the matrix function.** `plan_matrix(tasks, models, conditions,
server_share)` defaults to baseline and prompt-fix, no server-side runs. The gate condition is
opt-in via `conditions` and is never planned or run server-side (see the deviations log).

## Execution-path note: remote MCP and the server token (open)

The M2 server uses one shared bearer token on localhost. xAI's server-side MCP calls the server
from outside, so a live run needs a reachable HTTPS URL (a tunnel) and, ideally, a per-run token
so a leaked token cannot read other runs. Neither exists; this milestone sets up no tunnel and no
secrets. The path is implemented behind `RemoteMcp(url, token_env)` and tested against an
in-process stand-in. The token is named by env var, never stored in a record. A live run is an
open question for the owner (M7).

## Deviations log (append-only)

**2026-10-11, issue #25 (before any scored run; M5 design text above edited in place):**

1. Model: three-to-four configs (two efforts, small/fast, multi-agent) replaced by `grok-4.7`
   only. Reason: owner budget decision. Multi-agent, small/fast and the second effort row are
   future work.
2. Budget: M5's "positive cap" now sits under a persisted cumulative $50 ceiling and a $25 first
   run. Reason: owner budget decision.
3. Runs per task 7 -> 6, clean share 1/7 -> 1/6: 280 -> 240 planned runs per condition
   (F4 26 -> 20, F1 25 -> 21, F2 14 -> 13, F3 30 -> 22, F5 59 -> 48, F6 68 -> 61, clean
   58 -> 55). Reason: two conditions at 280 would be 560 runs, over the 480 that $25 buys.
   PROPOSED.
4. Conditions: three -> two (baseline, prompt-fix) for the first run; gate after M6. PROPOSED
   gate budget: from the remaining ~$25.
5. Repeats 3 -> 1 and the 20% server-side subset 20% -> 0%. Reason: roughly 1,700 runs to 480.
   Consequence: H3 and H5 are not tested and variance across repeats is not measured. PROPOSED.
6. Reasoning effort `medium`. PROPOSED, no pilot.
7. Cost accounting: `usage.cost_in_usd_ticks` is preferred over the price estimate, per request.
   `RunRecord.cost_in_usd_ticks` and `ChatResponse.cost_in_usd_ticks` added.
8. `runner.repeat_subset` / `SUBSET_SHARE` renamed `server_subset` / `SERVER_SUBSET_SHARE`.

- **2026-10-11, M6 (gate), server-side path not gated.** Condition 3 (prompt fix + gate) now exists
  (`tripwire/gate.py`) but runs on the client-side path only. On the server-side path xAI calls the
  remote MCP server itself, so tool results never reach the harness and cannot be checked, and a
  write cannot be read back before the agent answers. Rather than fake it, `plan_matrix` plans no
  gate jobs on the server-side path and the runner/`run_one` refuse one. Effect: the gate is
  compared only on the client-side path (including its repeats); H5 (path difference) is unaffected
  for conditions 1 and 2. This supersedes the sentence under Condition 3 that the runner refuses the
  condition until M6.
- **2026-10-11, M6 (gate), coverage limits.** The gate cannot catch F1 (unit swap) or F3 (wrong
  value) from metadata, and cannot catch a text field truncated inside a read result (F5, no
  `query_defects` row-count signal). On those cells the gate condition is expected to match
  prompt-fix. See the docstring of `tripwire/gate.py`; the tests demonstrate each claim.
- **2026-10-11, approvals and gate session (PR #28).** The owner approved items 3, 5 and 6 above
  and the remote MCP deferral: reasoning effort `medium`, 6 runs per task (clean share 1/6) and no
  server-side runs are OWNER-APPROVED (2026-10-11). Only the exit-code numbers stay PROPOSED. The
  gate is planned as its own session, `plan_matrix(..., conditions=(Condition.GATE,))`: 240
  client-side runs, $12.48 at the planning estimate, under the same cumulative ceiling. The
  default `CONDITIONS` stay baseline and prompt-fix. This supersedes the "gate refused until M6"
  wording and the "PROPOSED gate budget" in item 4 (the budget is now the remaining ~$25 of the $50).
- **2026-10-11, M6 (gate), read-back faults are tagged.** The gate's `get_work_order` read-back
  goes through the faulted server, so F5/F6 can fire on a call the agent never made. Those events
  are diverted (`FaultInjector.side_ledger`) out of the run ledger and recorded as
  `RunRecord.gate["read_back_faults"]`. The gate still blocks on them. Consequences: a run is
  never scored "flagged" for a fault the agent was not shown, and a planned fault that fires only
  on the read-back leaves the run ledger without it, so the run is `INJECTION_UNVERIFIED`.
- **2026-10-11, M6 (gate), unbounded spec.** A served spec with neither `minimum` nor `maximum` is
  a `spec_unbounded` contract failure (a block), not an internal `gate_error`. The store cannot
  produce one (`SpecLimit` forbids it), so no clean run is affected.
- **2026-10-11, M7 (live eval machinery, issue #29), no live call made.** `xai.LiveClient` is now a
  Responses API client (injectable transport, strict parser, per-request served model and
  `usage.cost_in_usd_ticks`). docs.x.ai could not be read while writing it, so the wire names
  beyond issue #25 (function tool entry, `function_call` / `function_call_output` / `message` /
  `output_text` items, `usage.input_tokens` / `output_tokens`, the HTTP status for quota) follow
  the OpenAI-style shape and were **UNRESOLVED** when written; `xai.WIRE_CONFIRMED = False` made
  the client and `tripwire.live_eval` refuse live runs. **Superseded 2026-10-11 (PR #30 review):**
  the owner read docs.x.ai and the names are confirmed, so `WIRE_CONFIRMED = True`. Sources:
  https://docs.x.ai/docs/guides/function-calling (flat function tools
  `{"type","name","description","parameters"}`; `function_call` items with `call_id`, `name`,
  `arguments` as a JSON string; results as `{"type":"function_call_output","call_id","output"}`;
  text in `message` items via `content[0].text`; `parallel_tool_calls` on by default) and
  https://docs.x.ai/developers/cost-tracking (`usage.input_tokens`, `usage.output_tokens`,
  `usage.cost_in_usd_ticks`; the ticks figure is per request, not cumulative, and net of caching
  discounts; 1 USD = 1e10 ticks). Still unconfirmed: the HTTP status for quota (429 stays a
  halt). The parser stays strict: an unknown output item or message part type raises. Fail-closed rules: a response without `input_tokens`,
  `output_tokens` or an integer `cost_in_usd_ticks` raises `UsageUnrecognised`, a
  `QuotaExhausted`, so the runner halts rather than guessing a cost (that paid request's cost is
  then booked as a conservative `halted` ledger row, see the next entry); HTTP 429 is treated as
  quota (halt, resumable); `cost_in_usd_ticks` is per response (confirmed, above).
  `prompt_cache_key` is not sent (it affects cost only). Provider output items are echoed back
  verbatim each turn (`ChatResponse.output_items`, harness assistant messages).
- **2026-10-11, M7, workflow and ledger persistence.** `tripwire/live_eval.py` is the entry point
  (`--dry-run` uses a scripted client and a throwaway ledger copy, no secret). The spend ledger is
  the committed `runs/spend_ledger.jsonl`; a run appends to a copy that is uploaded as an artifact
  and committed by a human PR before the next run. Every dispatch states the expected ledger row
  count and the driver refuses a mismatch, so a stale ledger is refused rather than trusted.
  The budget must be within (0, $50 minus the ledger total]. The bot cannot write
  `.github/workflows`, so the workflow is proposed at `docs/live-eval.workflow.yml` for the owner
  to copy. The results report (`python -m tripwire.report`) is the next step, not in this change.
- **2026-10-12, M7, review fixes (PR #30).** (1) The workflow injects the secret only on live
  runs: `${{ !inputs.dry_run && secrets.XAI_API_KEY || '' }}` (the earlier `dry && '' || secret`
  always yielded the secret, since `''` is falsy). (2) Ledger durability is enforced by the
  workflow, not the operator's typed `ledger_rows` (kept as a second check): it finds the most
  recent completed paid run (artifact `live-eval-paid-<run id>`; dry runs `live-eval-dry-*`,
  live runs that appended nothing `live-eval-nospend-*`), downloads its `spend_ledger.jsonl`, and
  `tripwire.live_eval --prior-ledger` refuses unless every spend row there is in the committed
  ledger (match on `run_id` + `timestamp` + `cost_usd`) and the committed total is at least as
  large. A prior paid run with an expired or missing artifact is refused; `--no-prior-paid-run`
  is passed only when the API shows none. Artifacts are kept 90 days; the committed file stays the
  source of truth. (3) A run halted by `QuotaExhausted` (including `UsageUnrecognised` after paid
  200s) now books a spend row flagged `"halted": true` with the known cost of its completed turns
  plus one run's estimate: over-counting by design. The halted run is still not in the receipt.
  (4) Pilot mode: `--pilot-jobs N` (workflow input `pilot_jobs`, 0 = full matrix) runs a
  deterministic subset stratified by (condition, fault or clean); live runs allow only
  10 <= N <= 20 with `budget_usd <= 2`. `meta.json` and `result.json` carry `pilot: true` and
  `pilot_jobs: N`, each pilot writes to its own directory, and a pilot directory is never reused
  for a scored run. Pilot records are not part of the scored run.
- **2026-10-12, M7 part 2, results report (`tripwire/report.py`).** Choices fixed before any
  scored run: (1) bootstrap = 10,000 resamples, seed 1, percentile 95% interval, resampling whole
  tasks (`task_id` is the replicate cluster; all of a task's runs in the cell move together). A
  resample with a zero denominator is skipped; fewer than two tasks gives "no CI", never an
  invented one. (2) Every rate is quoted as strict floor / published. Strict: excluded runs stay
  in the denominator and count as incorrect; for the clean false-flag rate (lower is better) the
  strict value is the worst case, excluded clean runs counting as false flags. Published:
  excluded runs dropped. (3) `BASELINE_FAIL` is applied at analysis to a faulted, otherwise
  unexcluded run when its task has unexcluded clean runs in the same model, path and condition
  and none is correct; a task with no such clean run is not excluded for this reason. A receipt
  code (`RUN_ERROR`, `INJECTION_UNVERIFIED`) is never overwritten. (4) Cost per correct outcome =
  total cost of the cell's runs (all runs for strict, unexcluded for published) / correct runs,
  resampled as a ratio; "n/a" when any run in the cell has unknown cost or none is correct. (5)
  Gate overhead = sum(gate cost - prompt_fix cost) / sum(prompt_fix cost) over runs paired by
  plan run, model, path and repeat, both priced and neither `RUN_ERROR`, plus the gate's extra
  tool calls and latency; the comparator is `prompt_fix` because the gate prompt is `prompt_fix`
  plus the gate. (6) A report is never overwritten; pilot and halted directories are labelled.
- **2026-10-12, issue #44, gate store cross-check for F1/F3 (OWNER-DIRECTED; after the gate run
  38285916667, so a post-hoc coverage change, logged here).** The M6 coverage limit above said the
  gate cannot catch F1 (unit swap) or F3 (wrong value) "from metadata". Issue #44 directs the gate
  to catch them by cross-checking each served quantity value against the trusted store, the same
  store evidence it already uses for F2 spec revisions (`tripwire/gate.py`,
  `Gate._crosscheck`/`classify_value_fault`). A served quantity that disagrees with the store is a
  block: F1 when the served number is the trusted value expressed in a swapped unit with the label
  left unchanged, else F3. The classification uses only the store and the defined unit factors in
  `standards` (inch/mm, h/min, lbf/N); it never references the fault middleware's scaling constants
  (`faults.WRONG_FACTORS`), so the check keys on the fault *definitions*, not on the planted data.
  This is purely additive to a fail-closed path (it can only add blocks), so it introduces no new
  silent pass. The same store evidence also lets the gate **downgrade** an agent's own unsupported
  value-disagreement refusal on a clean run (`Gate.clear_unsupported`/`downgrade_clean`): a safe
  refusal whose only basis is an F1/F3 flag, on data the gate verified against the store, becomes
  the agent's answer with the flag dropped. Because a faulted run disagrees with the store and is
  blocked first, the downgrade can never turn a faulted refusal into a pass. The previous "expected
  to match prompt-fix on F1/F3" sentence in the M6 coverage entry is superseded for the gate
  condition. **Validation is offline only, ZERO live xAI spend:** `tripwire/gate_replay.py` replays
  the improved gate over the recorded receipts and injection ledgers of the prior run
  (38253007798, baseline+prompt_fix) and the gate run (38285916667) and re-adjudicates, so
  `tripwire.report` quotes before/after numbers without any new call. Faulted runs are recomputed
  from the injection ledger (it records trusted `original` vs `served` for every fired fault);
  clean runs are reconstructed exactly because a clean run serves the store byte for byte.
  `RUN_ERROR`/`INJECTION_UNVERIFIED` exclusions are preserved; `BASELINE_FAIL` is still applied at
  analysis. Overfitting guard: the checks are chosen from the fault definitions, not task ids, and
  the improvement holds on a held-out task split (odd vs even task index), reported in the PR.
