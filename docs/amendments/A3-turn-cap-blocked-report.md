# Exploratory amendment A3: a gated run that hits the turn cap after a block records the gate's report

**Status:** post-hoc, exploratory. **Decided after seeing the gate-confirm-1 (+1b) live results**
([docs/results/gate-confirm-1.md](../results/gate-confirm-1.md)), which showed 3 faulted gate runs
scored incorrect only because the agent hit the turn cap after the gate had already blocked the
planted fault. This is NOT part of the preregistered analysis in `docs/PREREGISTRATION.md`, which is
unchanged. Numbers re-scored under A3 are labelled A3 and quoted beside, never instead of, the
pre-A3 numbers.

**2026-10-13, OWNER-APPROVED.**

**Rule.** In the gate condition, when a run ends at `MAX_TURNS` with no reply (`turn_cap_hit`), no
`RUN_ERROR`, and the gate has already recorded at least one failure (`gate.blocked`), the run's
verdict is the gate's fail-closed blocked report (`Gate.report`: status `blocked`, flags = the
vocabulary terms for the faults its checks named), adjudicated as usual. Implemented in
`tripwire/harness.py` (`run_one`); recorded receipts are re-scored offline by `tripwire/rescore.py`
(re-scored rows carry `gate.a3_turn_cap_report = true`).

**Rationale.** The gate is designed to fail closed: once a check fails, no answer derived from that
data may pass, and the system's output is a block naming the fault. Before A3 the block was only
emitted by rewriting the agent's final reply, so a run with no final reply produced *no output at
all* even though the gate had already decided to block. A3 makes the recorded verdict what the
gated system would actually emit. It cannot create a pass on wrong data: the verdict is always a
refusal, scored correct only if it names every injected fault (adjudicator rules unchanged), and on
a clean run a block is still a false flag.

**What changes in scoring (and what does not).**

* Changes: only gate-condition rows matching the rule above, from `verdict: null` (incorrect) to a
  `blocked` verdict adjudicated normally. `turn_cap_hit` stays `true` on the receipt.
* Unchanged: turn-cap runs where the gate blocked nothing (still `verdict: null`, incorrect, not a
  false flag); `RUN_ERROR` and other exclusions; `BASELINE_FAIL`; the adjudicator; the strict floor
  / published definitions; CIs; cost (A3 adds no model call, tokens or spend).

**Symmetry with baseline and prompt_fix.** Each condition is scored on what that system itself emits.
Baseline and prompt_fix have no gate, so a turn-capped run there emits nothing and stays incorrect;
A3 does not invent an output for them, and no condition gets a verdict from information it did not
produce. The asymmetry is the treatment being measured (the gate is an extra fail-closed component),
not a scoring advantage. For transparency, turn-cap counts in the recorded data: prompt_fix 14 (run
`38253007798`), baseline 0, gate 6 (run `38285916667`, none blocked as recorded) and 3
(gate-confirm-1, all blocked).

**Disclosure: A1's offline replay already behaved like A3.** `tripwire/gate_replay.py` assigns the
blocked verdict to any replayed gate row whose (old or new) checks block, without regard to the turn
cap. Three turn-capped faulted slots of run `38285916667` (T26-r01 F3, T25-r06 F3, T25-r03 F1) became
correct in the A1 "after" numbers this way. So A1's predicted 100% published already assumed A3; the
gate-confirm-1 shortfall (93.3%) is the live harness not doing so. A1's numbers are not changed here.

## Offline re-scoring impact (zero live spend)

`python scripts/gate_confirm_report.py <38253007798 run> <38285916667 run> <gate-confirm-1 dir>
<gate-confirm-1b dir> --a3` writes `reports/*_a3.{md,json}`. Rows changed by A3:

| run | rows | turn-cap rows | changed by A3 |
| --- | --- | --- | --- |
| `38253007798` (baseline + prompt_fix) | 480 | 14 (prompt_fix, no gate) | 0 |
| `38285916667` (gate, recorded) | 186 | 6 (gate blocked none) | 0 |
| `38398347678` (gate-confirm-1) | 53 | 3 (all blocked) | **3**: T26-r05 F1, T22-r03 F5, T22-r05 F6 → correct |
| `38421505642` (gate-confirm-1b) | 7 | 0 | 0 |

Live gate on the 60 gate-confirm-1 slots (comparators unchanged by A3):

| | baseline | prompt_fix | gate, pre-A3 | **gate, A3** |
| --- | --- | --- | --- | --- |
| All faults, strict | 72.9% | 75.0% | 87.5% [72.9–100.0] | **93.8% [80.4–100.0]** |
| All faults, published | 72.9% | 81.8% | 93.3% [83.7–100.0] | **100.0% [100.0–100.0]** |
| F1 / F5 / F6 strict | 37.5% / — / — | 75.0% / — / — | 87.5% / 87.5% / 86.7% | **100% / 100% / 93.3%** |
| Clean false-flag, strict | 0.0% | 25.0% | 0.0% | **0.0%** |
| $/correct, published | $0.0241 | $0.0708 | $0.0732 | **$0.0683** |

Full 240-slot gate plan (186 A1-replayed + 54 live; mixed, as in the results doc): all faults
strict 72.4% → **73.5%**, published 98.5% → **100.0%**, clean false-flag 1.8% / 0.0% (unchanged),
$/correct published $0.0596 → **$0.0588**. The remaining strict gap is the unchanged exclusions
(`BASELINE_FAIL` 45, `RUN_ERROR` 4), which count as incorrect and are not the gate's to fix.
