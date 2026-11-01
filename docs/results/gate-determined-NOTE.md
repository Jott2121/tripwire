# Note: the gate's headline numbers are set by the code (2026-11-01)

**Claim.** In v1 and in the v2 plan, the store cross-check gate's catch rate is fixed before any
model runs. A paid run cannot measure it; it can only confirm that the fault fired and the harness
ran. The generated table is [`gate-determined.md`](gate-determined.md)
(`python -m tripwire.gate_determined`, about a minute, no model, no network, no spend).

## Why, from the code

- `Gate(store, crosscheck=True)` compares every served quantity with the **same clean store** the
  tools read from (`tripwire/gate.py`, `_crosscheck`). A fired F1, F3, F7 or F8 changes what is
  served but not the store, so the comparison always fails and the gate blocks, naming the fault.
  The classifier imports the fault design's own decimal factors (`faults.DECIMAL_FACTORS`) and
  looks for another record's value to name F7, so it names exactly the planted code.
- F2 is caught against the store's registry of current revisions, and F4, F9 and write-side F5
  by a read-back the gate makes itself. None of these depends on what the agent does.
- On a clean run the served data equals the store, so the gate never blocks. If the agent refused
  and every reason it gave is a value-disagreement code, `downgrade_clean` rewrites the refusal to
  `ok`. That removes value-flag false alarms using the same trusted store.

## What the scripted agents show

Four agents that behave as differently as possible (always right, always confidently wrong,
always refusing with every value code, always refusing with a stale-revision code) run through
the real harness, injector, gates and adjudicator: 96 runs on the v1 holdout's 24 slots (plus the
live gate condition) and 1,920 on the v2 plan's 480 slots. 0 excluded. Every check below is
recomputed from the table each time it is generated.

1. **The cross-check gate scores 100% on every catchable fault for all four agents**, on v1 and
   v2. Its lift over the agent is therefore 1 minus the agent's own rate, by construction.
2. **The contract-only gate adds nothing on value faults**: its F1/F3/F7/F8 rate equals the
   agent's own rate exactly, for every agent.
3. **v1's live gate condition equals the shadow cross-check** on every cell.
4. **False alarms.** A value-code refusal on clean data is cut from 100% to 25% (v2) or 17% (v1)
   by the downgrade; a stale-revision refusal survives every gate at 100%. So a gate's low false
   flag rate partly reflects which codes the agent happened to use.
5. **Blanket flagging is rewarded.** An agent that refuses every task naming all four value codes
   is scored 100% correct on every value fault without a gate, because a refusal that names extra
   codes still counts. The v2 scoring rule would need to penalise extra codes, or score "any
   value-disagreement flag" as one outcome, before an ungated rate means much.

## How this fits the committed v1 results

- Holdout-1 (A4, `reports/holdout_1.md`): gate 18/18 correct on faulted runs. Point 1 predicts
  exactly that for any agent, so the 100% is a property of the gate and the fault design, not
  evidence about grok-4.7.
- Holdout clean runs: gate 0/6 false flags against prompt_fix's 1/6 (the gate condition uses the
  prompt_fix prompt). That gap is consistent with the downgrade in point 4, but which codes the
  agent used is in the run receipts, which are not committed; this note does not verify it.
- v1's H1 (an ungated agent gets silent writes right under 10% of the time) was refuted: baseline
  F4 was 80% [61-95] correct on 20 runs (`reports/gate_240_mixed.md`) and 3/3 in the holdout.

## Replaying the recorded v1 runs

The holdout receipts are in the Actions artifact of run `38434106473` (kept until 2027-01-11).
This session could not download it (the artifact host is not reachable from here), so the replay
is a step for the owner's machine:

```
gh run download 38434106473 --dir .receipts/38434106473
```

The issue #44 replay (`tripwire/gate_replay.py`, pinned by `tests/test_gate_replay_recorded.py`)
already re-scores recorded runs offline the same way.

## What a paid run would still measure

Only the ungated agent's own behaviour per model and fault, and what the warning prompt costs in
tokens and false alarms. Published work already reports silent-corruption rates across 6 to 10
models (see the prior-work section of `docs/PREREGISTRATION-v2-multimodel.md`), which is why the
v2 stage-one spend was not made.
