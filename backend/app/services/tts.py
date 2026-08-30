"""
Enteri AI TTS service — neural voice synthesis with automatic fallback.

Primary:  edge-tts (Microsoft Edge neural voices, free, no API key)
Fallback: gTTS (Google TTS, robotic but always available)

Voice gender is controlled by the ENTERI_AI_VOICE_GENDER env var:
  "female" (default) -> en-IN-NeerjaNeural
  "male"             -> en-IN-PrabhatNeural
Default is female to match the Enteri AI avatar image (frontend/assets/avatars/enteri-ai-female.png,
hardcoded in interview.html) — keep these in sync if you ever change one.
"""
import logging
import os
import re

log = logging.getLogger(__name__)

VOICE_MALE = "en-IN-PrabhatNeural"
VOICE_FEMALE = "en-IN-NeerjaNeural"

# Maps brand terms to phonetic respellings fed to the TTS engine only.
# Does NOT affect DB, API responses, or on-screen text.
# Enteri AI target: "En-tuh-ree" (three syllables) + spoken letters "A I".
# Tune ONLY the value here if the pronunciation is still wrong:
#   alternatives in order: "En-tuh-ree A I", "En-teh-ree A.I.", "Enter-ee Ay-Eye"
PRONUNCIATION_FIXES: dict[str, str] = {
    "Enteri AI": "En-tuh-ree A.I.",
    "NexHire": "Nex-hire",
}

def _normalize_pronunciation(text: str) -> str:
    """Replace brand terms with phonetic respellings for TTS only."""
    for term, phonetic in PRONUNCIATION_FIXES.items():
        text = re.sub(rf"\b{re.escape(term)}\b", phonetic, text, flags=re.IGNORECASE)
    return text


def _resolve_voice(voice: str) -> str:
    """Return voice string; if empty/None, read from ENTERI_AI_VOICE_GENDER env var."""
    if voice:
        return voice
    gender = os.environ.get("ENTERI_AI_VOICE_GENDER", "female").lower().strip()
    return VOICE_MALE if gender == "male" else VOICE_FEMALE


async def synthesize_speech(
    text: str,
    out_path: str,
    voice: str = "",
) -> str:
    """
    Synthesize text to an MP3 at out_path. Returns out_path.

    Tries edge-tts (neural) first. Falls back to gTTS automatically
    if edge-tts is unavailable or raises any error.
    """
    voice = _resolve_voice(voice)
    tts_text = _normalize_pronunciation(text)

    # Always printed so callers can confirm substitutions ran (visible even without logging config).
    print(f"[TTS] ORIGINAL  : {text}")
    print(f"[TTS] NORMALIZED: {tts_text}")

    try:
        import edge_tts
        communicate = edge_tts.Communicate(tts_text, voice)
        await communicate.save(out_path)
        log.debug("edge-tts: synthesized %d chars → %s (voice=%s)", len(tts_text), out_path, voice)
        print(f"[TTS] engine=edge-tts  voice={voice}  out={out_path}")
        return out_path
    except ImportError:
        log.warning("edge-tts not installed — falling back to gTTS")
        print("[TTS] edge-tts not installed — falling back to gTTS")
    except Exception as exc:
        log.warning("edge-tts failed (%s) — falling back to gTTS", exc)
        print(f"[TTS] edge-tts failed ({exc}) — falling back to gTTS")

    # gTTS fallback
    from gtts import gTTS
    gTTS(text=tts_text, lang="en", tld="co.in").save(out_path)
    log.debug("gTTS fallback: synthesized → %s", out_path)
    print(f"[TTS] engine=gTTS  out={out_path}")
    return out_path
