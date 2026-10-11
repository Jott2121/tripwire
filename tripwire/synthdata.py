"""Fixed-seed synthetic dataset: defects, lots, spec limits, test results, work orders.

    python -m tripwire.synthdata --seed 1 --out runs/synth-seed1.json

Everything is fictional (makes, suppliers, part numbers) and shaped by
``tripwire.standards``: control-number ids, four-digit JASC codes, prefix-less
registration marks, whole-hour total times, crack lengths in inches. The same
``(seed, sizes)`` is byte-identical in canonical JSON. Public spec rows come
from :mod:`tripwire.public_specs`; synthetic specs carry a revision history
(``r1`` superseded by ``r2``) so stale-revision faults have something to be
stale against. No real SDR rows are used.
"""

from __future__ import annotations

import argparse
import random
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

from tripwire import standards as s
from tripwire.contract import (
    Defect,
    Lot,
    Provenance,
    Quantity,
    SpecLimit,
    TestResult,
    WorkOrder,
    canonical_json,
)
from tripwire.public_specs import public_spec_limits

GENERATOR_URL = "https://github.com/Jott2121/tripwire/blob/main/tripwire/synthdata.py"
EPOCH = datetime(2026, 1, 1, tzinfo=UTC)  # fixed so provenance is deterministic
SDR_OFFSET = timezone(timedelta(hours=-5))  # SubmissionDate offset in real files (fixed -05:00)
DESIGNATOR = "TWSY"  # fictional 4-char submitter designator
MAKES = ("ACME AERO", "ZENITH AERO", "ORION AERO", "VEGA AERO")
SUPPLIERS = ("NORTHWIND PARTS", "BLUE MESA SUPPLY", "HARBORLIGHT AEROSPACE")
PARTS = (
    ("BRACKET", "PN-10001"),
    ("ACTUATOR", "PN-10002"),
    ("FITTING", "PN-10003"),
    ("PANEL", "PN-10004"),
    ("BEARING", "PN-10005"),
    ("VALVE", "PN-10006"),
)
CONDITIONS = ("CRACKED", "WORN", "CORRODED", "FAILED", "LEAKING")
JASC = tuple(sorted(s.JASC_TITLES.codes))
ACTIONS = ("replace part", "inspect and blend", "re-torque and re-inspect", "quarantine lot")
_LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ"


@dataclass(frozen=True)
class Dataset:
    seed: int
    defects: tuple[Defect, ...]
    lots: tuple[Lot, ...]
    spec_limits: tuple[SpecLimit, ...]
    test_results: tuple[TestResult, ...]
    work_orders: tuple[WorkOrder, ...]


def _prov(seed: int, source: str) -> Provenance:
    return Provenance(source, GENERATOR_URL, f"seed={seed}", EPOCH)


def _registry(rng: random.Random) -> str:
    """A prefix-less mark valid under 14 CFR 47.15 (digits, or digits + 1-2 letters)."""
    digits = str(rng.randint(1, 999))
    tail = "".join(rng.choice(_LETTERS) for _ in range(rng.randint(0, 2)))
    return digits + tail


def _synthetic_specs(seed: int, rng: random.Random) -> list[SpecLimit]:
    """Per-part crack-length maxima with an r1 -> r2 history; r2 is current."""
    out: list[SpecLimit] = []
    prov = _prov(seed, "tripwire-synth-spec")
    for name, pn in PARTS:
        r2 = round(rng.uniform(0.25, 2.0), 2)
        r1 = round(r2 * rng.choice((1.5, 2.0)), 2)  # the superseded limit was looser
        for rev, value, superseded in (("r1", r1, "r2"), ("r2", r2, None)):
            out.append(
                SpecLimit(
                    spec_id=f"TW-SRM-{pn}",
                    revision=rev,
                    parameter="crack_length_max",
                    applies_to=f"{name} {pn}",
                    source_url=GENERATOR_URL,
                    provenance=prov,
                    maximum=Quantity(value, s.Unit.INCH),
                    superseded_by=superseded,
                )
            )
    return out


def generate(seed: int, n_defects: int = 30, n_lots: int = 12, n_work_orders: int = 5) -> Dataset:
    """Build a dataset deterministically from ``seed``; sizes must be non-negative."""
    if min(n_defects, n_lots, n_work_orders) < 0:
        raise ValueError("sizes must be non-negative")
    rng = random.Random(seed)
    specs = list(public_spec_limits()) + _synthetic_specs(seed, rng)
    current = {sp.applies_to: sp for sp in specs if sp.is_current and sp.spec_id.startswith("TW-")}

    defects: list[Defect] = []
    dprov = _prov(seed, "tripwire-synth-defect")
    for i in range(n_defects):
        name, pn = rng.choice(PARTS)
        difficulty = date(2026, 1, 1) + timedelta(days=rng.randint(0, 240))
        submission = datetime.combine(
            difficulty, datetime.min.time(), tzinfo=SDR_OFFSET
        ) + timedelta(days=rng.randint(0, 3), hours=rng.randint(6, 20))
        condition = rng.choice(CONDITIONS)
        crack = (
            Quantity(round(rng.uniform(0.1, 3.0), 2), s.Unit.INCH)
            if condition == "CRACKED"
            else None
        )
        defects.append(
            Defect(
                defect_id=f"{DESIGNATOR}{submission:%Y%m%d}{i + 1:04d}",
                difficulty_date=difficulty,
                submission_date=submission,
                jasc_code=rng.choice(JASC),
                registry=_registry(rng),
                part_number=pn,
                part_condition=condition,
                discrepancy=f"{name} found {condition} on {rng.choice(MAKES)} airframe",
                provenance=dprov,
                crack_length=crack,
                aircraft_total_time=Quantity(rng.randint(100, 40000), s.Unit.HOUR),
                aircraft_total_cycles=Quantity(rng.randint(10, 30000), s.Unit.CYCLE),
            )
        )

    lots: list[Lot] = []
    tests: list[TestResult] = []
    lprov, tprov = _prov(seed, "tripwire-synth-lot"), _prov(seed, "tripwire-synth-test")
    for i in range(n_lots):
        name, pn = rng.choice(PARTS)
        lot = Lot(
            f"LOT-{seed}-{i + 1:03d}",
            pn,
            rng.randint(5, 200),
            date(2026, 1, 1) + timedelta(days=rng.randint(0, 200)),
            rng.choice(SUPPLIERS),
            lprov,
        )
        lots.append(lot)
        spec = current[f"{name} {pn}"]
        limit_in = spec.maximum.value  # type: ignore[union-attr]
        measured_in = round(limit_in * rng.uniform(0.3, 1.3), 3)
        # Some results are recorded in millimetres: the unit travels with the value.
        measured = Quantity(measured_in, s.Unit.INCH)
        if rng.random() < 0.3:
            measured = measured.to(s.Unit.MILLIMETRE)
        tests.append(
            TestResult(
                f"TR-{seed}-{i + 1:03d}",
                lot.lot_id,
                spec.parameter,
                measured,
                spec.spec_id,
                spec.revision,
                datetime.combine(lot.received_on, datetime.min.time(), tzinfo=UTC)
                + timedelta(days=rng.randint(1, 10), hours=rng.randint(8, 17)),
                tprov,
            )
        )

    work_orders: list[WorkOrder] = []
    wprov = _prov(seed, "tripwire-synth-workorder")
    for i in range(min(n_work_orders, len(defects))):
        d = defects[i]
        work_orders.append(
            WorkOrder(
                f"WO-{seed}-{i + 1:03d}",
                d.defect_id,
                rng.choice(ACTIONS),
                d.submission_date + timedelta(days=rng.randint(1, 5)),
                "planner-01",
                wprov,
            )
        )
    return Dataset(
        seed, tuple(defects), tuple(lots), tuple(specs), tuple(tests), tuple(work_orders)
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--out", type=Path, help="write canonical JSON here (default: stdout)")
    parser.add_argument("--defects", type=int, default=30)
    parser.add_argument("--lots", type=int, default=12)
    parser.add_argument("--work-orders", type=int, default=5)
    args = parser.parse_args(argv)
    text = canonical_json(generate(args.seed, args.defects, args.lots, args.work_orders))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
