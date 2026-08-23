# Handoff: Enternly — Deep Navy & Royal Blue Rebrand

## Overview
This is a **theme + typography rebrand** of the Enternly recruitment/ATS web app,
built by EnternsTech. It replaces the old Amnex-era navy/purple theme with the
current EnternsTech brand palette led by **Deep Navy** and **Royal Blue**, and swaps
the fonts to the brand typefaces (Space Grotesk / IBM Plex Sans / IBM Plex Mono).

This is a re-skin, not a redesign: layout, structure, components, data, and flows
stay the same. Only colors, gradients, accents, and fonts change. The logo changes
only when a new asset file is explicitly dropped into `frontend/assets/`.

## About the Design Files
`Enternly Dashboard.dc.html` in this bundle is a **design reference created in HTML**
— a prototype of the Team Dashboard showing the theme applied. It is **not
production code to copy directly**; it's a visual reference. The live app
(`frontend/*.html`) has already had this same token migration applied directly.

`BRANDING_GUIDELINES.md` in this folder is the working reference — the source of
truth for colors, type, spacing, and component patterns, derived from the official
EnternsTech Brand Guidelines v2.3.

> Note: the original `Brand Guide.pdf` that shipped with this folder was Amnex's
> proprietary brand book from the previous rebrand cycle. It has been removed since
> it no longer applies — `BRANDING_GUIDELINES.md` now carries the current EnternsTech
> palette and typography directly.

## Fidelity
**High-fidelity.** Colors, gradients, and type in `BRANDING_GUIDELINES.md` are final
for this cycle.

---

## Design Tokens — see `BRANDING_GUIDELINES.md` §2–4 for the full palette, typography,
and spacing system. Summary:

- **Deep Navy** `#0A1F44` — primary, dark surfaces
- **Royal Blue** `#2563EB` — action colour, buttons, links, active states
- **Signal Cyan** `#22D3EE` — signature accent, used sparingly
- **Sky Blue** `#5FB4FF` — supporting accent, data/graphics
- **Navy Tint** `#11305C` — elevated dark surface (cards, chips)
- **Typography:** Space Grotesk (headings) · IBM Plex Sans (body/UI) · IBM Plex Mono (labels/data)

## Screens / Views

### Team Dashboard (only screen in this mock)
**Layout:** fixed 200px navy sidebar (sticky, full height, scrollable) + fluid main
column. Main = sticky white top bar, then a padded vertical stack.

**Components (top → bottom):**
1. **Sidebar** — logo at top; grouped nav under uppercase section labels (PIPELINE,
   SOURCING, ANALYTICS, ADMIN); active item = blue pill + blue left bar + white text;
   footer with Change password, Sign out, and "POWERED BY ENTERNSTECH".
2. **Top bar** — "Team Dashboard" (navy), green Download Excel button, user name +
   gradient avatar.
3. **Hero banner** — BRONZE / 0 pts / XP block, Progress bar, My Stats button;
   Deep Navy → Royal Blue gradient with a soft cyan radial glow top-right.
4. **KPI row** — equal cards, big navy number + uppercase label, alternating
   blue/sky-blue top border.
5. **NexAI Interview Pipeline** — stat tiles + "Recent Invites" table (Candidate,
   Requisition, Status pill, Score).
6. **Recruiter Performance** (table) + **Recruiter Comparison** (grouped bar chart,
   Royal Blue = Open Reqs, Sky Blue = Applications).
7. **Hiring Manager Overview** (empty state) + **All Requisitions** (table with OPEN
   badges, blue pipeline count).
8. **Team Pipeline Funnel** (horizontal navy→blue bars) + **Team Conversion Cohort**
   (blue area/line chart).
9. **Organisation-wide Diversity** (conic-gradient donut) + **Quick Actions** (3
   full-width outline buttons, blue hover).

## Interactions & Behavior
Re-skin only — preserve all existing behavior (nav routing, table sorting/paging,
Download Excel, chart tooltips, hover states). Quick-action buttons get
`background:#EAF0FF; border-color:#2563EB` on hover; nav items lighten toward white.
No new animations required.

## State Management
No change. Keep the app's existing state, data fetching, and routing intact.

## Assets
- **Enternly logo:** `frontend/assets/Enternly_logo.png` — the current production
  asset. Do not stretch, recolor, or redraw it.
- **Brand fonts:** Space Grotesk, IBM Plex Sans, IBM Plex Mono — all free/self-hostable
  via Google Fonts, already wired into the live app's `<head>` font imports.

## Files
- `Enternly Dashboard.dc.html` — themed dashboard reference (open in a browser to view).
- `BRANDING_GUIDELINES.md` — EnternsTech brand + UI guidelines (color/type/spacing source of truth).
