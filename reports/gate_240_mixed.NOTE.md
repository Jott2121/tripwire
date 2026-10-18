# Note on `reports/gate_240_mixed.md`

`reports/gate_240_mixed.md` is generated (`python -m tripwire.report`, invoked from
`scripts/gate_confirm_report.py`). Do not hand-edit the generated file; this note is the
clarification.

The opening `INCOMPLETE: run halted in gate-confirm-1-38398347678` banner is emitted because
that source directory's `result.json` exit is not `OK` or `RUN_ERRORS` (`tripwire/report.py`).
It does not mean the baseline or prompt arm is unfinished.

- **Baseline and prompt rows** are the complete 480-record pre-registered run (source directory
  `run`, 240 per condition). The H2 correctness figures (strict 65.4% / 63.2%, clean false
  flags 1.8% / 14.5%) are those rows.
- **Gate rows** are the exploratory mixture in
  [`docs/results/gate-confirm-1.md`](../docs/results/gate-confirm-1.md) §3: offline replay of
  the halted pre-registered gate run, plus the live gate-confirm slots that complete the
  240-slot plan. The committed source line names `run` (480 records),
  `gate-confirm-1-38398347678` (53), and `gate-confirm-1b-38421505642` (7). Analysed records
  are 720 (240 per condition). The gate column is not the pre-registered gate result; that
  result is the holdout.
