"""
Seed Market Intelligence data (Enternly Batch 1 -- PLACEHOLDER).

The market_intelligence table schema is not yet finalized (see handoff:
"placeholder, confirm fields before final"), so this script does NOT
upsert -- there's no agreed natural key yet (role_family/location/skill/
as_of_date could all repeat legitimately across sources). Every run
APPENDS new rows. Re-running the same CSV twice will duplicate rows --
this is intentional until the schema/key strategy is confirmed, at which
point this script should be revisited to add a real upsert key.

CSV columns (header row required):
    role_family,location,skill,median_ctc,p25_ctc,p75_ctc,demand_index,source,as_of_date

Usage:
    py -3 seed_market_intelligence.py --csv path/to/market_intel.csv
"""
import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.db import query  # noqa: E402

REQUIRED_COLS = {
    "role_family", "location", "skill", "median_ctc",
    "p25_ctc", "p75_ctc", "demand_index", "source", "as_of_date",
}


def _num_or_none(v):
    v = (v or "").strip()
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    args = ap.parse_args()

    inserted = skipped = 0

    with open(args.csv, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        missing_cols = REQUIRED_COLS - set(reader.fieldnames or [])
        if missing_cols:
            print(f"ERROR: CSV is missing column(s): {', '.join(sorted(missing_cols))}")
            sys.exit(1)

        for i, row in enumerate(reader, start=2):
            role_family = (row.get("role_family") or "").strip() or None
            location    = (row.get("location") or "").strip() or None
            skill       = (row.get("skill") or "").strip() or None
            if not role_family and not location and not skill:
                print(f"  [row {i}] SKIP -- role_family, location, and skill all blank")
                skipped += 1
                continue

            query(
                """INSERT INTO market_intelligence
                     (role_family, location, skill, median_ctc, p25_ctc, p75_ctc,
                      demand_index, source, as_of_date)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                [
                    role_family, location, skill,
                    _num_or_none(row.get("median_ctc")),
                    _num_or_none(row.get("p25_ctc")),
                    _num_or_none(row.get("p75_ctc")),
                    _num_or_none(row.get("demand_index")),
                    (row.get("source") or "").strip() or None,
                    (row.get("as_of_date") or "").strip() or None,
                ],
                fetch=False,
            )
            inserted += 1
            print(f"  [row {i}] Inserted: {role_family or '—'} / {location or '—'} / {skill or '—'}")

    print(f"\nDone. inserted={inserted} skipped={skipped}")


if __name__ == "__main__":
    main()
