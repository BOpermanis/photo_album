"""Fill photo.date from the date written at the start of each description.

The app does this automatically once, when it first adds the `date` column.
Run this to re-apply it later (e.g. after editing descriptions):

    python scripts/migrate_photo_dates.py --dry-run     # preview only
    python scripts/migrate_photo_dates.py               # fill empty dates
    python scripts/migrate_photo_dates.py --overwrite   # re-parse every photo
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from backend import db  # noqa: E402
from backend.date_parse import parse_description_date  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--overwrite", action="store_true", help="re-parse photos that already have a date"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print what would change without writing"
    )
    args = parser.parse_args()

    if args.dry_run:
        with db.engine.connect() as conn:
            cols = {r[1] for r in conn.execute(text("PRAGMA table_info(photo)"))}
            date_sql = "COALESCE(date, '')" if "date" in cols else "''"
            rows = list(
                conn.execute(
                    text(f"SELECT id, COALESCE(description, ''), {date_sql} FROM photo")
                )
            )
        changed = []
        for pid, description, old in rows:
            if old and not args.overwrite:
                continue
            new = parse_description_date(description)
            if new != old:
                changed.append((pid, old, new))
        total = len(rows)
    else:
        db.init_db()  # adds the column (with its one-time backfill) if missing
        with db.engine.begin() as conn:
            changed = db.backfill_photo_dates(conn, overwrite=args.overwrite)
            total = conn.execute(text("SELECT COUNT(*) FROM photo")).scalar()

    for pid, old, new in changed:
        print(f"photo {pid}: {old or '-'!s:>10} -> {new or '-'}")
    verb = "would change" if args.dry_run else "changed"
    print(f"{verb} {len(changed)} of {total} photos")


if __name__ == "__main__":
    main()
