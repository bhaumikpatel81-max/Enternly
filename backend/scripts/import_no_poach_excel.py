"""
Import the No-Poach company list from an Excel workbook (Enternly Batch 1.1).

Unlike seed_no_poach.py (CSV, upserts existing rows), this importer treats
the existing no_poach_company table as authoritative: it only ever ADDS new
companies, never touches an existing row (no update, no deactivate). This
matches a one-way "here's a fresh Excel export from Legal/HR, add whatever's
new" workflow rather than a full resync.

Excel columns (header row required, case-insensitive, any column order):
    company_name, location

Match key: normalized_name = re.sub(r'[^a-z0-9]', '', company_name.lower()).
`location` is stored as informational only -- it is NOT part of the match
key, so two rows for the same company at different locations collapse into
one company (only the first-seen location in the file is kept for a
company that doesn't already exist).

Two-step run, always safe to re-run:
    py -3 import_no_poach_excel.py --file no_poach.xlsx
        Dry-run (default). Reads the file, reports totals, and prints every
        NEW company name found. Writes nothing to the database.

    py -3 import_no_poach_excel.py --file no_poach.xlsx --confirm
        Inserts only the new companies found in the last dry-run pass over
        this same file. Existing rows are left completely untouched.

Usage:
    py -3 import_no_poach_excel.py --file path/to/no_poach.xlsx
    py -3 import_no_poach_excel.py --file path/to/no_poach.xlsx --confirm
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.db import query, query_one  # noqa: E402


def normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _find_header_indices(header_row):
    """Case-insensitive, whitespace-trimmed match for company_name / location."""
    idx = {}
    for i, cell in enumerate(header_row):
        if cell is None:
            continue
        key = str(cell).strip().lower()
        if key in ("company_name", "company name", "company"):
            idx.setdefault("company_name", i)
        elif key in ("location",):
            idx.setdefault("location", i)
    return idx


def _read_rows(path):
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    if not rows:
        print("ERROR: workbook is empty")
        sys.exit(1)

    idx = _find_header_indices(rows[0])
    if "company_name" not in idx:
        print("ERROR: could not find a 'company_name' column in the header row")
        sys.exit(1)

    out = []
    for raw_row in rows[1:]:
        if raw_row is None or all(v is None or str(v).strip() == "" for v in raw_row):
            continue  # skip blank rows
        name = raw_row[idx["company_name"]]
        name = str(name).strip() if name is not None else ""
        if not name:
            continue
        location = None
        if "location" in idx and idx["location"] < len(raw_row):
            loc_val = raw_row[idx["location"]]
            location = str(loc_val).strip() if loc_val is not None else None
            location = location or None
        out.append({"company_name": name, "location": location})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    ap.add_argument("--confirm", action="store_true",
                     help="Actually insert the new companies. Omit for a dry-run.")
    args = ap.parse_args()

    rows = _read_rows(args.file)
    total_read = len(rows)

    seen_in_file = set()
    new_companies = []
    already_exists = 0
    duplicate_in_file = 0

    for row in rows:
        norm = normalize(row["company_name"])
        if not norm:
            continue
        if norm in seen_in_file:
            duplicate_in_file += 1
            continue
        seen_in_file.add(norm)

        existing = query_one(
            "SELECT id FROM no_poach_company WHERE normalized_name = %s", [norm]
        )
        if existing:
            already_exists += 1
            continue

        new_companies.append({
            "company_name": row["company_name"],
            "normalized_name": norm,
            "location": row["location"],
        })

    print(f"Read {total_read} data row(s) from {args.file}")
    print(f"  Already in database (skipped): {already_exists}")
    print(f"  Duplicate within the file:     {duplicate_in_file}")
    print(f"  New companies:                 {len(new_companies)}")

    if new_companies:
        print("\nNew companies found:")
        for c in new_companies:
            loc = f" ({c['location']})" if c["location"] else ""
            print(f"  - {c['company_name']}{loc}")

    if not args.confirm:
        print("\nDRY RUN -- nothing written. Re-run with --confirm to insert the new companies above.")
        return

    inserted = 0
    for c in new_companies:
        query(
            """INSERT INTO no_poach_company (company_name, normalized_name, location, status)
               VALUES (%s, %s, %s, 'current')
               ON CONFLICT (normalized_name) DO NOTHING""",
            [c["company_name"], c["normalized_name"], c["location"]],
            fetch=False,
        )
        inserted += 1

    print(f"\nDone. inserted={inserted} already_existed={already_exists} duplicate_in_file={duplicate_in_file}")


if __name__ == "__main__":
    main()
