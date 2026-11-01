#!/usr/bin/env python
"""Profile the downloaded registry files and the mining rules against real SDRs: counts only.

    PYTHONPATH=. python3 scripts/profile_registry.py --master data/raw/registry/MASTER.txt \\
        --acftref data/raw/registry/ACFTREF.txt --sdr data/raw/SDR-2026.csv \\
        --out reports/registry_profile.json

The report holds header names, row and reject counts, reject reasons by kind (never the value),
join coverage, and the number of items each mining rule settles, leaves unsettled or skips, plus
the rules' version and fingerprint. It never holds a row, a mark, a serial or a name, so it is
safe to commit. This is an owner step: the FAA hosts are not reachable from the build container.
Fails closed (exit 2) on a file the loaders refuse.

``--peek RULE`` instead prints a spread of that rule's items (serials, dates, makes; never a
registrant) to the terminal so the owner can check a rule by eye; it writes nothing.
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


def peek(master: Path, acftref: Path, sdrs: Sequence[Path], rule: str, n: int) -> list[str]:
    """Up to ``n`` mined items of ``rule`` as tab-separated lines, for the owner's eyes only:
    printed to the terminal, never written to the report. A spread, not the first rows."""
    reg = registry.Registry.build(
        registry.load_master(master).records, registry.load_reference(acftref).records
    )
    rows = [r for p in sdrs for r in load_sdr_csv(p).records]
    mined = mining.mine(rows, reg)
    pool = [*mined.settled, *mined.unsettled, *mined.controls]
    picked = mining.sample([i for i in pool if i.rule == rule], n, salt="peek")
    head = "rule\tcontrol_number\tn_number\tfield\tsdr_value\tregistry_value"
    return [head, *(f"{i.rule}\t{i.control_number}\t{i.n_number}\t{i.field}\t{i.sdr_value}\t"
                    f"{i.registry_value}" for i in picked)]  # fmt: skip


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
    joined = [(r, reg.lookup(r.registry_n_number)) for r in rows if r.registry_n_number]
    joined = [(r, rec) for r, rec in joined if rec is not None]
    found = len(joined)
    relations = Counter(
        str(mining.serial_relation(r.aircraft_serial_number, rec.serial_number))
        for r, rec in joined
    )
    serials = {
        "sdr_digits_only": sum(1 for r, _ in joined if mining.serial_key(
            r.aircraft_serial_number).isdigit()),
        "registry_with_letters": sum(1 for _, rec in joined if any(
            ch.isalpha() for ch in mining.serial_key(rec.serial_number))),
        "relation": dict(sorted(relations.items())),  # "None" = a blank or placeholder side
    }  # fmt: skip
    return {
        "master": {"header": list(m.header), "rows": len(m.records),
                   "rejects": len(m.rejects), "reject_kinds": _kinds(m.rejects),
                   "duplicate_n_numbers": len(reg.duplicate_n_numbers)},
        "acftref": {"header": list(a.header), "rows": len(a.records),
                    "rejects": len(a.rejects), "reject_kinds": _kinds(a.rejects)},
        "sdr": {"files": [p.name for p in sdrs], "rows": len(rows), "rejects": sdr_rejects,
                "with_n_number": with_n, "found_in_registry": found,
                "with_total_time": sum(1 for r in rows if r.aircraft_total_time_hours is not None)},
        "serials": serials,
        "mining": {"version": mining.RULES_VERSION, "fingerprint": mining.fingerprint(),
                   "counts": mined.counts()},
    }  # fmt: skip


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--master", type=Path, required=True)
    p.add_argument("--acftref", type=Path, required=True)
    p.add_argument("--sdr", type=Path, action="append", required=True)
    p.add_argument("--out", type=Path, default=None)
    p.add_argument(
        "--peek",
        default=None,
        metavar="RULE",
        help="print a sample of one rule's items (S1, S2, T1, D1, M1, control) "
        "to the terminal instead of profiling; nothing is written",
    )
    p.add_argument("--peek-n", type=int, default=20)
    args = p.parse_args(argv)
    try:
        if args.peek:
            if args.peek not in (*mining.RULES, "control"):
                p.error(f"--peek takes one of {', '.join((*mining.RULES, 'control'))}")
            print("\n".join(peek(args.master, args.acftref, args.sdr, args.peek, args.peek_n)))
            return 0
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
