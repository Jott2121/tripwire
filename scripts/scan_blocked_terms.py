#!/usr/bin/env python
"""Fail-closed scan for blocked terms (employer names) without naming them.

The repo must never name the author's employer, including in the guard that
enforces that. So the committed list holds only SHA-256 hashes of the blocked
terms, lowercased, with words joined by one space:

    .github/blocked-terms.sha256   one 64-hex digest per line, '#' comments allowed

    python harness/scripts/scan_blocked_terms.py              # every tracked file
    python harness/scripts/scan_blocked_terms.py --paths F..  # just these files

Every file is lowercased and split into alphanumeric words. Each word, each
adjacent two-word phrase ("a b"), and each two-word run written as one word
("ab") is hashed and looked up. A hit prints path:line and a digest prefix,
never the matched text.

`EMPLOYER_MARKERS` (a regex alternation, e.g. from an Actions secret of the same
name) adds extra patterns, matched case-insensitively against raw file text and
never printed. It only adds to the hash list: with it unset, the hash list
still catches the employer name.

Fails closed: exit 2 when the hash file is missing, empty, or malformed, when
`EMPLOYER_MARKERS` is set but invalid or can match the empty string, when `git
ls-files` fails, or when a file cannot be read. Exit 1 on any hit. Exit 0 only
when every scanned file is clean. Standard library only, Python 3.9+.

Scanned: every tracked file, AGENTS.md, .cursor/ and .github/ included.
Excluded: the hash file itself and generated junk directories (node_modules,
build output); `git ls-files` never lists `.git/`.
"""

import argparse
import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

HASH_FILE = Path(".github") / "blocked-terms.sha256"
ENV_VAR = "EMPLOYER_MARKERS"
EXCLUDED_DIRS = frozenset({"node_modules", "build", "dist", "__pycache__", ".venv"})
_WORD = re.compile(r"[a-z0-9]+")
_DIGEST = re.compile(r"[0-9a-f]{64}")


class ScanError(Exception):
    """A condition under which the scan cannot vouch for the tree."""


def digest(term):
    normalized = " ".join(_WORD.findall(term.lower()))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def load_hashes(path):
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ScanError(f"cannot read hash file {path}: {exc}") from exc
    hashes = set()
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip().lower()
        if not line:
            continue
        if not _DIGEST.fullmatch(line):
            raise ScanError(f"{path}:{lineno}: not a SHA-256 hex digest")
        hashes.add(line)
    if not hashes:
        raise ScanError(f"hash file {path} holds no digests")
    return hashes


def env_markers(environ):
    raw = environ.get(ENV_VAR, "").strip()
    if not raw:
        return None
    try:
        pattern = re.compile(raw, re.IGNORECASE)
    except re.error as exc:
        raise ScanError(f"{ENV_VAR} is not a valid regex: {exc}") from exc
    if pattern.search(""):
        raise ScanError(f"{ENV_VAR} can match the empty string")
    return pattern


def tracked_files(root):
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ScanError(f"git ls-files failed in {root}: {exc}") from exc
    files = [p for p in out.decode("utf-8", "surrogateescape").split("\0") if p]
    if not files:
        raise ScanError(f"git ls-files listed no files in {root}")
    return [root / p for p in files if not EXCLUDED_DIRS.intersection(Path(p).parts[:-1])]


def _candidates(words):
    for i, word in enumerate(words):
        yield i, word
        if i + 1 < len(words):
            yield i, f"{word} {words[i + 1]}"
            yield i, word + words[i + 1]


class Scanner:
    def __init__(self, hashes, markers=None):
        self.hashes = frozenset(hashes)
        self.markers = markers
        self._cache = {}

    def _hit(self, candidate):
        found = self._cache.get(candidate)
        if found is None:
            h = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
            found = h if h in self.hashes else ""
            self._cache[candidate] = found
        return found

    def scan_text(self, text):
        """Return [(line_number, label)] for every blocked term in text.

        Phrases are matched across line breaks; such a hit is reported on the
        line where the phrase starts.
        """
        lowered = text.lower()
        hits = {}
        matches = list(_WORD.finditer(lowered))
        words = [m.group() for m in matches]
        for i, candidate in _candidates(words):
            h = self._hit(candidate)
            if h:
                start = matches[i].start()
                hits[(lowered.count("\n", 0, start) + 1, f"sha256 {h[:12]}…")] = None
        if self.markers is not None:
            for m in self.markers.finditer(text):
                hits[(text.count("\n", 0, m.start()) + 1, ENV_VAR)] = None
        return list(hits)


def scan_files(paths, scanner, root):
    findings = []
    for path in paths:
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise ScanError(f"cannot read {path}: {exc}") from exc
        text = data.decode("utf-8", "replace")
        for lineno, label in scanner.scan_text(text):
            try:
                shown = path.relative_to(root)
            except ValueError:
                shown = path
            findings.append(f"{shown}:{lineno}: blocked term ({label})")
    return findings


def repo_root():
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            check=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ScanError(f"not inside a git checkout: {exc}") from exc
    return Path(out)


def main(argv=None, environ=None):
    environ = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, help="repo root (default: git toplevel)")
    parser.add_argument("--hash-file", type=Path, help=f"default: <root>/{HASH_FILE}")
    parser.add_argument("--paths", nargs="+", type=Path, help="scan only these files")
    args = parser.parse_args(argv)
    try:
        root = (args.root or repo_root()).resolve()
        hash_file = (args.hash_file or root / HASH_FILE).resolve()
        hashes = load_hashes(hash_file)
        markers = env_markers(environ)
        paths = args.paths if args.paths else tracked_files(root)
        paths = [p for p in paths if p.resolve() != hash_file]
        findings = scan_files(paths, Scanner(hashes, markers), root)
    except ScanError as exc:
        print(f"blocked-term scan FAILED CLOSED: {exc}", file=sys.stderr)
        return 2
    for line in findings:
        print(line)
    if findings:
        print(f"{len(findings)} blocked-term hit(s) in {len(paths)} file(s) scanned")
        return 1
    extra = f", {ENV_VAR} set" if markers is not None else f", {ENV_VAR} unset"
    print(f"no blocked terms in {len(paths)} file(s) ({len(hashes)} term digests{extra})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
