"""Migrate an old SQLite CAN log to the compatibility schema in batches.

This is a thin command line wrapper around :class:`can.io.sqlite.SqliteMigrator`.
It is safe to stop and run again: progress is committed after every batch.
"""

import argparse
import errno
import sys
from typing import NoReturn

from can.io.sqlite import SqliteMigrator


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        self.print_help(sys.stderr)
        self.exit(errno.EINVAL, f"{self.prog}: error: {message}\n")


def main() -> None:
    parser = ArgumentParser(
        description="Migrate an old SQLite CAN log to the compatibility schema."
    )
    parser.add_argument("input", metavar="INFILE", help="source SQLite log")
    parser.add_argument(
        "output",
        metavar="OUTFILE",
        nargs="?",
        default=None,
        help="target SQLite log (default: upgrade INFILE in place)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="rows to commit per batch (default: 500)",
    )
    parser.add_argument(
        "--table",
        default="messages",
        help="target table name (default: messages)",
    )
    parser.add_argument(
        "--source-table",
        default=None,
        help="source table name if it differs from --table",
    )
    parser.add_argument(
        "--source-db",
        default=None,
        help="stable original database number/name stored as provenance",
    )
    args = parser.parse_args()

    migrator = SqliteMigrator(
        args.input,
        args.output,
        table_name=args.table,
        source_table_name=args.source_table,
        batch_size=args.batch_size,
        source_db=args.source_db,
    )
    try:
        total = migrator.run()
    except KeyboardInterrupt:
        print(
            f"Interrupted after {migrator.rows_migrated} rows; "
            "run the same command to resume.",
            file=sys.stderr,
        )
        raise SystemExit(130) from None
    print(f"Migrated {total} rows this run ({migrator.migrated} cumulative).")


if __name__ == "__main__":
    main()
