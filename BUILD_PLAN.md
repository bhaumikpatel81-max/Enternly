# Enternly — Build Plan (execute in order, one step at a time)

This is the master sequence for finishing Enternly. It exists because an earlier all-at-once instruction caused the backend to be built but the frontend redesign to be skipped. To prevent that, **each step below must be completed and visibly verified in the browser before starting the next.** Do not batch steps. At the end of each step, tell the user exactly what to run, what URL to open, and what they should see.

## Current state (verified)

- Backend: auth (JWT + bcrypt), login page, admin user CRUD, Google OAuth (per-recruiter) — all built and working.
- Backend: pipeline, screening (keyword + experience + AI stub), notetaker (consent-gated) — built.
- Frontend: STILL the original single-page prototype (`index.html`). The multi-screen design in `DESIGN_SPEC.md` was NOT built. This is the main gap.
- Resume intake: only accepts pasted text. No file upload, no parsing.

## Roles (three, each distinct — they must NOT see the same screen)

- **recruiter** — works requisitions and candidates; runs the pipeline; schedules interviews on their own linked Google Calendar.
- **ta_manager** — the admin: creates/manages user logins and roles, and sees analytics across all recruiters, BUs, and reports.
- **hiring_manager** — lighter role: reviews shortlisted profiles and gives panel/interview feedback; does not run the pipeline.

Add `hiring_manager` to the role options in the `app_user` table and seed at least one. After login, each role routes to a different home screen.

---

## STEP 1 — Add the hiring_manager role
Add `hiring_manager` to the allowed roles (DB check constraint + any backend enum) and seed one hiring manager user. Verify: the admin user-management screen can create a user with role hiring_manager.

## STEP 2 — Resume intake: file upload + parsing (PDF and Word first)
Change the apply flow to accept an uploaded file (PDF or .docx), not just pasted text. On upload: store the file (GCP path field already exists as `candidate.resume_url`), extract the text server-side (use a PDF text library and a docx library), and feed that extracted text into the existing `score_application`. Keep pasted-text as a fallback option.
- Image resumes (JPEG/PNG) via OCR: scaffold the hook but mark as a later sub-step.
- Naukri/LinkedIn import: do NOT build now — these need paid API/partnership access. Leave a clearly-marked `import_from_jobboard` stub so it can plug in later.
Verify: upload a real PDF resume in the browser, see the match score and breakdown appear.

## STEP 3 — The shared app shell (sidebar + top bar)
Build the layout shell from DESIGN_SPEC.md: dark left sidebar (~200px) with the Enternly logo and nav items, light working canvas, top bar with screen title + the logged-in user's name/role + Google Calendar status. Nav items shown depend on role:
- recruiter: Dashboard, Requisitions, Candidates, Interviews, Reports
- ta_manager: Dashboard, Requisitions, Candidates, Interviews, Reports, Team, Users, Analytics
- hiring_manager: Dashboard, Profiles to review, Interviews
Verify: after login, each role sees the shell with the correct nav items.

## STEP 4 — The dashboard (role-specific)
Build the dashboard from DESIGN_SPEC.md with the pipeline count cards in this exact order and naming: Open Requisitions, Applications Received, Under Screening, Screening Cleared, AI Interview, Panel Interview (single combined count here), Selected, Offer Stage, Joined (green). Emphasise Average Time to Hire with the orange accent. Below: requisitions list + gender-diversity bar. ta_manager dashboard adds a Recruiter Load panel (use `v_recruiter_load`). hiring_manager dashboard shows only profiles awaiting their review + their upcoming interviews.
Verify: each role's dashboard looks different and shows real counts from the database.

## STEP 5 — Requisitions list + New Requisition form
List screen per DESIGN_SPEC.md (status pills, table with Role/BU/Band/In pipeline/Levels). The New Requisition form lets the recruiter set the number of panel levels and name them (Level 1 Panel, Level 2 Panel, Level 3 Panel, Final Panel — customizable count), writing `round_config` rows.
Verify: create a requisition with 2 panel levels and one with 4; both save and show the right "Levels" value.

## STEP 6 — Requisition detail: the KANBAN board
The candidate kanban per DESIGN_SPEC.md. Columns reflect THIS requisition's configured levels dynamically (Applications → Screening → AI Interview → Level 1 … Final Panel → Selected → Offer → Joined). Cards are draggable; dragging advances the candidate (existing advance endpoint + stage_event log).
Verify: drag a candidate from one column to the next; status updates and persists on refresh.

## STEP 7 — Candidates, Interviews, Reports screens
Candidates: cross-requisition sortable table. Interviews: scheduled list + the existing Google Calendar connect/disconnect card. Reports: the 7 existing views as clean cards/charts, not raw JSON.
Verify: each screen loads real data.

## STEP 8 — Hiring Manager review flow
The hiring_manager's "Profiles to review" screen: shortlisted candidates awaiting their feedback, with a simple approve/comment action that feeds the panel decision. 
Verify: a hiring manager can log in, see a shortlisted profile, and submit feedback.

## Theme (applies to every screen)
Light theme, logo at `frontend/assets/enternly-logo.svg`, fire-orange `#f15a22` as a sparing accent only (active nav, primary buttons, key metrics), dark sidebar `#0c0d10`, page `#faf9f6`, success green `#1d6e56`. Sentence case, generous whitespace, flat surfaces.

## Rule for every step
Build it, then STOP and tell the user: the command to run, the URL, and what to look for. Wait for them to confirm it works before the next step. If a step needs a backend API that doesn't exist, say so before inventing one.

---

# PART 2 — Role views, reports, and NexAI (continue in order after Part 1)

## STEP 9 — Fix the role views so all four roles differ (CRITICAL BUG)
Right now TA Admin and TA Manager see the same view. They must not. Make each role land on a genuinely different home screen and see different nav, per these definitions:
- **TA Admin** — system administration: manage logins, grant custom access per user, monitor NexAI bot health/status, manage/inspect the database. NOT recruiting analytics. Nav: Users & Access, Bot Health, Database, System Logs.
- **TA Manager** — people + analytics: assign recruiters to requisitions, view and DOWNLOAD all reports. Nav: Dashboard, Requisitions, Team, Reports, Analytics.
- **Recruiter** — works requisitions, builds candidate data, runs pipeline. Nav: Dashboard, Requisitions, Candidates, Interviews, My Reports.
- **Hiring Manager** — reviews profiles, interviews, gives feedback. Nav: Profiles to Review, Interviews, My Reports.
Verify: log in as each of the four roles and confirm four clearly different screens.

## STEP 10 — Add missing report fields to the database
The management Excel needs fields Enternly does not yet store. Add to the requisition/application model: `is_p1` (boolean priority flag), `risk` (text/enum), `hiring_location`, `aging_days` (computed from open date), `aging_bracket` (0-15, 16-30, 31-45, 46-60, 61-90, 91+), and `internal_movement` (boolean). Aging and bracket are computed, not entered. 
Verify: these fields appear on a requisition and populate correctly.

## STEP 11 — TA Manager reports — match the management Excel, shown as pivots + charts
Build a Reports area for TA Manager that reproduces EXACTLY these pivots from the weekly Excel, each rendered as an interactive pivot table AND an appropriate chart (pie/bar/line/cohort), not raw numbers:
1. Net Open Positions vs Total Demand — by company, split On-Roll/Off-Roll (bar)
2. Diversity Hiring YTD — Female/Male by company (pie + bar, with % like the Excel's 5.4%/94.6%)
3. Status of Open Positions by Entity & Band (stacked bar)
4. Status of Open Positions by Hiring Stage — Sourcing/Screening/Interview/Selected (funnel or stacked bar)
5. Internal Movement against open positions (bar + rate %)
6. Aging of Open Positions — the day brackets (cohort/heatmap + bar)
7. Recruiter-Wise Productivity YTD (bar)
8. Total Joined/Offered/Selected YTD (stacked bar)
Add a time-period selector: Weekly, Monthly, Quarterly, Half-Yearly, Yearly. Add a "Download as Excel" button that exports the pivots into an .xlsx matching the existing Summary-sheet layout (so management gets the same file they get today). Use openpyxl on the backend to generate the file.
Verify: TA Manager picks "Weekly", sees the pivots and charts, clicks Download, gets an .xlsx that mirrors the current report.

## STEP 12 — Recruiter reports (their own activity)
Same charting toolkit, scoped to the logged-in recruiter: profiles processed, screened, advanced, rejected; conversion ratios (applied→screened→interviewed→selected→joined as a funnel/cohort); their requisitions' aging. Same Weekly/Monthly/Quarterly/Half-Yearly/Yearly selector and Excel download.
Verify: a recruiter sees only their own numbers, can download.

## STEP 13 — Hiring Manager reports (interviews they conducted)
Scoped to the logged-in hiring manager: interviews taken, by period; outcomes (selected/rejected ratio); average feedback turnaround; pending reviews. Same period selector, same chart types, Excel download.
Verify: a hiring manager sees only their interview activity.

## STEP 14 — NexAI: the interview bot (named NexAI), built in STAGES
The current "bot" is a stub returning a fake score. Build NexAI properly, in stages — do NOT build the photorealistic face first.
- **14a (voice-first):** NexAI conducts a structured interview using text-to-speech to ask JD-based questions and speech-to-text to capture spoken answers; it produces a transcript and an ASSISTIVE score (recruiter still decides). Consent-gated via the existing notetaker consent flow.
- **14b (face, later):** add an AI-generated talking human face via a vendor avatar service, only after 14a works and the per-interview cost is validated.
Keep NexAI assistive only — it never auto-rejects. Surface NexAI's health/status on the TA Admin "Bot Health" screen.
Verify (14a): a candidate hears a spoken question, answers by voice, and a transcript + score is saved.

## Honest scope note for NexAI
A real-time human-face, human-voice interviewer means combining speech-to-text, a conversational model, text-to-speech, and a talking-avatar service — mostly paid vendors with per-minute cost. Voice-first (14a) delivers most value cheaply; the face (14b) is the expensive, risky part. Validate 14a with real candidates before paying for avatars.