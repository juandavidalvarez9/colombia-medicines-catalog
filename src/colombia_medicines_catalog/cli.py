"""Command-line entry point."""

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from colombia_medicines_catalog import __version__, source
from colombia_medicines_catalog.contract import ContractError
from colombia_medicines_catalog.pipeline import EXAMPLE_CONTRACT, run

PROG = "colmed"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Starting point for small, tested data products.",
    )
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")

    commands = parser.add_subparsers(dest="command", title="commands")
    run_parser = commands.add_parser(
        "run",
        help="check a CSV file and write it as Parquet",
        description="Check a CSV file against the contract and write it as Parquet.",
    )
    run_parser.add_argument("source", type=Path, help="CSV file to read")
    run_parser.add_argument("dest", type=Path, help="Parquet file to write")

    commands.add_parser(
        "check-source",
        help="check that the CUM datasets on datos.gov.co can be read",
        description=(
            "Read the metadata and a first page of rows of each CUM dataset on "
            "datos.gov.co, and report what looks wrong. Set SOCRATA_APP_TOKEN "
            "to send an app token."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line and return the process exit code.

    0 means success, 1 means the input was rejected, and argparse exits with 2
    when the command itself is wrong. A bare call prints the help text.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "check-source":
        return _check_source()
    return _run(args.source, args.dest)


def _check_source() -> int:
    token = os.environ.get("SOCRATA_APP_TOKEN")
    if token:
        print("Using the app token in SOCRATA_APP_TOKEN.")
    else:
        print("No app token: requests share the anonymous limit.")

    checks = source.check_source(token=token)
    for check in checks:
        label = f"{check.dataset.id}  {check.dataset.status}"
        if check.ok:
            print(
                f"{label}  updated {check.updated_on}  {check.columns} columns  "
                f"{check.license}  sample of {check.sample_rows} rows  ok"
            )
        else:
            print(f"{label}  PROBLEM")
            for problem in check.problems:
                print(f"  - {problem}")

    failed = sum(not check.ok for check in checks)
    if failed:
        print(f"{failed} of {len(checks)} datasets have problems.")
        return 1
    print(f"All {len(checks)} datasets can be read.")
    return 0


def _run(source: Path, dest: Path) -> int:
    try:
        report = run(source, dest, EXAMPLE_CONTRACT)
    except FileNotFoundError as error:
        print(error, file=sys.stderr)
        return 1
    except ContractError as error:
        print(f"{source} does not meet the contract:", file=sys.stderr)
        for violation in error.violations:
            print(f"- {violation}", file=sys.stderr)
        return 1
    print(f"Wrote {report.rows_out} rows to {report.output}")
    return 0
