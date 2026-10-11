"""Load FAA Service Difficulty Reports (SDRs) into typed, frozen records.

The public SDR annual CSV downloads are UTF-8 encoded with 76 columns (see
``docs/DATA.md``; 77 is the separate batch *upload* layout, not the download).
This module maps the subset of columns Tripwire reasons about into
:class:`SDRRecord`, rejecting any malformed row with its line number rather
than silently skipping it or returning a half-filled record. A file that is not
valid UTF-8 fails closed with :class:`SDRFormatError` naming the byte offset.

No real SDR rows are ever committed; fixtures are synthetic (see
:mod:`tripwire.synth`).
"""

import csv
import io
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

# The exact 76-column header of the SDR-<year>.csv download, in file order.
SDR_COLUMNS: tuple[str, ...] = (
    "OperatorControlNumber",
    "DifficultyDate",
    "SubmissionDate",
    "OperatorDesignator",
    "SubmitterDesignator",
    "SubmitterTypeCode",
    "ReceivingRegionCode",
    "ReceivingDistrictOffice",
    "SDRType",
    "JASCCode",
    "NatureOfConditionA",
    "NatureOfConditionB",
    "NatureOfConditionC",
    "PrecautionaryProcedureA",
    "PrecautionaryProcedureB",
    "PrecautionaryProcedureC",
    "PrecautionaryProcedureD",
    "StageOfOperationCode",
    "HowDiscoveredCode",
    "RegistryNNumber",
    "AircraftMake",
    "AircraftModel",
    "AircraftSerialNumber",
    "AircraftTotalTime",
    "AircraftTotalCycles",
    "EngineMake",
    "EngineModel",
    "EngineSerialNumber",
    "EngineTotalTime",
    "EngineTotalCycles",
    "PropellerMake",
    "PropellerModel",
    "PropellerSerialNumber",
    "PropellerTotalTime",
    "PropellerTotalCycles",
    "PartMake",
    "PartName",
    "PartNumber",
    "PartSerialNumber",
    "PartCondition",
    "PartLocation",
    "PartTotalTime",
    "PartTotalCycles",
    "PartTimeSince",
    "PartSinceCode",
    "ComponentMake",
    "ComponentModel",
    "ComponentName",
    "ComponentPartNumber",
    "ComponentSerialNumber",
    "ComponentLocation",
    "ComponentTotalTime",
    "ComponentTotalCycles",
    "ComponentTimeSince",
    "ComponentSinceCode",
    "FuselageStationFrom",
    "FuselageStationTo",
    "StringerFrom",
    "StringerFromSide",
    "StringerTo",
    "StringerToSide",
    "WingStationFrom",
    "WingStationFromSide",
    "WingStationTo",
    "WingStationToSide",
    "ButtLineFrom",
    "ButtLineFromSide",
    "ButtlineTo",
    "ButtlineToSide",
    "WaterLineFrom",
    "WaterLineTo",
    "CrackLength",
    "NumberOfCracks",
    "CorrosionLevel",
    "StructuralOther",
    "Discrepancy",
)

# Source columns that must be present for the loader to vouch for the file.
REQUIRED_COLUMNS: tuple[str, ...] = (
    "OperatorControlNumber",
    "DifficultyDate",
    "SubmissionDate",
    "JASCCode",
    "AircraftMake",
    "AircraftModel",
    "AircraftTotalTime",
    "AircraftTotalCycles",
    "PartName",
    "PartNumber",
    "PartCondition",
    "CrackLength",
    "Discrepancy",
)

ENCODING = "utf-8"
_JASC = re.compile(r"\d{4}")


class SDRFormatError(Exception):
    """The file is missing, empty, not valid UTF-8, or lacks a required column."""


@dataclass(frozen=True)
class SDRRecord:
    """One SDR row, with the fields Tripwire reasons about parsed to types."""

    control_number: str
    difficulty_date: date
    submission_date: datetime  # tz-aware
    jasc_code: str  # 4 digits
    aircraft_make: str
    aircraft_model: str
    aircraft_total_time_hours: float | None
    aircraft_total_cycles: int | None
    part_name: str
    part_number: str
    part_condition: str
    crack_length_in: float | None
    discrepancy: str


@dataclass(frozen=True)
class Reject:
    """A row that could not be parsed, kept with its physical line number."""

    line: int
    reason: str


@dataclass(frozen=True)
class LoadResult:
    """The outcome of loading a file: good records and rejected rows."""

    records: list[SDRRecord]
    rejects: list[Reject]


def _opt_float(value: str) -> float | None:
    text = value.strip()
    return None if text == "" else float(text)


def _opt_int(value: str) -> int | None:
    text = value.strip()
    return None if text == "" else int(text)


def _parse_row(row: list[str], index: dict[str, int]) -> SDRRecord:
    """Build an :class:`SDRRecord` from one CSV row. Raises on malformed data."""

    def col(name: str) -> str:
        return row[index[name]]

    jasc = col("JASCCode").strip()
    if not _JASC.fullmatch(jasc):
        raise ValueError(f"JASCCode is not 4 digits: {jasc!r}")

    difficulty = datetime.strptime(col("DifficultyDate").strip(), "%m/%d/%Y").date()
    submission = datetime.fromisoformat(col("SubmissionDate").strip())
    if submission.tzinfo is None:
        raise ValueError("SubmissionDate is not timezone-aware")

    return SDRRecord(
        control_number=col("OperatorControlNumber"),
        difficulty_date=difficulty,
        submission_date=submission,
        jasc_code=jasc,
        aircraft_make=col("AircraftMake"),
        aircraft_model=col("AircraftModel"),
        aircraft_total_time_hours=_opt_float(col("AircraftTotalTime")),
        aircraft_total_cycles=_opt_int(col("AircraftTotalCycles")),
        part_name=col("PartName"),
        part_number=col("PartNumber"),
        part_condition=col("PartCondition"),
        crack_length_in=_opt_float(col("CrackLength")),
        discrepancy=col("Discrepancy"),
    )


def load_sdr_csv(path: str | Path) -> LoadResult:
    """Load an SDR CSV into typed records, collecting malformed rows as rejects.

    Fails closed: a missing or empty file, bytes that are not valid UTF-8, or a
    header missing any required column raises :class:`SDRFormatError`. A row
    with the wrong column count or an unparseable field becomes a
    :class:`Reject` carrying its line number.
    """
    path = Path(path)
    try:
        data = path.read_bytes()
    except FileNotFoundError as exc:
        raise SDRFormatError(f"SDR file not found: {path}") from exc

    # Decode the whole file at once so a bad byte is reported at its absolute
    # offset in the file (a streaming decoder only knows its chunk offset).
    try:
        text = data.decode(ENCODING)
    except UnicodeDecodeError as exc:
        line = data.count(b"\n", 0, exc.start) + 1
        raise SDRFormatError(
            f"SDR file is not valid {ENCODING}: {exc.reason} at byte offset {exc.start}"
            f" (line {line}): {path}"
        ) from exc

    with io.StringIO(text, newline="") as f:
        reader = csv.reader(f)
        try:
            header = next(reader)
        except StopIteration as exc:
            raise SDRFormatError(f"SDR file is empty: {path}") from exc

        index = {name: i for i, name in enumerate(header)}
        missing = [c for c in REQUIRED_COLUMNS if c not in index]
        if missing:
            raise SDRFormatError(f"SDR header missing required column(s): {', '.join(missing)}")

        records: list[SDRRecord] = []
        rejects: list[Reject] = []
        for row in reader:
            line = reader.line_num
            if len(row) != len(header):
                rejects.append(Reject(line, f"expected {len(header)} columns, got {len(row)}"))
                continue
            try:
                records.append(_parse_row(row, index))
            except (ValueError, KeyError) as exc:
                rejects.append(Reject(line, str(exc)))

    return LoadResult(records=records, rejects=rejects)
