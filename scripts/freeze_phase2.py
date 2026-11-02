#!/usr/bin/env python
"""Draw the phase two item list on the owner's files and record what the preregistration freezes.

    PYTHONPATH=. python3 scripts/freeze_phase2.py --master data/raw/registry/MASTER.txt \\
        --acftref data/raw/registry/ACFTREF.txt --sdr data/raw/SDR-2026.csv \\
        --expect-fingerprint <the fingerprint in reports/registry_profile.json> \\
        --out reports/phase2_freeze.json --items data/phase2/items.json

Runs the frozen mining rules and ``phase2.plan_items`` with the salt and sizes committed in
``tripwire/phase2.py`` (``PLAN_SALT``, ``N_SETTLED``, ``N_CONTROLS``), so the draw cannot be
re-rolled after seeing it. Two outputs:

* ``--items``: the drawn tasks, which hold real serials and dates. Local only: ``data/phase2/`` is
  gitignored, and the script refuses an ``--items`` path outside it.
* ``--out``: counts and hashes only, safe to commit: rules version and fingerprint, salt and
  sizes, tasks per rule and field, distinct aircraft, the item-list sha256 and the sha256 of each
  input file. The registry is a daily snapshot, so the freeze pins the exact copy used: keep
  those files; a fresh download gives a different hash and a different list.

No model is called and nothing is spent. Fails closed (exit 2, nothing written) when a loader
refuses a file, the rules' fingerprint differs from ``--expect-fingerprint``, or the plan has
fewer than ``MIN_SETTLED_AIRCRAFT`` settled aircraft (the preregistration's re-scope bar).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from tripwire import mining, phase2, registry
from tripwire.sdr import SDRFormatError, load_sdr_csv

ITEMS_DIR = Path("data/phase2")


class FreezeRefused(RuntimeError):
    pass


def _sha256(path: Path) -> dict[str, object]:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return {"name": path.name, "bytes": path.stat().st_size, "sha256": h.hexdigest()}


def _by(tasks: Sequence[phase2.Task], attr: str) -> dict[str, int]:
    return dict(sorted(Counter(getattr(t.item, attr) for t in tasks).items()))


def draw(
    master: Path, acftref: Path, sdrs: Sequence[Path], expect_fingerprint: str | None
) -> tuple[dict, list[phase2.Task]]:
    """The freeze record (counts and hashes) and the drawn tasks. Raises FreezeRefused."""
    fp = mining.fingerprint()
    if expect_fingerprint is not None and fp != expect_fingerprint:
        raise FreezeRefused(f"mining rules fingerprint is {fp}, expected {expect_fingerprint}")
    reg = registry.Registry.build(
        registry.load_master(master).records, registry.load_reference(acftref).records
    )
    rows = [r for p in sdrs for r in load_sdr_csv(p).records]
    mined = mining.mine(rows, reg)
    settled, controls = phase2.plan_items(
        mined, phase2.N_SETTLED, phase2.N_CONTROLS, phase2.PLAN_SALT
    )
    if len(settled) < phase2.MIN_SETTLED_AIRCRAFT:
        raise FreezeRefused(
            f"{len(settled)} settled aircraft, fewer than {phase2.MIN_SETTLED_AIRCRAFT}: "
            "re-scope before freeze"
        )
    s_tasks, c_tasks = phase2.build_tasks(settled), phase2.build_tasks(controls)
    tasks = phase2.build_tasks([*settled, *controls])  # also checks ids are unique across both
    record = {
        "rules": {"version": mining.RULES_VERSION, "fingerprint": fp},
        "plan": {"salt": phase2.PLAN_SALT, "n_settled": phase2.N_SETTLED,
                 "n_controls": phase2.N_CONTROLS,
                 "min_settled_aircraft": phase2.MIN_SETTLED_AIRCRAFT},
        "inputs": {"master": _sha256(master), "acftref": _sha256(acftref),
                   "sdr": [_sha256(p) for p in sdrs]},
        "pools": {"counts": mined.counts(), "aircraft": mined.aircraft()},
        "settled": {"tasks": len(s_tasks), "by_rule": _by(s_tasks, "rule"),
                    "by_field": _by(s_tasks, "field"),
                    "aircraft": len({t.item.n_number for t in s_tasks})},
        "controls": {"tasks": len(c_tasks), "by_field": _by(c_tasks, "field"),
                     "aircraft": len({t.item.n_number for t in c_tasks})},
        "tasks": len(tasks),
        "item_list_sha256": phase2.item_list_sha256(tasks),
    }  # fmt: skip
    return record, tasks


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def main(argv: list[str] | None = None, *, items_dir: Path = ITEMS_DIR) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--master", type=Path, required=True)
    p.add_argument("--acftref", type=Path, required=True)
    p.add_argument("--sdr", type=Path, action="append", required=True)
    p.add_argument("--expect-fingerprint", default=None)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--items", type=Path, required=True)
    args = p.parse_args(argv)
    if not _inside(args.items, items_dir):
        print(f"REFUSED: --items must be under {items_dir}/ (gitignored)", file=sys.stderr)
        return 2
    try:
        record, tasks = draw(args.master, args.acftref, args.sdr, args.expect_fingerprint)
    except (registry.RegistryFormatError, SDRFormatError, FreezeRefused) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    items = {"item_list_sha256": record["item_list_sha256"],
             "tasks": [{"task_id": t.task_id, "question": t.question, "item": asdict(t.item)}
                       for t in tasks]}  # fmt: skip
    args.items.parent.mkdir(parents=True, exist_ok=True)
    args.items.write_text(json.dumps(items, indent=2, sort_keys=True) + "\n")
    text = json.dumps(record, indent=2, sort_keys=True) + "\n"
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text)
    print(text, end="")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
