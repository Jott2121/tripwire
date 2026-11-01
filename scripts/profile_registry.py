#!/usr/bin/env python
"""Profile the downloaded registry files and the mining rules against real SDRs: counts only.

    python scripts/profile_registry.py --master data/raw/registry/MASTER.txt \\
        --acftref data/raw/registry/ACFTREF.txt --sdr data/raw/SDR-2026.csv \\
        --out reports/registry_profile.json

The report holds header names, row and reject counts, reject reasons by kind (never the value),
join coverage, and the number of items each mining rule settles, leaves unsettled or skips, plus
the rules' version and fingerprint. It never holds a row, a mark, a serial or a name, so it is
safe to commit. This is an owner step: the FAA hosts are not reachable from the build container.
Fails closed (exit 2) on a file the loaders refuse.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from tripwire import mining, registry
from tripwire.sdr import SDRFormatError, load_sdr_csv


def _kinds(rejects: Sequence) -> dict[str, int]:
    """Reject reasons by kind: the text before any ':' (values are dropped)."""
    return dict(sorted(Counter(r.reason.split(":")[0] for r in rejects).items()))


def profile(master: Path, acftref: Path, sdrs: Sequence[Path]) -> dict:
    m = registry.load_master(master)
    a = registry.load_reference(acftref)
    reg = registry.Registry.build(m.records, a.records)
    rows, sdr_rejects = [], 0
    for p in sdrs:
        loaded = load_sdr_csv(p)
        rows += loaded.records
        sdr_rejects += len(loaded.rejects)
    mined = mining.mine(rows, reg)
    with_n = sum(1 for r in rows if r.registry_n_number)
    found = sum(1 for r in rows if r.registry_n_number and reg.lookup(r.registry_n_number))
    return {
        "master": {"header": list(m.header), "rows": len(m.records),
                   "rejects": len(m.rejects), "reject_kinds": _kinds(m.rejects),
                   "duplicate_n_numbers": len(reg.duplicate_n_numbers)},
        "acftref": {"header": list(a.header), "rows": len(a.records),
                    "rejects": len(a.rejects), "reject_kinds": _kinds(a.rejects)},
        "sdr": {"files": [p.name for p in sdrs], "rows": len(rows), "rejects": sdr_rejects,
                "with_n_number": with_n, "found_in_registry": found},
        "mining": {"version": mining.RULES_VERSION, "fingerprint": mining.fingerprint(),
                   "counts": mined.counts()},
    }  # fmt: skip


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--master", type=Path, required=True)
    p.add_argument("--acftref", type=Path, required=True)
    p.add_argument("--sdr", type=Path, action="append", required=True)
    p.add_argument("--out", type=Path, default=None)
    args = p.parse_args(argv)
    try:
        rep = profile(args.master, args.acftref, args.sdr)
    except (registry.RegistryFormatError, SDRFormatError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    text = json.dumps(rep, indent=2, sort_keys=True) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text)
    print(text, end="")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
