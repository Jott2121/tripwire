"""Command-line entry point for tripwire."""

import argparse

from tripwire import __version__

EPILOG = """\
commands (none of these makes a model call or spends money):
  make demo                          replay the recorded results into artifacts/ and docs/
  make check                         lint, tests with coverage, no-employer-data scan
  python -m tripwire.phase2          phase two scorer on a seeded fictional registry
  python -m tripwire.server --seed 1 v1's MCP tool server (needs TRIPWIRE_SERVER_TOKEN)
  python -m tripwire.report RUN...   scored report from downloaded v1 run directories

docs: README.md, WRITEUP.md, docs/README.md (map of every preregistration and result)
"""


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the tripwire CLI."""
    parser = argparse.ArgumentParser(
        prog="tripwire",
        description=(
            "Measures whether tool-using AI agents notice wrong engineering data, and whether\n"
            "they act on it. Scored mechanically, with no LLM judge."
        ),
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="print the tripwire version and exit",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the tripwire CLI. Returns the process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.version:
        print(f"tripwire {__version__}")
        return 0
    parser.print_help()
    return 0
