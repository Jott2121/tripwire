import hashlib
import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path

from tripwire.synth import make_sdr_rows, write_sdr_csv

_MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "fetch_sdr.py"


def _load_fetch_module():
    spec = importlib.util.spec_from_file_location("fetch_sdr", _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeResponse:
    def __init__(self, data: bytes):
        self._data = data
        self.closed = False

    def read(self) -> bytes:
        return self._data

    def close(self) -> None:
        self.closed = True


def test_fetch_year_writes_csv_and_manifest_without_network(tmp_path):
    fetch = _load_fetch_module()

    # Build a synthetic CSV payload; the fake opener serves it, no network.
    csv_src = tmp_path / "src.csv"
    write_sdr_csv(make_sdr_rows(9, seed=1), csv_src)
    payload = csv_src.read_bytes()

    captured = {}

    def fake_opener(url):
        captured["url"] = url
        return _FakeResponse(payload)

    fixed_now = datetime(2026, 1, 2, 19, 9, 41, tzinfo=UTC)
    manifest_path = fetch.fetch_year(
        2026, dest_dir=tmp_path / "data", opener=fake_opener, now=fixed_now
    )

    assert captured["url"] == "https://external.apic4e.faa.gov/sdrs/retrieve/SDR-2026.csv"

    csv_out = tmp_path / "data" / "raw" / "SDR-2026.csv"
    assert csv_out.read_bytes() == payload

    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    assert manifest["url"] == captured["url"]
    assert manifest["bytes"] == len(payload)
    assert manifest["sha256"] == hashlib.sha256(payload).hexdigest()
    assert manifest["row_count"] == 9
    assert manifest["retrieved_utc"] == "2026-01-02T19:09:41+00:00"


def test_fetch_year_counts_rows_ignoring_quoted_newlines(tmp_path):
    fetch = _load_fetch_module()
    # Two data rows, one with a quoted embedded newline.
    payload = b'h1,h2\r\n"a\nb",c\r\nd,e\r\n'

    def fake_opener(url):
        return _FakeResponse(payload)

    fetch.fetch_year(2025, dest_dir=tmp_path / "data", opener=fake_opener, now=None)
    manifest = json.loads(
        (tmp_path / "data" / "manifest" / "SDR-2025.json").read_text(encoding="utf-8")
    )
    assert manifest["row_count"] == 2


def test_sdr_url_pattern():
    fetch = _load_fetch_module()
    assert fetch.sdr_url(2024) == "https://external.apic4e.faa.gov/sdrs/retrieve/SDR-2024.csv"
