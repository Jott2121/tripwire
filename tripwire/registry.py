"""Load the FAA aircraft registry (releasable database) into typed records. Phase two's source.

The releasable database (``ReleasableAircraft.zip``, see ``docs/DATA.md``) holds comma-delimited
text files; their layout is the FAA's ``ardata.pdf``. Two are read here:

* the **Aircraft Registration Master** file (``MASTER.txt``): one row per N-number;
* the **Aircraft Reference** file (``ACFTREF.txt``): make, model and series by code.

Privacy, by construction: the master file also carries registrants' names and street addresses.
This loader **never reads those columns**. It maps the few columns it needs by name and ignores the
rest, and :class:`RegistryRecord` has no field that could hold a name or an address.

Header names are matched after normalisation (upper case, letters and digits only), and each field
accepts both the layout document's spelling and the text file's short header, so a header that
matches neither fails closed with :class:`RegistryFormatError` rather than loading a wrong column.
The exact header of the current download is recorded by ``scripts/profile_registry.py`` (an owner
step: the FAA hosts are not reachable from the build container). No real registry row is ever
committed; tests use synthetic files.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from datetime import date
from pathlib import Path

ENCODING = "utf-8-sig"  # tolerate a byte-order mark; any other non-UTF-8 byte fails closed

# our field -> accepted header spellings (the layout document's name, the text file's short name)
MASTER_FIELDS: Mapping[str, tuple[str, ...]] = {
    "n_number": ("N-Number", "N-NUMBER"),
    "serial_number": ("Serial Number", "SERIAL NUMBER"),
    "mfr_mdl_code": ("Aircraft Mfr Model Code", "MFR MDL CODE"),
    "year_mfr": ("Year Mfr", "YEAR MFR"),
    "type_aircraft": ("Type Aircraft", "TYPE AIRCRAFT"),
    "status_code": ("Status Code", "STATUS CODE"),
    "cert_issue_date": ("Certificate Issue Date", "CERT ISSUE DATE"),
}
ACFTREF_FIELDS: Mapping[str, tuple[str, ...]] = {
    "code": ("Aircraft Manufacturer, Model and Series Code", "CODE"),
    "manufacturer": ("Aircraft Manufacturer Name", "MFR"),
    "model": ("Model Name", "MODEL"),
    "type_aircraft": ("Type Aircraft", "TYPE-ACFT"),
    "engines": ("Number of Engines", "NO-ENG"),
    "seats": ("Number of Seats", "NO-SEATS"),
}
# Columns the master file has that this loader must never read (names and addresses).
PERSONAL_COLUMNS: tuple[str, ...] = (
    "NAME", "STREET", "STREET2", "CITY", "STATE", "ZIP CODE", "COUNTY", "COUNTRY",
    "OTHER NAMES(1)", "OTHER NAMES(2)", "OTHER NAMES(3)", "OTHER NAMES(4)", "OTHER NAMES(5)",
    "Registrant's Name", "Street1", "Street2", "Registrant's City", "Registrant's State",
    "Registrant's Zip Code", "Other Name 1", "Other Name 2", "Other Name 3", "Other Name 4",
    "Other Name 5",
)  # fmt: skip

_N_NUMBER = re.compile(r"[0-9][0-9A-Z]{0,4}")  # stored without the leading N; starts with a digit
_CODE = re.compile(r"[0-9A-Z]{7}")
_DATE = re.compile(r"\d{8}")  # YYYYMMDD in the text files


class RegistryFormatError(Exception):
    """The file is missing, empty, not UTF-8, or lacks a required column."""


@dataclass(frozen=True)
class RegistryRecord:
    """One registered aircraft: identity fields only, never the registrant."""

    n_number: str
    serial_number: str
    mfr_mdl_code: str
    year_mfr: int | None
    type_aircraft: str
    status_code: str
    cert_issue_date: date | None


@dataclass(frozen=True)
class AircraftReference:
    """One make, model and series row of the reference file."""

    code: str
    manufacturer: str
    model: str
    type_aircraft: str
    engines: int | None
    seats: int | None


@dataclass(frozen=True)
class Reject:
    line: int
    reason: str


@dataclass(frozen=True)
class Loaded:
    records: list
    rejects: list[Reject]
    header: tuple[str, ...]


def norm_header(name: str) -> str:
    return re.sub(r"[^0-9A-Z]", "", name.upper())


def norm_n_number(text: str) -> str:
    """``N12345`` and ``12345`` are the same mark; the registry stores it without the N."""
    t = text.strip().upper()
    return t[1:] if t.startswith("N") and len(t) > 1 and t[1].isdigit() else t


def _index(header: Sequence[str], wanted: Mapping[str, tuple[str, ...]], what: str) -> dict:
    by_norm = {norm_header(h): i for i, h in enumerate(header)}
    index, missing = {}, []
    for name, spellings in wanted.items():
        hits = {by_norm[norm_header(s)] for s in spellings if norm_header(s) in by_norm}
        if len(hits) != 1:
            missing.append(name)
        else:
            index[name] = hits.pop()
    if missing:
        raise RegistryFormatError(f"{what} header lacks column(s) for: {', '.join(missing)}")
    return index


def _opt_int(text: str) -> int | None:
    t = text.strip()
    return None if t == "" else int(t)


def _opt_date(text: str) -> date | None:
    t = text.strip()
    if t == "":
        return None
    if not _DATE.fullmatch(t):
        raise ValueError(f"date is not YYYYMMDD: {t!r}")
    return date(int(t[:4]), int(t[4:6]), int(t[6:]))


def _master(row: Sequence[str], ix: Mapping[str, int]) -> RegistryRecord:
    def col(name: str) -> str:
        return row[ix[name]].strip()

    n = norm_n_number(col("n_number"))
    if not _N_NUMBER.fullmatch(n):
        raise ValueError(f"N-number is malformed: {n!r}")
    code = col("mfr_mdl_code")
    if code and not _CODE.fullmatch(code):
        raise ValueError(f"manufacturer/model code is not 7 characters: {code!r}")
    return RegistryRecord(
        n_number=n,
        serial_number=col("serial_number"),
        mfr_mdl_code=code,
        year_mfr=_opt_int(col("year_mfr")),
        type_aircraft=col("type_aircraft"),
        status_code=col("status_code"),
        cert_issue_date=_opt_date(col("cert_issue_date")),
    )


def _reference(row: Sequence[str], ix: Mapping[str, int]) -> AircraftReference:
    def col(name: str) -> str:
        return row[ix[name]].strip()

    code = col("code")
    if not _CODE.fullmatch(code):
        raise ValueError(f"code is not 7 characters: {code!r}")
    return AircraftReference(
        code=code,
        manufacturer=col("manufacturer"),
        model=col("model"),
        type_aircraft=col("type_aircraft"),
        engines=_opt_int(col("engines")),
        seats=_opt_int(col("seats")),
    )


def _load(path: Path, wanted: Mapping[str, tuple[str, ...]], parse, what: str) -> Loaded:
    try:
        data = path.read_bytes()
    except FileNotFoundError as exc:
        raise RegistryFormatError(f"{what} file not found: {path}") from exc
    try:
        text = data.decode(ENCODING)
    except UnicodeDecodeError as exc:
        line = data.count(b"\n", 0, exc.start) + 1
        raise RegistryFormatError(
            f"{what} file is not valid UTF-8 at byte offset {exc.start} (line {line}): {path}"
        ) from exc
    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        header = next(reader)
    except StopIteration as exc:
        raise RegistryFormatError(f"{what} file is empty: {path}") from exc
    ix = _index(header, wanted, what)
    records, rejects = [], []
    for line, row in enumerate(reader, start=2):
        if not row:
            continue
        if len(row) != len(header):
            rejects.append(Reject(line, f"{len(row)} columns, header has {len(header)}"))
            continue
        try:
            records.append(parse(row, ix))
        except ValueError as exc:
            rejects.append(Reject(line, str(exc)))
    return Loaded(records, rejects, tuple(header))


def load_master(path: str | Path) -> Loaded:
    """Load the registration master file; malformed rows become :class:`Reject` with a line."""
    return _load(Path(path), MASTER_FIELDS, _master, "registry master")


def load_reference(path: str | Path) -> Loaded:
    """Load the aircraft reference file."""
    return _load(Path(path), ACFTREF_FIELDS, _reference, "aircraft reference")


@dataclass(frozen=True)
class Registry:
    """The two files joined: N-number -> record, code -> reference."""

    by_n_number: Mapping[str, RegistryRecord]
    by_code: Mapping[str, AircraftReference]
    duplicate_n_numbers: frozenset[str]

    @classmethod
    def build(cls, master: Sequence[RegistryRecord], refs: Sequence[AircraftReference]):
        seen: dict[str, RegistryRecord] = {}
        dupes: set[str] = set()
        for rec in master:
            if rec.n_number in seen:
                dupes.add(rec.n_number)
            seen[rec.n_number] = rec
        for n in dupes:  # an ambiguous mark settles nothing
            del seen[n]
        return cls(seen, {r.code: r for r in refs}, frozenset(dupes))

    def lookup(self, n_number: str) -> RegistryRecord | None:
        return self.by_n_number.get(norm_n_number(n_number))

    def reference(self, rec: RegistryRecord) -> AircraftReference | None:
        return self.by_code.get(rec.mfr_mdl_code)


def contract_failures(rec: RegistryRecord, ref: AircraftReference | None) -> list[str]:
    """Phase two's frozen contract check for a served registry record (no truth store used)."""
    out = []
    for f in fields(RegistryRecord):
        if getattr(rec, f.name) is None and f.name not in ("year_mfr", "cert_issue_date"):
            out.append(f"missing:{f.name}")
    if not _N_NUMBER.fullmatch(rec.n_number):
        out.append("malformed:n_number")
    if rec.year_mfr is not None and not 1900 <= rec.year_mfr <= 2100:
        out.append("implausible:year_mfr")
    if rec.mfr_mdl_code and ref is None:
        out.append("unresolved:mfr_mdl_code")
    if ref is not None and ref.code != rec.mfr_mdl_code:
        out.append("mismatch:mfr_mdl_code")
    return out
