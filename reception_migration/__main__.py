"""Explicit command; accepts only a generated synthetic temporary workspace."""

import argparse
import json
import sqlite3

from .migration import MigrationError, migrate
from .safety import SafetyError, Workspace


def main(argv=None):
    parser = argparse.ArgumentParser(description='Synthetic temporary DB ONLY: SQLite v1 -> v2 migration lab')
    parser.add_argument('--workspace', required=True)
    parser.add_argument('--database', required=True)
    parser.add_argument('--expected-sha256', required=True)
    args = parser.parse_args(argv)
    try:
        result = migrate(Workspace(args.workspace),args.database,expected_sha256=args.expected_sha256)
    except (SafetyError,MigrationError,sqlite3.Error,OSError,ValueError) as exc:
        parser.exit(2,f'Migration error: {exc}\n')
    print(json.dumps(result,ensure_ascii=False,sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
