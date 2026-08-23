# Enternly — Design specification for the frontend rebuild

This document describes the target design for the Enternly interface. The current `frontend/index.html` is a working prototype (a single scrolling page); this spec turns it into a proper multi-screen ATS workspace. Build to this spec. All the backend APIs already exist and work — this is a frontend/UI rebuild plus a login layer, not a backend change.

## Brand and theme

Light theme, clean and professional. The logo is at `frontend/assets/enternly-logo.svg`. Use the logo on the login screen and the top bar.

Colour system: a mostly neutral canvas with fire-orange as a sparing accent. Do not flood the interface with orange — use it only for the active navigation item, primary action buttons, key metrics, and pipeline highlights. Everything else is neutral.
- Fire-orange accent: `#f15a22`
- Dark sidebar / brand bar: `#0c0d10`
- Page background: `#faf9f6`
- Card background: `#ffffff`
- Success / "joined" / positive: green `#1d6e56`
- Borders: light grey, 0.5px
- Text: near-black primary, muted grey secondary

Use a clean sans-serif (system UI stack is fine). Sentence case everywhere. Generous whitespace. Flat surfaces — no heavy shadows or gradients.

## Layout shell (every screen after login shares this)

A persistent dark left sidebar (about 200px) carrying the Enternly flame icon and name at top, then nav items: Dashboard, Requisitions, Candidates, Interviews, Reports, Settings. The active item has a fire-orange left border and lightened background. Below the nav, the TA Admin role additionally sees a "Team" item.

A top bar across the working area showing the current screen name and fiscal year on the left, and on the right: the recruiter's Google Calendar connection status (green "Calendar linked" when connected) and a round avatar with the user's initials.

The working area to the right of the sidebar uses the light page background.

## Login screen

One login page for everyone (recruiters and TA Admin/Manager use the same page). A centered card on a neutral background. Dark header band inside the card with the Enternly logo and the "One click hire" tagline. Below: a "Sign in" heading, a work-email field, a password field, and a full-width fire-orange "Sign in" button. A small line of helper text: "Your role is detected automatically after sign in." On successful login, the backend returns the user's role; recruiters land on the recruiter dashboard, TA Admin lands on the admin dashboard.

## Roles and login behaviour

Add a simple login/auth layer. Two roles exist in the `app_user` table already: `recruiter` and `ta_manager` (treat ta_manager as the TA Admin). Same login page for both. After login, route by role. Recruiters see only their own requisitions and candidates; TA Admin sees everything plus a Team view and recruiter-load panel. Keep auth simple and standard (hashed passwords, a session or token) — this is an internal tool, not a public app.

## Dashboard (landing screen)

The pipeline shown as a row of count cards in flow order. Use these exact stage names:
Open Requisitions, Applications Received, Under Screening, Screening Cleared, AI Interview, Panel Interview (a single combined count on the dashboard — the per-level breakdown lives in the requisition detail), Selected, Offer Stage, Joined.

"Joined" displays in green (success). Give "Average time to hire" visual emphasis with a fire-orange left border, since the 3–4 day target is the headline goal. Below the stage cards: a "My requisitions" list (recruiter) or "All requisitions" (TA Admin), and a "Diversity (gender)" snapshot as a simple two-colour bar (female/male, the only axis tracked). The TA Admin dashboard adds a "Recruiter load" panel showing each recruiter's open reqs and candidate count (data already available from the `v_recruiter_load` view).

## Requisitions list screen

A primary "New requisition" button (fire-orange) top right. Status filter pills: Open, On hold, Closed with counts. A table with columns: Role, Business unit, Band, In pipeline (candidate count), and Levels. The "Levels" column shows the customizable panel structure for that role, e.g. "3 + final" meaning three panel levels plus a final round. Clicking a row opens the requisition detail.

## New requisition form

When creating a requisition, the recruiter sets, among the standard fields (title, business unit, band, roll type, key skills, experience, budget, openings), the number of panel interview levels for this role. Let them name/number the levels — e.g. Level 1 Panel, Level 2 Panel, Level 3 Panel, Final Panel — and choose how many. This writes `round_config` rows per requisition (the backend already supports per-requisition rounds). So each requisition can have a different number of levels.

## Requisition detail screen — KANBAN board

This is the heart of the candidate experience. Show the requisition's candidates as a kanban board: columns in pipeline order, with the panel columns reflecting THIS requisition's configured levels (Applications → Screening → AI Interview → Level 1 Panel → Level 2 Panel → … → Final Panel → Selected → Offer → Joined). Each candidate is a draggable card showing name, combined score, and a small status hint. Dragging a card to the next column advances the candidate (calls the existing advance endpoint and logs the stage event). The columns are dynamic — a requisition with two panel levels shows two panel columns, not four. Make it attractive: clean cards, clear column headers with counts, fire-orange accent on the card being acted on.

## Candidates screen

A cross-requisition view of all candidates the recruiter is working, as a sortable, filterable ranked table (name, requisition, combined score, current stage, gender). This complements the per-requisition kanban — it's the "everyone at once" list.

## Interviews screen

Shows scheduled interviews and the Google Calendar connection card (already built — per-recruiter OAuth: connect / linked-with-email / disconnect). Scheduling an interview here uses the acting recruiter's linked calendar; block if not linked.

## Reports screen

The existing report views (TAT, recruiter load, gender split, positions by FY, budget vs offered, BU summary, on/off-roll) presented as clean cards or simple charts rather than raw JSON. Tabs or a sidebar sub-nav to switch between them.

## Build order

Do the login/auth layer and the shared shell first, then the dashboard, then requisitions list and the new-requisition form, then the kanban requisition detail, then candidates, interviews, and reports. Test each screen in the browser before moving on. Keep all the existing backend APIs working — wire the new screens to them.
