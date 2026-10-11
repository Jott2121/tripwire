"""Command-line entry point for tripwire."""

import argparse

from tripwire import __version__


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the tripwire CLI."""
    parser = argparse.ArgumentParser(
        prog="tripwire",
        description="Planted-fault integrity benchmark for Grok agents on engineering data.",
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
