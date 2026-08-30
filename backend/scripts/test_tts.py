"""
Quick smoke-test for the Enteri AI TTS pipeline.
Synthesizes a sample sentence and saves it to test_output_v2.mp3 in this directory.
Run from the backend/ folder:

    python scripts/test_tts.py
"""
import asyncio
import os
import sys

# Allow running from backend/ without installing the package
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.tts import VOICE_MALE, VOICE_FEMALE, synthesize_speech, _normalize_pronunciation

# Must contain "Enteri AI" to verify the pronunciation fix.
SAMPLE_TEXT = "Welcome to your Enteri AI interview. Enteri AI will ask you a few questions."

OUT_PATH = os.path.join(os.path.dirname(__file__), "test_output.mp3")


async def main():
    voice = os.environ.get("ENTERI_AI_VOICE_GENDER", "female")
    selected = VOICE_MALE if voice.lower() == "male" else VOICE_FEMALE

    normalized = _normalize_pronunciation(SAMPLE_TEXT)

    print("=" * 60)
    print(f"Voice gender  : {voice}")
    print(f"Voice ID      : {selected}")
    print(f"Output file   : {OUT_PATH}")
    print(f"ORIGINAL text : {SAMPLE_TEXT}")
    print(f"NORMALIZED    : {normalized}")
    print("=" * 60)

    if "Enteri AI" in normalized:
        print("WARNING: 'Enteri AI' still present in normalized text — substitution did NOT apply!")
    else:
        print("OK: 'Enteri AI' was substituted before synthesis.")

    print("Synthesizing …")
    await synthesize_speech(SAMPLE_TEXT, OUT_PATH, voice=selected)

    abs_path = os.path.abspath(OUT_PATH)
    size_kb = os.path.getsize(abs_path) / 1024
    print("=" * 60)
    print(f"Done — {size_kb:.1f} KB")
    print(f"Path  : {abs_path}")
    print(f"Spoken: {normalized}")
    print("Open test_output.mp3 — should say 'neks A-I'.")


if __name__ == "__main__":
    asyncio.run(main())
