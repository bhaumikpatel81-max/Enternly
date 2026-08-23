"""
Human-readable channel labels for candidate.source / application.source
(where a candidate/application came from — career site, vendor, campus
drive, or a direct/manual add). Separate from cv_repository.source, which
describes how the physical CV *file* entered the system (upload/watcher/
email/bulk_folder) — that's an unrelated concept.
"""
from ..db import query


def attach_source_labels(rows: list[dict], source_key: str = "source") -> None:
    """
    Mutates rows in place, adding a 'source_label' field derived from
    row[source_key] (e.g. 'vendor:<uuid>', 'campus_bulk', 'career_site').
    Batches the vendor-name lookup so this stays O(1) queries regardless
    of row count.
    """
    vendor_ids = {
        row[source_key].split(":", 1)[1]
        for row in rows
        if row.get(source_key) and str(row[source_key]).startswith("vendor:")
    }
    vendor_names: dict[str, str] = {}
    if vendor_ids:
        vrows = query(
            "SELECT id, name FROM vendor WHERE id = ANY(%s::uuid[])",
            [list(vendor_ids)],
        )
        vendor_names = {str(v["id"]): v["name"] for v in (vrows or [])}

    for row in rows:
        src = row.get(source_key)
        if not src:
            row["source_label"] = "Pool"
        elif src.startswith("vendor:"):
            vid = src.split(":", 1)[1]
            row["source_label"] = f"Vendor: {vendor_names.get(vid, 'Unknown vendor')}"
        elif src == "campus_bulk":
            row["source_label"] = "Campus"
        elif src in ("career_site", "direct"):
            row["source_label"] = "Career Site" if src == "career_site" else "Pool"
        else:
            row["source_label"] = src.replace("_", " ").title()
