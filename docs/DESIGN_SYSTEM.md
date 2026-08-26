# Vitals — Design System

> **Scope note:** Vitals ships **one** interface shell — **Masthead**. `<body>`
> always carries `.ui-masthead` (see [`base.html`](../web/templates/base.html));
> there is no per-user toggle and no `ui_version` setting anymore. Design every
> new screen against Masthead. The older `classic` frame is gone; where this
> document still describes it, treat that as historical background only.
>
> Grounded entirely in the current implementation — every token and class below
> exists in [`web/static/vitals.css`](../web/static/vitals.css) and
> [`web/static/vitals-masthead.css`](../web/static/vitals-masthead.css) at the time
> of writing (updated 2026-08-02). If you change a token, update this file in the
> same PR.

## At a glance

- **Warm health companion, not a clinical terminal.** Dim plum-charcoal, never
  pure black, never white.
- **One accent, spent on purpose.** Amber (`--accent`) is reserved for wayfinding
  (the active nav item) and the page's single primary CTA — not for data values,
  not for decoration. Everything else stays neutral so those signals keep meaning.
- **No monospace, anywhere.** Numbers use Inter with `tabular-nums`; columns still
  align.
- **One type ladder, ten steps, no others.** `--text-eyebrow` → `--text-hero`.
  Don't reach for an arbitrary `text-[17px]`, and don't redefine a token under a
  breakpoint — move the class down a step instead.
- **A ladder, not a wall of red.** System alerts are `info` / `warn` / `block` —
  calm by default, loud only when a save must actually be stopped.

## Table of contents

1. [Principles](#1-principles)
2. [Foundations](#2-foundations)
3. [Layout & shell](#3-layout--shell)
4. [Components](#4-components)
5. [Patterns](#5-patterns)
6. [Accessibility](#6-accessibility)
7. [Governance — extending the system](#7-governance--extending-the-system)

---

## 1. Principles

These aren't aspirational — each one is a direct, enforced constraint in the CSS
today, and the reasoning is worth carrying into every new screen:

1. **Navigator, not overseer.** Vitals surfaces data and lets the owner decide;
   it doesn't nag. That's why validation is a three-step ladder (info/warn/block)
   instead of red everywhere, and why the tone throughout is "here's what's
   happening," not "you did something wrong."
2. **Warm and dim, deliberately not two other things.** Not the near-black /
   electric-accent "AI dashboard" look (tried, rejected), and not a bright/white
   clinical UI. Plum-charcoal surfaces with a single honey-amber accent.
3. **Amber is scarce on purpose.** At rest, amber appears in exactly three kinds
   of places: the *active nav indicator* (top-nav link, masthead rail icon, or
   masthead tab underline — all the same "you are here" signal), the page's
   *one primary CTA*, and small *brand/live chrome* (logo pulse, brand dot) that
   isn't data. Metric values, chips, tags, filter pills and section markers all
   stay neutral — see the repeated `.v-metric-value`, `.v-chip`, `.v-tag`,
   `.v-pill`, `.v-bar` comments in `vitals.css` that spell this out. If you're
   about to add a new amber element, ask whether it's actually one of those
   three things — if not, it should be neutral.
4. **No monospace, full stop.** A hard owner constraint. `.font-mono`/`.tnum`
   are aliased back to Inter + `tabular-nums` so number columns still line up
   without a mono typeface anywhere in the product.
5. **Closed inventories.** One type ladder, one radius set, one border set,
   two icon sizes, three breakpoints, one display face. Each is enforced by
   `tests/test_design_language.py`, because the failure mode is never one bad
   value — it is a second way of saying a thing, added because the first was
   hard to find. Adding a step should be rare enough to need a reason.
6. **Editorial over "boxes of boxes."** Masthead's header (eyebrow → tabs → big
   title → key figures) reads like a magazine section opener, not a SaaS KPI
   dashboard. Prefer that hierarchy to another grid of stat cards.

## 2. Foundations

### 2.1 Color

All colors are CSS custom properties defined once, in `:root`, in
[`vitals.css`](../web/static/vitals.css). Templates and component classes
consume them via `var(--token)` — never a hardcoded hex.

**Surfaces** (layered page → card → raised)

| Token | Value | Use |
|---|---|---|
| `--bg` | `#1D1A21` | Page background |
| `--bg-inset` | `#151318` | Recessed wells — inputs, table heads, dropdown triggers |
| `--surface` | `#332F3C` | Cards — the default "lifted" surface |
| `--surface-2` | `#3D3848` | Hover states, active segmented-control pill, raised rows |
| `--surface-3` | `#46404F` | Chips sitting on top of a card |
| `--line` | `#443E4F` | Hairline borders |
| `--line-2` | `#564E63` | Stronger borders (ghost-button outline, switch track) |

**Text**

| Token | Value | Use |
|---|---|---|
| `--fg` | `#F3F0F6` | Primary text |
| `--fg-2` | `#CFC8D8` | Secondary text |
| `--muted` | `#A39AB0` | Labels, muted values |
| `--faint` | `#8C829C` | Placeholders, faint/tertiary text |

**Accent — honey-amber**

| Token | Value | Use |
|---|---|---|
| `--accent` | `#F5A623` | The one accent. Primary CTA fill, active-state color. |
| `--accent-2` | `#FFC25A` | Hover state, active-state icon/text tint |
| `--accent-ink` | `#2A1B03` | Text/icon color **on top of** `--accent` — never put light text on amber |
| `--accent-soft` | `rgba(245,166,35,.13)` | Tinted background for active/hover chrome |
| `--accent-line` | `rgba(245,166,35,.34)` | Tinted border, focus rings |

**Semantic**

| Token | Value | Use |
|---|---|---|
| `--good` / `-soft` / `-line` | `#6FC58E` / `.13` / `.30` | Positive tone on a metric or chip |
| `--bad` / `-soft` / `-line` | `#EE7A60` / `.13` / `.30` | Negative tone; also the `block` alert color |
| `--bad-strong` | `#FF8469` | Critical / out-of-range emphasis |
| `--warn` / `-soft` / `-line` | `#F0B24A` / `.13` / `.30` | The `warn` alert tier |
| `--cool` / `-soft` / `-line` | `#6FB6C9` / `.13` / `.30` | Temporal/category tag — "day" side of a day/night pairing |
| `--violet` / `-soft` / `-line` | `#BCA4DC` / `.13` / `.30` | Temporal/category tag — "evening/night" side. The base tone was lightened from `#B093D6` so text on `--violet-soft` clears AA |

**Every tone has the same three parts** — the base, a `-soft` fill at 13%, a
`-line` border at 30%. Before that rule there were twenty border colours for
five tones (`warn` at .35, .28 and .3; several `bad` borders still mixing the
pre-AA `#E87056` the token had stopped using). A border never carries its own
`rgba()`; `tests/test_design_language.py` fails the build if one appears.

`.good`/`.bad` are **direction-agnostic** — the page decides which way is good.
On the Weight page, for instance, a negative weekly slope (losing weight) maps
to `good` and a positive one to `bad`; the token pair itself carries no
assumption about which sign is desirable.

> **Tailwind note:** `tailwind.config.js` remaps Tailwind's `slate` and `teal`
> scales onto this same palette (`slate` → plum-charcoal, `teal` → honey-amber)
> so legacy utility-class markup (`bg-slate-800`, `text-teal-500`, …) inherits
> the theme automatically. That remap is a compatibility shim for old markup —
> **new markup should reach for `.v-*` classes or `var(--token)`, not raw
> Tailwind color utilities.**

### 2.2 Typography

Two families, self-hosted as woff2 under `web/static/fonts/` and loaded via
`web/static/fonts.css` (linked from `base.html` and `oauth_authorize.html` —
no Google Fonts CDN dependency):

| Family | Weights loaded | Role |
|---|---|---|
| **Inter** | 400–800 | Body text, UI chrome, all numbers (via `tabular-nums`) |
| **Bricolage Grotesque** | 600–800 | Every display setting: page titles, card titles, tabs, key figures |

There used to be a third. Outfit sat on `.v-text-card` / `.v-text-heading` /
`.v-text-metric` — that is, on the title of every card — while Bricolage held
the page title directly above them, so one vertical carried two display faces
for no reason anyone could name. Outfit is no longer loaded and its woff2 files
are gone. The surviving stack is named once, as `--display`, and referenced
everywhere else.

**Cyrillic:** only Inter has it. Bricolage Grotesque has no Cyrillic subset
upstream at all, so **`--display` names `'Inter'` immediately after it** — Latin
and digits render in Bricolage, Russian falls to a font we actually ship instead
of an arbitrary system font that differs per device. Never write a stack that
goes straight from a display family to a generic (`sans-serif`, `system-ui`);
`tests/test_ui_static_contracts.py` enforces it.

No monospace typeface is loaded or used. `.font-mono` / `.tnum` force Inter with
`font-variant-numeric: tabular-nums` and `cv01`/`ss01` feature settings, so
number-heavy tables still align in columns.

**One ladder, ten steps, nothing between them.** Two scales used to run in
parallel — these tokens plus a `--mh-text-*` set inside `body.ui-masthead` — and
once the em-relative Tailwind utilities were counted the app was setting
**twenty** actual sizes, 19.2px (73 elements) and 13.5px (117) among them, which
nobody had chosen. The masthead scale is gone; every `.mh-*` rule names a step
of the ladder below.

| Token | Size | Use |
|---|---|---|
| `--text-eyebrow` | 11px | Uppercase micro-labels, bottom-bar captions — **the floor** |
| `--text-micro` | 12px | Units, dates, secondary info (muted) |
| `--text-label` | 13px | Labels, column headers (muted) |
| `--text-body` | 14px | Table values, body copy, buttons |
| `--text-card` | 15px | Card titles, section tabs |
| `--text-heading` | 18px | Section headings, the phone's sticky section bar |
| `--text-lead` | 21px | Secondary key figures, rail wordmark |
| `--text-title` | 26px | Page `<h1>` and the primary key figure on a phone |
| `--text-display` | 32px | The same pair on a tablet |
| `--text-hero` | 36px | Page `<h1>` on the desktop shell |

Rules for using it:

- **A breakpoint moves a class down the ladder; it never redefines a token.**
  `--text-heading` used to mean 18px on a desktop and 16px on a phone, which is
  two scales again under one name. The phone block re-points `.v-text-heading`
  at `--text-card` instead.
- **No rule writes a size.** The single literal left in either stylesheet is the
  `16px` iOS requires on a focused form control so it does not zoom the page —
  a platform constraint, not a type decision.
- **The ladder is monotonic and every step is used.** A step nothing references
  is a step to delete, not one to keep "for later".

### 2.3 Spacing & radius

4px-base spacing scale:

| Token | Value |
|---|---|
| `--space-1` … `--space-12` | 4 / 8 / 12 / 16 / 24 / 32 / 48px |

Radius scale, applied by role rather than by component:

| Token | Value | Typical use |
|---|---|---|
| `--radius-xs` | 8px | Corners inside a tight track — stepper keys, inline code |
| `--radius-sm` | 10px | Icon buttons, chips, dropdown options, filter pills |
| `--radius` | 14px | Buttons, inputs, alerts, `.v-card-inset` |
| `--radius-lg` | 20px | Cards, modals, metric tiles |
| `--radius-pill` | 999px | Switch track, section chips, meters |

Ten values were in use before this — `999px` and `9999px`, the same corner
written twice, plus 4 / 8 / 9 / 12 / 18px picked one rule at a time. **No rule
writes a radius.** The only literals a `border-radius` may carry are `0`, `50%`
and `inherit`.

**Nesting rule: an inner corner is always smaller than the corner it sits in**
(inner ≈ outer − padding). A card is `--radius-lg` at every width — no
breakpoint drops it to `--radius` — so anything nested in it can be `--radius`
and still read as inside it rather than stuck on top of it. Where the padding is
tighter than that, go smaller still: the stepper keys on `/weight` are
`--radius-xs` inside a `--radius` track 6px away. The segmented control follows
the same rule with a capsule outside and `--radius` on the pill.

**Depth rule: two frames, never four.** A card, then one thing inside it. And a
card nested in a card is **raised, not sunk** — `.v-card .v-card-tile` takes
`--surface-2`, because `.v-card-tile`'s own `--bg-inset` is darker than the page
*behind* the card and read as a hole punched through it. A well
(`.v-card-inset`: a table body, an input) stays recessed; being sunk is the
whole point of a well.

### 2.4 Elevation

Two shadow tokens only:

- `--shadow` — `0 1px 2px rgba(0,0,0,.25), 0 12px 30px -16px rgba(0,0,0,.55)` —
  default card lift. Paired with a `inset 0 1px 0 rgba(255,255,255,.05)`
  top-highlight on cards/metrics for a subtle bevel.
- `--shadow-lg` — `0 24px 60px -22px rgba(0,0,0,.7)` — modals and floating
  dropdown panels, i.e. anything above the card layer.

### 2.5 Iconography

Every icon in the product follows one contract: 24×24 viewBox, Heroicons-outline
style —

```html
<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
  <path stroke-linecap="round" stroke-linejoin="round" d="…" />
</svg>
```

Color always comes from `currentColor` (inherits text color / token), never a
hardcoded fill.

**Two rendered sizes, and no rule writes its own.** Six were in use
(15/16/17/20/22/24px) with nothing saying which belonged where:

| Token | Size | Use |
|---|---|---|
| `--ico` | 16px | Inline beside a label — rail rows, dropdown chevrons |
| `--ico-lg` | 22px | Standalone — bottom bar, /more rows, drop zones, empty states |

### 2.6 Motion

- **Micro-interactions:** 120–200ms `ease` on color/background/border/shadow
  (hover, focus, press). Buttons add a 1px `translateY` press on `:active`;
  `.v-card-tile` lifts 2px on hover.
- **Page navigation:** the View Transitions API (90ms fade-out / 210ms fade-in)
  with an htmx opacity-swap fallback (~150–200ms) for browsers without it.
- **Ambient/brand only:** the masthead logo pulse (2.6s) and classic header's
  brand-icon glow (2.5s) are the only looping animations — reserved for "this is
  alive" chrome, not for data or content.
- **`prefers-reduced-motion: reduce`** collapses all animation/transition
  durations to ~0 globally. Any new animation must respect this for free by
  using standard `transition`/`animation` properties rather than JS-driven motion.

## 3. Layout & shell

### 3.1 Masthead (canonical)

**Desktop (≥768px):** a fixed 240px rail (`--mh-rail-w`, class `.mh-rail`) on
the left: wordmark → ⌘K search row → «Сегодня» → three rubric groups → sync
status card → footer (Settings + log out). One width at every desktop size — the
old 68px icon-only step below 901px is gone, because hiding the labels removed
the one thing a rail is for.

The rail's contents, the in-content tab row, the phone's bottom bar and the
«Ещё» screen all derive from **one registry** — `MODULE_REGISTRY` /
`nav_modules()` / `bottom_slots()` / `more_rubrics()` in
[`modules_service.py`](../vitals/services/modules_service.py), exposed as Jinja
globals — so no two surfaces can drift apart. Sections are grouped into three
rubrics: Health (weight, garmin, hevy, nutrition, timeline, reports, charts),
Markers (glp1, hrt, labs, genetics), Lifestyle (supplements, skincare,
interactions); membership is gated by `enabled_modules`.

**Every section is a row, always on screen.** Collapsing the rubrics to one open
group was tried (it fits a 1440x900 laptop without scrolling) and reverted at the
owner's request: a rail exists to show where you can go, and a list that has to
be opened first does not do that. The August 2026 nav handoff bought that
vertical space by density instead — a **30px** `.mh-rail-btn` row (36px for the
pinned «Сегодня»), and the section's own glyph **only on the active row**. Every
other row carries a 5px `.mh-rail-dot` in the icon's column, so the labels stay
aligned and the eye has exactly one glyph to find. The active row keeps its icon,
an `--accent-soft` fill, an `--accent-line` border and the 3px amber `::before`
bar, and hover never restyles it. The registry-driven rows plus their three
headings fit in the intended rail; `.mh-rail-nav` still carries `overflow-y: auto`
as a safety net only. Avoid hard-coded row counts here: the registry is the source
of truth and `body_comp` is a toggle inside Weight rather than a rail row.

Above the rubrics sits one pinned row, `.mh-rail-pinned` → «Сегодня» (`/today`).
It is deliberately **not** in `MODULE_REGISTRY`: it is the entry point, not a
domain, it has no toggle, and a registry entry would give it a rubric number in
the masthead eyebrow.

**Status card.** `.mh-rail-stats` fills the space the dense list gave back:
today's numbers, one row per enabled domain — weight with the week's direction,
last night's sleep with readiness, today's intake against the ceiling, when the
last session was. From
[`nav_status_service.py`](../vitals/services/nav_status_service.py) via the
`load_nav_status` global dependency (HTML GETs only).

It reports **numbers, not plumbing**. The first version reported how fresh each
source was ("Labs · 99 days ago") — true every single day and useful on none of
them. Staleness only replaces a number once a source has actually gone quiet,
which is the one time that fact is worth the space. A domain with nothing logged
yet simply has no row, and a domain that throws loses its row rather than the
page.

There is no search row and no command palette: everything fits on screen, so a
second way to reach a section that is already one click away was chrome for its
own sake.

**Mobile (<768px):** the rail is replaced by the section's own sticky bar (see
"Section header" below). `.mh-topbar` still exists but is reduced to a bare
safe-area spacer — height `env(safe-area-inset-top)`, nothing in it — so `<main>`
starts below the status bar on every page, including the ones that render no
section header (`/today`, `/more`). It used to be a 52px strip carrying the
wordmark, which told someone already inside the app the name of the app. The
phone once showed three navigation surfaces at once — that bar's "Menu" button,
the bottom bar's own "More" button, and the single drawer both opened; the
bottom bar and the chips are what is left.

`.mh-bnav` is **56px** tall plus the home-indicator inset (it was 78px, sized
for a caption that never grew into it).

`.mh-bnav` is **always five equal columns** (`repeat(5, minmax(0, 1fr))`),
independent of how many modules are on — the grid used to be sized from the
enabled-module count, so every toggle shifted every icon and clipped the
captions. The ends are fixed («Сегодня», «Ещё»); the three middle slots come from
`bottom_slots()`: a whole rubric (tapping it opens the rubric's first section,
the `.mh-tabs` chips switch within it) or the one module that earns its own
column. **`/more`** is a real page with a real URL (system Back works, it can be linked
to, nothing floats over the content) and it lists **every** section, grouped by
rubric — not only the rubrics that got no slot. The chips reach a slot's siblings
fine, but that is a way to *switch*, not a way to *find*; listing only the
leftovers made half the app look missing on a phone. The «Ещё» cell stays lit for
everything reachable only through it (`more_routes()`), otherwise standing on
Labs would leave all five cells dark.

Below 768px `.mh-tabs` becomes a horizontally scrolling row of pills, ordered
**below** the H1 (it is source-ordered above it, where a desktop tab row
belongs). The active chip is scrolled into view with `scrollLeft` on the row
itself — never `scrollIntoView`, which also scrolls every ancestor and on iOS
drags `<main>` and the fixed shell with it.

**Rubric tabs** (`.mh-tabs`, rendered by `masthead_header`) list the sibling
sections of the page you are on, in the content column, at every width. They
repeat what the rail says, and that is the point: the rail is a place to go, this
row is where you already are.

**Section header**, rendered by the `masthead_header(section, title, metrics)`
macro at the top of every module page:

```html
{% from "partials/masthead.html" import masthead_header with context %}
{{ masthead_header('weight', t('nav.weight'), [
    {'label': t('weight.latest'), 'unit': t('common.kg'), 'primary': true, 'value': …},
    {'label': t('weight.weekly_change'), 'unit': t('common.kg'),
     'tone': 'good' if trend.slope_per_week < 0 else 'bad', 'value': …},
]) }}
```

emits **four independent blocks** — `.mh-bar` (the `<h1>` plus the optional
actions passed via `{% call %}`), `.mh-eyebrow`, `.mh-tabs`, `.mh-metrics` — and
each width arranges the same four its own way instead of inheriting the other's
arrangement.

- **From 768px** `.mh-head` is a grid and `.mh-bar` is `display: contents`, so
  the reading order is the editorial one: eyebrow + right-aligned actions →
  underline tabs → the big `<h1>` beside an inline key-figures row
  (`.mh-metrics`, divider-separated, one figure flagged `primary` in display
  type). This row **replaces** the classic KPI-card grid — don't build both.
  Below 900px the title and the figures stack instead of sharing a line.
- **Below 768px** `.mh-head` is the one that goes `display: contents`, so its
  children stick against `.v-page` — the whole scroll — rather than against a
  header they would leave behind after two swipes. `.mh-bar` becomes a 52px
  sticky bar (title at `--mh-text-bar`, truncating; the action to its right,
  always `.v-btn-ghost`), the chips pin under it at `top: 52px`, and the eyebrow
  is dropped: the bar and the active chip already say the section's name. A
  scroll-direction handler in `base.html` puts `.mh-bar-hidden` on `<body>` on
  the way down and takes it off on the way up; the chips slide to `top: 0` with
  the bar.

The action button is one thing in one place: `.v-btn-ghost` in `.mh-bar`, never
a filled `.v-btn` on some pages and an outline on others.

A metric dict may also carry `href`: that entry renders as `<a class="mh-metric">`
instead of `<div>`, for a key figure that should double as a shortcut (e.g.
Garmin's Sleep figure linking straight to the latest night's detail page).
Omit it and you get the plain non-interactive tile, same as before.

**Not every value in the strip is a figure**, so the macro sorts them and the
template author doesn't have to. A dash for data that hasn't arrived gets
`.is-empty` (quiet, `--faint`, never the headline size — it was being drawn
larger than the page title it stood under); a word like "Tirzepatide" gets
`.is-text` (text face, wraps instead of running over the next column); a long
figure-ish string like a date gets `.is-compact` (still a figure, one size down,
so it stops breaking mid-token); a negative number gets `.is-neg`, which hangs
the minus into the margin so the digits stay aligned with the column above.
On a phone the strip is `auto-fit`, not two fixed columns — three figures in a
hard 2-col grid left a hole in the bottom-right corner.

### 3.2 Classic (removed)

The old frame: a blurred-glass top `.v-header` navbar (4rem tall, active link
picking up the amber wayfinding treatment) plus a `.v-metric` KPI-card grid
where Masthead uses the inline key-figures row. It no longer ships — there is
no `ui_version` setting and no toggle. Some `.v-header` / `.v-metric` CSS
survives because Masthead reuses parts of it; don't build new screens on the
classic frame.

### 3.3 Responsive & PWA plumbing

- **Three rebuild points, and only three:** `≤767px` phone, `768–1199px`
  tablet, `≥1200px` desktop. There were eight — 480 / 560 / 640 / 767 / 768 /
  900 / 1024 / 1200 — and which of them won a property was a question of which
  had been written last, which is how `.v-card.p-6` ended up with 18px of
  padding nobody had chosen. Write `max-width: 767px`, `max-width: 1199px`,
  `min-width: 768px` or `min-width: 1200px`, and nothing else.
- Below 768px, inputs are forced to 16px font (`.v-input`/`.v-select`/`.v-textarea`)
  to stop iOS Safari's auto-zoom-on-focus; interactive elements grow to ≥44px
  touch targets (buttons, segmented control, filter pills, chips, icon buttons,
  stepper keys, date arrows, checkboxes, `summary` disclosures).
- **`html { color-scheme: dark }`** tells the browser the page is dark, once,
  instead of every native control being repainted by hand. The calendar glyph
  and the file button used to be `filter: invert(0.85)`-ed and a checkbox was
  left as a white square drawn in the browser's own blue — which is also why
  growing it to a 22px tap target made it louder rather than better.
- Safe-area insets (`env(safe-area-inset-*)`) are cached into `--sat`/`--sab`/
  `--sal`/`--sar` by a small viewport-sync script in `base.html`, because iOS
  standalone-PWA mode resolves `env()`/`dvh` unreliably on cold start. Use
  `max(env(safe-area-inset-top), var(--sat, 0px))` — don't assume `env()` alone
  is populated on first paint.
- `.v-app-shell` sizes to `var(--app-height, 100dvh)` for the same reason —
  never hardcode `100vh` for the app frame.
- The bottom nav (`.v-bottom-nav`) is an in-flow flex child, deliberately
  **not** `position: fixed` — fixed positioning was found to drift in iOS PWA
  mode when the body has `overflow: hidden`.

## 4. Components

Reference for the `.v-*` component classes in `vitals.css`. These are shared by
both interfaces — build with these before reaching for raw Tailwind utilities.

### Buttons

| Class | Look | Use |
|---|---|---|
| `.v-btn` | Solid amber, `--accent-ink` text, glow shadow | The page's **one** primary CTA |
| `.v-btn-ghost` | Transparent, `--line-2` border → amber-tinted on hover | Secondary actions, modal "Cancel" |
| `.v-btn-danger` | Solid `--bad`, dark text | Destructive confirms (e.g. override) |
| `.v-icon-btn` | 32px square, muted → accent-2 on hover; `.danger` variant → `--bad` | Row-level edit/delete/archive |

### Cards

| Class | Look | Use |
|---|---|---|
| `.v-card` | `--surface` + border + `--radius-lg` + shadow + top highlight | Default content container |
| `.v-card-flat` | Same, no shadow | Quiet/nested contexts |
| `.v-card-inset` | `--bg-inset`, `--radius` | Recessed "well" sub-panels |
| `.v-card-tile` | `--bg-inset`, lifts + amber-line border on hover | Clickable grid tiles |

**Every card is headed the same way**, by the `card_header` macro in
`partials/masthead.html` — a neutral `--line-2` bar, the title, and an optional
right-hand slot:

```jinja
{{ card_header(t("today.goal_title"), meta=goal.target) }}

{% call card_header(t("reports.digest_title")) %}
  <button class="v-btn-ghost text-xs">…</button>
{% endcall %}
```

There is no bar-less variant, no amber bar, no subtitle inside the header (a
hint goes under it as a `.v-text-micro` line) and no per-card underline. The bar
never takes a colour: a card heading is structure, and amber stays the active
nav tab and the page's one primary button.

### Metrics / key figures

Classic uses a KPI grid of `.v-metric` tiles:

```html
<div class="v-metric">
  <span class="v-metric-label">{{ t("weight.latest") }}</span>
  <div class="flex items-baseline">
    <span class="v-metric-value">{{ weights[0].weight_kg | format_number }}</span>
    <span class="v-metric-unit">{{ t("common.kg") }}</span>
  </div>
</div>
```

Masthead replaces this with the inline `.mh-metrics` row produced by
`masthead_header()` (see [3.1](#31-masthead-canonical)) — don't render both on
the same page.

### Forms

`.v-label` + `.v-input` / `.v-select` / `.v-textarea` share one look: `--bg-inset`
well, `--line` border, focus ring = `--accent-line` border + `0 0 0 3px
var(--accent-soft)`. `.v-select` gets a custom SVG chevron (native arrows can't
be restyled consistently across browsers).

For anywhere a native `<select>`'s unstylable OS popup would clash with the
dark theme, use the `.v-dropdown` trio instead: `.v-dropdown-trigger` (mirrors
`.v-select` exactly) + `.v-dropdown-panel` (a floating card, add `.drop-up` when
JS detects it would overflow the viewport below) + `.v-dropdown-option`
(`.is-selected` gets the accent tint).

Date inputs use `.v-date-wrap` / `.v-date-display` to work around iOS Safari
rendering native date text centered and unreadably — JS overlays a left-aligned
span and hides the native text below 768px.

### Segmented control

`.v-seg` (track) / `.v-seg-btn.is-active` (surface-2 pill, **not** amber) — used
for the Settings language switch, chart range pickers, and similar
mutually-exclusive choices **inside one page**.

It is **not** navigation. Route sub-tabs used to wear it, which put two
navigation rows in two different shapes one under the other — a phone read them
as two unrelated systems. They are `.mh-tabs.mh-subtabs` now: the same chips as
the section row above them, sized at `--text-label`, with a neutral raised
"active" instead of amber, and `position: static` so only the section row pins
itself to the top of the scroll. See `garmin/_tabs.html` and
`weight/_tabs.html`.

The track **wraps** (`flex-wrap: wrap`) — it never scrolls sideways. Four tabs
need 368px and the entry sidebar is 291px wide, so the old hidden-bar sideways
scroll left the last label cut mid-word with nothing saying the row continued.
Buttons carry `flex: 1 1 40%`, so a track that has to wrap breaks into even rows
of two rather than three and a lonely fourth; the phone, where all four fit on
one line, resets that to `auto`.

On a phone the track is `width: 100%` — every other control in a form card runs
edge to edge, and a fit-content switch ending short of them read as an object
dropped into the card rather than part of it. Desktop keeps `fit-content`.

The track is `--radius-lg`, **not** a capsule. A capsule is the shape of "where
you are" (`.mh-tab`), and it only reads as one while the control is a single
row — wrapped, a 999px track is a lozenge with a ragged tail.

### Chips, tags, pills, dots

| Class | Modifiers | Note |
|---|---|---|
| `.v-chip` | `.good`, `.bad`, `.v-chip-sm` | Neutral by default (surface-3) — deliberately **no** `.accent` modifier. `.v-chip-sm` is a compact-size modifier (10px/tight padding), combined with the base class — e.g. `class="v-chip v-chip-sm good"` — for a status badge sitting inline with a label. `.bad` paints its text with `--bad-strong`, not `--bad`: the plain tone on `--bad-soft` measures 3.58:1 |
| `.v-tag` | `.cool`, `.violet`, `.good`, `.bad`, `.muted` | `.cool`/`.violet` pair for day/evening-night style temporal tags |
| `.v-pill` / `.v-pill-on`, `.v-site-btn` / `.v-site-on` | — | Filter pills / body-map site picker; `--radius-sm`, never a capsule, and "selected" = neutral `--surface-2` elevation, never amber. A capsule with an amber fill is what `.mh-tab` means — /genetics stacked a row of filters straight under the section chips wearing exactly that shape |
| `.v-dot` | `.amber`, `.cool`, `.violet`, `.good` | 7px inline status dot |

### Switch

```html
<label class="v-switch">
  <input type="checkbox" role="switch" checked hx-post="/settings/modules" …>
  <span class="v-switch-track"><span class="v-switch-thumb"></span></span>
</label>
```

Checked state turns the track `--accent-soft` and the thumb `--accent` — this is
one of the few places a *filled* amber surface appears outside the primary
button, because it's directly reporting a binary state, not decorating one.

### Table

`.v-table` — sticky `--surface` header, `--line` row dividers, `--surface-2` row
hover. `.v-num` forces tabular-nums on numeric cells. `.v-col-date`/`.v-col-actions`
pin fixed-width columns; `.v-table-wrap` scrolls horizontally on mobile;
`.hide-xs` drops low-priority columns below 480px.

**`.v-table.v-rows` — the same table, stacked into rows on a phone.** A data
table that has to hold more than three columns is 1.5–1.9× wider than the 402px
phone column: it scrolled sideways inside its card with nothing on screen to say
it could, and cut what didn't fit — a lab's reference range that reads `3.6–5.…`
is worse than no range. Add `.v-rows` and, below 768px, the header row is
dropped and each `<tr>` becomes a two-column grid of label/value pairs. The
markup stays a table, so the desktop is byte-identical.

Every `<td>` in an opted-in table must say what it is — with the header row
gone, a bare value is a number with no name:

| On the cell | Result below 768px |
|---|---|
| `data-label="…"` | label left, value right, half the row |
| `.v-row-date` | full width, muted, leads the row |
| `.v-row-title` | full width, `--fg`, 600 — what the row is about; wraps instead of truncating |
| `.v-row-wide` | full width, keeps its own type — prose, a list of examples |
| `.v-row-actions` | edit/delete, pinned right of the date |
| `.hide-xs` | dropped — an empty cell, or a value that repeats down every row |

Don't opt in three narrow numeric columns (lap splits, a scan's metric list) —
they already fit, and stacking only makes them tall. Never pin `min-width` on a
`.v-rows` table: no media query can undo it. Contracts live in
`tests/test_mobile_tables.py`.

`.v-night-row` — not a `.v-table` row. A CSS-grid link-row (`<a class="v-night-row">`)
that reads like a table row but is a single anchor, for lists where every row
navigates somewhere (Garmin's sleep history). See
[5.5](#55-link-row-instead-of-a-clickable-tr).

### Modal

`.v-backdrop` (blurred scrim) + `.v-modal` (surface panel). Below 640px the
modal becomes a bottom sheet: rounded top corners only, `margin-top: auto`,
capped at 92vh with internal scroll.

### Alert ladder — `info` / `warn` / `block`

```html
<div class="v-alert info">✅ {{ t("settings.saved.ui_version") }}</div>
<div class="v-alert warn">…</div>
<div class="v-alert block">…</div>
```

This is the visual half of the conflict-engine rule in `CLAUDE.md`: `info` is a
passive badge, `warn` is a status callout that never blocks, `block` is a
pre-save validation failure. See [5.1](#51-the-alertoverride-ladder) for the
full flow.

### Toast

`.v-toast-container` (fixed bottom-right, safe-area aware) / `.v-toast.is-visible`
(fade + translate in). Repositions above the bottom nav on mobile so it never
overlaps tap targets.

### Ribbon — `.v-ribbon`

A row that scrolls sideways has to say so. The chip rows get away without help
because a chip clipped by the screen edge *is* the signal; a strip of square
thumbnails ends flush with the card and reads as "that is all of them". Add
`.v-ribbon` beside `overflow-x-auto` and the last 28px fade out. Masked rather
than overlaid on purpose — an overlay would need to know the container's
background, and these sit on more than one surface.

### Empty state & file drop

`.v-empty-state` — centered icon + one line of muted copy, used wherever a
list/table has no rows yet. The icon is drawn at 0.65 opacity / 1.8px stroke: any
fainter and it reads as an image that failed to load rather than as "nothing here
yet".

`.v-card.is-empty` — the *card* variant. A card whose only content is one grey
sentence costs ~100px of chrome to say nothing; adding `is-empty` when the
collection is empty lays the header and that sentence out on one line and cuts
the padding to match. `/today` sets it on all four of its collection cards. Use
it rather than hiding the card: the section still has to be visible, it just
doesn't need a hundred pixels.

`.v-file-drop` (+ `__text`, `__hint`) — dashed `--bg-inset` well that turns
`--accent-soft`/`--accent-line` on hover. This exact pattern recurs everywhere
the app ingests a file: labs uploads, genetics VCF import, Garmin export,
weight body-scan photos, settings data import. Reuse it rather than styling a
one-off dropzone.

### Loading / progress

`.v-progress-bar` — a thin amber gradient sweep at the very top of the viewport
during htmx navigation (NProgress-style). `.v-loading-overlay` — full-screen
blurred scrim + spinner for a blocking operation.

## 5. Patterns

### 5.1 The alert/override ladder

Straight from the conflict-engine rule in `CLAUDE.md`, with its UI half:

0. `note` renders inline as `.v-alert.note` — `--bg-inset` on `--line`, text in
   `--fg-2`, no semantic colour at all. It is an **interpretation**, never a
   failure: the app read the numbers and has something to say (recovery is low,
   the dose has plateaued). It never blocks and never demands dismissal. Split
   out of `warn` because painting a reading of the data in the same amber as
   "Garmin needs MFA" taught the owner to skip both.
1. `info` / `warn` render inline as `.v-alert` — never interrupt a save.
2. `block` + no override → the service raises `ConflictBlocked` → the router
   responds `409` with the violation payload.
3. The frontend shows [`partials/conflict_modal.html`](../web/templates/partials/conflict_modal.html):
   a `.v-modal` listing each violation (left-bordered in `--bad`, domain-pair +
   evidence line in `--faint`), ending in `.v-btn-danger` "Override" next to
   `.v-btn-ghost` "Cancel."
4. Confirming re-submits with `override: true`; the row's `override_at` is
   stamped `now`.

Reuse this exact shape for any new blocking validation — don't invent a second
confirm-dialog pattern.

### 5.1a One progress language — `.v-meter`

"Share of a target" is drawn exactly one way: a 10px `--radius-pill` track
(`.v-meter`, `--bg-inset`) with a `.v-meter-fill` sized by an inline `width: N%`
and toned `.is-good` / `.is-warn` / `.is-bad`. Calories and protein on
`/nutrition`, the cycle position on `/hrt`. It replaced an SVG donut, a
bespoke bar and a bare percentage that all meant the same thing.

A bar rather than a ring: it reads more precisely and survives a narrow column.
The name avoids `.v-progress-bar`, which is the HTMX page loader. `.mh-macro-bar`
is **not** this component — a composition summing to 100% is not progress
towards anything, so it keeps its own segmented treatment.

### 5.1b One decimal mark — `vitals.i18n.decimal()`

`.` in English, `,` in Russian, and one function decides. This is not a
preference: an `<input type="number">` is painted by the browser in the *user's*
locale, so on a Russian phone Chrome writes the value `86.1` into the field as
"86,1" and no attribute on the element changes that. On `/weight` the same
weight therefore read "86,1" in the field and "86.1" in the table under it.
Only the readouts can move, so they follow the platform.

Three code paths used to print a number to this dashboard, each rounding it its
own way — the `format_number` Jinja filter, `today_service._num`/`_signed`, and
a bare f-string in `nav_status_service`. All three call `decimal()` now. When
you add a fourth, call it too; `tests/test_design_language.py` pins the outputs.

Out of scope on purpose: chat nudges, chart annotations and anything the model
writes. Those are prose, not readouts.

### 5.2 Upload-first ingestion

Every place the app accepts an external document (lab PDF, genetics VCF,
Garmin export, InBody/MedAss photo, settings JSON import) uses the same
`.v-file-drop` well with the same two-line copy shape (bold action + muted
hint that updates to the picked filename). New import flows should match this
rather than a bespoke `<input type="file">`.

### 5.3 Markdown content (AI digests)

LLM-generated reports render through `.v-text-body`, which layers typographic
rules for `h2`–`h4`, `blockquote` (amber left-rule), inline `code` (still
tabular Inter, never mono), and tables — on top of the plain body-text class.
Wrap long-form generated content in `.v-digest` to cap line length at 52rem for
readability, independent of the card's own width.

### 5.4 One navigation registry, three consumers

The rail, the phone's bottom bar, the «Ещё» screen and the "section N" numbering
all read `MODULE_REGISTRY` / `nav_modules()` from
`vitals/services/modules_service.py` (see [3.1](#31-masthead-canonical)). When
adding a module, register it there once — resist the urge to add a section to
just one surface. The single exception is «Сегодня», written out by hand in the
rail and the bottom bar precisely because it is not a module.

### 5.4a The landing screen — `/today`

`web/templates/today/index.html` is the one page with no `masthead_header()`: it
opens the app, so it carries its own compact hero: a stable localized page name
in `<h1>`, the complete daily narrative as secondary body text, then five key
figures (`.v-today-figure`). Generated health interpretation must not become a
heading, be truncated, or push the figures below the first mobile viewport.
Everything it renders is composed in `vitals/services/today_service.py` from
services the domain pages already use; no analytics live there. Two rules the
screen depends on:

* **The narrative never blocks on the LLM.** Today's `daily_brief` row is used
  when it exists, otherwise a deterministic sentence is assembled from the same
  context — a page must never wait on a model.
* **A block whose module is off is not assembled at all.** An instance running
  "weight + Garmin only" gets a shorter screen, not five empty cards, and a
  quick-log chip pointing at a disabled section is never rendered.

The right column holds the page's one `.v-btn` and its one amber accent: the
weight quick-log, which posts to the conflict-aware `/weight/log` and therefore
includes `partials/conflict_modal.html`. In the day's feed the dot colour marks
provenance — integration `--cool`, manual `--good`, the proactive layer
`--accent`, a signal from the bot `--violet`. That accent is the only place on
the page where a value carries the brand colour, and only because it marks the
app's own message rather than a measurement.

### 5.4b Capped lists — `.v-scroll-cap`

A list with `max-h-[…] overflow-y-auto` gives the desktop a tidy card and the
phone a trap: the finger lands inside the list, the list moves and the page
stands still. Every capped list therefore also carries `.v-scroll-cap`, which
drops the cap below 768px and lets the page be the only thing that scrolls. Keep
the utility class beside it — it still sets the desktop height. The one exception
is a modal's own viewport cap (the photo lightbox on `/weight/measures`), where
the cap *is* the layout.

### 5.4c Charts on a phone are a different chart

A 332×220 canvas is not a small desktop chart. `app.js` and `charts.js` each read
one `phone` flag (`matchMedia('(max-width: 767px)')`) at build time and branch on
it: four X ticks instead of eight (eight `dd-mm-yyyy` labels run together into one
string of digits), a narrower legend, and — on the weight chart — the two
lean-mass series `hidden` and only the current dose phase labelled. Hidden, not
dropped: Chart.js leaves a hidden series out of the axis range, so the Y axis fits
the weight instead of stretching to 70–150 for data living in 100–140, and the
legend entry still turns it back on.

### 5.4d The day a screen is about

`garmin_service.latest_daily` returns the newest day that actually carries
numbers, not merely the newest row: the sync writes a row as soon as the date
turns, and at half past midnight every metric on it is still null. Read as "the
latest day" that placeholder turned `/garmin` and `/today` into a screen of
dashes with yesterday's complete row sitting one place behind it. When the day
shown isn't today, the screen says so (`garmin.showing_day` on the day strip;
`/today` already names the date in its sync line). A screen that silently shows
yesterday's numbers is worse than one that shows none.

### 5.5 Link-row instead of a clickable `<tr>`

`<tr>` cannot be wrapped in `<a>` — it's invalid HTML — and distributing a
click handler across every `<td>` instead has the same UX problems anyway:
dead space between cells that doesn't respond to a click, no native "open in
new tab," inconsistent hover. Where every row in a list navigates somewhere
(Garmin's sleep history, `garmin/sleep_list.html`), skip `<table>` entirely
and render each row as one `<a>` styled with CSS grid (`.v-night-row`) instead
of table markup: the whole row is the target, `:hover` is honest, and
keyboard/middle-click/"open in new tab" work for free. Reach for this pattern
any time you're tempted to make a table row clickable.

Where the row is *not* a link — it carries edit and delete buttons, or it is
simply data — keep the `<table>` and add `.v-rows` instead (see
[4 · Table](#table)). Rewriting a history table into `<div>`s buys nothing the
phone rules don't already give, and costs the desktop layout.

### 5.6 Care conversations stay one shared screen

The patient-side «Care team» destination is permanent navigation, not a link
that exists only while a consent task is pending. It is reachable from the
desktop rail, the phone's More screen, and Settings before the first invitation
and after the last relationship ends. Messages are a separate immediate task;
access, invitations, pause, and end controls belong in the team hub.

The same patient hub leads with the current working result: active care plans
and a short recent-note window. Show at most five active plans there, with a
clear path to the wider published record when more exist, so long guidance does
not bury the relationship safety controls. Draft plans remain professional
working material and archived plans remain patient-visible history. A patient
should not have to open the professional record route accidentally to discover
what their specialist asked them to do.

Patient and professional read the same conversation template. The subject and
authorization basis stay visible above it, active participants are named in the
thread, and opening a thread replaces the inbox/new-thread form rather than
stacking three unrelated tasks down the page. Unread state is a small neutral
chip and a PHI-free count in navigation; never put sender, topic, or body into
shared chrome.

The normal care-team path is one stable conversation per relationship: the
patient chooses a named doctor or trainer on their team card, and the service
reuses that exact two-person room. Do not describe this pair chat as a message
to an unspecified «team». Historical topic threads remain readable, while any
future group conversation must be introduced as a separate explicit feature.

Consent may offer only domains present in
`vitals.services.care.record_projection.SECTIONS` and enabled for that patient.
The form, its recommended preset, the stored summary, and the professional
projection all derive from this one registry; never promise a section the care
screen cannot render or a module the patient switched off.

Attachments are optional supporting material, not a second composer. Keep the
standard `.v-file-drop` inside a native disclosure below the message field so
the ordinary text exchange stays visually primary. Render an uploaded file as
one explicit download action inside its message; no inline medical preview, no
storage path, and no attachment surface outside the conversation whose current
access check protects it.

## 6. Accessibility

- **Focus rings are consistent everywhere:** `border-color: var(--accent-line)`
  + `box-shadow: 0 0 0 3px var(--accent-soft)` on inputs, dropdown triggers, and
  switches. Reuse this pair for any new focusable custom control.
- **Never put light text on the accent.** Amber (`--accent`) is a light,
  saturated color — text/icons on top of it use `--accent-ink` (`#2A1B03`), not
  white or `--fg`.
- **`prefers-reduced-motion: reduce`** is honored globally; don't ship an
  animation that bypasses standard `transition`/`animation` timing to dodge it.
- **Touch targets ≥44px** on every interactive element below 768px. What a
  control is *drawn* as is not what a thumb has to *hit*: a rubric chip was 33px,
  a stepper key 34, a date arrow 28, a `summary` triangle 17 and a checkbox 13.
  Give the element `min-height: 2.75rem` (and `min-width` where it is square)
  rather than scaling up its glyph.
- **Nothing is set below 11px.** `--text-eyebrow` (11px) is the floor and is for
  uppercase micro-labels only — bottom-bar captions, key-figure captions, rail
  group headings. Everything else starts at `--text-micro` (12px).
- **`touch-action: manipulation`** on tap targets (`.v-btn`, `.v-btn-ghost`,
  `.v-icon-btn`, `.v-pill`, `.v-seg-btn`, `.v-bnav-link`) — they are single-purpose
  controls, not double-tap-to-zoom candidates, and saying so up front removes the
  browser's ~300ms click delay. Add any new tap-target class to that list.
- **`[x-cloak]`** hides Alpine-bound markup until it's initialized — apply it
  to anything that would otherwise flash unstyled/uninitialized on load.
- **i18n is not optional:** all copy goes through `t("key")` (see
  `vitals/i18n.py`); Russian and English stay in parity. Don't hardcode a
  user-facing string in a template.

## 7. Governance — extending the system

- **Tokens live in exactly one place:** the `:root` block in `vitals.css`.
  A template should never contain a raw hex value — reference `var(--token)`
  (inline `style="color: var(--fg)"` is fine; `style="color: #F3F0F6"` is not)
  or, better, an existing `.v-*` class.
- **New semantic color?** Follow the existing pattern: a base tone plus a
  `-soft` background tint (and a `-line` border tint if it needs one), the same
  shape as `--good`/`--bad`/`--warn`/`--cool`/`--violet`. Don't add a one-off
  color outside that family.
- **Don't add a new raw Tailwind color utility.** The `slate`/`teal` remap in
  `tailwind.config.js` exists only to keep legacy markup on-theme; new markup
  should use `.v-*` classes or tokens instead of e.g. `bg-teal-600`.
- **Rebuild `web/static/tailwind.css` after touching templates or
  `tailwind.config.js`** — it's a committed artifact, not generated at runtime
  (see root `CLAUDE.md`). Run `npm run build:css` from `web/` (script defined
  in `web/package.json`), then diff the class list against the previous build
  and click through a few unrelated pages — a rescan drops classes whose
  markup disappeared, not just adds new ones.
- **Extending Masthead navigation** means editing the one registry described in
  [5.4](#54-one-navigation-registry-three-consumers) — never hand-roll a
  parallel tab list or rail-icon set for a single page.
- **Before adding a new component class**, check the inventory in
  [Section 4](#4-components) first — most needs (a status dot, a filter pill, a
  neutral tag) already have a class; a near-duplicate with a different name is
  a bug waiting to cause visual drift.
- **The inventories are closed sets, and a test says so.**
  `tests/test_design_language.py` reads both stylesheets and fails if a rule
  writes its own font size, corner radius, border colour or icon size, or if a
  fourth breakpoint appears. When one of them fails, the fix is to name the
  value you meant — not to widen the test. Adding a step to the ladder is a
  deliberate act: it means every screen now has one more size to be inconsistent
  with.
- **One meaning, one shape.** Amber and a capsule mean "this is where you are"
  (`.mh-tab`). A filter narrows what is on the page and is therefore a
  rectangle with a neutral raised state (`.v-pill`). A segmented control
  (`.v-seg`) switches state *inside* a page and never links between routes —
  route sub-tabs are `.mh-tabs.mh-subtabs`, the same chips as the section row
  above them, one step quieter, and only the section row pins itself.
