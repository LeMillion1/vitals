# Vitals UI/UX rework plan

Date: 2026-09-05. Status: proposed product structure, authorized for incremental
implementation after the current functional QA and release acceptance checks.

## Decision and evidence

Keep the Masthead visual system. Reorganize information and interactions before
considering a new visual identity. The primary problem observed during the live
QA is that entering a fact, interpreting history, configuring the application,
and granting sensitive access compete for attention.

This is an expert assessment based on real synthetic member, doctor, and trainer
journeys, desktop and phone checks, and inspection of the current implementation.
It is not a usability study with representative users. Expected benefits below
are hypotheses to validate, not measured improvements in conversion or speed.
Functional evidence and external prerequisites remain in
[the QA audit](QA_AUDIT_2026-09-05.md).

### What already works and should remain

- One recognizable Masthead shell and one shared navigation registry.
- A calm visual language, neutral data, and one primary action per page.
- Today as the member's short daily entry point.
- Optional modules and role-specific workspaces rather than one universal menu.
- Private-by-default records, explicit consent, and current access checks.
- Source history that survives exclusion from an analysis or withdrawal of access.

## Experience principles

1. One screen should answer one primary user question.
2. Daily logging should take one short form, not a wizard.
3. Rare, consequential operations should expose their consequences before commit.
4. A setting, a navigation link, a filter, and an action must look distinct.
5. Saving should confirm what changed and keep the person at the relevant task.
6. Empty, unconfigured, disabled, pending, refused, and failed are different states.
7. Hide unavailable controls by role/module, but explain an actionable prerequisite.
8. Never simplify the interface by weakening ownership, consent, or provenance.

## Priority 1: Settings becomes an index and focused pages

### Observed problem

The current Settings page mixes language, personal profile, care links, support,
provider credentials, connector tokens, protected export/restore, sign-in and
two-factor authentication. Other preferences are included from shared partials.
An ordinary preference competes with security and data-replacement operations.
Long scrolling also makes the outcome of a save harder to locate.

### Proposed structure

The following paths are proposals, not a list of already implemented routes.
Reuse existing destinations where they already provide the correct boundary.

| Area | Main question | Contents |
| --- | --- | --- |
| Settings index | What can I configure? | Short linked rows with safe status summaries; no secret values or long forms |
| Profile and preferences | How should my record work? | Language, saved timezone, units where supported, profile inputs |
| Modules | What do I want to track? | Core versus optional modules, clear on/off states, brief explanations |
| Integrations | Where does my data come from? | Provider-specific connection state and configuration; separate connection detail pages when needed |
| Brief and notifications | When should Vitals prepare or deliver something? | Personal Brief schedule; separate channel availability and delivery options |
| Security and connected access | Who or what can sign in or connect? | Identity-provider account link, applicable 2FA, MCP and API access, revocation |
| Data and portability | How do I take my record with me? | Protected download, restore entry point, clear scope and consequences |

Care-team management should remain its own workspace, reachable from Settings
without being embedded as another long settings form. Support and platform
operations retain their existing independent role/authorization boundaries.

Separate personal settings from installation-wide controls. A member should not
be led to believe their Garmin polling choice changes a shared worker cadence.
Do not show unavailable email or push delivery as a working option.

### First implementation slice

- Introduce the settings index and focused GET pages within the existing shell.
- Extract reusable sections; avoid duplicated form markup and a parallel nav.
- Initially preserve existing POST endpoints and domain behavior.
- Return successful saves and validation errors to the correct focused page.
- Preserve old deep links, recent-auth returns, provider callbacks, and 2FA steps.
- Preserve module gates and capacity-one pool behavior while loading page context.
- Do not put secrets, health values, passphrases, or tokens in redirect URLs.

## Priority 2: Weight, measurements, and goals

### Observed problem

Daily entry, several measurement modes, multiple histories, and analytical
exclusions have different frequencies and meanings. Goals currently live in
Reports even though a weight goal is most naturally understood beside weight.
Live QA also showed why exclusions must explicitly promise that source entries
remain: excluding one day changed the mean while the two observations survived.

### Proposed structure

- **Weight overview:** current observation, understandable trend, active goal,
  and one obvious Log weight action.
- **History:** dated observations with source labels and permitted row actions.
- **Body measurements:** the relevant measurement form and its own history.
- **Analysis settings:** excluded periods, explanation of their effect, and
  a clear distinction between excluding a period and deleting a source entry.
- **Goal detail/edit:** reachable from the weight overview; target, status,
  optional deadline, and honest current progress.

Keep these as local routes inside Weight, not new top-level rail entries. Use
existing route sub-tabs for navigation and segmented controls only for state
inside a page. Separate pages only when they serve a genuinely separate task;
do not create a route for every individual input.

Daily weight entry remains one short form with sensible date defaults. Optional
fields can expand inline. Show the saved result and a direct path to its history.
Do not fabricate a goal percentage when the baseline is absent or unsuitable.

## Priority 3: Reports, summaries, and sharing

### Observed problem

A goal is a future intention, a Brief is an account of observations, and a public
share is a separately protected snapshot. Their placement should reflect those
different purposes. A newly registered member can otherwise interpret an empty
feed or unavailable AI as a failure to save their record.

### Proposed structure

- Keep Reports focused on saved summaries, with type and period filters.
- Give a report a stable detail page suitable for reading on a phone.
- Show deterministic versus AI-assisted output honestly without technical clutter.
- Link to domain goals from the relevant domain; preserve existing goal URLs
  or compatibility navigation during the transition.
- Keep Share as an explicit action with its own snapshot lifecycle and access UI.
- Clearly identify current-day partial coverage and the snapshot creation time.

For sharing, a short sequence can be appropriate: choose period and data scope,
review exactly what will be disclosed, then create the protected snapshot and
show its link/password controls. Reuse the existing authorization protocol and
period validation. Revocation must remain easy to find after creation.

## Priority 4: Care-team journey and professional workspace

### Observed problem

Registration, professional review, accepting an invitation, forming a
relationship, and receiving scoped consent are separate gates. Users need to
understand which gate is pending. Live QA exercised cases where a relationship
existed but messaging was correctly unavailable, and where a stale conversation
had to lose its mutation controls immediately after access withdrawal.

### Owner journey

1. Choose the intended professional/recipient through the supported identity flow.
2. Specify available data and actions in plain language.
3. Review recipient, scope, duration where supported, and consequences.
4. Confirm through the existing service/consent boundaries.
5. Arrive at a relationship detail page with its current state and next action.

The UI may guide these steps, but must not imply that sending an invitation
already grants access. Where the current backend requires relationship
establishment before consent, preserve that order and explain the pending step.

Relationship detail should distinguish pause, withdrawal of consent, and ending
the relationship. Explain what happens to access and retained history before
each confirmation. Keep authored notes, plans, and historical conversations
consistent with the existing retention rules.

### Professional journey

- Make draft, submitted, changes requested, verified, and suspended states legible.
- Present one appropriate next action for the current state.
- Keep a recordless professional in the care workspace, not an empty personal diary.
- Show patient links and conversation actions only when current scopes allow them.
- Use patient-context navigation so the professional knows whose record is open.
- Separate profile approval from proof of mailbox ownership; neither substitutes
  for patient consent. SMTP-free operation is a separate security/product decision.

## Interaction policy: page, steps, modal, or inline

| Pattern | Use it for | Avoid it for |
| --- | --- | --- |
| Dedicated page | Reports, detailed history, integrations, access management | One-field changes that do not need a new destination |
| Multi-step flow | Restore, complex sharing, care access, provider setup with dependencies | Daily weight, food, or short note entry |
| Modal or phone sheet | Short contextual edit, rename, specific destructive confirmation | Long settings, medical reports, stacked dialogs |
| Inline expansion | Optional fields, short explanation, advanced filters | Hiding the only route to an important feature |

Every multi-step flow needs Back, preserved non-secret draft input, visible step
meaning, a final review when consequential, and an explicit completed state.
Do not introduce a second confirmation protocol over the conflict engine.

Modals need a programmatic label, initial focus, focus containment, Escape and
cancel behavior, focus restoration, and a visible close affordance. Avoid nested
modals. On a phone, a longer edit belongs on a page rather than inside a sheet
with several independent scroll regions. Warn before abandoning material edits.

Protected restore especially needs an explicit distinction between selecting a
file, validating it, reviewing supported consequences, and actually applying it.
Do not invent a preview capability or replacement option the backend lacks.
Recent authentication and passphrase handling must survive any UI restructuring.

## Copy and state cleanup

- Prefer user-facing terms such as "Morning summary" over unexplained internal
  terms such as "proactive layer", subject to a consistent RU/EN terminology pass.
- State the saved timezone next to time-sensitive settings.
- Explain missing prerequisites with an actionable next step, not an empty button.
- Keep errors beside the relevant input and include a concise form-level summary
  where several inputs fail. Corrected input must clear stale validation state.
- Distinguish "no entries", "module off", "not connected", "not yet prepared",
  "access unavailable", and actual request failure.
- Confirm destructive scope precisely: one record, one snapshot, one access grant.
- Do not present missing clinical evaluation as a normal result.
- Add English and Russian keys together and use shared number/date formatting.

## Delivery order and acceptance gates

| Stage | Deliverable | Gate |
| --- | --- | --- |
| 0 | Current functional fixes deployed and rechecked | Fast suite, focused PostgreSQL, browser acceptance; external limitations recorded honestly |
| 1 | Settings index and focused settings pages | All former tasks still reachable; save/error/re-auth returns correct; no role/module leakage |
| 2 | Weight/measurements/history separation and contextual goals | One-form daily log; correct trend/history/source preservation; phone flow verified |
| 3 | Reports and protected-sharing structure | Snapshot/period/revocation semantics unchanged; reading and back navigation work |
| 4 | Guided care-team and professional states | Exact scopes and lifecycle preserved; stale-tab refusal and retained history retested |
| 5 | Terminology and interaction consistency pass | RU/EN parity, keyboard flow, mobile layouts, no duplicate navigation patterns |

Implement small releasable slices, not a simultaneous rewrite. Record findings
that change this plan before expanding scope. Unconfigured external services are
not proof of a pass; keep their test limitations separate from completed local
and browser acceptance so they do not silently disappear during the redesign.

For every layout slice:

- Read and follow [the design system](DESIGN_SYSTEM.md); reuse `.v-*` components.
- Rebuild committed Tailwind and inspect the generated diff.
- Run i18n, design-language, static-contract, router-page, and relevant mobile tests.
- Run focused security tests for moved auth, upload, token, or public surfaces.
- Run the full fast suite, Ruff when available, and `git diff --check`.
- Visually exercise a desktop width and a 390-pixel phone width; verify 44-pixel
  touch targets, visible focus, reduced motion, keyboard access, and no clipping.
- Check reload, browser Back, deep links, validation failures, and session expiry.
- Use only synthetic records; do not send messages or call vendor APIs merely
  to validate a layout change.

## Open questions to validate, not blockers for the first slice

- Which three actions do members actually repeat most often?
- Do users look for goals under a health domain, Today, or a dedicated goal index?
- Which professional onboarding explanations need fewer words or an example?
- Are notification expectations about an in-app summary, a push, or an email?
- What SMTP-free identity workflow is acceptable without treating an unverified
  address as proof of ownership?

No numerical usability improvement is claimed yet. After the structural slices,
compare task completion, wrong turns, validation recovery, and comprehension of
access consequences using the same scripted tasks and representative users.
