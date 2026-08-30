# Enternly — AI Avatar + Proctoring build spec (Enteri AI interview)

This extends the working Enteri AI voice interview (TTS speaks questions, STT captures answers, scores saved). It adds: (1) an AI-generated talking-face avatar using free open-source models, and (2) a consent-gated proctoring layer. Execute in order, one step at a time, verifying each in the browser before the next. Do not batch.

## HARD LEGAL GATE — read first
Proctoring records candidates' camera, microphone, and screen, and runs identity capture. This is highly regulated personal/biometric data. Build everything consent-gated from the first line. **No real external candidate may be recorded until legal sign-off is obtained.** Testing is permitted ONLY with internal team members who have consented. Every proctoring feature must: show a clear consent screen first, show a persistent "recording active" indicator, store data on company GCP, and surface any AI flag to a HUMAN reviewer as information — never auto-reject.

---

## PART A — The AI avatar (free, no paid vendor)

### Approach
Two layers behind one interface so the visual can be swapped without touching the rest:
1. **Default visual (build now):** an animated branded orb/waveform that pulses while Enteri AI's audio plays. Pure frontend (canvas/SVG + the Web Audio API analyser). Zero cost, no uncanny-valley risk. This is what runs out of the box.
2. **Talking-face avatar (build the hook + GPU pipeline):** open-source lip-sync — SadTalker or Wav2Lip — that takes a single face image + the question audio and renders a short video of that face speaking in sync. Runs on a GPU instance on the company's GCP (compute cost only, no per-use licence). Provide one AI-generated male and one female face image (generate free from an open image model / "this-person-does-not-exist"-style source; store under `frontend/assets/avatars/`).

### Interaction pattern
Question-by-question, because the open models render best from complete audio rather than live streaming:
1. Enteri AI generates the next question text → TTS audio (already working).
2. The avatar service renders the chosen face speaking that audio (or, if face disabled, the orb just pulses to the audio).
3. Candidate watches the question, then answers; STT captures + scores (already working).
4. Loop to next question.

### STEP A1 — Orb visual (frontend only)
Replace the static Enteri AI interview screen with an animated orb/waveform that reacts to the playing audio amplitude. Enternly fire-orange accent. Verify: start a Enteri AI interview, the orb animates while each question is spoken.

### STEP A2 — Avatar service interface (swappable)
Create a backend service `avatar.py` with one function, e.g. `render_speaking_clip(face_id, audio_path) -> video_url`, and a provider setting (`orb` | `sadtalker` | `wav2lip` | `vendor`). Default `orb` returns nothing (frontend handles it). This is the seam that keeps the face swappable. Verify: setting persists; default path still works with the orb.

### STEP A3 — SadTalker/Wav2Lip pipeline (GPU)
Implement the `sadtalker`/`wav2lip` provider: containerised, runs on a GCP GPU instance, takes face image + audio, returns an MP4 stored on GCP, returns its URL. Document the GPU instance type and the rough hourly cost so the team can budget. Mark clearly that this provider only runs when a GPU box is available; the orb is the fallback when it is not. Verify (on a GPU box): a question renders as a talking-face clip; without GPU, it cleanly falls back to the orb.

### Voice
Keep the existing TTS voice. If a more natural voice is wanted later, the voice provider is already isolated in the TTS layer — swap there. Note: high-naturalness neural voices are often a paid service; the current voice is the free baseline.

---

## PART B — Proctoring (consent-gated; build what is genuinely buildable)

### Build these (web-buildable, real security value)
- **B1 Consent screen:** before any camera/screen access, a clear notice of what is captured and why, with explicit accept/decline. Decline = no proctored interview. Log consent (reuse the `recording_consent` pattern). Persistent on-screen "recording active" badge throughout.
- **B2 Identity capture:** capture a webcam still at start and store it with the session (face image on file for human comparison). NOTE: automated biometric matching against a government ID is a specialist paid service — scaffold an `identity_match` hook but mark it out of scope for native build.
- **B3 Webcam recording:** continuous webcam video during the interview → GCP.
- **B4 Screen recording:** capture the candidate's screen via the browser screen-capture API (requires candidate to grant it; if declined, flag the session as un-proctored rather than blocking silently) → GCP.
- **B5 Audio monitoring:** ambient audio captured with the webcam stream.
- **B6 AI behaviour analysis on the recorded frames (assistive, human-reviewed):** multi-face detection (someone else appears), face-absent / looked-away detection, and basic forbidden-object detection (phone in frame) using an open vision model. Each produces an AI-flagged timestamp.
- **B7 Flag review tool:** flags surface to a human recruiter on a timeline with jump-to-timestamp; the human decides. Never auto-reject. Generate a downloadable incident summary.

### Do NOT attempt natively (mark as out of scope / specialist vendor)
These are not reliably possible from a browser web app and must not be faked:
- Lockdown browser (blocking tabs/copy/paste/print) — needs an installed native app.
- Secondary-device / hidden-phone / extra-monitor detection via audio or Wi-Fi — needs native + hardware signals.
- Virtual machine blocking — needs native system access.
- Government-ID biometric face matching — specialist paid identity service.
- Keystroke-dynamics identity — low reliability for interviews; skip.
Leave clearly-labelled stubs/notes for each so the team knows these need a vendor or a native app, and does not waste effort trying to build them in the browser.

### Storage & retention
All proctoring media on company GCP, with a retention period field per session (legal sets the value). No proctoring data in the main app database except references and the AI-flag timestamps.

### Rule for every step
Build it, then STOP: tell the user the command, the URL, what to look for, and wait for confirmation. For any proctoring step, restate the legal gate in the verification note.
