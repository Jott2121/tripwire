# Tripwire in 60 seconds

A script to read aloud over the screen (about 160 words, one minute at a steady pace). Every
number comes from a committed file, linked under each beat.

**1. The problem (0:00 to 0:10).** *Show: the README headline.*
"An AI agent never sees raw data. It sees whatever its tools hand back. If a tool quietly
converts millimetres to inches, does the agent notice?"

**2. One run, three ways (0:10 to 0:35).** *Show: the table in [demo-1](results/demo-1.md).*
"Here is one task: the crack-length limit for an aircraft bracket. The truth is 0.49 inches. The
tool serves 12.446, still labelled inches. With no warning, the agent reported 12.446 all three
times. Twice it even said the unit looked swapped, and reported the wrong number anyway.
Told to check its data, it got all three right. With the gate, all three were blocked by
construction, because the gate checks served values against the trusted records."
([demo-1](results/demo-1.md): 9 live runs, $0.55, a demo, not a result)

**3. Does it hold up? (0:35 to 0:50).** *Show: the holdout table.*
"On a 72-job test with pass marks fixed before it ran, the gate was right on every corrupted job,
against 61% for the plain agent, with no false alarms on clean data."
([holdout-1](results/holdout-1.md), [A4](amendments/A4-holdout-confirmation.md))

**4. The honest part (0:50 to 1:00).** *Show: the caveats line.*
"Small sample, one model, and no AI judge anywhere: every verdict is computed from the record of
what was planted. Replay it all with `make demo`, at zero cost."

## Replay what the script shows

```bash
make demo   # re-renders docs/results/demo-1.md and the cost chart from committed records
```

The demo's records are [`runs/demo-1/`](../runs/demo-1/) (receipt and full transcripts); its nine
spend rows are in [`runs/spend_ledger.jsonl`](../runs/spend_ledger.jsonl).
