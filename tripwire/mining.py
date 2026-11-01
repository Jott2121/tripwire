"""Mine real, unplanted record disagreements between FAA SDRs and the aircraft registry.

Phase two (``docs/PREREGISTRATION-phase2-real-data.md``) asks agents about errors nobody planted.
These rules find them. They are deterministic, use no model, and are frozen before any agent sees
a mined item: the preregistration records :func:`fingerprint` (the sha256 of this file) at freeze,
and a run refuses a different file. A rule either **settles** an item (the records cannot both be
right) or **skips** it with a reason; it never guesses. Rules whose answer depends on judgment are
kept apart as **unsettled** and enter a study only with a recorded owner label.

Rules, version ``mine-v2``. ``mine-v0`` and ``mine-v1`` were each profiled once on real data
(2026-11-01) and tightened from what the counts and a sample of items showed; no agent has seen
an item. The history is in ``docs/PREREGISTRATION-phase2-real-data.md``.

* Serials are compared on a **key**: letters and digits only, upper case, leading zeros
  dropped; a placeholder (``UNK``, ``NONE``, ``N/A`` ...) or an all-zero serial counts as blank.
  The SDR serial is digits only in about 99% of real 2026 rows while registry serials often carry
  letters, so an all-digit SDR serial equal to the registry serial's digits also agrees.
* ``S1 serial``: the two serial keys differ in a way none of S2's tests explains. Settled.
* ``S2 serial_partial``: one key contains the other; or they differ only by zeros (a dropped
  or extra zero, as in ``3100412`` against ``31000412``); or one ends with the part of the other
  after a model prefix (``4200071`` against ``520-0071``). Each may be notation or a slip on the
  same airframe, not another one. Unsettled.
* ``T1 total_time``: the SDR's aircraft total time in hours exceeds the hours in the calendar from
  1 January of the registry's year of manufacture to the report date. Physically impossible.
  Settled.
* ``D1 before_build``: the report's year is before the registry's year of manufacture. Settled.
  Expect almost none in one report year: the registry rarely lists a build year in the future.
* ``M1 make``: no word of the SDR make (a 3 to 6 letter code in the real file) starts a word of
  the registry manufacturer name, or starts the name with its spaces removed. Unsettled: makers
  rename and merge, and the code is an abbreviation.

Every rule skips a row whose N-number is blank, absent from the registry, or listed twice, and a
row whose registration was issued after the report (the mark may have moved to another
aircraft). ``T1`` and ``D1`` also skip when ``S1`` fires (the registry may describe another
airframe) or the year of manufacture is blank. A row with any finding gives no control.

Items that every applicable rule checked and found consistent are **controls**: the expected
answer is that the records agree, which measures false alarms on real data.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from tripwire.registry import Registry, RegistryRecord, norm_n_number
from tripwire.sdr import SDRRecord

RULES_VERSION = "mine-v2"
RULES = {
    "S1": "serial: SDR aircraft serial differs from the registry's for the N-number",
    "S2": "serial_partial: one serial key contains the other (unsettled)",
    "T1": "total_time: SDR total hours exceed calendar hours since 1 Jan of the year built",
    "D1": "before_build: report year is before the registry's year of manufacture",
    "M1": "make: no SDR make word starts a registry manufacturer word (unsettled)",
}
SETTLED = ("S1", "T1", "D1")
_WORD = re.compile(r"[A-Z0-9]+")
# Words too generic to count as a shared maker name.
_STOP = frozenset({"AIRCRAFT", "AVIATION", "AIRCRAFTS", "INC", "CORP", "CO", "CORPORATION",
                   "COMPANY", "LTD", "LLC", "THE", "AND", "OF", "INDUSTRIES", "INTL",
                   "INTERNATIONAL", "AIRPLANE", "AEROSPACE", "GROUP"})  # fmt: skip


@dataclass(frozen=True)
class Item:
    """One mined question: a field on which the SDR and the registry disagree (or agree)."""

    rule: str  # S1/T1/D1/M1, or "control"
    control_number: str
    n_number: str
    field: str
    sdr_value: str
    registry_value: str
    expected: str  # "disagree" or "agree"


@dataclass(frozen=True)
class Skip:
    control_number: str
    reason: str


@dataclass(frozen=True)
class Mined:
    settled: list[Item]
    unsettled: list[Item]
    controls: list[Item]
    skips: list[Skip]

    def aircraft(self) -> dict[str, int]:
        """Distinct aircraft (N-numbers) per rule: one operator can report one airframe often."""
        seen: dict[str, set[str]] = {r: set() for r in RULES}
        seen["controls"] = set()
        for item in [*self.settled, *self.unsettled]:
            seen[item.rule].add(item.n_number)
        seen["controls"] = {c.n_number for c in self.controls}
        return {k: len(v) for k, v in seen.items()}

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {r: 0 for r in RULES}
        for item in [*self.settled, *self.unsettled]:
            out[item.rule] += 1
        out["controls"] = len(self.controls)
        reasons: dict[str, int] = {}
        for s in self.skips:
            reasons[s.reason] = reasons.get(s.reason, 0) + 1
        out |= {f"skip:{k}": v for k, v in sorted(reasons.items())}
        return out


def fingerprint() -> str:
    """sha256 of this file: what the preregistration freezes."""
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


# Serial placeholders seen in maintenance records, compared after norm_serial.
_SERIAL_BLANKS = frozenset({"UNK", "UNKN", "UNKNOWN", "NA", "NONE", "TBD", "NIL", "NS", "NSN"})


def norm_serial(text: str) -> str:
    return "".join(_WORD.findall(text.upper()))


def serial_key(text: str) -> str:
    """Letters and digits, upper case, leading zeros dropped; "" for a blank or placeholder."""
    key = norm_serial(text).lstrip("0")
    return "" if key in _SERIAL_BLANKS else key


def serial_relation(sdr_serial: str, reg_serial: str) -> str | None:
    """``agree``, ``partial`` or ``differ``; ``None`` when either side is blank."""
    a, b = serial_key(sdr_serial), serial_key(reg_serial)
    if not a or not b:
        return None
    if a == b or (a.isdigit() and "".join(ch for ch in b if ch.isdigit()).lstrip("0") == a):
        return "agree"
    if a in b or b in a:
        return "partial"
    if a.replace("0", "") == b.replace("0", ""):
        return "partial"  # a dropped or extra zero: a typo of the same serial, or notation
    for raw, other in ((reg_serial, a), (sdr_serial, b)):
        tail = _hyphen_tail(raw)
        if len(tail) >= 3 and other.endswith(tail):
            return "partial"  # the other side ends with the part after a model prefix
    return "differ"


def _hyphen_tail(raw: str) -> str:
    """The serial's last letters-and-digits run when it has a separator (``520-0071`` ->
    ``0066``); "" when it has none."""
    runs = _WORD.findall(raw.upper())
    return runs[-1] if len(runs) > 1 else ""


def make_matches(sdr_make: str, manufacturer: str) -> bool:
    """Whether the SDR's make code names the registry manufacturer (see rule M1)."""
    joined = "".join(_WORD.findall(manufacturer.upper()))
    targets = [*words(manufacturer), joined]
    return any(t.startswith(w) for w in words(sdr_make) if len(w) >= 3 for t in targets)


def words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.upper()) if w not in _STOP}


def calendar_hours(year_built: int, on: date) -> float:
    """Hours from 1 January of ``year_built`` to the end of day ``on``: the most an airframe can
    have flown by then."""
    return ((on - date(year_built, 1, 1)).days + 1) * 24.0


def _skip_reason(sdr: SDRRecord, reg: Registry) -> tuple[RegistryRecord | None, str | None]:
    n = norm_n_number(sdr.registry_n_number)
    if not n:
        return None, "no_n_number"
    if n in reg.duplicate_n_numbers:
        return None, "duplicate_n_number"
    rec = reg.lookup(n)
    if rec is None:
        return None, "not_in_registry"
    if rec.cert_issue_date is not None and rec.cert_issue_date > sdr.difficulty_date:
        return None, "registered_after_report"
    return rec, None


def mine_one(sdr: SDRRecord, reg: Registry) -> tuple[list[Item], list[Item], Skip | None]:
    """``(settled_or_unsettled, controls, skip)`` for one SDR row."""
    rec, reason = _skip_reason(sdr, reg)
    if rec is None:
        return [], [], Skip(sdr.control_number, reason or "")
    found: list[Item] = []
    agree: list[Item] = []

    def item(rule: str, field: str, a: str, b: str, expected: str) -> Item:
        return Item(rule, sdr.control_number, rec.n_number, field, a, b, expected)

    relation = serial_relation(sdr.aircraft_serial_number, rec.serial_number)
    serial_differs = relation == "differ"
    if relation is not None:
        rule = {"agree": "control", "partial": "S2", "differ": "S1"}[relation]
        expected = "agree" if relation == "agree" else "disagree"
        (agree if relation == "agree" else found).append(item(
            rule, "aircraft_serial_number", sdr.aircraft_serial_number, rec.serial_number,
            expected))  # fmt: skip
    if rec.year_mfr is not None and not serial_differs:
        built = str(rec.year_mfr)
        if sdr.difficulty_date.year < rec.year_mfr:
            found.append(item("D1", "difficulty_date", sdr.difficulty_date.isoformat(), built,
                              "disagree"))  # fmt: skip
        else:
            hours = sdr.aircraft_total_time_hours
            if hours is not None:
                limit = calendar_hours(rec.year_mfr, sdr.difficulty_date)
                bad = hours > limit
                (found if bad else agree).append(item(
                    "T1" if bad else "control", "aircraft_total_time", f"{hours:g}", built,
                    "disagree" if bad else "agree"))  # fmt: skip
    ref = reg.reference(rec)
    if ref is not None and sdr.aircraft_make.strip():
        if not make_matches(sdr.aircraft_make, ref.manufacturer):
            found.append(item("M1", "aircraft_make", sdr.aircraft_make, ref.manufacturer,
                              "disagree"))  # fmt: skip
    controls = [] if found else agree
    return found, controls, None


def mine(sdrs: Iterable[SDRRecord], reg: Registry) -> Mined:
    settled: list[Item] = []
    unsettled: list[Item] = []
    controls: list[Item] = []
    skips: list[Skip] = []
    for sdr in sdrs:
        found, ok, skip = mine_one(sdr, reg)
        if skip:
            skips.append(skip)
        settled += [i for i in found if i.rule in SETTLED]
        unsettled += [i for i in found if i.rule not in SETTLED]
        controls += ok
    return Mined(settled, unsettled, controls, skips)


def sample(items: Sequence[Item], n: int, salt: str) -> list[Item]:
    """A deterministic sample: items ordered by sha256 of ``salt`` and their identity."""

    def key(i: Item) -> str:
        return hashlib.sha256(f"{salt}:{i.rule}:{i.control_number}:{i.field}".encode()).hexdigest()

    return sorted(items, key=key)[:n]
