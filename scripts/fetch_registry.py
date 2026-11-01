#!/usr/bin/env python
"""Download the FAA releasable aircraft database and write a provenance manifest.

Stdlib only. The download takes an injectable ``opener`` so tests never touch the network; the
real fetch runs only when a person runs this script (the FAA hosts are not reachable from the
build container).

    python scripts/fetch_registry.py

Writes ``data/raw/registry/MASTER.txt`` and ``ACFTREF.txt`` (gitignored: real rows are never
committed, and the master file holds registrants' names and addresses) and
``data/manifest/registry.json`` with the url, the zip's byte count and sha256, each extracted
member's sha256, and the UTC retrieval time. Only the two named members are extracted, each to a
fixed file name, so a member path inside the archive can never choose where a file lands.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import urllib.request
import zipfile
from datetime import UTC, datetime
from pathlib import Path

URL = "https://registry.faa.gov/database/ReleasableAircraft.zip"
LAYOUT_URL = "https://registry.faa.gov/database/ardata.pdf"
MEMBERS = ("MASTER.txt", "ACFTREF.txt")


class FetchError(RuntimeError):
    pass


def _member(zf: zipfile.ZipFile, name: str) -> bytes:
    hits = [i for i in zf.infolist() if Path(i.filename).name.upper() == name.upper()]
    if len(hits) != 1:
        raise FetchError(f"archive has {len(hits)} members named {name}")
    return zf.read(hits[0])


def fetch(
    dest_dir: str | Path | None = None,
    opener=urllib.request.urlopen,
    now: datetime | None = None,
) -> Path:
    base = Path("data") if dest_dir is None else Path(dest_dir)
    response = opener(URL)
    try:
        data = response.read()
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise FetchError("download is not a zip archive") from exc
    raw = base / "raw" / "registry"
    raw.mkdir(parents=True, exist_ok=True)
    members = {}
    for name in MEMBERS:
        blob = _member(zf, name)
        (raw / name).write_bytes(blob)
        members[name] = {"bytes": len(blob), "sha256": hashlib.sha256(blob).hexdigest()}
    manifest = {
        "url": URL,
        "layout": LAYOUT_URL,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "members": members,
        "retrieved_utc": (now or datetime.now(UTC)).astimezone(UTC).isoformat(),
    }
    out = base / "manifest" / "registry.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dest-dir", default=None, help="base dir (default: data/)")
    args = parser.parse_args(argv)
    print(f"wrote {fetch(dest_dir=args.dest_dir)}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
