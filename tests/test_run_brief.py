"""Acceptance tests for scripts/run-brief.sh self-detach behavior.

These run the shell script via subprocess only. They never touch the network,
never invoke `claude`, and never invoke `gh`: validation and the dry-run path
both return before any of that is reached.
"""

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run-brief.sh"


def test_script_passes_bash_syntax_check():
    result = subprocess.run(
        ["bash", "-n", str(SCRIPT)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_missing_argument_exits_2_without_creating_log_dir(tmp_path):
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "TRIPWIRE_WT_ROOT": str(tmp_path)},
    )
    assert result.returncode == 2
    assert "usage" in result.stderr.lower()
    assert not (tmp_path / "logs").exists()


def test_non_numeric_argument_exits_2_without_creating_log_dir(tmp_path):
    result = subprocess.run(
        ["bash", str(SCRIPT), "abc"],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "TRIPWIRE_WT_ROOT": str(tmp_path)},
    )
    assert result.returncode == 2
    assert not (tmp_path / "logs").exists()


def test_dryrun_prints_would_exec_line_and_launches_nothing(tmp_path):
    result = subprocess.run(
        ["bash", str(SCRIPT), "7"],
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "TRIPWIRE_WT_ROOT": str(tmp_path),
            "TRIPWIRE_DETACH_DRYRUN": "1",
        },
    )
    assert result.returncode == 0
    lines = [ln for ln in result.stdout.splitlines() if ln.startswith("would exec: ")]
    assert len(lines) == 1, result.stdout
    assert "setsid" in lines[0]
    assert "issue-7" in lines[0]
    # dry run must not launch anything or create the log directory
    assert not (tmp_path / "logs").exists()
