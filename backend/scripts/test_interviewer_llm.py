#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Live test for interviewer_llm.py -- confirms the Groq API key works and
that the bot sounds natural before any UI is wired up.

What it does:
  1. Runs 3 conversational turns against next_turn() and prints the bot's replies.
  2. Runs score_transcript() on a fake 3-turn transcript and prints the JSON.

Usage (run from the backend/ directory so the app package resolves):
  cd c:\\Users\\bhaumik.patel\\Desktop\\Enternly\\Enternly\\backend
  py scripts\\test_interviewer_llm.py

Required env vars (loaded automatically from .env.prod if present):
  GROQ_API_KEY   -- your Groq key (starts with gsk_)
  GROQ_BASE_URL  -- defaults to https://api.groq.com/openai/v1
  LLM_MODEL      -- defaults to llama-3.3-70b-versatile
"""
import asyncio
import io
import json
import os
import sys

# Force UTF-8 output so box-drawing / non-ASCII chars render on Windows
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# -- Path bootstrap (handles running as a plain script) -----------------------
_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
if _backend not in sys.path:
    sys.path.insert(0, _backend)

# Load env vars from .env.prod at the project root (one level above backend/)
try:
    from dotenv import load_dotenv
    _project_root = os.path.dirname(_backend)
    for _f in (".env.prod", ".env"):
        _p = os.path.join(_project_root, _f)
        if os.path.exists(_p):
            load_dotenv(_p, override=False)
            print(f"[env] Loaded {_f}")
            break
except ImportError:
    pass  # python-dotenv not installed; rely on shell env

from app.services.interviewer_llm import next_turn, score_transcript

# -- Test role ----------------------------------------------------------------

ROLE_CONTEXT = {
    "title": "Senior Python Developer",
    "key_skills": ["Python", "FastAPI", "PostgreSQL", "Docker"],
    "job_description": (
        "Building scalable backend services for an AI-powered hiring platform. "
        "The role involves designing REST APIs, writing database migrations, "
        "and improving the overall reliability of our infrastructure."
    ),
}

FAKE_TRANSCRIPT = [
    {
        "speaker": "bot",
        "text": "Could you start by telling me about your background and what draws you to this role?",
    },
    {
        "speaker": "candidate",
        "text": (
            "Sure! I have about six years of Python experience, mostly building REST APIs "
            "for SaaS products. I have used FastAPI extensively over the last two years and "
            "I really enjoy the async-first design. I am drawn to this role because I want "
            "to work on a product that has a clear real-world impact."
        ),
    },
    {
        "speaker": "bot",
        "text": "That is a solid background. How comfortable are you with PostgreSQL at scale?",
    },
    {
        "speaker": "candidate",
        "text": (
            "Very comfortable. I have managed schemas with hundreds of millions of rows, "
            "written complex window functions, and run zero-downtime migrations using "
            "pg_repack and concurrent index builds. I have also done query-plan analysis "
            "with EXPLAIN ANALYZE when we had slow endpoints."
        ),
    },
    {
        "speaker": "bot",
        "text": "Can you describe a time you improved the reliability of a service you owned?",
    },
    {
        "speaker": "candidate",
        "text": (
            "We had a background job that would silently fail and lose data. "
            "I added structured logging, replaced fire-and-forget calls with a proper "
            "task queue using RQ, and set up dead-letter handling. After that change, "
            "we went from discovering failures a day later to getting alerts within seconds."
        ),
    },
]


# -- Test runner --------------------------------------------------------------

async def main() -> None:
    SEP = "=" * 62
    print(f"\n{SEP}")
    print("  NexAI LLM Interviewer -- Live Test")
    print(SEP)

    if not os.environ.get("GROQ_API_KEY"):
        print("\n[ERROR] GROQ_API_KEY is not set. Add it to .env.prod and re-run.")
        sys.exit(1)

    model = os.environ.get("LLM_MODEL", "llama-3.3-70b-versatile")
    print(f"[config] model={model}  base_url={os.environ.get('GROQ_BASE_URL', 'https://api.groq.com/openai/v1')}")

    # Part 1: 3-turn live conversation
    print("\n-- Part 1: Live Conversation (3 bot turns) --\n")

    turns: list = []

    # Turn 1 -- bot opens
    print("Calling next_turn() with empty conversation (bot opens)...")
    r = await next_turn({"role_context": ROLE_CONTEXT, "turns": turns})
    print(f"\n  BOT:         {r['reply']}")
    print(f"  is_complete: {r['is_complete']}")
    turns.append({"speaker": "bot", "text": r["reply"]})

    # Turn 2 -- candidate responds
    answer1 = (
        "I have six years of Python experience building REST APIs with FastAPI. "
        "I am particularly strong in async patterns and database design."
    )
    turns.append({"speaker": "candidate", "text": answer1})
    print(f"\n  CANDIDATE:   {answer1}")

    print("\nCalling next_turn() after first candidate reply...")
    r = await next_turn({"role_context": ROLE_CONTEXT, "turns": list(turns)})
    print(f"\n  BOT:         {r['reply']}")
    print(f"  is_complete: {r['is_complete']}")
    turns.append({"speaker": "bot", "text": r["reply"]})

    # Turn 3 -- candidate responds again
    answer2 = (
        "For Docker I am comfortable writing multi-stage builds, managing compose "
        "files for local dev, and deploying containers to ECS."
    )
    turns.append({"speaker": "candidate", "text": answer2})
    print(f"\n  CANDIDATE:   {answer2}")

    print("\nCalling next_turn() after second candidate reply...")
    r = await next_turn({"role_context": ROLE_CONTEXT, "turns": list(turns)})
    print(f"\n  BOT:         {r['reply']}")
    print(f"  is_complete: {r['is_complete']}")

    # Part 2: LLM scoring
    print("\n-- Part 2: LLM Scoring on Fake Transcript --\n")
    print("Calling score_transcript() on a 3-exchange fake transcript...")

    score = await score_transcript(
        {"role_context": ROLE_CONTEXT, "turns": FAKE_TRANSCRIPT}
    )

    print(f"\n  raw_score:    {score['raw_score']}")
    print(f"  score_detail:\n{json.dumps(score['score_detail'], indent=4)}")

    print(f"\n{SEP}")
    print("  Test complete -- Groq key works, bot sounds natural.")
    print(f"{SEP}\n")


if __name__ == "__main__":
    asyncio.run(main())
