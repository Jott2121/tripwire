"""Mine real, unplanted record disagreements between FAA SDRs and the aircraft registry.

Phase two (``docs/PREREGISTRATION-phase2-real-data.md``) asks agents about errors nobody planted.
These rules find them. They are deterministic, use no model, and are frozen before any agent sees
a mined item: the preregistration records :func:`fingerprint` (the sha256 of this file) at freeze,
and a run refuses a different file. A rule either **settles** an item (the records cannot both be
right) or **skips** it with a reason; it never guesses. Rules whose answer depends on judgment are
kept apart as **unsettled** and enter a study only with a recorded owner label.

Rules, version ``mine-v0``:

* ``S1 serial``: the SDR's aircraft serial number differs from the registry's for the same
  N-number (compared on letters and digits only, upper case). Settled.
* ``T1 total_time``: the SDR's aircraft total time in hours exceeds the hours in the calendar from
  1 January of the registry's year of manufacture to the report date. Physically impossible.
  Settled.
* ``D1 before_build``: the report's year is before the registry's year of manufacture. Settled.
* ``M1 make``: the SDR make shares no word with the registry manufacturer name. Unsettled: makers
  rename and merge, so a mismatch may be a stale name, not an error.

Every rule skips a row whose N-number is blank, absent from the registry, or listed twice, and a
row whose registration was issued after the report (the mark may have moved to another
aircraft). ``T1`` and ``D1`` also skip when ``S1`` fires (the registry may describe another
airframe) or the year of manufacture is blank.

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

RULES_VERSION = "mine-v0"
RULES = {
    "S1": "serial: SDR aircraft serial differs from the registry's for the N-number",
    "T1": "total_time: SDR total hours exceed calendar hours since 1 Jan of the year built",
    "D1": "before_build: report year is before the registry's year of manufacture",
    "M1": "make: SDR make shares no word with the registry manufacturer (unsettled)",
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


def norm_serial(text: str) -> str:
    return "".join(_WORD.findall(text.upper()))


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

    s_sdr, s_reg = norm_serial(sdr.aircraft_serial_number), norm_serial(rec.serial_number)
    serial_differs = bool(s_sdr and s_reg and s_sdr != s_reg)
    if s_sdr and s_reg:
        target = found if serial_differs else agree
        target.append(item("S1" if serial_differs else "control", "aircraft_serial_number",
                           sdr.aircraft_serial_number, rec.serial_number,
                           "disagree" if serial_differs else "agree"))  # fmt: skip
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
        if not words(sdr.aircraft_make) & words(ref.manufacturer):
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
