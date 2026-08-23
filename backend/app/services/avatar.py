"""
STEP A2 — Avatar service interface (swappable).

One function: render_speaking_clip(face_id, audio_path) -> dict.
Provider is set by the AVATAR_PROVIDER env var:

  orb        (default) — frontend handles visuals, no server rendering
  sadtalker  — open-source GPU lip-sync (see backend/gpu-services/)
  wav2lip    — open-source GPU lip-sync (see backend/gpu-services/)
  vendor     — paid avatar vendor (D-ID, HeyGen, etc.)

Only 'orb' works without a GPU instance.
All others fall back to orb cleanly if their service is unreachable.
"""
import os
import logging

log = logging.getLogger(__name__)

PROVIDER = os.environ.get("AVATAR_PROVIDER", "orb")

# ── Public API ────────────────────────────────────────────────────────────────

def get_config() -> dict:
    return {
        "provider": PROVIDER,
        "service_reachable": PROVIDER == "orb" or _service_reachable(PROVIDER),
        "face_ids": _available_face_ids(),
        "gpu_notes": _GPU_NOTES.get(PROVIDER, ""),
    }


def render_speaking_clip(face_id: str, audio_path: str) -> dict:
    """
    Render a speaking avatar clip for face_id + audio at audio_path.
    Returns:
      { video_url: str|None, provider: str, fallback: bool, reason: str|None }
    When video_url is None the frontend falls back to the animated orb.
    """
    if PROVIDER == "sadtalker":
        return _sadtalker(face_id, audio_path)
    if PROVIDER == "wav2lip":
        return _wav2lip(face_id, audio_path)
    if PROVIDER == "vendor":
        return _vendor(face_id, audio_path)
    return _orb()


# ── Provider notes ────────────────────────────────────────────────────────────

_GPU_NOTES = {
    "sadtalker": (
        "GCP n1-standard-4 + NVIDIA T4 GPU. "
        "Approx cost: $0.35/hr preemptible | $0.95/hr on-demand. "
        "Render time: ~60-90 s per 10-15 s question on T4. "
        "Set SADTALKER_SERVICE_URL to the running GPU container endpoint. "
        "Container: backend/gpu-services/Dockerfile.sadtalker"
    ),
    "wav2lip": (
        "GCP n1-standard-4 + NVIDIA T4 GPU. "
        "Approx cost: same as SadTalker. "
        "Render time: ~30-50 s per question on T4 (faster than SadTalker). "
        "Set WAV2LIP_SERVICE_URL. "
        "Container: backend/gpu-services/Dockerfile.wav2lip"
    ),
    "vendor": (
        "Paid avatar vendor (D-ID, HeyGen, etc.). "
        "Set AVATAR_VENDOR_URL and AVATAR_VENDOR_KEY. "
        "Validate per-interview cost BEFORE enabling in production."
    ),
    "orb": "No extra infrastructure. Frontend renders an animated orb.",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _face_dir() -> str:
    return os.environ.get(
        "AVATAR_FACE_DIR",
        os.path.normpath(
            os.path.join(os.path.dirname(__file__), "..", "..", "frontend", "assets", "avatars")
        ),
    )


def _available_face_ids() -> list:
    d = _face_dir()
    if not os.path.isdir(d):
        return []
    return [f.rsplit(".", 1)[0] for f in os.listdir(d) if f.lower().endswith((".png", ".jpg", ".jpeg"))]


def _service_reachable(provider: str) -> bool:
    url = {
        "sadtalker": os.environ.get("SADTALKER_SERVICE_URL"),
        "wav2lip":   os.environ.get("WAV2LIP_SERVICE_URL"),
        "vendor":    os.environ.get("AVATAR_VENDOR_URL"),
    }.get(provider)
    if not url:
        return False
    try:
        import requests
        return requests.get(f"{url}/health", timeout=3).status_code == 200
    except Exception:
        return False


def _find_face_image(face_id: str) -> str | None:
    d = _face_dir()
    for ext in (".png", ".jpg", ".jpeg"):
        p = os.path.join(d, f"{face_id}{ext}")
        if os.path.isfile(p):
            return p
    return None


# ── Provider implementations ─────────────────────────────────────────────────

def _sadtalker(face_id: str, audio_path: str) -> dict:
    """
    STEP A3 — SadTalker GPU provider.
    Sends face image + audio to the GPU service container.
    Falls back to orb automatically if service is unreachable.
    """
    url = os.environ.get("SADTALKER_SERVICE_URL")
    if not url:
        return _fallback("SADTALKER_SERVICE_URL not set — start the GPU container first")
    face = _find_face_image(face_id)
    if not face:
        return _fallback(f"Face image '{face_id}' not found in {_face_dir()}")
    try:
        import requests
        with open(face, "rb") as img_f, open(audio_path, "rb") as aud_f:
            r = requests.post(f"{url}/render", files={"face": img_f, "audio": aud_f}, timeout=180)
        if r.status_code == 200:
            return {"video_url": r.json()["video_url"], "provider": "sadtalker", "fallback": False, "reason": None}
        return _fallback(f"SadTalker returned HTTP {r.status_code}")
    except Exception as exc:
        log.warning("SadTalker unavailable (%s) — falling back to orb", exc)
        return _fallback(str(exc))


def _wav2lip(face_id: str, audio_path: str) -> dict:
    """STEP A3 — Wav2Lip GPU provider (same pattern as SadTalker)."""
    url = os.environ.get("WAV2LIP_SERVICE_URL")
    if not url:
        return _fallback("WAV2LIP_SERVICE_URL not set")
    face = _find_face_image(face_id)
    if not face:
        return _fallback(f"Face image '{face_id}' not found")
    try:
        import requests
        with open(face, "rb") as img_f, open(audio_path, "rb") as aud_f:
            r = requests.post(f"{url}/render", files={"face": img_f, "audio": aud_f}, timeout=120)
        if r.status_code == 200:
            return {"video_url": r.json()["video_url"], "provider": "wav2lip", "fallback": False, "reason": None}
        return _fallback(f"Wav2Lip returned HTTP {r.status_code}")
    except Exception as exc:
        log.warning("Wav2Lip unavailable — falling back to orb")
        return _fallback(str(exc))


def _vendor(face_id: str, audio_path: str) -> dict:
    """
    Paid vendor avatar (D-ID, HeyGen, etc.).
    Requires AVATAR_VENDOR_URL + AVATAR_VENDOR_KEY.
    Validate per-interview cost BEFORE enabling.
    TODO: implement vendor-specific call once vendor is selected.
    """
    if not os.environ.get("AVATAR_VENDOR_KEY"):
        return _fallback("AVATAR_VENDOR_KEY not set — vendor not configured")
    return _fallback("Vendor integration pending — implement _vendor() once vendor is chosen")


def _orb() -> dict:
    return {"video_url": None, "provider": "orb", "fallback": False, "reason": None}


def _fallback(reason: str) -> dict:
    return {"video_url": None, "provider": "orb", "fallback": True, "reason": reason}
