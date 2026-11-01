#!/usr/bin/env python
"""Download the FAA releasable aircraft database and write a provenance manifest.

Stdlib only. The download takes an injectable ``opener`` so tests never touch the network; the
real fetch runs only when a person runs this script (the FAA hosts are not reachable from the
build container).

    PYTHONPATH=. python3 scripts/fetch_registry.py
    PYTHONPATH=. python3 scripts/fetch_registry.py --from-zip ~/Downloads/ReleasableAircraft.zip

The FAA site sometimes refuses scripted downloads (HTTP 403, seen 2026-11-01). Then download
the zip in a browser from the URL below and pass it with ``--from-zip``: the same two files and
the same manifest are written, plus ``source_file`` naming the zip that was loaded. The script
never retries with a changed client identity; the manual download is the way round a refusal.

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
    """Download the zip from :data:`URL` and extract it (see :func:`extract`)."""
    response = opener(URL)
    try:
        data = response.read()
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()
    return extract(data, dest_dir, now)


def fetch_from_zip(
    zip_path: str | Path, dest_dir: str | Path | None = None, now: datetime | None = None
) -> Path:
    """Load a ``ReleasableAircraft.zip`` the owner downloaded in a browser, as :func:`fetch`
    would have; the manifest also names the file it came from."""
    path = Path(zip_path).expanduser()
    if not path.is_file():
        raise FetchError(f"no such file: {path}")
    return extract(path.read_bytes(), dest_dir, now, source_file=path.name)


def extract(
    data: bytes,
    dest_dir: str | Path | None = None,
    now: datetime | None = None,
    source_file: str | None = None,
) -> Path:
    """Write the two named members and the manifest from the zip's bytes."""
    base = Path("data") if dest_dir is None else Path(dest_dir)
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
    if source_file is not None:
        manifest["source_file"] = source_file  # loaded from a manual download of URL
    out = base / "manifest" / "registry.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dest-dir", default=None, help="base dir (default: data/)")
    parser.add_argument(
        "--from-zip", default=None, help="a ReleasableAircraft.zip downloaded in a browser"
    )
    args = parser.parse_args(argv)
    try:
        if args.from_zip:
            out = fetch_from_zip(args.from_zip, dest_dir=args.dest_dir)
        else:
            out = fetch(dest_dir=args.dest_dir)
    except FetchError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
