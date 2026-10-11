"""Measurement standards for the data Tripwire serves (milestone M0).

This module is the machine-readable twin of ``docs/MEASUREMENT_STANDARDS.md``.
Every unit, format, identifier rule, and code list that the rest of the build
relies on is defined here once, with the source it came from and how confident
we are in it. Nothing here is guessed: an item we could not settle from an
authoritative source is marked :attr:`Confidence.UNRESOLVED` and listed in
:data:`UNRESOLVED`, and the doc and this module are kept in sync by tests.

Conventions: stdlib only; frozen dataclasses; validators raise
:class:`StandardsError` (a decision path never silently coerces); conversions
exist only between units of the same dimension and use exact defined factors.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from types import MappingProxyType


class StandardsError(ValueError):
    """A value does not conform to the documented standard."""


class Confidence(StrEnum):
    """How well a standard is supported by its cited source."""

    HIGH = "HIGH"  # stated by the authoritative source (FAA/CFR/NASA text)
    MEDIUM = "MEDIUM"  # consistent secondary sources, or primary text seen only in excerpt
    LOW = "LOW"  # inference from examples; not stated anywhere we could read
    UNRESOLVED = "UNRESOLVED"  # no source settles it; see UNRESOLVED for how to close it


class Dimension(StrEnum):
    LENGTH = "length"
    TIME_IN_SERVICE = "time_in_service"
    CYCLES = "cycles"
    FORCE = "force"
    COUNT = "count"
    NONE = "none"


class Unit(StrEnum):
    """Units that appear in the data Tripwire serves. The value is the label."""

    INCH = "in"
    MILLIMETRE = "mm"
    HOUR = "h"
    MINUTE = "min"
    CYCLE = "cycle"
    NEWTON = "N"
    POUND_FORCE = "lbf"
    COUNT = "count"
    NONE = ""


UNIT_DIMENSION: Mapping[Unit, Dimension] = MappingProxyType(
    {
        Unit.INCH: Dimension.LENGTH,
        Unit.MILLIMETRE: Dimension.LENGTH,
        Unit.HOUR: Dimension.TIME_IN_SERVICE,
        Unit.MINUTE: Dimension.TIME_IN_SERVICE,
        Unit.CYCLE: Dimension.CYCLES,
        Unit.NEWTON: Dimension.FORCE,
        Unit.POUND_FORCE: Dimension.FORCE,
        Unit.COUNT: Dimension.COUNT,
        Unit.NONE: Dimension.NONE,
    }
)

# The unit a quantity of each dimension is stored in by the FAA data (and so
# the unit Tripwire's own records use). Force is not an FAA SDR quantity; it is
# the unit NASA-STD-8739.4A tabulates crimp pull strength in (newtons, with
# pounds-force in parentheses), so newton is canonical there.
CANONICAL_UNIT: Mapping[Dimension, Unit] = MappingProxyType(
    {
        Dimension.LENGTH: Unit.INCH,
        Dimension.TIME_IN_SERVICE: Unit.HOUR,
        Dimension.CYCLES: Unit.CYCLE,
        Dimension.FORCE: Unit.NEWTON,
        Dimension.COUNT: Unit.COUNT,
        Dimension.NONE: Unit.NONE,
    }
)

# Exact defined factors, (from, to) -> multiply by. The inch is defined as
# exactly 25.4 mm (international yard and pound agreement, 1959); the hour is
# exactly 60 minutes; the pound-force is exactly 4.4482216152605 N (defined via
# 0.45359237 kg and standard gravity 9.80665 m/s^2).
CONVERSION_FACTORS: Mapping[tuple[Unit, Unit], float] = MappingProxyType(
    {
        (Unit.INCH, Unit.MILLIMETRE): 25.4,
        (Unit.HOUR, Unit.MINUTE): 60.0,
        (Unit.POUND_FORCE, Unit.NEWTON): 4.4482216152605,
    }
)


def convert(value: float, src: Unit, dst: Unit) -> float:
    """Convert ``value`` from ``src`` to ``dst`` using the defined factors.

    Fails closed: units of different dimensions, or a pair with no defined
    factor, raise :class:`StandardsError` rather than returning the input.
    """
    if src is dst:
        return float(value)
    if UNIT_DIMENSION[src] is not UNIT_DIMENSION[dst]:
        raise StandardsError(f"cannot convert {src.value or 'none'} to {dst.value or 'none'}")
    factor = CONVERSION_FACTORS.get((src, dst))
    if factor is not None:
        return value * factor
    inverse = CONVERSION_FACTORS.get((dst, src))
    if inverse is not None:
        return value / inverse
    raise StandardsError(f"no defined factor between {src.value} and {dst.value}")


class FieldKind(StrEnum):
    NUMERIC = "numeric"  # decimal number; unit given
    INTEGER = "integer"  # whole number; unit given
    DATE = "date"
    DATETIME = "datetime"
    CODE = "code"  # value from a look-up table
    IDENTIFIER = "identifier"  # structured string with a documented shape
    TEXT = "text"  # free text or vendor-defined string (make, model, part name)


# Source documents. Every FieldStandard cites one of these by URL.
SRC_FIELD_INSTRUCTIONS = "https://sdrs.faa.gov/Documents/SDRS%20Field%20Instructions.pdf"
SRC_SINGLE_SUBMISSION = (
    "https://sdrs.faa.gov/Documents/Instructions%20for%20Single%20Submission.pdf"
)
SRC_BATCH_LAYOUT = "https://sdrs.faa.gov/Documents/Batch%20File%20Layout.pdf"
SRC_DOWNLOAD_PAGE = "https://www.faa.gov/av-info/download_SDR"
# The committed profile receipt (PROF): scripts/profile_sdr.py on SDR-2026.csv, 2026-10-11.
SRC_PROFILE = "https://github.com/Jott2121/tripwire/blob/main/reports/sdr_profile_2026.json"
SRC_JASC = "https://sdrs.faa.gov/documents/JASC_Code.pdf"
SRC_CFR_47_15 = "https://www.ecfr.gov/current/title-14/section-47.15"
SRC_CFR_1_1 = "https://www.ecfr.gov/current/title-14/section-1.1"
SRC_CFR_121_703 = "https://www.ecfr.gov/current/title-14/section-121.703"
SRC_ORDER_8300_12 = "https://www.faa.gov/documentLibrary/media/Order/8300.12.pdf"
SRC_NASA_8739_4A = (
    "https://standards.nasa.gov/sites/default/files/standards/NASA/A/4/"
    "nasa-std-87394a_w_change_4_0.pdf"
)
SRC_ISSUE_4 = "https://github.com/Jott2121/tripwire/issues/4"


@dataclass(frozen=True)
class FieldStandard:
    """What one SDR CSV column means, in what unit/format, and how we know."""

    column: str
    kind: FieldKind
    unit: Unit
    confidence: Confidence
    source: str
    note: str
    fmt: str | None = None  # strptime format for DATE/DATETIME
    pattern: str | None = None  # regex the value must fullmatch, when known


_FI = SRC_FIELD_INSTRUCTIONS
_H, _M, _L, _U = Confidence.HIGH, Confidence.MEDIUM, Confidence.LOW, Confidence.UNRESOLVED
_K = FieldKind
_NUM, _INT, _DATE, _DT, _CODE, _ID, _TXT = (
    _K.NUMERIC,
    _K.INTEGER,
    _K.DATE,
    _K.DATETIME,
    _K.CODE,
    _K.IDENTIFIER,
    _K.TEXT,
)
_NONE, _HOUR, _CYC, _INCH, _CNT = Unit.NONE, Unit.HOUR, Unit.CYCLE, Unit.INCH, Unit.COUNT
_WHOLE_HOURS = (
    "Hours of time in service (14 CFR 1.1: airborne time). 'Whole hours' is stated for "
    "component times; the SDR-2026 profile found every hour column 100% whole numbers."
)
_CYCLES = "Count of cycles; entered only when applicable, so often empty."
_SINCE = "Hours since the most recent overhaul, repair, or inspection; see the SinceCode."
_SINCE_CODE = (
    "Which event the TimeSince counts from; stored values O, R, I observed in SDR-2026, "
    "matching the form's Overhaul / Repair / Inspection choices (SINCE_CODE)."
)
_STATION = (
    "Airframe station coordinate; inches on US-built aircraft, datum set by the manufacturer. "
    "Not stated in the SDRS instructions."
)
_SIDE = "Left/right side qualifier; stored values L and R observed in SDR-2026 (SIDE_CODE)."
_FREE = "Free text."
_MFR_PN = "Manufacturer's part number, never an operator-internal number. No universal format."
_MFR_SN = "Manufacturer's serial number."
_CN_NOTE = (
    "Up to 17 alphanumerics: 4-char submitter designator + YYYYMMDD submission date "
    "+ submitter-defined sequence. SDR-2026 profile: 44,879 of 45,910 match; treat a "
    "mismatch as a warning, not a reject."
)
_SUB_DT_NOTE = (
    "ISO-8601 with a UTC offset. The SDR-2026 profile found the offset fixed at -05:00 "
    "in every month Jan-Oct 2026 (no DST change)."
)
_OP_DESIG_NOTE = (
    "First four characters of the operator's certificate number (not the repair "
    "station's when filing on an operator's behalf)."
)
_REG_NOTE = (
    "US registration WITHOUT the leading 'N' ('Do not enter N'); foreign aircraft "
    "carry a country code plus as much of the mark as fits. Shape per 14 CFR 47.15. "
    "SDR-2026 profile: 0% start with N; 45,621 of 45,663 valid without it."
)
_CRACK_NOTE = (
    "'In inches, indicate the length of the crack.' (Single Submission instructions). "
    "Optional; the 2021 field instructions say the field will be removed in a future release."
)
_CORR_NOTE = (
    "Levels 1/2/3 as defined for corrosion prevention and control programs "
    "(FAA Order 8300.12); stored as a single digit (2 and 3 observed in SDR-2026)."
)
_SOO_NOTE = "Two-letter code (batch layout: Alpha 2); flight and ground stages. List partial."
_HOW_NOTE = "One alphanumeric (batch layout: Alpha-numeric 1); 13 values observed (HOW_DISCOVERED)."
_FSDO_NOTE = (
    "Two-digit FAA district office number; 30 values observed in SDR-2026; meanings not read."
)
_PCOND_NOTE = (
    "From a drop-down; 95% of values are longer than four characters (word-like, e.g. ODOR, "
    "WORN, NONE, FOD were the short ones observed). Full table not read."
)
_STYPE_NOTE = "One letter from the submitter-type look-up (see SUBMITTER_TYPE; list partial)."
_SDRTYPE_NOTE = "One-letter code; A and G observed in SDR-2026 (FI mentions 'G'). Meanings unknown."
_JASC_NOTE = "Four digits: first two = system (ATA-100-like chapter), last two = component."
_REGION_NOTE = "Two-letter FAA region code; ten values observed in SDR-2026 (RECEIVING_REGION)."
_DISC_NOTE = "Operator-submitted narrative. Never committed to this repo (docs/DATA.md)."

# (column, kind, unit, confidence, source, note[, fmt[, pattern]])
# All 76 columns of the annual SDR-<year>.csv header, in file order. Confirmed by
# the SDR-2026 profile (76 columns, expected order, 45,910 rows). The batch
# *upload* layout has 77 fields; it is a different layout from the download.
_SDR_SPEC: tuple[tuple, ...] = (
    ("OperatorControlNumber", _ID, _NONE, _H, _FI, _CN_NOTE, None, r"[A-Z0-9]{1,17}"),
    (
        "DifficultyDate",
        _DATE,
        _NONE,
        _H,
        SRC_SINGLE_SUBMISSION,
        "mm/dd/yyyy on the form.",
        "%m/%d/%Y",
    ),
    ("SubmissionDate", _DT, _NONE, _H, SRC_PROFILE, _SUB_DT_NOTE, "ISO-8601"),
    ("OperatorDesignator", _ID, _NONE, _H, _FI, _OP_DESIG_NOTE, None, r"[A-Z0-9]{4}"),
    ("SubmitterDesignator", _ID, _NONE, _H, SRC_SINGLE_SUBMISSION, "FAA-assigned; not editable."),
    ("SubmitterTypeCode", _CODE, _NONE, _M, _FI, _STYPE_NOTE, None, r"[A-Z]"),
    ("ReceivingRegionCode", _CODE, _NONE, _M, SRC_PROFILE, _REGION_NOTE, None, r"[A-Z]{2}"),
    ("ReceivingDistrictOffice", _CODE, _NONE, _M, SRC_PROFILE, _FSDO_NOTE, None, r"\d{2}"),
    ("SDRType", _CODE, _NONE, _M, SRC_PROFILE, _SDRTYPE_NOTE, None, r"[A-Z]"),
    ("JASCCode", _CODE, _NONE, _H, SRC_JASC, _JASC_NOTE, None, r"\d{4}"),
    ("NatureOfConditionA", _CODE, _NONE, _M, _FI, "Required; 1-3 codes, A first."),
    ("NatureOfConditionB", _CODE, _NONE, _M, _FI, "Optional second code."),
    ("NatureOfConditionC", _CODE, _NONE, _M, _FI, "Optional third code."),
    ("PrecautionaryProcedureA", _CODE, _NONE, _M, _FI, "Required; 1-4 codes."),
    ("PrecautionaryProcedureB", _CODE, _NONE, _M, _FI, "Optional."),
    ("PrecautionaryProcedureC", _CODE, _NONE, _M, _FI, "Optional."),
    ("PrecautionaryProcedureD", _CODE, _NONE, _M, _FI, "Optional."),
    ("StageOfOperationCode", _CODE, _NONE, _H, SRC_BATCH_LAYOUT, _SOO_NOTE, None, r"[A-Z]{2}"),
    ("HowDiscoveredCode", _CODE, _NONE, _M, SRC_BATCH_LAYOUT, _HOW_NOTE, None, r"[A-Z0-9]"),
    ("RegistryNNumber", _ID, _NONE, _H, _FI, _REG_NOTE),
    ("AircraftMake", _TXT, _NONE, _H, _FI, "Manufacturer name, free text."),
    ("AircraftModel", _TXT, _NONE, _H, _FI, "Model designation, free text."),
    ("AircraftSerialNumber", _TXT, _NONE, _H, _FI, _MFR_SN),
    ("AircraftTotalTime", _INT, _HOUR, _H, _FI, _WHOLE_HOURS),
    ("AircraftTotalCycles", _INT, _CYC, _H, _FI, _CYCLES),
    ("EngineMake", _TXT, _NONE, _H, _FI, _FREE),
    ("EngineModel", _TXT, _NONE, _H, _FI, _FREE),
    ("EngineSerialNumber", _TXT, _NONE, _H, _FI, _MFR_SN),
    ("EngineTotalTime", _INT, _HOUR, _H, _FI, _WHOLE_HOURS),
    ("EngineTotalCycles", _INT, _CYC, _H, _FI, _CYCLES),
    ("PropellerMake", _TXT, _NONE, _H, _FI, _FREE),
    ("PropellerModel", _TXT, _NONE, _H, _FI, _FREE),
    ("PropellerSerialNumber", _TXT, _NONE, _H, _FI, _MFR_SN),
    ("PropellerTotalTime", _INT, _HOUR, _H, _FI, _WHOLE_HOURS),
    ("PropellerTotalCycles", _INT, _CYC, _H, _FI, _CYCLES),
    ("PartMake", _TXT, _NONE, _H, _FI, "Part manufacturer, free text."),
    ("PartName", _TXT, _NONE, _H, _FI, "Manufacturer's name for the part; from a drop-down."),
    ("PartNumber", _TXT, _NONE, _H, _FI, _MFR_PN),
    ("PartSerialNumber", _TXT, _NONE, _H, _FI, _MFR_SN),
    ("PartCondition", _CODE, _NONE, _M, _FI, _PCOND_NOTE),
    ("PartLocation", _TXT, _NONE, _M, _FI, "Where on the aircraft; free text."),
    ("PartTotalTime", _INT, _HOUR, _H, _FI, _WHOLE_HOURS),
    ("PartTotalCycles", _INT, _CYC, _H, _FI, _CYCLES),
    ("PartTimeSince", _INT, _HOUR, _H, _FI, _SINCE),
    ("PartSinceCode", _CODE, _NONE, _M, _FI, _SINCE_CODE, None, r"[A-Z]"),
    ("ComponentMake", _TXT, _NONE, _H, _FI, _FREE),
    ("ComponentModel", _TXT, _NONE, _H, _FI, _FREE),
    ("ComponentName", _TXT, _NONE, _H, _FI, _FREE),
    ("ComponentPartNumber", _TXT, _NONE, _H, _FI, _MFR_PN),
    ("ComponentSerialNumber", _TXT, _NONE, _H, _FI, _MFR_SN),
    ("ComponentLocation", _TXT, _NONE, _M, _FI, _FREE),
    ("ComponentTotalTime", _INT, _HOUR, _H, _FI, _WHOLE_HOURS),
    ("ComponentTotalCycles", _INT, _CYC, _H, _FI, _CYCLES),
    ("ComponentTimeSince", _INT, _HOUR, _H, _FI, _SINCE),
    ("ComponentSinceCode", _CODE, _NONE, _M, _FI, _SINCE_CODE, None, r"[A-Z]"),
    ("FuselageStationFrom", _NUM, _INCH, _M, _FI, _STATION),
    ("FuselageStationTo", _NUM, _INCH, _M, _FI, _STATION),
    ("StringerFrom", _TXT, _NONE, _M, _FI, "Stringer designation (From)."),
    ("StringerFromSide", _CODE, _NONE, _M, _FI, _SIDE, None, r"[LR]"),
    ("StringerTo", _TXT, _NONE, _M, _FI, "Stringer designation (To)."),
    ("StringerToSide", _CODE, _NONE, _M, _FI, _SIDE, None, r"[LR]"),
    ("WingStationFrom", _NUM, _INCH, _M, _FI, _STATION),
    ("WingStationFromSide", _CODE, _NONE, _M, _FI, _SIDE, None, r"[LR]"),
    ("WingStationTo", _NUM, _INCH, _M, _FI, _STATION),
    ("WingStationToSide", _CODE, _NONE, _M, _FI, _SIDE, None, r"[LR]"),
    ("ButtLineFrom", _NUM, _INCH, _M, _FI, _STATION),
    ("ButtLineFromSide", _CODE, _NONE, _M, _FI, _SIDE, None, r"[LR]"),
    ("ButtlineTo", _NUM, _INCH, _M, _FI, _STATION),
    ("ButtlineToSide", _CODE, _NONE, _M, _FI, _SIDE, None, r"[LR]"),
    ("WaterLineFrom", _NUM, _INCH, _M, _FI, _STATION),
    ("WaterLineTo", _NUM, _INCH, _M, _FI, _STATION),
    ("CrackLength", _NUM, _INCH, _H, SRC_SINGLE_SUBMISSION, _CRACK_NOTE),
    ("NumberOfCracks", _INT, _CNT, _H, _FI, "Count of cracks found. Optional; slated for removal."),
    ("CorrosionLevel", _CODE, _NONE, _M, SRC_ORDER_8300_12, _CORR_NOTE),
    ("StructuralOther", _TXT, _NONE, _M, _FI, _FREE),
    ("Discrepancy", _TXT, _NONE, _H, _FI, _DISC_NOTE),
)
SDR_FIELDS: tuple[FieldStandard, ...] = tuple(FieldStandard(*row) for row in _SDR_SPEC)
SDR_COLUMNS: tuple[str, ...] = tuple(f.column for f in SDR_FIELDS)
SDR_FIELD_BY_NAME: Mapping[str, FieldStandard] = MappingProxyType({f.column: f for f in SDR_FIELDS})

SDR_ENCODING = "utf-8"  # PROF: 0 UTF-8 decode errors, 151 under cp1252. Some text is
# mis-encoded in the FAA file itself (a stray 'Â°'); not loader damage.
SDR_URL_TEMPLATE = "https://external.apic4e.faa.gov/sdrs/retrieve/SDR-{year}.csv"
SDR_MISSING_VALUE = ""  # empty field = not reported; no sentinel like 'N/A' or 0 is documented.


@dataclass(frozen=True)
class CodeList:
    """A look-up table. ``complete`` is False when we only saw part of it."""

    name: str
    codes: Mapping[str, str]
    complete: bool
    confidence: Confidence
    source: str

    def check(self, code: str) -> bool | None:
        """True if known, False if known-absent, None if the list is partial."""
        if code in self.codes:
            return True
        return False if self.complete else None


def _codes(name: str, complete: bool, conf: Confidence, source: str, **codes: str) -> CodeList:
    return CodeList(name, MappingProxyType(dict(codes)), complete, conf, source)


_OBS = "observed in SDR-2026 (meaning not in any source read)"
SUBMITTER_TYPE = _codes(
    "SubmitterTypeCode",
    False,
    _M,
    "https://sdrs.faa.gov/SubmissionsGeneralAviation.aspx",
    A="CARRIER PART 121",
    B="REPAIR STATION PART 145",
    C="OPERATOR PART 91",
    D="MECHANIC",
    E="CARRIER PART 135",
    F="MANUFACTURER",
    G="FEDERAL AVIATION ADMIN",
    H="OTHER",
    I="PART 125",
    L="PART 133",
    M=_OBS,
    P=_OBS,
    S=_OBS,
    W=_OBS,
    Z=_OBS,
)
SDR_TYPE = _codes("SDRType", False, _M, SRC_PROFILE, A=_OBS, G=_OBS)
NATURE_OF_CONDITION = _codes(
    "NatureOfCondition",
    False,
    _M,
    "https://sdrs.faa.gov/SubmissionsGeneralAviation.aspx",
    A="FLAME/FIRE",
    B="SMOKE/FUMES/ODORS/SPARKS",
    C="F.O.D.",
    D="INFLIGHT SEPARATION",
    E="VIBRATION/BUFFET",
    F="FLIGHT CONTROL AFFECTED",
    G="MULTIPLE FAILURE",
    H="ELECTRICAL",
    **dict.fromkeys("IJKLMNOPRSTWXYZ", _OBS),
)
PRECAUTIONARY_PROCEDURE = _codes(
    "PrecautionaryProcedure",
    False,
    _M,
    "https://sdrs.faa.gov/SubmissionsGeneralAviation.aspx",
    A="UNSCHEDULED LANDING",
    B="EMERGENCY DESCENT",
    C="ABORTED TAKEOFF",
    D="RETURN TO BLOCK",
    E="ENGINE SHUTDOWN",
    F="ACTIVATE FIRE EXTINGUISHER",
    J="FUEL DUMPED",
    **dict.fromkeys("GHKLOR", _OBS),
)
STAGE_OF_OPERATION = _codes(
    "StageOfOperationCode",
    False,
    _M,
    "https://www.aviationdb.com/Aviation/SdrQuery.shtm",
    AA="AIR AMBULANCE",
    AB="AEROBATIC",
    AG="AGRICULTURE",
    AP="APPROACH",
    CL="CLIMB",
    CR="CRUISE",
    DE="DESCENT",
    ET="ETOPS",
    EX="EXTERNAL LOAD",
    FF="FIRE FIGHTING",
    HO="HOVERING",
    IN=_OBS,
    TX=_OBS,
    NR=_OBS,
    TO=_OBS,
    LD=_OBS,
    UK=_OBS,
)
HOW_DISCOVERED = _codes(
    "HowDiscoveredCode", False, _M, SRC_PROFILE, **dict.fromkeys("VOCE9UBMXLTDW", _OBS)
)
RECEIVING_REGION = _codes(
    "ReceivingRegionCode",
    False,
    _M,
    SRC_PROFILE,
    **dict.fromkeys(("SW", "GL", "SO", "CE", "NM", "EA", "WP", "AL", "FS", "NE"), _OBS),
)
SINCE_CODE = _codes(
    "PartSinceCode / ComponentSinceCode",
    False,
    _M,
    _FI,
    O="OVERHAUL (observed; initial matches the form)",
    R="REPAIR (observed; initial matches the form)",
    I="INSPECTION (observed; initial matches the form)",
)
SIDE_CODE = _codes("*Side", False, _M, SRC_PROFILE, L=_OBS + "; left", R=_OBS + "; right")
CORROSION_LEVEL = _codes(
    "CorrosionLevel",
    False,
    _M,
    SRC_ORDER_8300_12,
    **{
        "1": "Reworkable within manufacturer limits (SRM/SB)",
        "2": "Beyond rework limits; repair or replacement of a principal structural element",
        "3": "Potential airworthiness concern needing prompt action",
    },
)  # observed stored values: 2 (5,130 rows) and 3 (3 rows); 1 not seen in 2026
# Titles for the JASC codes the synthetic generator uses (issue #4 / PR #6).
JASC_TITLES = _codes(
    "JASCCode",
    False,
    _M,
    SRC_JASC,
    **{
        "2820": "AIRCRAFT FUEL DISTRIBUTION SYSTEM",
        "3220": "NOSE/TAIL LANDING GEAR",
        "3230": "LANDING GEAR RETRACT/EXTEND SYSTEM",
        "5320": "FUSELAGE MISCELLANEOUS STRUCTURE",
        "5710": "WING MAIN FRAME STRUCTURE",
        "7230": "TURBINE ENGINE COMPRESSOR SECTION",
    },
)
CODE_LISTS: tuple[CodeList, ...] = (
    SUBMITTER_TYPE,
    SDR_TYPE,
    HOW_DISCOVERED,
    RECEIVING_REGION,
    SINCE_CODE,
    SIDE_CODE,
    NATURE_OF_CONDITION,
    PRECAUTIONARY_PROCEDURE,
    STAGE_OF_OPERATION,
    CORROSION_LEVEL,
    JASC_TITLES,
)

# --- Identifier rules -------------------------------------------------------

# 14 CFR 47.15(b): after the prefix N, at most five symbols: all digits, or
# 1-4 digits + 1 letter, or 1-3 digits + 2 letters; I and O never used; the
# first zero must be preceded by a digit 1-9 (so the mark cannot begin with 0).
_LETTER = "[A-HJ-NP-Z]"
N_NUMBER_SUFFIX_RE = re.compile(
    rf"[1-9]\d{{0,4}}|[1-9]\d{{0,3}}{_LETTER}|[1-9]\d{{0,2}}{_LETTER}{{2}}"
)
N_NUMBER_RE = re.compile(rf"N(?:{N_NUMBER_SUFFIX_RE.pattern})")
JASC_RE = re.compile(r"\d{4}")
CONTROL_NUMBER_RE = re.compile(
    r"(?P<designator>[A-Z0-9]{4})(?P<date>\d{8})(?P<sequence>[A-Z0-9]{0,5})"
)


def parse_n_number(value: str, *, prefix: bool | None = None) -> str:
    """Validate a US registration mark and return it in canonical 'N…' form.

    ``prefix=True`` requires the leading N, ``False`` forbids it (the SDR
    convention), ``None`` accepts either. Raises :class:`StandardsError`.
    """
    text = value.strip().upper()
    has_prefix = text.startswith("N")
    if prefix is True and not has_prefix:
        raise StandardsError(f"registration mark must start with N: {value!r}")
    if prefix is False and has_prefix:
        raise StandardsError(f"SDR registration field must omit the N: {value!r}")
    suffix = text[1:] if has_prefix else text
    if not N_NUMBER_SUFFIX_RE.fullmatch(suffix):
        raise StandardsError(f"not a valid 14 CFR 47.15 registration mark: {value!r}")
    return "N" + suffix


def is_valid_n_number(value: str, *, prefix: bool | None = None) -> bool:
    try:
        parse_n_number(value, prefix=prefix)
    except StandardsError:
        return False
    return True


def jasc_system(code: str) -> str:
    """Return the two-digit system (chapter) part of a four-digit JASC code."""
    if not JASC_RE.fullmatch(code):
        raise StandardsError(f"JASC code must be exactly four digits: {code!r}")
    return code[:2]


@dataclass(frozen=True)
class ControlNumber:
    designator: str
    submitted: date
    sequence: str

    @property
    def text(self) -> str:
        return f"{self.designator}{self.submitted:%Y%m%d}{self.sequence}"


def parse_control_number(value: str) -> ControlNumber:
    """Parse the documented 17-character-max operator control number shape.

    Real historical rows may not follow it (the shape is from the 2021 field
    instructions); callers decide whether a mismatch is a reject or a flag.
    """
    text = value.strip().upper()
    m = CONTROL_NUMBER_RE.fullmatch(text)
    if m is None or len(text) > 17:
        raise StandardsError(f"control number is not <designator><YYYYMMDD><seq>: {value!r}")
    try:
        submitted = datetime.strptime(m.group("date"), "%Y%m%d").date()
    except ValueError as exc:
        raise StandardsError(f"control number date is not a calendar date: {value!r}") from exc
    return ControlNumber(m.group("designator"), submitted, m.group("sequence"))


# --- Date rules -------------------------------------------------------------

DIFFICULTY_DATE_FORMAT = "%m/%d/%Y"


def parse_difficulty_date(text: str) -> date:
    try:
        return datetime.strptime(text.strip(), DIFFICULTY_DATE_FORMAT).date()
    except ValueError as exc:
        raise StandardsError(f"DifficultyDate is not mm/dd/yyyy: {text!r}") from exc


def parse_submission_datetime(text: str) -> datetime:
    """Parse an ISO-8601 SubmissionDate; a value without a UTC offset is rejected."""
    try:
        parsed = datetime.fromisoformat(text.strip())
    except ValueError as exc:
        raise StandardsError(f"SubmissionDate is not ISO-8601: {text!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise StandardsError(f"SubmissionDate has no UTC offset: {text!r}")
    return parsed


# 14 CFR 121.703(d): reports cover a 24-hour period starting 0900 local and are
# due within 96 hours (weekend/holiday extensions apply). So for part 121
# filers DifficultyDate normally precedes SubmissionDate by a few days; a
# difficulty date after the submission date is impossible for any filer.
PART_121_REPORT_DEADLINE_HOURS = 96


def difficulty_precedes_submission(difficulty: date, submission: datetime) -> bool:
    """True when the difficulty date is on or before the submission's local date."""
    return difficulty <= submission.date()


# --- Open items -------------------------------------------------------------


@dataclass(frozen=True)
class Unresolved:
    key: str
    question: str
    how_to_resolve: str


@dataclass(frozen=True)
class Resolved:
    key: str
    answer: str


# Closed by a maintainer run of scripts/profile_sdr.py on SDR-2026.csv (2026-10-11,
# 45,910 rows). The receipt is committed as reports/sdr_profile_2026.json.
PROFILE_SOURCE = (
    "reports/sdr_profile_2026.json (scripts/profile_sdr.py on SDR-2026.csv, 2026-10-11)"
)
RESOLVED: tuple[Resolved, ...] = (
    Resolved(
        "column_count", "76 columns in the expected order; the 77-field count is the upload layout."
    ),
    Resolved(
        "encoding",
        "UTF-8: 0 decode errors under UTF-8, 151 under cp1252. The file itself carries some "
        "pre-existing mis-encoded text (e.g. a stray 'Â°'); not loader damage.",
    ),
    Resolved("submission_tz", "Offset fixed at -05:00 in every month Jan-Oct 2026; no DST change."),
    Resolved("whole_hours", "Every hour column is 100% whole numbers."),
    Resolved(
        "registry_prefix", "0% start with N; 45,621 of 45,663 valid under 14 CFR 47.15 without it."
    ),
    Resolved("control_number_shape", "44,879 of 45,910 match 4+8+sequence; mismatch is a warning."),
)
RESOLVED_KEYS: frozenset[str] = frozenset(r.key for r in RESOLVED)

UNRESOLVED: tuple[Unresolved, ...] = (
    Unresolved(
        "code_tables",
        "Meanings of the code values observed in SDR-2026 (reports/sdr_profile_2026.json) for "
        "SDRType, ReceivingRegionCode, ReceivingDistrictOffice, HowDiscoveredCode, "
        "PartCondition, and the unlabelled values of NatureOfCondition / "
        "PrecautionaryProcedure / StageOfOperation / SubmitterTypeCode.",
        "Read the SDRS Look-up Tables and Field Instructions at sdrs.faa.gov (host blocked "
        "from the agent sandbox) and label each observed value.",
    ),
    Unresolved(
        "nasa_8739_4a_table",
        "Exact NASA-STD-8739.4A crimp pull-test minimums (Table 12-1) per AWG, in N and lbf.",
        "Read the PDF at standards.nasa.gov directly; web excerpts were garbled. Needed for M1.",
    ),
    Unresolved(
        "sdrs_2026_upgrade",
        "The SDRS 'What's New' page announces required fields, stronger validation and batch "
        "changes for summer 2026; does the download layout change?",
        "Re-profile the first file published after the upgrade and diff the header.",
    ),
)
UNRESOLVED_KEYS: frozenset[str] = frozenset(u.key for u in UNRESOLVED)
