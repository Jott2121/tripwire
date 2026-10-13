"""Read-only gate-confirm-1 (+1b) analysis (amendment A2): zero live spend.

Usage (after ``gh run download`` of runs 38253007798, 38285916667, 38398347678, 38421505642)::

    python scripts/gate_confirm_report.py PRIOR_RUN OLD_GATE_RUN LIVE_DIR... --out reports

Writes two ``tripwire.report`` reports and prints the top-up slot comparison and the live misses:

* ``gate_confirm_1_same_slots``: the live gate runs vs baseline and prompt_fix on the same slots.
* ``gate_240_mixed``: the 186 offline-replayed gate slots (A1) plus the 54 live skipped slots.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from tripwire import gate_replay as g
from tripwire import report as r
from tripwire import rescore


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("prior_dir", type=Path)
    ap.add_argument("old_gate_dir", type=Path)
    ap.add_argument("live_dirs", type=Path, nargs="+")
    ap.add_argument("--targets", type=Path, default=Path("runs/targets/gate-confirm-1.json"))
    ap.add_argument("--out", type=Path, default=Path("reports"))
    ap.add_argument("--a3", action="store_true", help="re-score live rows under amendment A3")
    a = ap.parse_args()
    sfx = "_a3" if a.a3 else ""
    doc = json.loads(a.targets.read_text())
    live, live_src = [], []
    for d in a.live_dirs:
        rows, src = r.load_records([d])
        live += rescore.rescore(rows, d / "ledgers") if a.a3 else rows
        live_src += src
    prior, prior_src = r.load_records([a.prior_dir])
    comp = g._filter_to_slots(prior, {g.slot(x["run_id"]) for x in live})
    r.write_report(
        r.analyse(comp + live, prior_src + live_src), a.out, "gate_confirm_1_same_slots" + sfx
    )

    before, after, _ = g.replay(a.prior_dir, a.old_gate_dir)
    bf = {g.slot(x["run_id"]): x for x in before if x["condition"] == "gate"}
    af = {g.slot(x["run_id"]): x for x in after if x["condition"] == "gate"}
    lv = {g.slot(x["run_id"]): x for x in live}
    print("top-up slot | fault | recorded gate | offline replay | live")
    for s in sorted(g.slot(j) for j in doc["topup_job_ids"]):
        f = lv[s]["planned_fault"] or "clean"
        print(s, f, r._correct(bf[s]), r._correct(af[s]), r._correct(lv[s]), sep=" | ")
    print("live faulted misses:")
    for x in live:
        if x["planned_fault"] and not r._correct(x):
            print(g.slot(x["run_id"]), x["planned_fault"], "turn_cap_hit", x["turn_cap_hit"],
                  "gate_blocked", x["gate"]["blocked"], "verdict", x["verdict"])  # fmt: skip

    skipped = {g.slot(j) for j in doc["skipped_job_ids"]}
    gate = [x for x in after if x["condition"] == "gate"]
    gate += [x for x in live if g.slot(x["run_id"]) in skipped]
    comp240 = g._filter_to_slots(prior, {g.slot(x["run_id"]) for x in gate})
    r.write_report(r.analyse(comp240 + gate, prior_src + live_src), a.out, "gate_240_mixed" + sfx)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
