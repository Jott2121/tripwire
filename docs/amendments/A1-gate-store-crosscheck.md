# Exploratory amendment A1: gate store cross-check for F1/F3 (issue #44)

**Status:** post-hoc, exploratory. Written after gate run 38285916667. This is NOT part of the preregistered analysis in `docs/PREREGISTRATION.md`, which is unchanged. Results under this amendment are labelled exploratory.

**2026-10-12, issue #44, gate store cross-check for F1/F3 (OWNER-DIRECTED; after the gate run
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
