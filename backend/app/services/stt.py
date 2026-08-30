"""
Speech-to-text via OpenAI Whisper API.
Used by the conversational Enteri AI interview to transcribe candidate audio.
Env: OPENAI_API_KEY (reuses the same key as the LLM brain).
"""
import os
import tempfile
from typing import Optional

import openai

_client: Optional[openai.OpenAI] = None


def _get_client() -> openai.OpenAI:
    global _client
    if _client is None:
        api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("GROQ_API_KEY", "")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY not set — required for Whisper STT.")
        base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
        _client = openai.OpenAI(api_key=api_key, base_url=base_url)
    return _client


def transcribe_audio(audio_bytes: bytes, filename: str = "audio.webm",
                     model: str = None) -> str:
    """
    Transcribe a single audio blob with Whisper. Returns plain text.
    model defaults to env WHISPER_MODEL or 'whisper-1'.
    """
    model = model or os.environ.get("WHISPER_MODEL", "whisper-1")
    suffix = "." + filename.rsplit(".", 1)[-1] if "." in filename else ".webm"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=True) as tf:
        tf.write(audio_bytes)
        tf.flush()
        tf.seek(0)
        with open(tf.name, "rb") as fh:
            resp = _get_client().audio.transcriptions.create(
                model=model,
                file=fh,
                response_format="text",
            )
    # SDK returns a str when response_format="text"
    return (resp if isinstance(resp, str) else getattr(resp, "text", "")).strip()
