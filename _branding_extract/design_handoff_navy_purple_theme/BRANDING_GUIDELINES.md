# Enternly Brand & UI Guidelines — Deep Navy & Royal Blue

> Version 2.3 · Based on the EnternsTech Brand Guidelines
> Scope: visual theme + typography for the Enternly recruitment platform.
> This is a **re-skin**: layout, structure, components, data, and flows stay the same.
> Only colors, gradients, accents, and fonts change. **The Enternly logo does not change
> without an explicit new asset drop into `frontend/assets/`.**

---

## 1. Brand Foundations

**EnternsTech philosophy:** professional, modern, human, intelligent — confident and
structured, technology-led and current, clear and approachable, purposeful and
evidence-led. The visual language favors hierarchy and whitespace over decoration.

**Design principles:**
- **Hierarchy first** — use scale, whitespace and contrast before decorative effects.
- **One colour, one job** — don't use Signal Cyan, Sky Blue and Royal Blue interchangeably.
- **Premium restraint** — gradients and glows are for campaign moments, not default decoration.
- **Readable by default** — check contrast in both light and dark modes.

> Discipline over decoration. When in doubt, remove an effect before adding one — the
> brand reads as corporate precisely because it restrains itself.

---

## 2. Color Palette

### 2.1 Core brand colors (source: EnternsTech Brand Guidelines v2.3)
| Name | Hex | Role |
|---|---|---|
| **Deep Navy** | `#0A1F44` | Primary. Trust + corporate foundation — hero sections, navigation, dark backgrounds |
| **Royal Blue** | `#2563EB` | Action colour — buttons, links, active states |
| **Signal Cyan** | `#22D3EE` | Signature technology accent — AI, highlights, micro accents (use sparingly) |
| **Sky Blue** | `#5FB4FF` | Supporting accent — data, graphics, secondary visuals |
| **Navy Tint** | `#11305C` | Elevated dark surface — cards, chips, dark UI surfaces |

### 2.2 Neutral & text system
| Token | Hex | Purpose |
|---|---|---|
| White | `#FFFFFF` | Primary light canvas |
| Surface | `#F8FCFE` | Soft light sections and cards |
| Tag Wash | `#EAF0FF` | Pills and badges |
| Border | `#D7E0EC` | Dividers and card boundaries |
| Ink | `#0A1F44` | Primary light text |
| Slate | `#33445F` | Secondary text |
| Muted | `#596A83` | Supporting copy |
| Placeholder | `#9AA7BD` | Form placeholders only |

### 2.3 Dark theme text
| Role | Value | Use |
|---|---|---|
| Primary White | `#FFFFFF` | Headings and primary content |
| Navigation | `#C3D0E6` | Navigation and UI links |
| Supporting | `#AFC0CD` | Secondary body copy |

### 2.4 Semantic / status colors (unchanged from the app's existing system — not brand
identity colors, kept for continuity)
| State | Text | Background |
|---|---|---|
| Success | `#0e9f6e` | `#e8f7f0` |
| Info / In progress | `#2563EB` | `#e7ecff` |
| Warning / Pending | `#b07d00` | `#fff2d6` |
| Danger | `#e2455a` | — |

### 2.5 Signature gradients
| Name | Value | Where |
|---|---|---|
| Deep technology | `linear-gradient(100deg, #0A1F44 0%, #14316B 52%, #2563EB 130%)` | Dark hero transitions, launches |
| Primary energy | `linear-gradient(90deg, #2563EB, #22D3EE)` | Hero accents, campaign graphics, CTA emphasis |
| Data bar | `linear-gradient(90deg, #0A1F44, #2563EB)` | Funnel bars, horizontal data |
| Avatar / chip | `linear-gradient(150deg, #0A1F44, #2563EB)` | Avatars, accent chips |
| Soft light | `linear-gradient(#5FB4FF, #FFFFFF)` | Light editorial sections |

### 2.6 Usage rules
- **Deep Navy is the anchor.** Sidebar, headings, and any dark surface.
- **Royal Blue is the action colour** — buttons, links, active states.
- **Signal Cyan is a signature accent, not a fill** — AI features, highlights, micro
  accents. Avoid large flat cyan areas.
- **Sky Blue is a supporting accent only** — a second data/graphics color next to blue.
- Never introduce colors outside this palette.
- Approved light-mode ratio: ~80% neutral surfaces, ~15% blue/navy structure, ~5% accent energy.

---

## 3. Typography

### 3.1 Production typefaces
| Role | Typeface | Weights | Use |
|---|---|---|---|
| **Display / headings** | **Space Grotesk** | 500 · 600 · 700 | Hero, H1–H3, large statistics |
| **Body / UI** | **IBM Plex Sans** | 400 · 500 · 600 · 700 | Paragraphs, navigation, forms, buttons |
| **Labels / data** | **IBM Plex Mono** | 400 · 500 | Metadata, technical IDs, eyebrow labels |

These are free, self-hostable Google Fonts — no licensing step needed, unlike the
previous Amnex-era brand fonts (Aktiv Grotesk / Proxima Nova).

### 3.2 Type scale
| Element | Size | Weight | Notes |
|---|---|---|---|
| Page title | 17px | 700 | Deep Navy, Space Grotesk |
| Section heading | 15px | 700 | Deep Navy, Space Grotesk |
| KPI number | 30px | 800 | Deep Navy, Space Grotesk |
| Stat number | 26px | 800 | Semantic color, Space Grotesk |
| Body / table | 12.5px | 400–600 | IBM Plex Sans |
| Micro-label / metadata | 9.5–10px | 700 | UPPERCASE, IBM Plex Mono, letter-spacing 0.6–1.5px |

---

## 4. Layout, Spacing, Radii

- **Grid:** 12-column, 1200px max container width.
- **Base spacing unit:** 8px. UI gaps 8/12/16px. Section gaps 48/64/96px.
- **Radii:** 8/12/16px (cards, tiles, buttons follow this scale).
- **Borders:** 1px, `#D7E0EC`.
- **Shadows:** soft and restrained; never heavy black shadows.
- If a page feels crowded, remove decoration before reducing type size.

---

## 5. Logo

- Current logo asset: `frontend/assets/Enternly_logo.png`.
- Do not stretch, rotate, distort, add effects to, or recolor the logo.
- Use the approved logo with sufficient clear space on both light and dark backgrounds.
- Logo should be secondary to the message in social/campaign creative — never compete
  with the headline.

---

## 6. Component Patterns

**Sidebar** — Deep Navy `#0A1F44`, full-height sticky, 200px. Nav grouped under
uppercase section labels. Idle item text `rgba(255,255,255,.72)`; active item = white
text + `rgba(37,99,235,.22)` pill + 3px `#2563EB` left bar. Footer: Change password,
Sign out, "POWERED BY ENTERNSTECH".

**Top bar** — White, sticky, navy page title, green Download Excel button, user name +
gradient avatar.

**Hero / progress banner** — Deep technology gradient (§2.5) with soft cyan/blue radial
glow, white text, translucent progress track, outline button.

**Cards / panels** — White or Surface `#F8FCFE`, 8–16px radius, `#D7E0EC` border, navy
section heading.

**KPI cards** — White, 3px top accent border alternating Royal Blue/Sky Blue, 30px navy
number, uppercase label.

**Status pills** — pill radius, semantic bg/fg pairs from §2.4, 10px 700 uppercase.

**Tables** — 12.5px, uppercase faint column headers, `#F8FCFE` row dividers, blue links.

**Charts** — series A = Royal Blue `#2563EB`, series B = Sky Blue `#5FB4FF`; funnel bars
use the Deep Navy → Royal Blue gradient.

**Buttons**
- Primary solid: Royal Blue, white text, 8–10px radius, 700 weight.
- Secondary/outline: `#D7E0EC` border on white; hover → `#EAF0FF` bg + `#2563EB` border.
- Links: `#2563EB`, hover Deep Navy.

**Accessibility rule:** do not rely on color alone for status, validation, or
navigation — pair color with text, iconography, or shape.

---

## 7. Implementation

### 7.1 CSS custom properties (drop into `:root`)
```css
:root {
  /* Core brand */
  --navy: #0A1F44;
  --accent: #2563EB;   /* Royal Blue — action colour */
  --signal: #22D3EE;   /* Signal Cyan — signature accent */
  --sky: #5FB4FF;       /* Sky Blue — supporting accent */
  --navy-tint: #11305C;
  --white: #ffffff;

  /* Surfaces */
  --app-bg: #F8FCFE;
  --surface: #ffffff;
  --tag-wash: #EAF0FF;
  --border: #D7E0EC;

  /* Text */
  --text-heading: #0A1F44;
  --text-body: #0A1F44;
  --text-muted: #596A83;
  --text-faint: #9AA7BD;
  --text-slate: #33445F;

  /* Status (unchanged, semantic) */
  --success: #0e9f6e;  --success-bg: #e8f7f0;
  --info: #2563EB;     --info-bg: #e7ecff;
  --warning: #b07d00;  --warning-bg: #fff2d6;
  --danger: #e2455a;

  /* Gradients */
  --grad-hero: linear-gradient(100deg,#0A1F44 0%,#14316B 52%,#2563EB 130%);
  --grad-progress: linear-gradient(90deg,#2563EB,#22D3EE);
  --grad-bar: linear-gradient(90deg,#0A1F44,#2563EB);
  --grad-avatar: linear-gradient(150deg,#0A1F44,#2563EB);

  /* Type */
  --font-head: 'Space Grotesk',system-ui,sans-serif;
  --font-body: 'IBM Plex Sans',system-ui,sans-serif;
  --font-mono: 'IBM Plex Mono',monospace;

  /* Radii */
  --r-card: 16px; --r-tile: 12px; --r-btn: 10px; --r-pill: 20px;
}
```

### 7.2 Migration map (Amnex navy/purple era → current EnternsTech palette)
| Element | Old (Amnex navy/purple) | New (EnternsTech) |
|---|---|---|
| Sidebar | Navy `#001451` bg, purple `#a100ff` active pill | Deep Navy `#0A1F44` bg, Royal Blue `#2563EB` active pill |
| Hero/progress banner | Navy→purple gradient | Deep Navy → Royal Blue gradient |
| Primary buttons/links | Purple `#a100ff` | Royal Blue `#2563EB` |
| Chart series A | Purple | Royal Blue |
| Chart series B | Intelligent Blue `#0031ff` | Sky Blue `#5FB4FF` |
| Body font | Mulish | IBM Plex Sans |
| Heading font | Hanken Grotesk | Space Grotesk |
| Labels/metadata font | (none — used body font) | IBM Plex Mono (new tier) |

### 7.3 Steps for the developer / Claude Code
1. Add the tokens in §7.1 to the app's theme layer (CSS variables — matches the app's
   existing approach).
2. Load fonts from Google Fonts: Space Grotesk, IBM Plex Sans, IBM Plex Mono.
3. Replace hard-coded colors using the migration map (§7.2). Do not touch layout,
   spacing, or component structure beyond what the theme requires.
4. Re-skin components per §6; preserve all existing behavior, routing, and data.
5. Verify contrast: white text on Deep Navy and on Royal Blue both pass AA for UI text.

---

## 8. Do & Don't

**Do**
- Lead with Deep Navy; act with Royal Blue.
- Keep Signal Cyan for highlights, AI features, and micro accents only.
- Keep surfaces white/neutral; let color come from brand tokens.
- Check contrast in both light and dark modes.

**Don't**
- Don't invent colors outside the palette.
- Don't fill large areas with flat cyan.
- Don't use cyan, sky blue, and royal blue interchangeably — each has one job.
- Don't recolor or redraw the logo.
- Don't change component layout, spacing, or flows — this is a re-skin only.
