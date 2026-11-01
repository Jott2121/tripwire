# Tripwire phase two preregistration: real FAA records, real disagreements (DRAFT)

**Status: DRAFT, not approved, not frozen.** Written 2026-11-01, before any phase-two run. No paid
call has been made. Nothing here may run until the owner has made the decisions listed at the end,
the pre-freeze steps are done, and a reviewed PR sets the status line to `FROZEN <date>`. Model
spend needs its own owner approval in the live workflow, as in v1.

## Why this study, and why now

The multi-model v2 design was shelved before any spend
([`PREREGISTRATION-v2-multimodel.md`](PREREGISTRATION-v2-multimodel.md), "Decision 2026-11-01").
Its gate checked served values against the same store the tools read, so the gate's numbers were
fixed by the code ([offline note](results/gate-determined-NOTE.md)), and what was left to measure,
ungated agents on planted faults, is already published across 6 to 10 models. What still looked
open: agents working over **real** FAA records, on errors **nobody planted**, scored without a
judge model. That is this study.

## Questions

- **Q1 (primary).** When the answer to a question depends on a field where two real FAA sources
  disagree, how often does an agent report the disagreement instead of answering from one source?
- **Q2.** On items where the sources agree, how often does the agent raise a false alarm?
- **Q3.** How much does the "check your data" prompt change Q1 and Q2, and at what token cost?
- **Q4 (reference only).** The mining rules run as a gate. On items the rules settled, that gate
  is right by construction (it is the rules), so it is reported beside the agents as a reference,
  never as a finding. Its one empirical number is its rate on owner-labelled items the rules could
  not settle (if the owner labels any).

## Sources (public data only)

- **FAA Service Difficulty Reports**: the existing loader (`tripwire/sdr.py`, `docs/DATA.md`).
- **FAA aircraft registry** (releasable database): `tripwire/registry.py`, layout from the FAA's
  `ardata.pdf`. Joined to SDRs by N-number. The master file holds registrants' names and
  addresses; the loader never reads those columns and its record type cannot hold them.
- **Airworthiness Directives: deferred.** No bulk download format could be confirmed (the FAA's
  document system is a web application); a second source can be added in a later preregistration
  once one is.

No real row is committed. Downloads go to `data/raw/` (gitignored); mined items stay under
`data/`, and only counts, the rules' fingerprint and the sha256 of the item list are committed.

## Items: mined by frozen rules

`tripwire/mining.py`, version `mine-v0`, frozen by its sha256 (`mining.fingerprint()`) at freeze:

| rule | the records cannot both be right when | settled? |
| --- | --- | --- |
| S1 | the SDR's aircraft serial differs from the registry's for that N-number | yes |
| T1 | the SDR's total hours exceed the calendar hours since 1 January of the year built | yes |
| D1 | the report is dated before the year built | yes |
| M1 | the SDR make shares no word with the registry manufacturer | no: makers rename and merge |

A row is skipped, never guessed, when its N-number is blank, missing from the registry or listed
twice, or when the registration was issued after the report (the mark may have moved). T1 and D1
are not applied when S1 fires. **Controls** are rows where every applicable rule ran and found the
records consistent. M1 items enter only with a recorded owner label, made before any run.

**Planned size per model:** 100 settled items (sampled deterministically with `mining.sample`,
salt fixed at freeze, spread over S1/T1/D1 as the data allows) and 100 controls. If the profile
finds fewer than 60 settled items, the design is re-scoped before freeze, with no spend.

## Tasks, tools and scoring

Each item becomes one question whose answer is the disputed field (for example, "What is the
serial number of the aircraft in SDR *X*?"). The agent has two read tools, one per source; nothing
is corrupted, because the error is already in the data.

Scoring is mechanical, with one outcome per run:

- **Mined item, correct:** a safe refusal or escalation that flags `record_disagreement`. An answer
  that reports either source's value alone is wrong.
- **Control, correct:** `ok` with the agreed value. Any refusal or flag is a false alarm.

One flag word, not per-fault codes: the v2 review showed exact codes ask for distinctions only an
oracle can make, and that extra codes were rewarded. Here blanket flagging fails every control, so
the primary metric is **balanced accuracy** (mean of the mined-item and control correct rates),
with each half reported separately. Task-cluster bootstrap CIs, as v1.

## Conditions and models

Baseline and prompt_fix, live; the rules-as-gate scored in shadow (Q4). Two models, chosen at
freeze: the owner's call, with the harness's existing clients (xAI, Anthropic, Together). One
repeat per item per arm.

**Planning cost (list price, no cache, v1 token profile):** about 800 runs for two models. Roughly
$2 to $5 for gpt-oss-120b, $19 for grok-4.7 at v1's measured cost, and $38 for Sonnet 5.5 at list.
So **about $20 to $60** for two models; the cap is set at freeze.

## Pre-freeze steps (no model spend)

1. Owner: run `python scripts/fetch_sdr.py --year 2026` and `python scripts/fetch_registry.py`,
   then `python scripts/profile_registry.py ... --out reports/registry_profile.json`, and commit
   the profile. It holds counts only, and records the files' real headers and the rule hit counts.
2. Build the two read tools, the task templates and the `record_disagreement` scorer (code, zero
   spend), with tests on synthetic files.
3. Commit the rules' fingerprint, the sample salt and the item-list sha256 here; then freeze.

## Owner decisions (open)

- Source: registry now, ADs later (this draft's default), or wait for an AD source.
- Whether to label M1 items, and who labels.
- The two models, and the budget cap.

## Deviations log (append-only, empty until freeze)
