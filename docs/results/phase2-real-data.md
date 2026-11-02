# Phase two results: agents on real FAA records, real disagreements

Preregistration: [`PREREGISTRATION-phase2-real-data.md`](../PREREGISTRATION-phase2-real-data.md),
frozen 2026-11-01 before any run. Numbers: [`reports/phase2_results.json`](../../reports/phase2_results.json),
written by `python -m tripwire.live_phase2 report` from the owner's run records on 2026-11-02.
The per-run records stay on the owner's machine by design (their answers hold real serial
numbers), so the committed JSON is the record of these numbers; `report` cannot be re-run from a
fresh clone. Facts marked *(owner's records)* below were read from those records and are not in
the committed JSON.

## What was measured

200 questions drawn from real FAA Service Difficulty Reports (SDRs), each answered by an agent
with two read tools: the report itself, and the FAA aircraft registry record for the same tail
number. The question names only the report.

- **100 mined items:** the report and the registry cannot both be right (the serial numbers
  differ, 86 items, rule S1; or the report's total hours exceed the hours since the aircraft was
  built, 14 items, rule T1). Nobody planted these errors; they are in the public data. The
  preregistered correct answer is to refuse or escalate and flag `record_disagreement`.
- **100 controls:** the two sources agree. The correct answer is `ok`, no flag, and the report's
  value.

Two models, `grok-4.7` (xAI) and Muse Spark Open (open weights, reached through OpenRouter,
pinned to Together with fallbacks off), each in two conditions: **baseline** and **prompt_fix**
(the "check your data" instruction). One run per item per condition: 400 runs per model, 800 in
all. No run had a client error and no reply was unparseable. *(Owner's records:)* no run hit the
turn cap, all 800 runs called the registry tool, at about three model turns per run, and every
turn was answered by the requested model name (`grok-4.7`; `meta/muse-spark-open`). OpenRouter's
reply names the model, not the server, so Muse being served by Together rests on the pin.

The primary metric is **balanced accuracy**, the mean of the mined and control correct rates, so
flagging everything scores 0.5, not 1.0. 95% CIs are percentile bootstraps over items, 2,000
resamples, seed 20261101. Where a half is 0 of 100 or 100 of 100, the bootstrap interval collapses
to a point; that is not zero uncertainty (a 0 of 100 rate is still compatible with up to about 3%).

## Results

| model | condition | balanced accuracy (95% CI) | mined correct | mined flagged | control false alarms |
| --- | --- | --- | ---: | ---: | ---: |
| grok-4.7 | baseline | 0.505 (0.49 to 0.525) | 2% | 97% | 1% |
| grok-4.7 | prompt_fix | 0.81 (0.75 to 0.865) | 84% | 100% | 21% |
| Muse Spark Open | baseline | 0.50 (collapsed, see above) | 0% | 99% | 0% |
| Muse Spark Open | prompt_fix | 0.515 (0.50 to 0.535) | 3% | 100% | 0% |

By rule, mined correct on S1 (86 items): grok-4.7 2% at baseline and 83% with the prompt, Muse
Spark Open 0% and 1%. T1 has 14 items and, as the plan decided, is not reported on its own (its
rates are in the JSON).

**Q1: do agents report the disagreement instead of answering from one source?** At baseline,
almost never: they refused or escalated on 2% (grok-4.7) and 0% (Muse Spark Open) of mined items.
But both models *noticed* the conflict almost every time. They set the disagreement flag on 97%
and 99% of mined items and still gave the report's value as the answer (88% and 85% of mined
items). The gap is between noticing and acting, not between looking and not looking.

**Q2: false alarms on agreeing records.** Near zero at baseline (1% and 0%). Neither model flags
blindly.

**Q3: what the "check your data" prompt changes.** It splits the two models.

- With the prompt, grok-4.7 acts on what it notices: mined correct goes from 2% to 84%, and
  balanced accuracy from 0.505 to 0.81. The intervals do not overlap (they are computed per arm,
  not paired). The cost is caution on clean records: false alarms go from 1% to 21%. One more
  control was answered `ok` with no flag but a value that matched neither source as scored (see
  Limits), so that condition's control rates add to 99%.
- Muse Spark Open moves from 0 to 3 of 100 mined items (0.50 to 0.515): it still flags almost
  every mined item and still answers it. It raises no false alarms in either condition.
- Token cost: output tokens rise 2.4x for grok-4.7 (103k to 249k over 200 runs) and 2.1x for
  Muse Spark Open (148k to 310k); input tokens are nearly flat (+3% and +2%).

**Q4: the mining rules as a gate (reference only).** 1.0 balanced accuracy on both halves. This
is true by construction, because the rules chose the items, so it is a reference, not a finding.
The owner labelled no unsettled items, so Q4's one empirical number does not exist in this study.

## What this means

- On real records, both agents saw the conflict and mostly answered anyway. A flag the agent
  sets but does not act on protects no one downstream; only an action does.
- A one-sentence prompt closed most of that gap for one model (grok-4.7), at the price of one
  false alarm in five clean records. For the other (Muse Spark Open) it changed 3 of 100.
  How much a prompt helps depends on the model; v1's planted-fault result (no gain for
  grok-4.7) and this one are different settings, not a contradiction to average away.
- Where the conflict can be written as a rule, a deterministic check does not depend on the
  model choosing to act. The rules-as-gate score is not evidence that the check beats the models
  in general, because the same rules chose the items.

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
- **Control serials were matched more strictly than the rules.** The frozen scorer matched a
  serial answer on letters and digits with zeros kept, stricter than the agreement test the mining
  rules use to pick controls (leading zeros dropped, an all-digit report serial matching the
  registry's digits). A control answered in the registry's form scored wrong. The scorer now uses
  the rules' own test (deviation 5); the controls themselves were always chosen by the rules and
  did not change. The numbers above are as scored at run time. The committed rates bound the
  effect at the one grok-4.7 prompt_fix control noted above: if it was a serial answered in an
  agreeing form, that arm's control correct rate is 79%, not 78%, and its balanced accuracy 0.815,
  not 0.81. No false-alarm rate can move. `python -m tripwire.live_phase2 rescore` settles it from
  the owner's records, offline.
- **The gate is the rules.** Q4 is a ceiling by design; the rules can be wrong in ways this study
  cannot see, because every item they settled is scored by the same rules.
- **Status counts are not in the committed report.** "Gave the report's value" is the
  `answered_from_sdr` rate, which does not record the reply's status; the correct rates are what
  pin how often a model refused.

## Deviations from the preregistration

All logged in its deviations log; none changed an item, rule, prompt or cap. Deviation 5
changed how a serial answer is matched, after the runs, at the owner's request.

1. Muse Spark Open called through OpenRouter, pinned to Together with fallbacks off (the owner's
   card was declined at Together).
2. A first Muse pilot with a malformed key (20 x HTTP 401, $0) was discarded and re-run.
3. Rate-limited requests (HTTP 429, unbilled) are resent after a wait instead of halting the run.
   *(Owner's observation:)* the Muse scored run hit them often and took longer; no run was lost.
4. The CIs use 2,000 bootstrap resamples, not v1's 10,000 as "as v1" in the plan implied; noted
   after the scored runs, and stated here instead of re-run.
5. A serial answer is now matched by the mining rules' own agreement test, not the stricter
   letters-and-digits match the frozen scorer used. It changes scoring only, can move at most one
   of the 400 control runs, and no item, rule, prompt or cap changed (see Limits).

## Cost

$7.86 of the $65 cap, pilots included: grok-4.7 $3.95, Muse Spark Open $3.92. Both ran far
below the planning estimate (about $0.009 a run against a planned $0.02 to $0.15), because real
runs used far fewer tokens than the v1 profile the plan assumed.
