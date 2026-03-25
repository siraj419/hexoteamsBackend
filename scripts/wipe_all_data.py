#!/usr/bin/env python3
"""
Delete all rows from every table mapped by SQLAlchemy models.

Uses PostgreSQL TRUNCATE ... RESTART IDENTITY CASCADE so foreign keys are handled.

Usage (from hexoteamsBackend directory, with .env loaded or env vars set):
  python scripts/wipe_all_data.py
  python scripts/wipe_all_data.py --yes
  python scripts/wipe_all_data.py --yes --include-alembic

By default the alembic_version table is skipped so migration history stays intact.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


def _qualify_table(table, preparer) -> str:
    if table.schema:
        return f"{preparer.quote_schema(table.schema)}.{preparer.quote(table.name)}"
    return preparer.quote(table.name)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Truncate all application tables (PostgreSQL). Destructive."
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip interactive confirmation",
    )
    parser.add_argument(
        "--include-alembic",
        action="store_true",
        help="Also truncate alembic_version (you will need to re-stamp or migrate)",
    )
    args = parser.parse_args()

    from sqlalchemy import text

    import app.models  # noqa: F401 — register all tables on Base.metadata
    from app.db.base import Base
    from app.db.sync_session import sync_engine

    preparer = sync_engine.dialect.identifier_preparer
    tables = [
        t
        for t in Base.metadata.sorted_tables
        if args.include_alembic or t.name != "alembic_version"
    ]

    if not tables:
        print("No tables found on Base.metadata (did models import correctly?)")
        return 1

    qualified = [_qualify_table(t, preparer) for t in tables]

    if not args.yes:
        print("This will DELETE ALL DATA in these tables:")
        for t in tables:
            print(f"  - {t.schema + '.' if t.schema else ''}{t.name}")
        print()
        confirm = input('Type "DELETE ALL" to proceed: ').strip()
        if confirm != "DELETE ALL":
            print("Aborted.")
            return 2

    sql = (
        "TRUNCATE TABLE "
        + ", ".join(qualified)
        + " RESTART IDENTITY CASCADE"
    )

    with sync_engine.begin() as conn:
        conn.execute(text(sql))

    print(f"[OK] Truncated {len(tables)} table(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
