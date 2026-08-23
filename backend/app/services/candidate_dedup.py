"""
Shared candidate dedup logic (email OR normalised phone match).

Used by every candidate-intake path (career site, vendor portal, campus bulk
upload) so duplicate detection is consistent regardless of entry point.
"""
from fastapi import HTTPException

from ..db import query, query_one


def find_existing_candidate(email: str, phone: str | None):
    """
    Return (row, matched_by) for an existing candidate matching by email OR
    by normalised phone number. matched_by is 'email' / 'phone' / None.
    """
    from .resume_parser import normalize_phone
    if email:
        row = query_one(
            "SELECT id, full_name FROM candidate WHERE lower(email) = %s",
            [email.lower()],
        )
        if row:
            return row, "email"
    norm = normalize_phone(phone) if phone else None
    if norm:
        row = query_one(
            """SELECT id, full_name FROM candidate
               WHERE regexp_replace(COALESCE(phone,''), '[^0-9]', '', 'g') = %s
               AND phone IS NOT NULL AND phone <> ''""",
            [norm],
        )
        if row:
            return row, "phone"
    return None, None


def dedup_or_create_candidate(
    full_name: str, email: str, phone: str | None,
    gender: str, source: str, resume_url: str | None,
    requisition_id: str,
):
    """
    Look for an existing candidate by email / phone.
    - If found AND already applied to this req -> raise 409.
    - If found but not yet applied -> reuse the candidate, update resume if provided.
    - If not found -> insert new candidate.
    Returns the candidate id.
    """
    from .email_validation import assert_real_email
    try:
        email = assert_real_email(email)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    existing, matched_by = find_existing_candidate(email, phone)
    if existing:
        cand_id = existing["id"]
        dup_app = query_one(
            "SELECT id FROM application WHERE requisition_id = %s AND candidate_id = %s",
            [requisition_id, cand_id],
        )
        if dup_app:
            raise HTTPException(
                409,
                f"Candidate '{existing['full_name']}' has already applied to this "
                f"requisition (duplicate detected by {matched_by}).",
            )
        if resume_url:
            query(
                "UPDATE candidate SET resume_url = %s WHERE id = %s",
                [resume_url, cand_id],
                fetch=False,
            )
        return cand_id

    from .resume_parser import normalize_phone
    norm_phone = normalize_phone(phone) if phone else None
    row = query_one(
        """INSERT INTO candidate (full_name, email, phone, gender, source, resume_url)
           VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
        [full_name, email.lower(), norm_phone, gender, source, resume_url],
    )
    return row["id"]
