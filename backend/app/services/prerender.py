"""
Enteri AI Avatar Pre-render Pipeline — Step 4.

Pre-renders per-question lip-sync MP4 videos using:
  edge-tts (via tts.py) → MP3 → ffmpeg WAV → SadTalker GPU → GCS (or local)

Triggered as a FastAPI BackgroundTask when a enteri_ai_invite is created.
The candidate-facing interview flow is completely independent — if pre-rendering
fails or the GPU is not deployed, the orb animation takes over seamlessly.

Storage priority:
  1. GCS (GCS_BUCKET_NAME env var set) — public HTTPS URL
  2. Local fallback (backend/media/avatar_videos/) — served at /media/avatar_videos/

Cache: avatar_video_cache table. cache_key = sha256(text|voice|avatar_name).
Identical questions across sessions reuse the same MP4 — zero extra GPU cost.
"""
import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
import uuid

import requests as _http

from ..db import query, query_one
from .tts import VOICE_FEMALE, VOICE_MALE, synthesize_speech

log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

AVATAR_IMAGE_MALE = "enteri-ai-male.png"
AVATAR_IMAGE_FEMALE = "enteri-ai-female.png"


# ── Config helpers ────────────────────────────────────────────────────────────

def _avatar_image_path() -> str:
    base = os.environ.get(
        "AVATAR_FACE_DIR",
        os.path.normpath(
            os.path.join(
                os.path.dirname(__file__), "..", "..", "frontend", "assets", "avatars"
            )
        ),
    )
    gender = os.environ.get("ENTERI_AI_VOICE_GENDER", "female").lower().strip()
    image_name = AVATAR_IMAGE_MALE if gender == "male" else AVATAR_IMAGE_FEMALE
    return os.path.join(base, image_name)


def _voice_id() -> str:
    gender = os.environ.get("ENTERI_AI_VOICE_GENDER", "female").lower().strip()
    return VOICE_MALE if gender == "male" else VOICE_FEMALE


def _sadtalker_url() -> str:
    return os.environ.get("SADTALKER_SERVICE_URL", "").rstrip("/")


def _gcs_bucket() -> str:
    return os.environ.get("GCS_BUCKET_NAME", "")


def _local_media_dir() -> str:
    d = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "..", "media", "avatar_videos")
    )
    os.makedirs(d, exist_ok=True)
    return d


def _app_base_url() -> str:
    return os.environ.get("APP_BASE_URL", "http://localhost:8080").rstrip("/")


# ── Cache helpers ─────────────────────────────────────────────────────────────

def _make_cache_key(question_text: str, voice_id: str, avatar_image_name: str) -> str:
    raw = f"{question_text}|{voice_id}|{avatar_image_name}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _cache_lookup(cache_key: str) -> str | None:
    row = query_one(
        "SELECT gcs_url FROM avatar_video_cache WHERE cache_key = %s", [cache_key]
    )
    return row["gcs_url"] if row else None


def _cache_insert(cache_key: str, url: str):
    query(
        """INSERT INTO avatar_video_cache (cache_key, gcs_url)
           VALUES (%s, %s) ON CONFLICT (cache_key) DO NOTHING""",
        [cache_key, url],
        fetch=False,
    )


# ── Storage helpers ───────────────────────────────────────────────────────────

def _gs_to_https(gs_url: str) -> str:
    """
    Convert a gs://bucket/path URL to a public HTTPS URL.
    Requires the GCS bucket to have uniform public read access (allUsers Storage Object Viewer).
    For production, replace with signed URL generation via google.cloud.storage.
    """
    # gs://bucket-name/path/to/file.mp4 -> https://storage.googleapis.com/bucket-name/path/to/file.mp4
    return "https://storage.googleapis.com/" + gs_url[5:]


def _upload_to_gcs(local_path: str, dest_name: str) -> str:
    """Upload a local file to GCS and return its public HTTPS URL."""
    bucket = _gcs_bucket()
    from google.cloud import storage as gcs
    client = gcs.Client()
    blob = client.bucket(bucket).blob(f"enteri-ai-avatars/{dest_name}")
    blob.upload_from_filename(local_path, content_type="video/mp4")
    blob.make_public()
    return blob.public_url


def _save_locally(local_path: str, dest_name: str) -> str:
    """Copy to the local media dir and return a servable URL."""
    dest = os.path.join(_local_media_dir(), dest_name)
    shutil.copy2(local_path, dest)
    return f"{_app_base_url()}/media/avatar_videos/{dest_name}"


# ── ffmpeg conversion ─────────────────────────────────────────────────────────

def _mp3_to_wav(mp3_path: str) -> str:
    """
    Convert MP3 to 16 kHz mono WAV — the format SadTalker expects.
    Raises RuntimeError with a clear install hint if ffmpeg is missing.
    """
    wav_path = mp3_path.replace(".mp3", ".wav")
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", mp3_path, "-ar", "16000", "-ac", "1", wav_path],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "ffmpeg not found on this server. "
            "Install it: apt-get install ffmpeg (Linux) or choco install ffmpeg (Windows)."
        )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg conversion failed (exit {result.returncode}): {result.stderr[-300:]}")
    return wav_path


# ── SadTalker call ────────────────────────────────────────────────────────────

def _call_sadtalker(face_path: str, wav_path: str) -> str:
    """
    POST face image + WAV audio to the SadTalker GPU service.
    Returns a public HTTPS video URL (from GCS or local fallback).

    The GPU service (sadtalker_service.py) handles its own GCS upload and
    returns {"video_url": "gs://bucket/path.mp4"}.
    We convert that gs:// URL to a public HTTPS URL.
    If the service returns raw MP4 bytes instead, we upload ourselves.
    """
    url = _sadtalker_url()
    if not url:
        raise RuntimeError("SADTALKER_SERVICE_URL is not set — GPU not deployed yet")

    with open(face_path, "rb") as img_f, open(wav_path, "rb") as aud_f:
        resp = _http.post(
            f"{url}/render",
            files={"face": img_f, "audio": aud_f},
            timeout=200,
        )

    if resp.status_code != 200:
        raise RuntimeError(f"SadTalker returned HTTP {resp.status_code}: {resp.text[:200]}")

    ct = resp.headers.get("content-type", "")

    # Case A: GPU service already uploaded to GCS and returned a JSON URL
    if "json" in ct:
        data = resp.json()
        video_url = data.get("video_url")
        if not video_url:
            raise RuntimeError(f"SadTalker returned no video_url: {data}")
        # Convert gs:// protocol URL to public HTTPS URL
        if video_url.startswith("gs://"):
            video_url = _gs_to_https(video_url)
        return video_url

    # Case B: GPU service returned raw MP4 bytes — upload ourselves
    dest_name = f"{uuid.uuid4()}.mp4"
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tf:
        tf.write(resp.content)
        mp4_tmp = tf.name
    try:
        bucket = _gcs_bucket()
        if bucket:
            try:
                return _upload_to_gcs(mp4_tmp, dest_name)
            except Exception as exc:
                log.warning("prerender: GCS upload failed (%s) — saving locally", exc)
        return _save_locally(mp4_tmp, dest_name)
    finally:
        try:
            os.unlink(mp4_tmp)
        except Exception:
            pass


# ── DB state helpers ──────────────────────────────────────────────────────────

def _update_render_state(session_id: str, render_status: str, question_videos: list):
    query(
        """UPDATE enteri_ai_session
           SET render_status = %s, question_videos = %s::jsonb
           WHERE id = %s""",
        [render_status, json.dumps(question_videos), session_id],
        fetch=False,
    )


# ── Main pipeline ─────────────────────────────────────────────────────────────

async def prerender_interview_videos(session_id: str):
    """
    Background task: pre-render a lip-sync MP4 for every question in the session.

    Fired immediately after enteri_ai_invite is created. The candidate interview flow
    is unaffected — the frontend checks question_videos and falls back to the orb
    for any question whose video_url is None or status is 'failed'.

    Degradation guarantees:
      - SADTALKER_SERVICE_URL not set   → all questions marked failed, orb used
      - Avatar image missing            → all questions marked failed, orb used
      - Any single question fails       → that question uses orb, rest continue
      - ffmpeg not installed            → all questions fail with a clear log message
    """
    log.info("prerender: starting session=%s", session_id)

    sess = query_one(
        "SELECT id, questions FROM enteri_ai_session WHERE id = %s", [session_id]
    )
    if not sess or not sess.get("questions"):
        log.warning("prerender: session=%s not found or has no questions", session_id)
        return

    questions = sess["questions"]
    question_videos = [
        {"seq": q["seq"], "question": q["text"], "video_url": None, "status": "pending"}
        for q in questions
    ]

    # Fast-fail checks: avatar image and GPU service must both be present
    avatar_path = _avatar_image_path()
    if not os.path.isfile(avatar_path):
        log.warning(
            "prerender: avatar image not found at %s — "
            "add the matching avatar PNG to frontend/assets/avatars/ to enable GPU rendering. "
            "Orb will be used for session=%s.",
            avatar_path, session_id,
        )
        for item in question_videos:
            item["status"] = "failed"
        _update_render_state(session_id, "failed", question_videos)
        return

    if not _sadtalker_url():
        log.warning(
            "prerender: SADTALKER_SERVICE_URL not set — GPU not deployed yet. "
            "Orb will be used for session=%s. "
            "Set SADTALKER_SERVICE_URL once the GPU container is running.",
            session_id,
        )
        for item in question_videos:
            item["status"] = "failed"
        _update_render_state(session_id, "failed", question_videos)
        return

    voice_id = _voice_id()
    _update_render_state(session_id, "rendering", question_videos)

    any_ready = False
    any_failed = False

    for i, q in enumerate(questions):
        seq = q["seq"]
        text = q["text"]
        cache_key = _make_cache_key(text, voice_id, os.path.basename(avatar_path))
        mp3_path = None
        wav_path = None

        try:
            # 1. Cache check — reuse existing MP4 if this exact question was rendered before
            cached_url = _cache_lookup(cache_key)
            if cached_url:
                log.info("prerender: cache hit seq=%d session=%s", seq, session_id)
                question_videos[i].update({"video_url": cached_url, "status": "ready"})
                any_ready = True
                _update_render_state(session_id, "rendering", question_videos)
                continue

            # 2. TTS → MP3
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tf:
                mp3_path = tf.name
            await synthesize_speech(text, mp3_path, voice=voice_id)

            # 3. MP3 → WAV (SadTalker requires 16 kHz mono WAV)
            wav_path = _mp3_to_wav(mp3_path)

            # 4. SadTalker GPU render
            video_url = _call_sadtalker(avatar_path, wav_path)

            # 5. Cache for future reuse across sessions
            _cache_insert(cache_key, video_url)

            question_videos[i].update({"video_url": video_url, "status": "ready"})
            any_ready = True
            log.info("prerender: ready seq=%d session=%s url=%s", seq, session_id, video_url)

        except (NameError, AttributeError, TypeError, KeyError, ImportError) as exc:
            # A bug in this code path, not an expected degraded-mode condition
            # (GPU down, TTS hiccup, etc.) -- log loud with a traceback so it
            # can't hide behind the same "FAILED" line every transient error
            # above prints, the way the AVATAR_IMAGE_NAME NameError once did.
            log.error(
                "prerender: BUG seq=%d session=%s — %s: %s",
                seq, session_id, type(exc).__name__, exc, exc_info=True,
            )
            question_videos[i]["status"] = "failed"
            any_failed = True

        except Exception as exc:
            log.warning("prerender: FAILED seq=%d session=%s — %s", seq, session_id, exc)
            question_videos[i]["status"] = "failed"
            any_failed = True

        finally:
            for p in [mp3_path, wav_path]:
                if p:
                    try:
                        os.unlink(p)
                    except Exception:
                        pass

        # Persist progress after each question so recruiters can see partial readiness
        _update_render_state(session_id, "rendering", question_videos)

    # Final session-level status
    if any_ready and any_failed:
        final_status = "partial"
    elif any_ready:
        final_status = "ready"
    else:
        final_status = "failed"

    _update_render_state(session_id, final_status, question_videos)
    log.info(
        "prerender: finished session=%s status=%s ready=%s failed=%s",
        session_id, final_status, any_ready, any_failed,
    )
