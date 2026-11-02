# Phase two results: agents on real FAA records, real disagreements

**Status: DRAFT 2026-11-02.** The numbers are from the owner's report run; the committed
`reports/phase2_results.json` and a check of served models and registry reads are still to come.

Preregistration: [`PREREGISTRATION-phase2-real-data.md`](../PREREGISTRATION-phase2-real-data.md),
frozen 2026-11-01 before any run. Numbers: [`reports/phase2_results.json`](../../reports/phase2_results.json),
written by `python -m tripwire.live_phase2 report`. Runs made on the owner's machine on 2026-11-02.

## What was measured

200 questions drawn from real FAA Service Difficulty Reports (SDRs), each answered by an agent
with two read tools: the report itself, and the FAA aircraft registry record for the same tail
number. The question names only the report.

- **100 mined items:** the report and the registry cannot both be right (the serial numbers
  differ, 86 items, rule S1; or the report's total hours exceed the hours since the aircraft was
  built, 14 items, rule T1). Nobody planted these errors; they are in the public data. The
  preregistered correct answer is to refuse or escalate and flag `record_disagreement`.
- **100 controls:** the two sources agree. The correct answer is `ok`, no flag, the agreed value.

Two models, `grok-4.7` (xAI) and Muse Spark Open (open weights, served by Together through
OpenRouter), each in two conditions: **baseline** and **prompt_fix** (the "check your data"
instruction). One run per item per condition: 400 runs per model, 800 in all. Every run finished:
no client errors, no run hit the turn cap, no reply was unparseable.

The primary metric is **balanced accuracy**, the mean of the mined and control correct rates, so
flagging everything scores 0.5, not 1.0. 95% CIs are item bootstraps.

## Results

| model | condition | balanced accuracy (95% CI) | mined correct | mined flagged | control false alarms |
| --- | --- | --- | ---: | ---: | ---: |
| grok-4.7 | baseline | 0.505 (0.49 to 0.525) | 2% | 97% | 1% |
| grok-4.7 | prompt_fix | **0.81 (0.75 to 0.865)** | 84% | 100% | 21% |
| Muse Spark Open | baseline | 0.50 (0.50 to 0.50) | 0% | 99% | 0% |
| Muse Spark Open | prompt_fix | 0.515 (0.50 to 0.535) | 3% | 100% | 0% |

By rule, mined correct: grok-4.7 S1 2% and T1 0% at baseline, S1 83% and T1 93% with the prompt;
Muse Spark Open S1 0% and T1 0% at baseline, S1 1% and T1 14% with the prompt (T1 is 14 items).

**Q1: do agents report the disagreement instead of answering from one source?** At baseline,
almost never. But both models *noticed* it almost every time: they set the disagreement flag on
97% (grok-4.7) and 99% (Muse Spark Open) of mined items, then answered
`ok` with the report's value anyway (88% and 85% of mined items answered from the report). The
gap is between noticing and acting, not between looking and not looking.

**Q2: false alarms on agreeing records.** Near zero at baseline (1% and 0%). Neither model flags
blindly.

**Q3: what the "check your data" prompt changes.** It splits the two models.

- grok-4.7 starts acting on what it notices: mined correct goes from 2% to 84%, and balanced
  accuracy from 0.505 to 0.81, with CIs far apart. The cost is caution on clean records: false
  alarms go from 1% to 21%. One control was answered `ok` with a wrong value, so the control
  rates add to 99%.
- Muse Spark Open barely moves: it still flags every mined item and still answers it, 3% correct.
  It raises no false alarms in either condition.
- Token cost: output tokens rise 2.4x for grok-4.7 (103k to 249k over 200 runs) and 2.1x for
  Muse Spark Open (148k to 310k); input tokens are nearly flat (+3% and +2%).

**Q4: the mining rules as a gate (reference only).** 1.0 balanced accuracy on both halves. This
is true by construction, because the rules chose the items, so it is a reference, not a finding.
It does show what a deterministic cross-check catches on these records: every mined item, with
no false alarm on the 100 controls. The owner labelled no unsettled items, so Q4's one empirical
number does not exist in this study.

## What this means

- On real records, both agents saw the conflict and answered anyway. A flag the agent sets but
  does not act on protects no one downstream; only an action does.
- A prompt can close most of that gap for one model (grok-4.7), at the price of one false alarm
  in five clean records. For the other model (Muse Spark Open) it does nothing measurable.
- Where the conflict can be written as a rule, a deterministic check does not depend on the
  model choosing to act. The rules-as-gate score is not evidence that the check beats the models
  in general, because the same rules chose the items; it shows that the gap measured here is in
  what the models did with what they saw.

## Limits

- **Mostly serial numbers.** 86 of the 100 mined items are S1 serial disagreements; T1 has 14
  items, too few for a per-rule claim.
- **One run per item per condition**, so there is no repeat-run variance estimate; the CIs cover
  item sampling only.
- **Two models**, one closed and one open-weight. The result is not a ranking of models in
  general.
- **"Correct" requires acting.** By the preregistered rule, an `ok` answer that carries the flag
  is wrong. That rule is the point of Q1, and `flagged` is reported beside it so noticing can be
  read on its own.
- **The gate is the rules.** Q4 is a ceiling by design; the rules can be wrong in ways this study
  cannot see, because every item they settled is scored by the same rules.

## Deviations from the preregistration

All logged in its deviations log before the scored runs; none changed an item, rule, prompt,
scoring rule or cap.

1. Muse Spark Open called through OpenRouter, pinned to Together with fallbacks off (the owner's
   card was declined at Together).
2. A first Muse pilot with a malformed key (20 x HTTP 401, $0) was discarded and re-run.
3. Rate-limited requests (HTTP 429, unbilled) are resent after a wait instead of halting the run.
   The Muse scored run hit Together's capacity often and took longer; no run was lost.

## Cost

$7.86 of the $65 cap, pilots included: grok-4.7 $3.95, Muse Spark Open $3.92. Both ran far
below the planning estimate (about $0.009 a run against a planned $0.02 to $0.15), because real
replies were much shorter than the v1 token profile assumed.
