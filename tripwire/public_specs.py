"""Spec limits transcribed from public standards, each row with its citation.

Only rows whose value appeared in at least two independent excerpts of the
standard are entered, and each row says how confident we are and whether it
has been checked against the PDF itself (the agent sandbox could not reach
``standards.nasa.gov``; see ``docs/MEASUREMENT_STANDARDS.md``). Nothing here is
invented: a row without a source URL cannot be constructed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from tripwire import standards as s
from tripwire.contract import ContractError, Provenance, Quantity, SpecLimit

NASA_8739_4A = "NASA-STD-8739.4A"
NASA_8739_4A_REVISION = "chg4"
NASA_8739_4A_URL = s.SRC_NASA_8739_4A
# Secondary copies the values were cross-checked against (third party; not authoritative).
NASA_8739_4A_SECONDARY = (
    "https://www.checkline.com/res/products/126677/wire_pull_test_standards.pdf",
    "https://www.slideshare.net/slideshow/nasa-std87394/75196591",
)
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
    "Table 12-1 crimp tensile strength, minimum, newtons (pounds). Value seen as "
    "'{n} ({lb})' in two excerpts and mapped to this gauge by the secondary copies; "
    "not yet read from the PDF."
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
        s.Confidence.MEDIUM,
        False,
        _CRIMP_NOTE.format(n=int(newtons), lb=int(pounds)),
    )


# Only gauges whose newton AND pound figures agreed across excerpts are entered.
# AWG 20, 18, 16 and 14 appeared with conflicting or unaligned figures: omitted.
NASA_8739_4A_CRIMP_TENSILE: tuple[PublicSpecRow, ...] = (
    _crimp(26, 22.0, 5.0),
    _crimp(24, 36.0, 8.0),
    _crimp(22, 57.0, 13.0),
    _crimp(12, 459.0, 103.0),
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
