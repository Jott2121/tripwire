#!/usr/bin/env python
"""Profile a locally downloaded FAA SDR CSV to close the M0 open items.

    python scripts/profile_sdr.py data/raw/SDR-2026.csv --out reports/sdr_profile_2026.json

Prints and writes statistics only, never narrative text: free-text columns
(``Discrepancy``, makes, models, names, numbers, serials, locations) contribute
fill rate, length and shape counts only; short code columns list their distinct
values with counts. The report is safe to commit; the input file is not
(real SDR rows are never committed; PR #6 gitignores ``data/raw/``).

Answers, keyed as in ``tripwire.standards.UNRESOLVED``: ``column_count``,
``encoding``, ``submission_tz``, ``whole_hours``, ``registry_prefix``,
``control_number_shape``; ``code_tables`` gets the distinct values seen.
Fails closed (exit 2) on a missing, empty, or undecodable file.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter
from pathlib import Path

from tripwire import standards as s

CANDIDATE_ENCODINGS = ("latin-1", "utf-8", "cp1252")
MAX_CODE_LENGTH = 4  # a CODE column value longer than this is reported by shape only
_DIGITS = re.compile(r"\d+")
_DECIMAL = re.compile(r"\d+\.\d+")
_ALPHA = re.compile(r"[A-Za-z]+")
_ALNUM = re.compile(r"[A-Za-z0-9]+")
_MDY = re.compile(r"\d{2}/\d{2}/\d{4}")
_ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?([+-]\d{2}:\d{2}|Z)")
_OFFSET = re.compile(r"([+-]\d{2}:\d{2}|Z)$")


class ProfileError(Exception):
    """The file cannot be vouched for."""


def shape(value: str) -> str:
    for name, rx in (
        ("iso_datetime", _ISO),
        ("date_mdy", _MDY),
        ("decimal", _DECIMAL),
        ("digits", _DIGITS),
        ("alpha", _ALPHA),
        ("alnum", _ALNUM),
    ):
        if rx.fullmatch(value):
            return name
    return "other"


def decode_report(data: bytes) -> dict[str, int | None]:
    """Decode-error count under each candidate encoding (None = clean)."""
    out: dict[str, int | None] = {}
    for enc in CANDIDATE_ENCODINGS:
        try:
            data.decode(enc)
            out[enc] = 0
        except UnicodeDecodeError:
            out[enc] = sum(1 for _ in data.decode(enc, "replace").split("�")) - 1
    return out


def _column_stats(name: str, values: list[str]) -> dict:
    std = s.SDR_FIELD_BY_NAME.get(name)
    filled = [v for v in values if v != ""]
    stats: dict = {
        "rows": len(values),
        "filled": len(filled),
        "fill_rate": round(len(filled) / len(values), 4) if values else 0.0,
        "shapes": dict(Counter(shape(v) for v in filled).most_common()),
        "length_min": min((len(v) for v in filled), default=0),
        "length_max": max((len(v) for v in filled), default=0),
        "expected_kind": std.kind.value if std else None,
        "expected_unit": std.unit.value if std else None,
    }
    numeric: list[float] = []
    for v in filled:
        try:
            numeric.append(float(v))
        except ValueError:
            pass
    if numeric and std and std.kind in (s.FieldKind.NUMERIC, s.FieldKind.INTEGER):
        ordered = sorted(numeric)
        stats["numeric"] = {
            "parsed": len(numeric),
            "min": ordered[0],
            "median": ordered[len(ordered) // 2],
            "max": ordered[-1],
            "integer_fraction": round(sum(1 for x in numeric if x == int(x)) / len(numeric), 4),
        }
    if std and std.kind is s.FieldKind.CODE:
        short = [v for v in filled if len(v) <= MAX_CODE_LENGTH]
        stats["distinct_values"] = dict(Counter(short).most_common(50))
        stats["values_longer_than_code"] = len(filled) - len(short)
    return stats


def _answers(header: list[str], columns: dict[str, list[str]], enc: dict, stats: dict) -> dict:
    expected = list(s.SDR_COLUMNS)
    reg = [v for v in columns.get("RegistryNNumber", []) if v]
    cn = [v for v in columns.get("OperatorControlNumber", []) if v]
    sub = [v for v in columns.get("SubmissionDate", []) if v]
    diff = columns.get("DifficultyDate", [])
    offsets_by_month: dict[str, Counter] = {}
    for v in sub:
        m = _OFFSET.search(v)
        if m:
            offsets_by_month.setdefault(v[:7], Counter())[m.group(1)] += 1
    lag_days: list[int] = []
    for d, t in zip(diff, columns.get("SubmissionDate", []), strict=False):
        try:
            lag_days.append(
                (s.parse_submission_datetime(t).date() - s.parse_difficulty_date(d)).days
            )
        except s.StandardsError:
            continue
    lag_days.sort()
    whole = {
        c: st["numeric"]["integer_fraction"]
        for c, st in stats.items()
        if "numeric" in st and s.SDR_FIELD_BY_NAME[c].unit is s.Unit.HOUR
    }
    return {
        "column_count": {
            "header_columns": len(header),
            "expected_named_columns": len(expected),
            "missing_from_file": [c for c in expected if c not in header],
            "extra_in_file": [c for c in header if c not in expected],
            "order_matches_expected": [c for c in header if c in expected]
            == [c for c in expected if c in header],
        },
        "encoding": enc,
        "submission_tz": {m: dict(c) for m, c in sorted(offsets_by_month.items())},
        "whole_hours": whole,
        "registry_prefix": {
            "filled": len(reg),
            "starts_with_n": sum(1 for v in reg if v.upper().startswith("N")),
            "valid_without_prefix": sum(1 for v in reg if s.is_valid_n_number(v, prefix=False)),
            "valid_with_prefix": sum(1 for v in reg if s.is_valid_n_number(v, prefix=True)),
        },
        "control_number_shape": {
            "filled": len(cn),
            "matches_documented_shape": sum(1 for v in cn if _safe(s.parse_control_number, v)),
            "length_histogram": dict(sorted(Counter(len(v) for v in cn).items())),
        },
        "difficulty_to_submission_lag_days": {
            "n": len(lag_days),
            "negative": sum(1 for x in lag_days if x < 0),
            "p50": lag_days[len(lag_days) // 2] if lag_days else None,
            "p95": lag_days[int(len(lag_days) * 0.95)] if lag_days else None,
            "max": lag_days[-1] if lag_days else None,
        },
    }


def _safe(fn, value: str) -> bool:
    try:
        fn(value)
    except s.StandardsError:
        return False
    return True


def profile(path: str | Path, max_rows: int | None = None) -> dict:
    """Profile one SDR CSV. Raises :class:`ProfileError` when it cannot vouch for the file."""
    path = Path(path)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ProfileError(f"cannot read {path}: {exc}") from exc
    if not data:
        raise ProfileError(f"{path} is empty")
    enc = decode_report(data)
    text = data.decode(s.SDR_ENCODING, errors="replace")  # never crash; errors are in `encoding`
    rows = list(csv.reader(text.splitlines()))
    if not rows or not rows[0]:
        raise ProfileError(f"{path} has no header")
    header = rows[0]
    body = rows[1:] if max_rows is None else rows[1 : 1 + max_rows]
    bad_width = sum(1 for r in body if len(r) != len(header))
    good = [r for r in body if len(r) == len(header)]
    columns = {name: [r[i] for r in good] for i, name in enumerate(header)}
    stats = {name: _column_stats(name, columns[name]) for name in header}
    return {
        "file": path.name,
        "bytes": len(data),
        "data_rows": len(body),
        "rows_with_wrong_column_count": bad_width,
        "answers": _answers(header, columns, enc, stats),
        "columns": stats,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "csv", type=Path, help="locally downloaded SDR-<year>.csv (never committed)"
    )
    parser.add_argument("--out", type=Path, help="write the JSON report here")
    parser.add_argument("--max-rows", type=int, default=None, help="profile only the first N rows")
    args = parser.parse_args(argv)
    try:
        report = profile(args.csv, args.max_rows)
    except ProfileError as exc:
        print(f"profile FAILED CLOSED: {exc}", file=sys.stderr)
        return 2
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered + "\n", encoding="utf-8")
        print(f"wrote {args.out}")
    print(json.dumps(report["answers"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
