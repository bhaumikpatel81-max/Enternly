"""
Proctoring media storage — Postgres BYTEA, not local disk.

routers/proctoring_api.py's own docstring is a hard legal gate: "All
proctoring data must stay on company GCP." Storing it in the existing
Postgres database (already company-owned infra, see .env.prod DB_HOST)
satisfies that gate with zero new infrastructure. More importantly, it fixes
the actual reliability bug: local-disk storage broke the moment a second
backend replica came into the picture — a chunk written to replica A's disk
was invisible to a stream request handled by replica B. Every replica
already shares this one Postgres, so reads work regardless of which
instance handled the corresponding upload.

Table: proctoring_media (see migration 64 in main.py's auto-migration list).
One row per identity snapshot (chunk_index always 0) or per webcam/screen
chunk (chunk_index = the recorder's sequence number). Re-uploading the same
(session_id, media_type, chunk_index) — a client retry, or a fresh identity
photo — overwrites in place rather than erroring.
"""
from typing import Optional

from ..db import query, query_one

IDENTITY = "identity"


def save_identity(session_id: str, data: bytes, ext: str, content_type: str) -> None:
    save_chunk(session_id, IDENTITY, 0, data, ext, content_type)


def read_identity(session_id: str) -> Optional[dict]:
    """Returns {"data": bytes, "ext": str, "content_type": str} or None."""
    row = query_one(
        """SELECT data, ext, content_type FROM proctoring_media
           WHERE session_id = %s AND media_type = %s AND chunk_index = 0""",
        [session_id, IDENTITY],
    )
    if not row:
        return None
    return {"data": bytes(row["data"]), "ext": row["ext"], "content_type": row["content_type"]}


def save_chunk(session_id: str, media_type: str, chunk_index: int, data: bytes, ext: str, content_type: str) -> None:
    query(
        """INSERT INTO proctoring_media
               (session_id, media_type, chunk_index, ext, content_type, data, byte_size)
           VALUES (%s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (session_id, media_type, chunk_index)
           DO UPDATE SET ext = EXCLUDED.ext, content_type = EXCLUDED.content_type,
                         data = EXCLUDED.data, byte_size = EXCLUDED.byte_size,
                         created_at = now()""",
        [session_id, media_type, chunk_index, ext, content_type, data, len(data)],
        fetch=False,
    )


def list_chunks(session_id: str, media_type: str) -> list:
    """[{"chunk_index": int, "ext": str, "byte_size": int}, ...] ordered by chunk_index."""
    rows = query(
        """SELECT chunk_index, ext, byte_size FROM proctoring_media
           WHERE session_id = %s AND media_type = %s
           ORDER BY chunk_index""",
        [session_id, media_type],
    ) or []
    return [dict(r) for r in rows]


def chunk_size(session_id: str, media_type: str, chunk_index: int) -> Optional[int]:
    row = query_one(
        """SELECT byte_size FROM proctoring_media
           WHERE session_id = %s AND media_type = %s AND chunk_index = %s""",
        [session_id, media_type, chunk_index],
    )
    return row["byte_size"] if row else None


def chunk_meta(session_id: str, media_type: str, chunk_index: int) -> Optional[dict]:
    row = query_one(
        """SELECT ext, content_type, byte_size FROM proctoring_media
           WHERE session_id = %s AND media_type = %s AND chunk_index = %s""",
        [session_id, media_type, chunk_index],
    )
    return dict(row) if row else None


def read_chunk_range(
    session_id: str, media_type: str, chunk_index: int,
    start: int = 0, length: Optional[int] = None,
) -> Optional[bytes]:
    """Byte range read via SQL substring() so Postgres slices the blob rather
    than this process pulling the whole thing into memory first. `start` is
    a 0-indexed offset (HTTP Range semantics); Postgres substring() is
    1-indexed, hence the +1."""
    if length is not None:
        row = query_one(
            """SELECT substring(data FROM %s FOR %s) AS chunk FROM proctoring_media
               WHERE session_id = %s AND media_type = %s AND chunk_index = %s""",
            [start + 1, length, session_id, media_type, chunk_index],
        )
    else:
        row = query_one(
            """SELECT substring(data FROM %s) AS chunk FROM proctoring_media
               WHERE session_id = %s AND media_type = %s AND chunk_index = %s""",
            [start + 1, session_id, media_type, chunk_index],
        )
    return bytes(row["chunk"]) if row else None
