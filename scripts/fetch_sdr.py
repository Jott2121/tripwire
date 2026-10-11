#!/usr/bin/env python
"""Download a public FAA SDR annual CSV and write a provenance manifest.

Stdlib ``urllib`` only. The download function takes an injectable ``opener`` so
tests can feed a fake response and never touch the network; the real network
fetch is only exercised when a human runs this script.

    python scripts/fetch_sdr.py --year 2026

Writes ``data/raw/SDR-<year>.csv`` (gitignored -- real rows are never
committed; see ``docs/DATA.md``) and ``data/manifest/SDR-<year>.json`` recording
the url, byte count, sha256, CSV row count, and UTC retrieval time.
"""

import argparse
import csv
import hashlib
import io
import json
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

URL_TEMPLATE = "https://external.apic4e.faa.gov/sdrs/retrieve/SDR-{year}.csv"
ENCODING = "latin-1"


def sdr_url(year: int) -> str:
    return URL_TEMPLATE.format(year=year)


def _row_count(data: bytes) -> int:
    """Number of CSV data rows (excluding the header), robust to quoted fields."""
    text = data.decode(ENCODING)
    rows = sum(1 for _ in csv.reader(io.StringIO(text)))
    return max(rows - 1, 0)


def fetch_year(
    year: int,
    dest_dir: str | Path | None = None,
    opener=urllib.request.urlopen,
    now: datetime | None = None,
) -> Path:
    """Fetch the SDR CSV for ``year`` and write the CSV plus its manifest.

    ``opener(url)`` must return a readable response (the real default is
    :func:`urllib.request.urlopen`); tests pass a fake. Returns the manifest path.
    """
    base = Path("data") if dest_dir is None else Path(dest_dir)
    url = sdr_url(year)

    response = opener(url)
    try:
        data = response.read()
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()

    raw_dir = base / "raw"
    manifest_dir = base / "manifest"
    raw_dir.mkdir(parents=True, exist_ok=True)
    manifest_dir.mkdir(parents=True, exist_ok=True)

    csv_path = raw_dir / f"SDR-{year}.csv"
    csv_path.write_bytes(data)

    retrieved = (now or datetime.now(UTC)).astimezone(UTC)
    manifest = {
        "url": url,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "row_count": _row_count(data),
        "retrieved_utc": retrieved.isoformat(),
    }
    manifest_path = manifest_dir / f"SDR-{year}.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--year", type=int, required=True, help="SDR annual file year, e.g. 2026")
    parser.add_argument("--dest-dir", default=None, help="base dir (default: data/)")
    args = parser.parse_args(argv)
    manifest_path = fetch_year(args.year, dest_dir=args.dest_dir)
    print(f"wrote {manifest_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
