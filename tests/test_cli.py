import subprocess
import sys

import tripwire
from tripwire.cli import main


def test_version_flag_exits_zero_and_prints_version(capsys):
    assert main(["--version"]) == 0
    captured = capsys.readouterr()
    assert captured.out == f"tripwire {tripwire.__version__}\n"


def test_no_args_prints_usage(capsys):
    assert main([]) == 0
    captured = capsys.readouterr()
    assert "usage" in captured.out.lower()


def test_dunder_main_wires_up_cli():
    import tripwire.__main__ as dunder_main

    assert dunder_main.main is main


def test_python_m_tripwire_version_runs_the_entry_point():
    result = subprocess.run(
        [sys.executable, "-m", "tripwire", "--version"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert result.stdout == f"tripwire {tripwire.__version__}\n"
