"""
Seed HRBP directory + Business-Unit -> HRBP default mapping (Enternly Batch 1).

business_unit rows already exist in this system (managed via Admin >
Organisation) -- this script does NOT create business units. It only
upserts hrbp rows and the bu_hrbp_map default assignment, matching each
CSV row's bu_name against an EXISTING business_unit by name (case-
insensitive). If a name matches business units in more than one group
company, the row is skipped -- rename one of them in Admin > Organisation
so the name is unique, then re-run.

CSV columns (header row required):
    bu_name,hrbp_full_name,hrbp_email

Usage:
    py -3 seed_hrbp_setup.py --csv path/to/hrbp_setup.csv
    py -3 seed_hrbp_setup.py --csv path/to/hrbp_setup.csv --dry-run
"""
import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.db import query, query_one  # noqa: E402

REQUIRED_COLS = {"bu_name", "hrbp_full_name", "hrbp_email"}


def _find_bu(bu_name):
    return query(
        "SELECT id, name FROM business_unit WHERE LOWER(name) = LOWER(%s)",
        [bu_name.strip()],
    ) or []


def _upsert_hrbp(full_name, email):
    return query_one(
        """INSERT INTO hrbp (full_name, email)
           VALUES (%s, %s)
           ON CONFLICT (email) DO UPDATE SET full_name = EXCLUDED.full_name
           RETURNING id, full_name, email, (xmax = 0) AS inserted""",
        [full_name.strip(), email.strip().lower()],
    )


def _upsert_map(bu_id, hrbp_id):
    row = query_one(
        """INSERT INTO bu_hrbp_map (bu_id, hrbp_id)
           VALUES (%s, %s)
           ON CONFLICT (bu_id) DO UPDATE SET hrbp_id = EXCLUDED.hrbp_id
           RETURNING (xmax = 0) AS inserted""",
        [bu_id, hrbp_id],
    )
    return bool(row and row["inserted"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--dry-run", action="store_true", help="Validate + report without writing")
    args = ap.parse_args()

    inserted = updated = skipped = 0

    with open(args.csv, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        missing_cols = REQUIRED_COLS - set(reader.fieldnames or [])
        if missing_cols:
            print(f"ERROR: CSV is missing column(s): {', '.join(sorted(missing_cols))}")
            sys.exit(1)

        for i, row in enumerate(reader, start=2):  # row 1 is the header
            bu_name    = (row.get("bu_name") or "").strip()
            hrbp_name  = (row.get("hrbp_full_name") or "").strip()
            hrbp_email = (row.get("hrbp_email") or "").strip()
            if not bu_name or not hrbp_name or not hrbp_email:
                print(f"  [row {i}] SKIP -- missing bu_name/hrbp_full_name/hrbp_email")
                skipped += 1
                continue

            matches = _find_bu(bu_name)
            if not matches:
                print(f"  [row {i}] SKIP -- no existing business_unit named '{bu_name}'. "
                      f"Create it first in Admin > Organisation.")
                skipped += 1
                continue
            if len(matches) > 1:
                print(f"  [row {i}] SKIP -- '{bu_name}' matches {len(matches)} business units "
                      f"across different companies (ambiguous name).")
                skipped += 1
                continue
            bu = matches[0]

            if args.dry_run:
                print(f"  [row {i}] DRY-RUN -- would map BU '{bu['name']}' -> {hrbp_name} <{hrbp_email}>")
                continue

            hrbp = _upsert_hrbp(hrbp_name, hrbp_email)
            print(f"  [row {i}] HRBP {'created' if hrbp['inserted'] else 'already existed'}: "
                  f"{hrbp_name} <{hrbp_email}>")

            if _upsert_map(bu["id"], hrbp["id"]):
                print(f"  [row {i}] Mapped BU '{bu['name']}' -> {hrbp_name}")
                inserted += 1
            else:
                print(f"  [row {i}] Updated existing mapping: BU '{bu['name']}' -> {hrbp_name}")
                updated += 1

    print(f"\nDone. inserted={inserted} updated={updated} skipped={skipped}")


if __name__ == "__main__":
    main()
