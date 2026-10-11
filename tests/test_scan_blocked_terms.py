"""Blocked-term scanner (scripts/scan_blocked_terms.py), the no-employer-data gate.

Every planted term here is a made-up word, hashed at test time and written only
to a throwaway git repo in tmp_path. No real blocked term appears in this file.
The last test runs the scanner over the real checkout with the committed hash list.
"""

import importlib.util
import pathlib
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
REPO = ROOT  # template layout: scripts/ and tests/ at repo root
spec = importlib.util.spec_from_file_location(
    "scan_blocked_terms", ROOT / "scripts" / "scan_blocked_terms.py"
)
scan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scan)

PLANTED = "zorblax quintaro"


def _git_repo(tmp_path, files, hashes=(PLANTED,)):
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    hash_file = tmp_path / ".github" / "blocked-terms.sha256"
    hash_file.parent.mkdir(parents=True, exist_ok=True)
    hash_file.write_text("# test list\n" + "".join(scan.digest(t) + "\n" for t in hashes))
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    return tmp_path


def _run(root, capsys, *extra, environ=None):
    code = scan.main(["--root", str(root), *extra], environ=environ or {})
    return code, capsys.readouterr()


def test_clean_tree_passes(tmp_path, capsys):
    root = _git_repo(tmp_path, {"README.md": "nothing to see\n", "results/a.json": "{}"})
    code, out = _run(root, capsys)
    assert code == 0
    assert "no blocked terms in 2 file(s)" in out.out


@pytest.mark.parametrize(
    "text",
    [
        "Built at Zorblax Quintaro.\n",
        "zorblax\n  QUINTARO\n",
        "the zorblax-quintaro program\n",
    ],
)
def test_planted_phrase_in_results_is_caught(tmp_path, capsys, text):
    root = _git_repo(tmp_path, {"README.md": "ok\n", "results/run1/answer.json": "x\n" + text})
    code, out = _run(root, capsys)
    assert code == 1
    assert "results/run1/answer.json:2: blocked term" in out.out
    assert "zorblax" not in out.out.lower()


@pytest.mark.parametrize("text", ["see ZorblaxQuintaro_internal\n", "Zorblax Quintaro\n"])
def test_joined_form_listed_once_catches_both_spellings(tmp_path, capsys, text):
    root = _git_repo(tmp_path, {"a.md": text}, hashes=("zorblaxquintaro",))
    assert _run(root, capsys)[0] == 1


def test_single_word_is_whole_word_only(tmp_path, capsys):
    root = _git_repo(tmp_path, {"a.md": "aqx qxs zqxz\n"}, hashes=("qx",))
    assert _run(root, capsys)[0] == 0
    root2 = _git_repo(tmp_path / "b", {"a.md": "the QX team\n"}, hashes=("qx",))
    assert _run(root2, capsys)[0] == 1


def test_employer_markers_regex_adds_patterns(tmp_path, capsys):
    root = _git_repo(tmp_path, {"a.md": "ok\na Fizz-Wick memo\n"})
    assert _run(root, capsys)[0] == 0
    code, out = _run(root, capsys, environ={"EMPLOYER_MARKERS": r"fizz-?wick|\bqqzz\b"})
    assert code == 1
    assert "a.md:2: blocked term (EMPLOYER_MARKERS)" in out.out
    assert "fizz" not in out.out.lower()


def test_hash_list_still_catches_with_markers_unset_or_blank(tmp_path, capsys):
    root = _git_repo(tmp_path, {"a.md": f"{PLANTED}\n"})
    assert _run(root, capsys)[0] == 1
    assert _run(root, capsys, environ={"EMPLOYER_MARKERS": "  "})[0] == 1
    assert _run(root, capsys, environ={"EMPLOYER_MARKERS": "unrelated"})[0] == 1


@pytest.mark.parametrize(
    "pattern", ["(unclosed", "a|", ".*"], ids=["invalid", "empty-alt", "matches-empty"]
)
def test_bad_employer_markers_fail_closed(tmp_path, capsys, pattern):
    root = _git_repo(tmp_path, {"a.md": "ok\n"})
    code, out = _run(root, capsys, environ={"EMPLOYER_MARKERS": pattern})
    assert code == 2
    assert "FAILED CLOSED" in out.err


def test_policy_files_are_scanned_and_hash_file_is_not(tmp_path, capsys):
    root = _git_repo(
        tmp_path,
        {"AGENTS.md": "ok\n", ".cursor/rules/a.mdc": "ok\n", ".github/workflows/ci.yml": "ok\n"},
    )
    hash_file = root / ".github" / "blocked-terms.sha256"
    code, out = _run(root, capsys, environ={"EMPLOYER_MARKERS": scan.digest(PLANTED)[:16]})
    assert code == 0
    assert "in 3 file(s)" in out.out
    for rel in ("AGENTS.md", ".cursor/rules/a.mdc", ".github/workflows/ci.yml"):
        (root / rel).write_text(f"x\n{PLANTED}\n")
    code, out = _run(root, capsys)
    assert code == 1
    for rel in ("AGENTS.md", ".cursor/rules/a.mdc", ".github/workflows/ci.yml"):
        assert f"{rel}:2: blocked term" in out.out
    assert hash_file.name not in out.out


def test_generated_junk_is_excluded_but_data_folders_are_not(tmp_path, capsys):
    root = _git_repo(
        tmp_path,
        {"node_modules/x/README.md": PLANTED, "build/out.txt": PLANTED, "data/ok.txt": "ok"},
    )
    assert _run(root, capsys)[0] == 0
    (root / "data" / "ok.txt").write_text(PLANTED)
    assert _run(root, capsys)[0] == 1


@pytest.mark.parametrize(
    "content",
    [None, "", "# only a comment\n\n", "not-a-digest\n"],
    ids=["missing", "empty", "comments", "malformed"],
)
def test_bad_hash_file_fails_closed(tmp_path, capsys, content):
    root = _git_repo(tmp_path, {"a.md": "ok\n"})
    hash_file = root / ".github" / "blocked-terms.sha256"
    if content is None:
        hash_file.unlink()
    else:
        hash_file.write_text(content)
    code, out = _run(root, capsys)
    assert code == 2
    assert "FAILED CLOSED" in out.err


def test_not_a_git_checkout_fails_closed(tmp_path, capsys):
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "blocked-terms.sha256").write_text(scan.digest(PLANTED) + "\n")
    code, out = _run(tmp_path, capsys)
    assert code == 2
    assert "git ls-files failed" in out.err


def test_unreadable_path_fails_closed(tmp_path, capsys):
    root = _git_repo(tmp_path, {"a.md": "ok\n"})
    code, out = _run(root, capsys, "--paths", str(root / "missing.md"))
    assert code == 2
    assert "cannot read" in out.err


def test_explicit_paths_mode(tmp_path, capsys):
    root = _git_repo(tmp_path, {"a.md": "ok\n"})
    untracked = root / "draft.md"
    untracked.write_text(f"draft mentions {PLANTED}\n")
    code, out = _run(root, capsys, "--paths", str(untracked))
    assert code == 1
    assert "draft.md:1: blocked term" in out.out


# Digests only, one per surface form (single word, abbreviation, two-word phrase,
# words run together). Removing any of them from the committed list must fail
# here; new digests may be added to the list without touching this set.
PINNED_DIGESTS = frozenset(
    {
        "cb5c36fe59056a6a1afb4b133d30981f672257d6c372bace38798b504fc0c6f2",
        "8c6a10a10f5de2282f957c71717b5a15ab371af5bd781b4d38e72f556964da06",
        "c376013522845bfce25461e2335a75a6808fd9365beba1c8ad75200ff2488ea3",
        "f277af9c64194778d745db1e6f142c724110717970ed251b4611f28e683957ae",
    }
)


def test_committed_hash_list_keeps_every_pinned_digest():
    hashes = scan.load_hashes(REPO / ".github" / "blocked-terms.sha256")
    missing = PINNED_DIGESTS - hashes
    assert not missing, f"digests removed from the blocked-term list: {sorted(missing)}"


def test_real_checkout_is_clean(capsys):
    code, out = _run(REPO, capsys)
    assert code == 0, out.out + out.err
