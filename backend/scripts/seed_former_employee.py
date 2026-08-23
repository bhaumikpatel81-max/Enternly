"""
Seed the Rehire (former employee) list (Enternly Batch 1).

CSV columns (header row required):
    full_name,email,phone,emp_code,last_designation,exit_date,exit_type,rehire_eligible,notes

email is required -- it is the upsert key (case-insensitive) and the
primary match field the live rehire lookup uses during candidate intake.
Rows with a blank email are skipped (phone alone is used as a secondary
match at intake time, but isn't unique enough to upsert on).

rehire_eligible accepts true/false/1/0/yes/no (case-insensitive); blank
defaults to true. exit_date must be YYYY-MM-DD or left blank.

Usage:
    py -3 seed_former_employee.py --csv path/to/former_employees.csv
"""
import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.db import query_one  # noqa: E402

REQUIRED_COLS = {
    "full_name", "email", "phone", "emp_code", "last_designation",
    "exit_date", "exit_type", "rehire_eligible", "notes",
}
_TRUE_VALUES = {"true", "1", "yes", "y"}
_FALSE_VALUES = {"false", "0", "no", "n"}


def _parse_bool(val, default=True):
    v = (val or "").strip().lower()
    if not v:
        return default
    if v in _TRUE_VALUES:
        return True
    if v in _FALSE_VALUES:
        return False
    return default


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
            full_name = (row.get("full_name") or "").strip() or None
            email     = (row.get("email") or "").strip().lower()
            if not email:
                print(f"  [row {i}] SKIP -- missing email (required as the upsert key)")
                skipped += 1
                continue

            phone            = (row.get("phone") or "").strip() or None
            emp_code         = (row.get("emp_code") or "").strip() or None
            last_designation = (row.get("last_designation") or "").strip() or None
            exit_date        = (row.get("exit_date") or "").strip() or None
            exit_type        = (row.get("exit_type") or "").strip() or None
            rehire_eligible  = _parse_bool(row.get("rehire_eligible"), default=True)
            notes            = (row.get("notes") or "").strip() or None

            result = query_one(
                """INSERT INTO former_employee
                     (full_name, email, phone, emp_code, last_designation,
                      exit_date, exit_type, rehire_eligible, notes)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (lower(email)) DO UPDATE
                     SET full_name        = EXCLUDED.full_name,
                         phone            = EXCLUDED.phone,
                         emp_code         = EXCLUDED.emp_code,
                         last_designation = EXCLUDED.last_designation,
                         exit_date        = EXCLUDED.exit_date,
                         exit_type        = EXCLUDED.exit_type,
                         rehire_eligible  = EXCLUDED.rehire_eligible,
                         notes            = EXCLUDED.notes
                   RETURNING (xmax = 0) AS inserted""",
                [full_name, email, phone, emp_code, last_designation,
                 exit_date, exit_type, rehire_eligible, notes],
            )
            if result["inserted"]:
                inserted += 1
                print(f"  [row {i}] Inserted: {full_name or email} <{email}>")
            else:
                updated += 1
                print(f"  [row {i}] Updated: {full_name or email} <{email}>")

    print(f"\nDone. inserted={inserted} updated={updated} skipped={skipped}")


if __name__ == "__main__":
    main()
