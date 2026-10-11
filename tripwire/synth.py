"""Seeded, synthetic SDR rows with the real 76-column download header.

No real SDR rows are ever committed (see ``docs/DATA.md``); every fixture is
generated here from committed code with a fixed seed, using fictional makes,
operators, and part data only. The same ``(n, seed)`` always yields a
byte-identical UTF-8 CSV so fixtures are reproducible from the command that
made them.

Rows follow the conventions observed in a profile of the real ``SDR-2026.csv``
download: ``SDRType`` and ``SubmitterTypeCode`` use only observed code values,
``StageOfOperationCode`` is a two-letter code, ``OperatorControlNumber`` is a
4-character designator + submission date (``YYYYMMDD``) + sequence, hour and
cycle totals are whole numbers, ``SubmissionDate`` carries a fixed ``-05:00``
offset, and ``RegistryNNumber`` omits the leading ``N``.
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
# Narrative words for the Discrepancy text only (not a coded field).
_INSPECTIONS = ("PREFLIGHT", "SCHEDULED", "WALKAROUND", "TEARDOWN")
# Code values observed in the real SDR-2026.csv download (docs/DATA.md).
_SDR_TYPES = ("A", "G")
_SUBMITTER_TYPES = ("A", "B", "C", "D", "E", "G", "H", "I", "M", "P", "S", "W", "Z")
_STAGE_CODES = ("AA", "AB", "AG", "AP", "CL", "CR", "DE", "ET", "EX", "FF", "HO")
# Fictional 4-character designator that prefixes every OperatorControlNumber.
_CONTROL_DESIGNATOR = "TWSY"
# The OperatorControlNumber sequence is 4 digits, so at most this many rows.
_MAX_ROWS = 9999
# Registration-mark suffix letters: A-Z except I and O (14 CFR 47.15).
_MARK_LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ"
_EASTERN = timezone(timedelta(hours=-5))  # SubmissionDate offset is fixed at -05:00


def _registration_mark(rng: random.Random) -> str:
    """A US registration mark without its leading ``N``, as the SDR file stores it.

    1-3 digits not starting with 0, optionally followed by one or two letters
    other than I and O -- a subset of the forms 14 CFR 47.15 allows.
    """
    digits = str(rng.randint(1, 999))
    letters = "".join(rng.choice(_MARK_LETTERS) for _ in range(rng.randint(0, 2)))
    return digits + letters


def make_sdr_rows(n: int, seed: int) -> list[dict[str, str]]:
    """Return ``n`` synthetic SDR rows as full 76-column string dicts.

    Deterministic in ``(n, seed)``: uses a local :class:`random.Random` only, so
    there is no dependence on global RNG state. ``n`` must be in
    ``0..9999`` so every ``OperatorControlNumber`` keeps its 4-digit sequence.
    """
    if n < 0:
        raise ValueError("n must be non-negative")
    if n > _MAX_ROWS:
        raise ValueError(f"n must be at most {_MAX_ROWS} (4-digit control-number sequence)")
    rng = random.Random(seed)
    rows: list[dict[str, str]] = []
    for i in range(n):
        make = rng.choice(_AIRCRAFT_MAKES)
        model = rng.choice(_AIRCRAFT_MODELS)
        part = rng.choice(_PART_NAMES)
        condition = rng.choice(_PART_CONDITIONS)
        inspection = rng.choice(_INSPECTIONS)

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

        total_time = str(rng.randint(100, 40000))  # whole hours, as in the real file
        total_cycles = str(rng.randint(10, 30000))
        crack = f"{rng.uniform(0.1, 6.0):.2f}" if rng.random() < 0.6 else ""

        row = {name: "" for name in SDR_COLUMNS}
        row["OperatorControlNumber"] = (
            f"{_CONTROL_DESIGNATOR}{submission.strftime('%Y%m%d')}{i + 1:04d}"
        )
        row["DifficultyDate"] = difficulty.strftime("%m/%d/%Y")
        row["SubmissionDate"] = submission.isoformat(timespec="milliseconds")  # as real files
        row["OperatorDesignator"] = f"OP{rng.randint(1, 9):02d}"
        row["SubmitterDesignator"] = _CONTROL_DESIGNATOR  # control numbers start with it
        row["SubmitterTypeCode"] = rng.choice(_SUBMITTER_TYPES)
        row["SDRType"] = rng.choice(_SDR_TYPES)
        row["JASCCode"] = rng.choice(_JASC_CODES)
        row["StageOfOperationCode"] = rng.choice(_STAGE_CODES)
        row["RegistryNNumber"] = _registration_mark(rng)
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
            f"{part} found {condition} during {inspection} inspection on {model} airframe"
        )
        rows.append(row)
    return rows


def write_sdr_csv(rows: list[dict[str, str]], path: str | Path) -> None:
    """Write ``rows`` to ``path`` as a UTF-8 SDR CSV with the real header.

    Byte-identical for identical ``rows``: a fixed ``\\r\\n`` line terminator and
    the canonical column order make the output reproducible.
    """
    path = Path(path)
    with path.open("w", encoding=ENCODING, newline="") as f:
        writer = csv.DictWriter(f, fieldnames=SDR_COLUMNS, lineterminator="\r\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name, "") for name in SDR_COLUMNS})
