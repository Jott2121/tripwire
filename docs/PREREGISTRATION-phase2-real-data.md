# Tripwire phase two preregistration: real FAA records, real disagreements (DRAFT)

**Status: DRAFT, not approved, not frozen.** Written 2026-11-01, before any phase-two run. No paid
call has been made. Nothing here may run until the owner has made the decisions listed at the end,
the pre-freeze steps are done, and a reviewed PR sets the status line to `FROZEN <date>`. Model
spend needs its own owner approval in the live workflow, as in v1. As of 2026-11-01 the mining
rules (`mine-v2`) meet the freeze bar; what is left is the item draw on the owner's files and the
owner decisions.

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

`tripwire/mining.py`, version `mine-v2`, frozen by its sha256 (`mining.fingerprint()`) at freeze:

| rule | the records cannot both be right when | settled? |
| --- | --- | --- |
| S1 | the serial keys differ in a way S2 does not explain | yes |
| S2 | one key contains the other, they differ only by zeros, or one ends with the other's part after a model prefix | no: may be notation or a slip on the same airframe |
| T1 | the SDR's total hours exceed the calendar hours since 1 January of the year built | yes |
| D1 | the report is dated before the year built | yes (expect about none) |
| M1 | no word of the SDR make code starts a word of the registry manufacturer | no: an abbreviation, and makers rename and merge |

A serial **key** is letters and digits, upper case, leading zeros dropped; a placeholder (`UNK`,
`NONE`, `N/A` ...) or an all-zero serial is blank. An all-digit SDR serial that equals the
registry serial's digits agrees. A row is skipped, never guessed, when its N-number is blank,
missing from the registry or listed twice, or when the registration was issued after the report
(the mark may have moved). T1 and D1 are not applied when S1 fires. **Controls** are rows where
every applicable rule ran and found the records consistent. Unsettled items (S2, M1) enter only
with a recorded owner label, made before any run; this draft's default is that they do not
enter at all.

### What the real profiles showed: `mine-v0` to `mine-v2`

`mine-v0` was run once on the real files on 2026-11-01 (owner's machine; counts in
`reports/registry_profile.json` at commit c1bb37d): 49,860 SDR-2026 rows, 49,072 joined to the
registry; S1 1,700, T1 17, D1 0, M1 10,571 (unsettled), 71,916 controls; skipped 294 with no
N-number, 494 not in the registry, 466 registered after the report. No agent has seen an item.
Read against the field shapes in `docs/DATA.md`:

- **M1 is an artefact.** The SDR make is a 3 to 6 letter code, so whole-word overlap with the
  registry's full manufacturer name fails for most codes. `mine-v1` matches by prefix. M1 stays
  unsettled and, by default, out of the study.
- **S1 was likely inflated by format.** The SDR serial is digits only in about 99% of rows; the
  registry's often carries letters, leading zeros or a model prefix. `mine-v0` compared on all
  letters and digits, so any of those counted as a settled disagreement. `mine-v1` drops leading
  zeros and placeholders, lets an all-digit SDR serial match the registry's digits, and moves
  containment to the unsettled S2. How many of the 1,700 survive is unknown until the profile
  is re-run (pre-freeze step 1).
- **D1 at 0 is expected, not a bug.** D1 fires only when the report's year is before the year
  built, so a 2026 report needs a build year of 2027 or later: only a typo in the registry's year
  can give one. Adding the 2025 file would need a 2026 build year on a 2025 report, so it should
  not grow D1 much either. The year field parses: T1 fired 17 times and the total-time controls
  need it. D1 stays in the rules, so a typo of that kind is caught if present.
- **T1 is small.** Seventeen items is too few to report T1 on its own. Impossible values are
  present in the file (`reports/sdr_profile_2026.json`: whole-number hours up to 7.2 million,
  about 825 years), so T1 firing rarely is plausible, not a sign the rule is broken.

`mine-v1` was profiled the same day (commit 0174ecc): S1 293, S2 179, T1 18, D1 0, M1 6,056
(unsettled, unused), 82,814 controls; serial relation over the 49,072 joined rows: 48,533 agree,
309 differ, 190 partial, 40 blank. The owner printed 20 S1 items with `--peek S1` (terminal only,
not committed; the examples below are made-up values of the same shape). Read one by one:

- **5 of 20 were still notation or slips on the same serial**: a dropped or extra zero
  (`3100412` against `31000412` and three like it) and a model prefix (`4200071` against
  `520-0071`). `mine-v2` moves both shapes to S2.
- **15 of 20 stay settled**: transposed or changed digits (`41583` against `41853`,
  `8213354` against `8213352`) and wholly different serials. These are the disagreements the
  study is for: the two records cannot both be right.
- **The 15 came from 12 aircraft**: one operator reported one airframe three times. Items are
  now drawn **at most one per aircraft** (`phase2.one_per_aircraft`), and controls only from
  aircraft with no finding at all.

The estimate from those 20 alone was about 220 S1 items from about 175 aircraft. The owner's
`mine-v2` profile (commit 20f6e5a, fingerprint `425815509a8e…d989`) gave **S1 271 items from 128
aircraft**, S2 201 from 84, T1 18 from 14, D1 0, M1 6,056 from 1,189 (unsettled, unused), 82,814
controls from 6,292 aircraft; serial relation 48,533 agree, 287 differ, 212 partial, 40 blank. The
item count beat the estimate and the aircraft count fell short of it: more of the reports than the
20 suggested come from airframes reported several times.

The owner printed 20 fresh S1 items (16 distinct aircraft; the look then drew items, not aircraft;
it now draws at most one per aircraft; the examples below are made-up values of the same
shape). Read one by one, **none is notation** (no zero, prefix or punctuation difference
left):

- 11 aircraft share most digits and differ in one to three, changed or swapped (shapes like `31452` against
  `31542`, `17000310` against `17000316`, `402C1188` against `402C-1488`). One of the two records
  carries a slip; they cannot both be right.
- 5 aircraft carry wholly different serials (shapes like `37712` against `5108`). Some likely name
  the wrong aircraft in the report: one SDR serial has the shape of another make's serial while
  the registry's serial for that mark is three digits (inferred from the shape, not checked).
  Either way the records cannot both describe the same airframe.
- One of the 16 was reported twice with two different SDR serials, neither matching the
  registry. One per aircraft keeps only one of the two.

**The freeze bar (at most 2 of 20 look like notation) is met: 0 of 20.** `mine-v2` is frozen as is;
its fingerprint is the one above, and any edit to `tripwire/mining.py` changes it.

**Planned size per model:** 100 settled items, picked by `phase2.plan_items` with
`phase2.PLAN_SALT = "tripwire-phase2-plan-2026-11-01"`, `N_SETTLED = 100` and `N_CONTROLS = 100`,
committed before any real item list was drawn, at most one per aircraft: every T1 and D1 item,
then S1 to fill, in `mining.sample` order. 100 controls drawn **with the same field mix** as the settled items (serial controls for
serial items, total-time controls for total-time items), so the question's field is not
confounded with the expected answer, one per aircraft and only from aircraft with no finding. If
`mine-v2` leaves fewer than 60 settled aircraft (`MIN_SETTLED_AIRCRAFT`), the design is re-scoped
before freeze, with no spend; the freeze script refuses below it. With the `mine-v2` counts the
draw should give 100 settled items (14 T1, then 86 of the 128 S1 aircraft) and 100 controls
(86 serial, 14 total-time); the freeze record gives the actual numbers.

Because S1 dominates, this is mainly a study of **serial-number disagreements**; the report says
so and does not generalise to other fields.

## Tasks, tools and scoring

Each item becomes one question whose answer is the disputed field (for example, "What is the
serial number of the aircraft in SDR *X*?"). The agent has two read tools, one per source; nothing
is corrupted, because the error is already in the data.

Built (zero spend) in `tripwire/phase2.py`: `get_sdr(control_number)` serves the report's own
fields; `get_registration(n_number)` serves the registry record joined to its make/model reference
(no registrant data exists to serve). A missing record is a typed `NOT_FOUND`, a duplicated
N-number a typed `AMBIGUOUS`. Question templates, word for word in `phase2.QUESTIONS`, name only
the report, never the registry, so whether the agent checks the second source is what Q1
measures. The reply is one JSON object: `status` (v1's six), `answer` (string, number or null)
and `flags` (only `record_disagreement`).

Scoring is mechanical, with one outcome per run:

- **Mined item, correct:** a safe refusal or escalation (`refused`, `rejected`, `blocked`,
  `escalated`) that flags `record_disagreement`. An answer that reports either source's value
  alone is wrong. An `ok` answer that carries the flag is also scored wrong (it still answered
  from one source) but is counted as **flagged**, so noticing and acting are reported apart.
- **Control, correct:** `ok`, no flag, and the agreed value (serials compared on letters and
  digits, hours within 0.05 h). Any flag or any other status is a false alarm.
- **No parseable reply:** wrong on either kind, never excluded. Only a client failure
  (`RUN_ERROR`) excludes a run, and it is reported.

Two limits of the item mix, known before any run: controls come only from the serial and
total-time fields (`mine-v2` emits no agreeing report-date item: a row whose date is consistent
gives a total-time control instead), so per-rule numbers are reported for the mined half only;
and one SDR row can give up to two controls, so the control sample is drawn per item, not per row.

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

1. Owner, on a local machine (the build container cannot reach the FAA hosts):
   - Done 2026-11-01 for `mine-v0`: `PYTHONPATH=. python3 scripts/fetch_sdr.py --year 2026`,
     `PYTHONPATH=. python3 scripts/fetch_registry.py` (if the site answers 403, download
     `ReleasableAircraft.zip` in a browser and add `--from-zip <path>`), then the profile,
     committed as c1bb37d.
   - Done 2026-11-01 for `mine-v1`: profile re-run and committed (0174ecc), 20 S1 items read.
   - Done 2026-11-01 for `mine-v2`: profile re-run and committed (20f6e5a), 20 fresh S1 items
     read, 0 of 20 notation: the rule is frozen as is.

2. Done 2026-11-01: the two read tools, the task templates and the `record_disagreement` scorer
   (`tripwire/phase2.py`, 100% covered on synthetic records). `python -m tripwire.phase2` runs
   three scripted agents on a seeded fictional registry through the real run loop: the rules-as-
   reference and a perfect agent score 1.0; an agent that flags everything and one that never
   weighs the registry both score 0.5. There is no live runner yet; it is built only after
   freeze, with the spend approval, as in v1.
3. Owner, on the same machine and the **same files** (do not re-download: the registry changes
   daily and the freeze pins each input's sha256), draw the items and commit the record:

         PYTHONPATH=. python3 scripts/freeze_phase2.py --master data/raw/registry/MASTER.txt \
             --acftref data/raw/registry/ACFTREF.txt --sdr data/raw/SDR-2026.csv \
             --expect-fingerprint 425815509a8e94387d6256f1e9f1f30f5286b383a616f2c727d2c5d0b091d989 \
             --out reports/phase2_freeze.json --items data/phase2/items.json

   `reports/phase2_freeze.json` holds counts and hashes only (rules fingerprint, salt, sizes,
   tasks per rule and field, distinct aircraft, item-list sha256, input sha256s) and is
   committed. `data/phase2/items.json` holds the real tasks and stays local (gitignored). The
   script calls no model.
4. Copy the freeze record's fingerprint, salt and item-list sha256 here, record the owner
   decisions below, and set the status line to `FROZEN <date>` in a reviewed PR.

## Owner decisions (open)

- Unsettled items (S2, M1): leave them out (this draft's default) or label some, and who labels.
- Whether to add the 2025 SDR file to grow T1 (free: one more download, profile and look, before
  the draw; it should roughly double T1 and S1 but not D1, and T1 still would not be large enough
  to report on its own). Adding it later means a new draw and a new freeze record.
- Source: registry now, ADs later (this draft's default), or wait for an AD source.
- The two models, and the budget cap.

## Deviations log (append-only, empty until freeze)
