"""Seeded, synthetic SDR rows with the real 77-column header.

No real SDR rows are ever committed (see ``docs/DATA.md``); every fixture is
generated here from committed code with a fixed seed, using fictional makes,
operators, and part data only. The same ``(n, seed)`` always yields a
byte-identical CSV so fixtures are reproducible from the command that made them.
"""

import csv
import random
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from tripwire.sdr import ENCODING, SDR_COLUMNS

# Fictional values only -- no real company, operator, or person names.
_AIRCRAFT_MAKES = ("ACME AERO", "ZENITH AERO", "ORION AERO", "VEGA AERO")
_AIRCRAFT_MODELS = ("AC-200", "ZN-310", "OR-120", "VG-450")
_JASC_CODES = ("2820", "3220", "5710", "7230", "3230", "5320")
_PART_NAMES = ("BRACKET", "ACTUATOR", "FITTING", "PANEL", "BEARING", "VALVE")
_PART_CONDITIONS = ("CRACKED", "WORN", "CORRODED", "FAILED", "LEAKING")
_STAGES = ("PREFLIGHT", "SCHEDULED", "WALKAROUND", "TEARDOWN")
_EASTERN = timezone(timedelta(hours=-5))


def make_sdr_rows(n: int, seed: int) -> list[dict[str, str]]:
    """Return ``n`` synthetic SDR rows as full 77-column string dicts.

    Deterministic in ``(n, seed)``: uses a local :class:`random.Random` only, so
    there is no dependence on global RNG state.
    """
    if n < 0:
        raise ValueError("n must be non-negative")
    rng = random.Random(seed)
    rows: list[dict[str, str]] = []
    for i in range(n):
        make = rng.choice(_AIRCRAFT_MAKES)
        model = rng.choice(_AIRCRAFT_MODELS)
        part = rng.choice(_PART_NAMES)
        condition = rng.choice(_PART_CONDITIONS)
        stage = rng.choice(_STAGES)

        difficulty = date(2026, 1, 1) + timedelta(days=rng.randint(0, 300))
        submission = datetime(
            difficulty.year,
            difficulty.month,
            difficulty.day,
            rng.randint(0, 23),
            rng.randint(0, 59),
            rng.randint(0, 59),
            rng.randint(0, 999) * 1000,
            tzinfo=_EASTERN,
        ) + timedelta(days=rng.randint(0, 3))

        total_time = f"{rng.uniform(100, 40000):.1f}"
        total_cycles = str(rng.randint(10, 30000))
        crack = f"{rng.uniform(0.1, 6.0):.2f}" if rng.random() < 0.6 else ""

        row = {name: "" for name in SDR_COLUMNS}
        row["OperatorControlNumber"] = f"OCN{i:06d}"
        row["DifficultyDate"] = difficulty.strftime("%m/%d/%Y")
        row["SubmissionDate"] = submission.isoformat()
        row["OperatorDesignator"] = f"OP{rng.randint(1, 9):02d}"
        row["SubmitterDesignator"] = f"SUB{rng.randint(1, 9):02d}"
        row["SubmitterTypeCode"] = rng.choice(("A", "O", "M"))
        row["SDRType"] = "SDR"
        row["JASCCode"] = rng.choice(_JASC_CODES)
        row["StageOfOperationCode"] = stage
        row["AircraftMake"] = make
        row["AircraftModel"] = model
        row["AircraftSerialNumber"] = f"ASN{rng.randint(1000, 9999)}"
        row["AircraftTotalTime"] = total_time
        row["AircraftTotalCycles"] = total_cycles
        row["PartMake"] = make
        row["PartName"] = part
        row["PartNumber"] = f"PN{rng.randint(10000, 99999)}"
        row["PartSerialNumber"] = f"PSN{rng.randint(1000, 9999)}"
        row["PartCondition"] = condition
        row["CrackLength"] = crack
        row["NumberOfCracks"] = str(rng.randint(1, 4)) if crack else ""
        row["Discrepancy"] = (
            f"{part} found {condition} during {stage} inspection on {model} airframe"
        )
        rows.append(row)
    return rows


def write_sdr_csv(rows: list[dict[str, str]], path: str | Path) -> None:
    """Write ``rows`` to ``path`` as a latin-1 SDR CSV with the real header.

    Byte-identical for identical ``rows``: a fixed ``\\r\\n`` line terminator and
    the canonical column order make the output reproducible.
    """
    path = Path(path)
    with path.open("w", encoding=ENCODING, newline="") as f:
        writer = csv.DictWriter(f, fieldnames=SDR_COLUMNS, lineterminator="\r\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name, "") for name in SDR_COLUMNS})
