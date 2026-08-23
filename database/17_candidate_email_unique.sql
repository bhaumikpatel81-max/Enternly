-- Migration: deduplicate candidate records and enforce unique email
-- Step 1: Re-point applications from duplicate candidate records to the oldest
-- canonical record for each email, then delete the duplicates.
-- Step 2: Add unique index on lower(email) to prevent future duplicates.

-- Re-assign applications from duplicate candidate_ids to the canonical (oldest) one
UPDATE application a
SET candidate_id = canon.id
FROM (
    SELECT DISTINCT ON (LOWER(email))
        id,
        LOWER(email) AS norm_email
    FROM candidate
    ORDER BY LOWER(email), created_at ASC   -- keep the oldest as canonical
) canon
JOIN candidate dup
     ON LOWER(dup.email) = canon.norm_email
     AND dup.id <> canon.id
WHERE a.candidate_id = dup.id
  -- skip if canonical already has an application to this requisition (would violate unique constraint)
  AND NOT EXISTS (
      SELECT 1 FROM application x
      WHERE x.candidate_id = canon.id
        AND x.requisition_id = a.requisition_id
  );

-- Delete applications that couldn't be re-assigned (canonical already had one for that req)
DELETE FROM application a
WHERE EXISTS (
    SELECT 1 FROM candidate c
    WHERE c.id = a.candidate_id
      AND EXISTS (
          SELECT 1 FROM candidate older
          WHERE LOWER(older.email) = LOWER(c.email)
            AND older.created_at < c.created_at
      )
);

-- Delete duplicate candidate rows (non-canonical)
DELETE FROM candidate c
WHERE EXISTS (
    SELECT 1 FROM candidate older
    WHERE LOWER(older.email) = LOWER(c.email)
      AND older.created_at < c.created_at
);

-- Enforce uniqueness going forward
CREATE UNIQUE INDEX IF NOT EXISTS uidx_candidate_email
    ON candidate (LOWER(email));
