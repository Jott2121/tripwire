"""Spec limits transcribed from public standards, each row with its citation.

Each row says how confident we are and whether it was read from the PDF itself.
The NASA-STD-8739.4A Table 12-1 rows were read from the official PDF by the
maintainer on 2026-10-11 and relayed to the build (the agent sandbox cannot
reach ``standards.nasa.gov``). Nothing here is invented: a row without a source
URL cannot be constructed, and a PDF-verified row must be HIGH confidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from tripwire import standards as s
from tripwire.contract import ContractError, Provenance, Quantity, SpecLimit

NASA_8739_4A = "NASA-STD-8739.4A"
NASA_8739_4A_REVISION = "chg4"
NASA_8739_4A_URL = s.SRC_NASA_8739_4A
TRANSCRIBED_AT = datetime(2026, 10, 11, tzinfo=UTC)


@dataclass(frozen=True)
class PublicSpecRow:
    """One transcribed limit: value in the standard's own unit plus its citation."""

    spec_id: str
    revision: str
    parameter: str
    applies_to: str
    minimum_value: float
    unit: s.Unit
    source_url: str
    confidence: s.Confidence
    verified_against_pdf: bool
    note: str

    def __post_init__(self) -> None:
        if not self.source_url.startswith("https://"):
            raise ContractError("a public spec row must cite a source URL")
        if self.verified_against_pdf and self.confidence is not s.Confidence.HIGH:
            raise ContractError("a PDF-verified row must be HIGH confidence")


_CRIMP_NOTE = (
    "Table 12-1 crimp tensile strength, minimum, newtons (pounds): '{n} ({lb})'. Read from "
    "the official PDF by the maintainer, 2026-10-11."
)


def _crimp(awg: int, newtons: float, pounds: float) -> PublicSpecRow:
    return PublicSpecRow(
        NASA_8739_4A,
        NASA_8739_4A_REVISION,
        "crimp_tensile_min",
        f"AWG {awg}",
        newtons,
        s.Unit.NEWTON,
        NASA_8739_4A_URL,
        s.Confidence.HIGH,
        True,
        _CRIMP_NOTE.format(n=int(newtons), lb=int(pounds)),
    )


# Full Table 12-1 as read from the official PDF (AWG: N (lb)). Note AWG 26 and 24
# share 36 N (8 lb); an earlier draft had mis-mapped the AWG 28 value to AWG 26.
NASA_8739_4A_CRIMP_TENSILE: tuple[PublicSpecRow, ...] = (
    _crimp(28, 22.0, 5.0),
    _crimp(26, 36.0, 8.0),
    _crimp(24, 36.0, 8.0),
    _crimp(22, 57.0, 13.0),
    _crimp(20, 92.0, 21.0),
    _crimp(18, 142.0, 32.0),
    _crimp(16, 183.0, 41.0),
    _crimp(14, 290.0, 65.0),
    _crimp(12, 459.0, 103.0),
    _crimp(10, 707.0, 159.0),
    _crimp(8, 1281.0, 288.0),
)

PUBLIC_SPEC_ROWS: tuple[PublicSpecRow, ...] = NASA_8739_4A_CRIMP_TENSILE


def public_spec_limits(rows: tuple[PublicSpecRow, ...] = PUBLIC_SPEC_ROWS) -> tuple[SpecLimit, ...]:
    """Build contract :class:`SpecLimit` records from the transcribed rows."""
    out = []
    for r in rows:
        prov = Provenance(
            source="public-standard",
            source_url=r.source_url,
            revision=f"{r.spec_id} {r.revision}; confidence={r.confidence.value}; "
            f"verified_against_pdf={str(r.verified_against_pdf).lower()}",
            retrieved_at=TRANSCRIBED_AT,
        )
        out.append(
            SpecLimit(
                spec_id=r.spec_id,
                revision=r.revision,
                parameter=r.parameter,
                applies_to=r.applies_to,
                source_url=r.source_url,
                provenance=prov,
                minimum=Quantity(r.minimum_value, r.unit),
            )
        )
    return tuple(out)
