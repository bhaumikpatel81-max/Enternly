"""
Seed the No-Poach company list (Enternly Batch 1).

CSV columns (header row required):
    company_name,status,effective_from,effective_to

status must be 'past' or 'current' (or left blank). effective_from /
effective_to are optional dates (YYYY-MM-DD). Upserts on normalized_name
(company_name lowercased with all non-alphanumeric characters stripped),
so re-running the same CSV is safe.

Usage:
    py -3 seed_no_poach.py --csv path/to/no_poach.csv
"""
import argparse
import csv
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.db import query_one  # noqa: E402

REQUIRED_COLS = {"company_name", "status", "effective_from", "effective_to"}


def normalize(name):
    return re.sub(r"[^a-z0-9]", "", name.lower())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    args = ap.parse_args()

    inserted = updated = skipped = 0

    with open(args.csv, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        missing_cols = REQUIRED_COLS - set(reader.fieldnames or [])
        if missing_cols:
            print(f"ERROR: CSV is missing column(s): {', '.join(sorted(missing_cols))}")
            sys.exit(1)

        for i, row in enumerate(reader, start=2):
            name     = (row.get("company_name") or "").strip()
            status   = (row.get("status") or "").strip().lower() or None
            eff_from = (row.get("effective_from") or "").strip() or None
            eff_to   = (row.get("effective_to") or "").strip() or None
            if not name:
                print(f"  [row {i}] SKIP -- missing company_name")
                skipped += 1
                continue
            if status and status not in ("past", "current"):
                print(f"  [row {i}] SKIP -- status must be 'past' or 'current', got '{status}'")
                skipped += 1
                continue

            norm = normalize(name)
            result = query_one(
                """INSERT INTO no_poach_company
                     (company_name, normalized_name, status, effective_from, effective_to)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (normalized_name) DO UPDATE
                     SET company_name    = EXCLUDED.company_name,
                         status          = EXCLUDED.status,
                         effective_from  = EXCLUDED.effective_from,
                         effective_to    = EXCLUDED.effective_to,
                         is_active       = true
                   RETURNING (xmax = 0) AS inserted""",
                [name, norm, status, eff_from, eff_to],
            )
            if result["inserted"]:
                inserted += 1
                print(f"  [row {i}] Inserted: {name}")
            else:
                updated += 1
                print(f"  [row {i}] Updated: {name}")

    print(f"\nDone. inserted={inserted} updated={updated} skipped={skipped}")


if __name__ == "__main__":
    main()
