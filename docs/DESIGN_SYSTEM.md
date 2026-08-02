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
> of writing (updated 2026-07-25). If you change a token, update this file in the
> same PR.

## At a glance

- **Warm health companion, not a clinical terminal.** Dim plum-charcoal, never
  pure black, never white.
- **One accent, spent on purpose.** Amber (`--accent`) is reserved for wayfinding
  (the active nav item) and the page's single primary CTA — not for data values,
  not for decoration. Everything else stays neutral so those signals keep meaning.
- **No monospace, anywhere.** Numbers use Inter with `tabular-nums`; columns still
  align.
- **Six type sizes. No others.** `--text-title` → `--text-micro`; don't reach for
  an arbitrary `text-[17px]`.
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
5. **A closed type scale.** Six sizes cover every heading, label and value in
   the app. Adding a seventh should be rare enough to need a reason.
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
| `--good` / `--good-soft` | `#6FC58E` / `rgba(111,197,142,.13)` | Positive tone on a metric or chip |
| `--bad` / `--bad-soft` | `#EE7A60` / `rgba(238,122,96,.13)` | Negative tone; also the `block` alert color |
| `--bad-strong` | `#FF8469` | Critical / out-of-range emphasis |
| `--warn` / `--warn-soft` | `#F0B24A` / `rgba(240,178,74,.13)` | The `warn` alert tier |
| `--cool` / `--cool-soft` | `#6FB6C9` / `rgba(111,182,201,.13)` | Temporal/category tag — "day" side of a day/night pairing |
| `--violet` / `--violet-soft` | `#BCA4DC` / `rgba(176,147,214,.13)` | Temporal/category tag — "evening/night" side. The base tone was lightened from `#B093D6` so text on `--violet-soft` clears AA |

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

Three families, self-hosted as woff2 under `web/static/fonts/` and loaded via
`web/static/fonts.css` (linked from `base.html` and `oauth_authorize.html` —
no Google Fonts CDN dependency):

| Family | Weights loaded | Role |
|---|---|---|
| **Inter** | 400–800 | Body text, UI chrome, all numbers (via `tabular-nums`) |
| **Outfit** | 400–900 | Headings, card titles, classic KPI metric values — the "display sans" |
| **Bricolage Grotesque** | 600–800 | Masthead-only: big editorial titles and tab labels (`--mh-display`) |

**Cyrillic:** only Inter has it. Outfit and Bricolage Grotesque have no Cyrillic
subset upstream at all, so **every display stack names `'Inter'` right after the
display family** — Latin and digits render in Outfit/Bricolage, Russian falls to
a font we actually ship instead of an arbitrary system font that differs per
device. Never write a stack that goes straight from a display family to a
generic (`sans-serif`, `system-ui`); `tests/test_review_run3.py` enforces it.

No monospace typeface is loaded or used. `.font-mono` / `.tnum` force Inter with
`font-variant-numeric: tabular-nums` and `cv01`/`ss01` feature settings, so
number-heavy tables still align in columns.

**Core scale** (six sizes, defined as tokens, shrink slightly ≤640px):

| Token | Desktop | ≤640px | Use |
|---|---|---|---|
| `--text-title` | 26px | 22px | Page hero `<h1>` |
| `--text-metric` | 28px | 24px | Big numbers in stat cards |
| `--text-heading` | 18px | 16px | Section headings |
| `--text-card` | 15px | 14px | Card titles |
| `--text-body` | 14px | 14px | Table values, body copy |
| `--text-label` | 13px | 13px | Labels, column headers (muted) |
| `--text-micro` | 12px | 12px | Units, dates, secondary info (muted) |

**Masthead scale** — the editorial layer needs sizes the core six don't cover,
so it has its own token set declared once in `body.ui-masthead`
(`web/static/vitals-masthead.css`). No `.mh-*` rule may write a raw pixel size;
sizes that already exist on the core scale (12/13/14px) reference the core token.

| Token | Size | Use |
|---|---|---|
| `--mh-text-hero` | 40px | `.mh-title` base |
| `--mh-text-hero-lg` | 36px | `.mh-title` on desktop |
| `--mh-text-hero-md` | 32px | `.mh-title` + hero figure on tablet |
| `--mh-text-hero-sm` | 30px | `.mh-title` on a narrow phone |
| `--mh-text-metric` | 36px | `.mh-metric-value.is-primary` |
| `--mh-text-metric-md` | 34px | the same on the desktop shell |
| `--mh-text-ring` / `-md` | 32px / 26px | value inside a progress ring |
| `--mh-text-wordmark` / `-sm` | 26px / 22px | rail wordmark, expanded / collapsed "V" |
| `--mh-text-lead` | 21px | `.mh-metric-value` (secondary figures) |
| `--mh-text-brand` | 19px | mobile topbar wordmark |
| `--mh-text-tab` | 15px | `.mh-tab` |
| `--mh-text-eyebrow` | 11px | uppercase eyebrows and key-figure captions |
| `--mh-text-nano` | 10px | smallest uppercase micro-label |

### 2.3 Spacing & radius

4px-base spacing scale:

| Token | Value |
|---|---|
| `--space-1` … `--space-12` | 4 / 8 / 12 / 16 / 24 / 32 / 48px |

Radius scale, applied by role rather than by component:

| Token | Value | Typical use |
|---|---|---|
| `--radius-sm` | 10px | Icon buttons, chips, dropdown options |
| `--radius` | 14px | Buttons, inputs, alerts, `.v-card-inset` |
| `--radius-lg` | 20px | Cards, modals, metric tiles |
| `--radius-pill` | 999px | Switch track, filter-pill shapes |

**Nesting rule: an inner corner is always smaller than the corner it sits in**
(inner ≈ outer − padding). A card is `--radius-lg` at every width — no
breakpoint drops it to `--radius` — so anything nested in it can be `--radius`
and still read as inside it rather than stuck on top of it. Where the padding is
tighter than that, go smaller still: the stepper buttons on `/weight` are 8px
inside a `--radius` track 6px away. The segmented control follows the same rule
with a capsule outside and `--radius` on the pill.

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
hardcoded fill. Rendered size varies by context: 18px in the masthead rail,
~15px inline next to nav labels, ~20–32px in headers and empty states.

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
interactions, signals); membership is gated by `enabled_modules`.

**Every section is a row, always on screen.** Collapsing the rubrics to one open
group was tried (it fits a 1440x900 laptop without scrolling) and reverted at the
owner's request: a rail exists to show where you can go, and a list that has to
be opened first does not do that. The August 2026 nav handoff bought that
vertical space by density instead — a **30px** `.mh-rail-btn` row (36px for the
pinned «Сегодня»), and the section's own glyph **only on the active row**. Every
other row carries a 5px `.mh-rail-dot` in the icon's column, so the labels stay
aligned and the eye has exactly one glyph to find. The active row keeps its icon,
an `--accent-soft` fill, an `--accent-line` border and the 3px amber `::before`
bar, and hover never restyles it. All sixteen sections plus three headings come
to ~660px of a ~860px rail; `.mh-rail-nav` still carries `overflow-y: auto` as a
safety net only.

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

- Breakpoints actually in use: **480 / 640 / 768px.**
- Below 768px, inputs are forced to 16px font (`.v-input`/`.v-select`/`.v-textarea`)
  to stop iOS Safari's auto-zoom-on-focus; interactive elements grow to ≥44px
  touch targets (buttons, segmented control, filter pills, icon buttons).
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
mutually-exclusive choices. Also doubles as real navigation: `<a class="v-seg-btn">`
for sub-tabs that are separate routes (e.g. Garmin's Overview/Sleep/Activities).
The `a.v-seg-btn { display: block; text-decoration: none; }` pair in `vitals.css`
is what makes the class selector — written for `<button>` — behave the same on
an anchor.

### Chips, tags, pills, dots

| Class | Modifiers | Note |
|---|---|---|
| `.v-chip` | `.good`, `.bad`, `.v-chip-sm` | Neutral by default (surface-3) — deliberately **no** `.accent` modifier. `.v-chip-sm` is a compact-size modifier (10px/tight padding), combined with the base class — e.g. `class="v-chip v-chip-sm good"` — for a status badge sitting inline with a label. `.bad` paints its text with `--bad-strong`, not `--bad`: the plain tone on `--bad-soft` measures 3.58:1 |
| `.v-tag` | `.cool`, `.violet`, `.good`, `.bad`, `.muted` | `.cool`/`.violet` pair for day/evening-night style temporal tags |
| `.v-pill` / `.v-pill-on`, `.v-site-btn` / `.v-site-on` | — | Filter pills / body-map site picker; "selected" = neutral `--surface-2` elevation, not amber |
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

### Empty state & file drop

`.v-empty-state` — centered, low-opacity icon + one line of muted copy, used
wherever a list/table has no rows yet.

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
opens the app, so it carries its own hero — an `<h1>` that is a **sentence**
about the day, then five key figures (`.v-today-figure`). Everything it renders
is composed in `vitals/services/today_service.py` from services the domain pages
already use; no analytics live there. Two rules the screen depends on:

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

## 6. Accessibility

- **Focus rings are consistent everywhere:** `border-color: var(--accent-line)`
  + `box-shadow: 0 0 0 3px var(--accent-soft)` on inputs, dropdown triggers, and
  switches. Reuse this pair for any new focusable custom control.
- **Never put light text on the accent.** Amber (`--accent`) is a light,
  saturated color — text/icons on top of it use `--accent-ink` (`#2A1B03`), not
  white or `--fg`.
- **`prefers-reduced-motion: reduce`** is honored globally; don't ship an
  animation that bypasses standard `transition`/`animation` timing to dodge it.
- **Touch targets ≥44px** on every interactive element below 640/768px
  (buttons, segmented control, pills, icon buttons, inputs).
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
