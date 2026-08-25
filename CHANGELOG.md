# Changelog

All notable changes to Vitals are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [Unreleased]

### Added — account invitations have a safe OIDC recipient handoff

An issued `invite_only` bearer can now travel in a URL fragment, be removed
before any other script runs, and be exchanged by a strict same-origin request
for a ten-minute signed cookie containing only an opaque invitation UUID. The
standalone landing page uses a nonce-bound CSP, no referrer, no cache, and stops
without sending the bearer if browser history cannot be scrubbed. A successful
exchange also ends every previous local session and pending authentication
handle on the device before forcing a fresh provider login with `prompt=login`
and bounded `max_age`.

The callback revalidates the same signed browser claim, current registration
mode, pending/revoked/consumed state, database-clock expiry, and the provider's
exact verified email under the identity-governance lock. It creates one member,
doctor, or trainer account atomically, never falls through to open registration,
and rolls back partial graphs on a uniform refusal. Existing linked and bootstrap
identities sign in normally without spending somebody else's invitation. This
ships the recipient boundary; supported operator issue/revoke/delivery UI and
scheduled retention remain separate unfinished work.

### Added — registration admission decisions are transactional

The invitation and administrator-approval modes now have dedicated domain
services without making either mode reachable from the browser yet. A shared
identity-governance lock serializes mode changes, token consumption, request
decisions, and account linking; row locks make every terminal transition
single-winner. Expiry checks use the database statement clock after lock waits,
so a transaction begun before a deadline cannot act after it. Invitations
return the bearer once and persist only its digest,
bind the provider's strictly verified email to a server-selected member,
doctor, or trainer account, and preserve the inviter as role provenance.
Approval requests accept only a member account shape, never merge by email, and
retain an accountable decision without treating the stored email snapshot as a
fresh login proof. Bounded expiry and retention services scrub terminal tokens,
applicant PII, free text, and user references while retaining opaque outcomes.
The code is grouped under `authentication/admission/` and leaves transaction
commit ownership to OIDC, operator, or scheduled-job boundaries.

### Added — registration admission has a fail-closed schema

Revision `0072` adds expiring, purge-ready invitation and operator-approval records
without opening either registration mode. Invitations store only a lowercase
SHA-256 token digest, bind one non-privileged account kind to a normalized
address, and enforce one live invitation for that address. Approval requests
key each live request to the exact provider issuer/subject pair while retaining
terminal attempts as separate history, and require an accountable, internally
consistent outcome. Both lifecycles expire, constrain every state and timestamp
in the database, and support terminal purging of applicant identity, contact,
free-text, and user links while retaining only the opaque outcome.

### Fixed — public account admission is validated and auditable

Account provisioning now applies the shared Unicode/email normalization rules
and a bounded control-character-free record display name before any identity row
is written. Invalid or colliding OIDC naming claims produce the same uniform
refusal as any unknown identity and roll back the graph instead of escaping as
an internal error. Stored mode decisions and successful open-mode admissions
write operational audit events containing only the selected mode and opaque
resource identifiers—never provider claims, email addresses, or display names.

### Fixed — closing registration now fences in-flight admission

Changing the stored registration mode and admitting an unknown OIDC identity
now take the same identity-governance lock before reading policy or creating an
account. A closure either waits for an already-authorized admission to commit or
wins first and makes the waiter re-read `disabled`; it can no longer leave a
post-closure account behind. Concurrent callbacks for the same immutable
provider identity also converge on one account, link, and health record instead
of making the second callback fail on the first callback's username.

### Fixed — federated sign-in failures keep a working retry

OIDC failures now render a dedicated anonymous screen with one safe retry back
through the identity provider. The screen never offers the removed password
form, preserves only a validated local destination, clears browser history state
left by a previous session, and carries matching English and Russian copy.

### Changed — registration now lives with authentication

The deployment-gated registration decision and the single account/health-record
provisioning boundary now live under `vitals.services.authentication` beside
federated identity resolution. Their old flat service modules were removed,
all callers moved together without compatibility shims, and the guarded flat
service debt dropped from 77 modules to 75 without changing admission behavior.

### Added — operators can complete professional review in the browser

Platform superadmins now have a dedicated responsive professional-review queue
with fixed verify, reject, suspend, and reinstate actions. Every change requires
recent authentication, locks against stale decisions and push claims, rechecks
the target account and exact role, and writes both an append-only operational
audit event and immutable review history. Credential references and review
reasons remain confined to the operator surface; no decision grants patient
access without a separate relationship and consent.

### Added — professionals have one clear onboarding home

Doctor- and trainer-only accounts now land on `/care` instead of bouncing
through a personal dashboard they do not own. The same responsive page keeps a
persistent “People in your care / Подопечные” destination at zero patients and
guides the professional through one role-derived profile form, independent
review, correction after rejection, verified roster, and suspension state.
Professional kind cannot be selected or escalated by form input, a rejected
claim keeps one identity and kind when resubmitted, and a suspended claim cannot
self-clear. Browser notification setup and the roster stay secondary until the
profile is verified.

### Fixed — care invitations require the exact professional role

A patient invitation no longer acts as an accidental role-granting surface.
Establishing doctor care requires the invited account to already hold the
doctor role, and trainer care requires the trainer role. The exact role is
rechecked while assembling every relationship grant, so revoking it closes the
next web, API, MCP, or background access without deleting the historical care
relationship; holding the other professional role is not a substitute.

### Fixed — OIDC verified email now unlocks only its exact care invitation

Vitals now projects an email onto the local account only when the validated ID
token carries the literal JSON boolean `email_verified: true`; truthy strings
and other malformed values do not count. A later login without that proof
revokes the local verification timestamp. Email remains display and invitation
matching data, never an identity lookup key: a collision with another account
refuses and rolls back the whole login instead of merging identities or leaving
a partial bootstrap link. This closes the production gap where an IdP-verified
doctor or trainer could sign in but could never accept an address-bound care
invitation.

### Added — care wakeups now become private, generic browser notifications

The root-scoped service worker accepts only the exact versioned care-message
wakeup and ignores malformed, unknown, or extended payloads. Notification copy
comes from the shared RU/EN catalog; the worker never accepts a title, message,
name, filename, identifier, or URL from the provider payload. A stable tag
coalesces wakeups, and a click uses the fixed `/messages` destination rather
than a subject or thread deep link. The device locale is persisted only after
the current account proves ownership of that browser subscription, preventing
a second account in a shared browser from changing the first account's
notification language. Permission remains an explicit per-device click.

### Added — care wakeups are dispatched at most once after fresh consent checks

Revision `0070` and the shared scheduler complete the server-side care-push
path. A bounded platform job locks pending rows with `SKIP LOCKED`, then checks
the active account, exact conversation participant and relationship,
professional role, current versioned read consent, and exact encrypted device
generation in one transaction. The claim commits before network I/O. Accepted,
gone, rejected, ambiguous, protocol, and transport outcomes are terminal and
never return to pending; a crashed or cancelled attempt becomes stale and
ambiguous instead of being sent twice. A provider `404/410` erases credentials
only if the browser has not re-enrolled since the attempt began. The payload is
still the fixed PHI-free care wakeup; the service worker renders it without
accepting presentation content from the sender.

### Added — Web Push transport has one PHI-free operation

Vitals now pins and verifies the async `pywebpush` protocol boundary for care
message wakeups. The integration accepts no caller-supplied title, body, name,
filename, URL, or identifier: its only payload is the versioned generic
`{"kind":"care_message","v":1}` envelope, encrypted for the browser before it
leaves the process. Provider response bodies and dependency exception strings
are discarded; trustworthy provider responses normalize to accepted, gone,
rejected, or ambiguous plus the HTTP status, while a missing trustworthy
response becomes a sanitized transport exception. The client never retries,
while disabled configuration still exposes no enrollment or call path. The
consent-rechecking care dispatcher is now its only call site.

### Added — care messages create a subject-isolated push outbox

Revision `0069` adds one PHI-free delivery claim for each currently enrolled
device of each active conversation participant except the author. The claims
are created in the message transaction, so a rollback leaves neither message
nor wakeup behind; removed participants, suspended accounts, revoked devices,
and devices enrolled later receive no historical claim. Composite foreign keys,
a unique message/recipient/device triple, and FORCE RLS prevent cross-subject,
cross-account, and duplicate rows. The outbox deliberately stores no rendered
notification, message text, name, filename, endpoint, or provider response.
Server-side dispatch rechecks every claim, while the service worker owns its
fixed presentation copy, so the outbox still carries no presentation content.

### Added — care notifications are an explicit per-device choice

Patients, doctors, and trainers can now enable or disable browser notifications
from their conversation inbox or patient roster. Vitals asks browser permission
only after an explicit click, exposes only the installation's public VAPID key,
and never lists device endpoints or account/patient identifiers. The control
detects unsupported and denied browsers, shared-browser ownership conflicts,
device limits, and VAPID key rotation; an existing subscription remains
removable even after browser permission is denied. Server-side delivery and the
worker now share only the fixed generic wakeup.

### Fixed — the PWA worker controls the application, not only static files

The service worker is now served and registered at `/sw.js` with root scope,
explicit revalidation, and `Service-Worker-Allowed: /`. Existing registrations
of `/static/sw.js` are removed during the next page load. Previously the worker
was confined to `/static/`, so its offline navigation handler never controlled
ordinary pages and a future notification click could not reliably return to the
app.

### Added — browser notification endpoints belong to accounts and devices

Revision `0068` adds the storage boundary for web push subscriptions. A browser
endpoint belongs to the signed-in account rather than to a patient record, so a
doctor's one device can later receive work across several authorized patients
without copying a delivery credential into each record. Endpoint URLs and
encryption keys are authenticated-encrypted under `VITALS_CREDENTIAL_KEY`; only
an opaque SHA-256 lookup digest remains readable. Registration accepts only
valid P-256 key material and reviewed public push-service hosts, which prevents
the future sender from becoming an authenticated SSRF relay. Active devices are
bounded to ten per account; revocation or account suspension erases ciphertext
instead of retaining a dormant delivery credential. Delivery and the permission
UI remain separate follow-up commits.

### Added — care messages accept private attachments

A patient, doctor, or trainer may attach one validated PDF or image to a care
message. Bytes are stored in an owner-only volume outside `/static`; database
constraints bind the file to the same patient and message, and every download
rechecks current conversation participation, live care, and consent. Download
URLs expose neither the original storage path nor the patient's identity, files
are served as attachments with `private, no-store`, and the backup sidecar now
archives the private-file volume.

### Added — unread conversations are visible from patient navigation

The desktop rail and phone More destination now show a PHI-free unread count,
and the phone More page has a direct Conversation row. No title, sender, or
message preview is exposed in shared chrome.

### Added — accepted care invitations become a patient task

Once a professional accepts an invitation, the patient sees a persistent,
one-click prompt to choose what to share. The prompt is derived from the live
relationship and disappears as soon as the first consent decision is saved.

### Added — one professional inbox across every patient

The patient roster is now a work queue: records with unread conversations come
first, link straight to their conversation list, and show the most recent
message date. Records without new work remain one click from the health record.

### Fixed — expired care consent no longer looks open

The professional roster now labels an expired consent explicitly and removes
the record link at the same instant that authorization closes the record.

### Added — conversations have real per-person unread state

Each patient and professional now has an independent read cursor in every care
conversation. A message from somebody else is shown as new until that person
opens the thread; sending a reply also acknowledges the messages it answers.
Existing history is marked read during the migration so an upgrade does not
manufacture a backlog.

### Added — professionals choose one patient when connecting an assistant

The connector approval screen now shows the health record being authorized. A
doctor or trainer with several patients must choose exactly one; the choice is
sealed into the single-use OAuth code and revalidated against the live care
relationship and consent at token exchange. The permission explanation is now
three short guarantees instead of a long, owner-specific feature list.

### Added — MCP credentials have a durable patient and consent boundary

New connector credentials now name exactly one health subject and persist exact
domain/action capabilities. A professional credential is additionally tied to
one care relationship and immutable consent version; pausing, revoking, or
superseding that consent makes the credential fail on its next verification.
Pre-cutover credentials are bound only to a record owned by their account, and
credentials whose accounts own no record are retained only as revoked history.

### Fixed — professionals keep navigation on a phone

Doctors and trainers without a personal health record now retain a compact
mobile bar for Patients and Sign out. Platform support appears there when the
role has it. Previously the personal record bar was correctly hidden but no
professional replacement was rendered, leaving care screens without global
navigation or logout below the desktop breakpoint.

### Fixed — care guidance and conversations use human names

Notes, plans, conversation participants, and message authors now prefer the
professional's submitted display name. The patient is named from their health
record; technical login handles remain only as a fallback when no profile has
been submitted.

### Changed — an open care conversation is one screen

Opening a conversation no longer leaves the new-conversation form and the full
thread list above the messages. The focused screen contains the conversation,
its composer, and one “all conversations” route back to the list.

### Fixed — shared record sections are shown once

The patient access screen no longer repeats every health section for the
internal read, list, and search permissions behind it. A full grant is one
plain “all record sections” summary; a narrowed grant uses the consent labels,
including the translated goals label instead of leaking an i18n key.

### Fixed — the bundled identity provider no longer invites orphan sign-ups

Fresh ZITADEL instances now start with public self-registration disabled.
Vitals deliberately provisions and links each local account, so the provider's
upstream registration default only produced identities that could never enter
the application. The OIDC runbook also explains how to close registration on
an existing identity database without deleting it.

### Added — care plans have visible lifecycle controls

The professional who wrote a plan can now start it from draft and archive it
from the patient record. The route keeps the patient in the URL, checks the
current consent on every request, and relies on the authored-record boundary so
another professional cannot change somebody else's plan.

### Added — patients choose exactly what a professional may use

The care-access screen now turns consent into a visible choice instead of one
all-or-nothing button. Patients select record sections and independently allow
professional notes/plans and shared conversations. Saving a change creates the
next immutable consent version; existing integrations that call the grant route
without the new form retain the documented default scope set.

### Fixed — professional guidance names its author

Shared notes and care plans now show who wrote them. Their authors are loaded
with the record query rather than through async template lazy-loading, so the
attribution is both visible and safe to render for patients and other members
of the care team.

### Changed — a care conversation starts in one step

Professionals and patients no longer create an empty conversation and then
write its first message on a second screen. The new-conversation form sends the
topic and first message together in one transaction, while existing clients
that create an empty thread remain compatible.

### Changed — authentication code now has one boundary

OIDC verification, provider identity binding, session revocation, OAuth client
metadata, MCP credentials, and the legacy local TOTP path now live together in
`vitals.services.authentication`. Their filenames describe the concepts they
own instead of adding six more `*_service.py` modules to the flat service root;
all internal callers moved atomically and the old paths are not retained as a
second API.

### Fixed — MCP issuer binding is now enforced

Registry-backed connector tokens already carried both audience and issuer, but
verification checked only the audience. A token with a valid shared signing
secret could therefore name another issuer—or omit either installation-binding
claim—and still pass. Verification now requires exact `aud` and `iss` values
for current tokens while retaining the explicit adoption path for older tokens
that predate the registry.

### Changed — care handoff now explains the next step

Accepting a professional invitation now returns to the care list with a clear
notice that the patient's separate sharing decision is still pending, instead
of sending the professional into a record that correctly answers 404. Open
patient rows are one large keyboard-accessible target and no longer expose an
internal consent version. The patient screen only offers conversations when
the grant permits them, and the already-supported care-plan draft flow now has
a visible form.

### Fixed — support decisions require recent authentication

Opening, answering, withdrawing, or revoking controlled support access used an
ordinary long-lived browser session. These sensitive transitions now require an
authentication proof from the last fifteen minutes. A stale federated session
is sent through an OIDC `prompt=login` step-up whose returned `auth_time` is
validated; legacy password mode clears the old cookie and requires a fresh
login before the action can be retried.

### Fixed — revoked federated sessions stop at the web boundary

Versioned OIDC cookies carried a revocable `session_version`, but protected web
routes only verified the cookie signature. Suspending an account or incrementing
its session version therefore did not end an already issued browser session.
The shared authentication dependency now confirms every federated session
against the current active user row, so one revocation closes the next request
across every protected route.

### Fixed — signing out also ends the provider session

Federated logout previously deleted only the Vitals cookie. The live OIDC
session remained at the provider, so returning to the login page could sign the
same person straight back in without asking. Vitals now follows the provider's
discovered `end_session_endpoint`, binds the registered post-logout URI with
its client ID, and still clears the local cookie if provider discovery is
unavailable.

### Fixed — the OIDC runbook no longer leaves a known first administrator

The pinned ZITADEL `v2.66.0` creates `zitadel-admin` with the publicly documented
default `Password1!` unless its first-instance settings override it. Compose did
not. The optional profile now has a preflight that requires a 32-character
master key, a database password, and an operator-chosen first administrator
password before either IDP container starts. The runbook and `.env.example`
name and generate all three, and the administrator is forced to change the
password on first login.

Partial OIDC application configuration now fails startup rather than quietly
keeping password authentication enabled. The runbook also corrects the pinned
tag's licence from AGPLv3 to Apache 2.0 and instructs operators to re-check the
exact tag before any image upgrade.

### Fixed — MCP overview counts no longer include other records

The dated sections of `get_data_overview` filtered by the connector's resolved
subject, but four count-only sections did not. PostgreSQL RLS hid the mistake in
production; SQLite, used by the fast path and local development, returned the
installation-wide totals for supplements, genetics, milestones and GLP-1 dose
phases. Every count now carries the same explicit subject predicate, with a
two-record regression that asserts the numbers rather than merely searching the
serialized response for somebody else's label.

### Changed — care-team code now lives with the care-team domain

The professional profile, invitation, relationship/consent, patient-record
projection and shared-thread services were five unrelated files in the flat
`vitals/services/` directory. They now form `vitals.services.care` and are named
after the concepts they own: `professionals`, `invitations`, `relationships`,
`records` and `threads`. All callers move with them; no compatibility shims keep
the obsolete module layout alive.

### Fixed — an operator-provisioned account can actually sign in

`scripts/provision_account.py` created accounts without a local password and
said that an operator would bind each one to its provider identity, but that
second operation did not exist. After the one-time owner bootstrap had been
spent, every additional account was therefore unreachable. The new
`scripts/link_identity.py` binds an existing active account to the provider's
exact `(issuer, subject)` pair under the identity-governance lock. It refuses
unknown or inactive accounts and never moves an identity already linked to
someone else.

### Fixed — the optional identity-provider profile blocked ordinary Compose commands

Compose interpolates every service before applying profiles. Required-value
expressions on ZITADEL's secrets therefore made even `docker compose ps` and a
plain `docker compose up` fail on installations that had deliberately not
configured the optional `idp` profile. The inactive profile now accepts empty
placeholders during interpolation; selecting it without real secrets still
fails closed in PostgreSQL and ZITADEL themselves.

### Added — a connector token that names what it is for, and can be taken back

The payload carried a username, a client id and a type. No audience, so a token
minted for one installation was a token for any installation sharing a signing
secret — a restored backup, a staging copy. And no id of its own, so the only
way to withdraw an issued token was rotating `VITALS_SESSION_SECRET`, which also
invalidates every web session: "disconnect the laptop I lost" and "sign the
whole household out and reconnect every client" were one operation, which is a
revocation mechanism in the sense that a fire alarm is a door.

It now carries `sub`, `aud`, `iss` and `jti`. The token stays a signed value, so
the signature still validates without a lookup; what the database answers is
everything a signature *cannot* say and that can become false while a valid
signature stays valid — that this token was minted for this resource, that the
account still exists and is active, and that nobody has since taken it back.

The audience is `{public_url}/mcp` rather than the origin. This origin also
serves a website, a JSON API and an OAuth authorization server, and a token whose
audience were the origin would be a token for all of them.

Revision `0064` adds `mcp_access_tokens`. No subject column and no row-security
policy: this is an account's credential, not a record's, and which record a
connector reaches is decided per request by the subject seam. Putting a subject
here would be a second answer to that question with no way to keep the two in
step.

**Old tokens are adopted rather than broken.** A token minted before the table
existed carries no `jti`, and the first time it is presented a row is recorded
for it — dated from the signature's own timestamp, so the list is truthful about
when the connector was actually authorized. From that moment it is listable and
revocable like any other, and marked `adopted` so somebody reading their
connections can see which predate the guarantee. Its row id is derived from the
token by hash, so the table does not become a copy of the secret.

One behaviour changed deliberately: a token naming no account is now refused
outright. It cannot be attributed, so it can be neither listed nor revoked, and
a credential nobody can withdraw is what this table exists to stop existing.
Nothing real is lost — `/oauth/token` has carried the authorizing account's name
since the flow was written.

Settings gains **Connected assistants**: what is connected, since when, and a
button that disconnects one and nothing else.


### Added — the MCP OAuth profile: client metadata documents, fetched as hostile input

A client can identify itself by an HTTPS URL now instead of by a pre-registered
id. The URL *is* the `client_id` and resolves to a JSON document declaring the
client's name and its redirect URIs. That is the profile's replacement for
Dynamic Client Registration, which it deprecates, and it is strictly tighter
than what stood here: a document names its callbacks in full, where the
configured allowlist could only ever name hosts. Same host, different path — an
authorization code delivered somewhere the client never declared — is the case
the old check could not make.

**The URL comes from whoever is authorizing, so fetching it is SSRF by
construction**, aimed from inside whatever network the installation runs in.
Every guard is load-bearing rather than defensive style: HTTPS only; the
destination checked *after* DNS resolution and against every address a name
returns, because `internal.example.com` pointing at `10.0.0.5` is the entire
trick and a hostname says nothing about it; no redirects at all, since following
one means validating a second destination and public-redirects-to-private is the
second half of the same trick; bounded time and body; and the document's own
`client_id` must equal the URL it came from, without which a client could name
somebody else's document as its identity and inherit their redirect URIs.

Every failure answers the same way. A caller who could tell a private address
from an unreachable host from a malformed body could map the network this runs
in, one client id at a time.

Documents are cached for five minutes — an authorization flow fetches this on
every attempt — and not longer, so a client whose redirect URIs change, or whose
document is taken over, stops being believed without anything being restarted.

`/oauth/authorize/approve` re-resolves the client rather than trusting the form
it reads: that endpoint is reachable on its own, and a document checked while
the consent page rendered proves nothing about the client id in the POST.

**The authorization response carries `iss`** (RFC 9207), and discovery
advertises that it does. A client talking to several authorization servers
cannot otherwise tell which one answered, and an attacker who can put a response
in front of it relies on exactly that. The issuer in the discovery document now
comes from the configured public URL rather than from `request.base_url` — it
has to be the same string the response puts in `iss`, and two values derived
differently disagree the moment this sits behind a proxy.

The consent screen shows what the client calls itself. The person there is
deciding whether to hand over their medical record, and a name is a better basis
for that than a URL they have to parse in their head. The pre-registered
connector gets no name: it brought no document, so it has made no claim, and
inventing one would be putting words in its mouth.

Both shapes are accepted. Claude.ai's connector uses a plain client id today and
breaking a working connection to adopt a newer identifier would be a change
nobody asked for.


### Fixed — the LLM export read every person's record (PR-10)

`data_portability_service.export_llm` had twenty-two selects and not one subject
among them. Written when the installation held one person, correct then, and a
cross-subject export the moment it held two — in the worst shape that mistake can
take, because the result is a single LLM-ready document of everybody's weight,
labs, meals, injections and notes.

Both callers made it worse by resolving a subject and then not passing it: the
MCP `export_everything` tool, and the `/settings/export-llm` download in the
browser. Verified rather than reasoned about: with two records seeded, the
export returned the other person's weight.

The subject is a required keyword now and every read is filtered by it. No
default, deliberately — an omittable scope is the shape `vitals/legacy_scope.py`
exists to keep out of this codebase, and this function is why: two callers had
one in hand and neither passed it, which a default would have hidden forever.

`get_data_overview` had the smaller version of the same defect — it resolved a
scope and then counted rows across every table. A count is a lesser disclosure
than a value and still tells one person how many lab results another has and
when the earliest was taken.

### Added — protocol conformance, proven rather than assumed (PR-10)

Most of what PR-10 asks for on the wire the SDK already does, and the work was
finding out which parts: `server/discover` answers with the versions this server
speaks, the session negotiates `2026-07-28`, results carry `resultType` and the
server identity in `_meta`, and caching defaults to `private` with a zero TTL.
Five tests hold each of those, because a default is a thing somebody can change
and the change that matters here would let an intermediary serve one patient's
weight to the next caller.

Two of those took a wrong turn first. `server/discover` answers "Method not
found" to a raw POST — the envelope decides whether a peer is legacy, and
without it the method does not exist for that caller. And the server identity
appears only after a client has discovered, which is correct: before that the
server has no reason to think its peer speaks the modern protocol.

**Tool descriptions are trimmed to their first paragraph.** The SDK sends a
docstring whole, and these docstrings are also where this codebase records
decisions — why a field moved out of `.env`, which refusal to expect. That is
21 KB of internal engineering history, sent to a third party on every listing,
that no model can act on. The summary goes out; the rest stays in the source
where the next person to read the code will find it.


### Changed — MCP moved to the official SDK and protocol 2026-07-28 (PR-10)

`fastmcp==3.4.5` is replaced by `mcp==2.0.0`, the Tier-1 Python SDK, released
on 2026-07-28 — the same day as the protocol revision it implements, and
`mcp.types.LATEST_PROTOCOL_VERSION` says so. It is a replacement rather than an
upgrade: FastMCP requires `mcp<2.0`, so the two cannot stand side by side.
Pinned exactly, because the wire contract is what a connector on the other side
depends on.

**The transport is stateless**, which is the `2026-07-28` contract rather than a
tuning knob: a request carries what it needs, there is no `Mcp-Session-Id` to
hold, and nothing about having completed a handshake can be mistaken for having
been authorized. A client now lists tools without calling `initialize` at all —
the old transport answered "Missing session ID".

**Authentication moved inside the SDK.** `MCPAuthMiddleware` — 144 lines of
hand-rolled ASGI that read the Bearer header, checked the signature and the
client id, and pushed the identity into a contextvar — is deleted. A
`TokenVerifier` answers the same three questions and returns an `AccessToken`
carrying subject, scopes and claims, which `get_access_token()` hands to every
tool. That is where PR-10 wants the identity ("stable user `sub`, subject,
audience, scopes"), and one authority on the door beats two that can drift.

**The module filter moved into `list_tools`.** It was a FastMCP `on_list_tools`
hook; the SDK's middleware only sees the wire, so keeping it there would have
made the listing an in-process caller gets and the listing a connector gets two
answers computed in two places.

Two things had to become configuration rather than observation. `VITALS_PUBLIC_URL`
names this installation, because a token's audience binds to that identifier and
one derived from an inbound `Host` header is one an attacker chooses. And DNS
rebinding protection is now on with an allowlist built from it — loopback is
allowed on any port, which is not a hole: a rebinding attack needs a name whose
resolution can be changed, and a literal address has none.

`/.well-known/oauth-protected-resource/mcp` is served alongside the bare path.
The SDK's 401 challenge points at the resource-specific form (RFC 9728 §3.1),
and a challenge pointing at a document nobody serves is worse than no challenge.

Two behaviours changed and are recorded rather than smoothed over. An
unauthenticated `OPTIONS` on `/mcp/` is 401 where the hand-written wrapper
answered 200 — that response carried no `access-control-allow-origin`, so it
granted nothing, and the connector is server-side and never sends a preflight.
And the protected-resource document now reports the configured URL rather than
the requested one.

Six wire-level tests arrive with it, which is the thing this codebase has never
had: the endpoint driven by the SDK's own client. PR-10 asks for exactly that —
*not only by calling decorated Python functions.* Every other MCP test calls a
Python function, and a tool can be perfectly correct while the transport in
front of it negotiates the wrong protocol.

They run over an ASGI transport rather than a socket, which took three attempts
to get honest. A server on a thread has an event loop of its own, the suite's
engine is bound to the test's, and asyncpg does not survive being used across
the two — the symptom was three tests passing every assertion and then erroring
at teardown inside the fixture that closes the session, which is nowhere near
the mistake. The socket was never the point: the client still negotiates a
version, frames JSON-RPC, and reads real responses. Only the kernel is missing.

222 MCP tests pass unchanged, because the SDK's `@server.tool()` returns the
plain function the way FastMCP's did.


### Fixed — an MCP connector reached whoever `.env` named, not whoever asked

The OAuth access token has carried the authorizing account's username since the
flow was written. The middleware read the signature and the client id and threw
the username away. Every one of the sixty tools then resolved its subject
through `resolve_legacy_ownership_context(actor_username=get_web_config()
.auth_username)` — the `.env` owner — so **the answer to "whose record is this"
did not depend on the credential at all**.

On a single-user machine those are the same person, which is why it went
unnoticed. On a shared installation any signed-in account could walk the
ordinary consent screen, obtain a connector token, and read *and write* somebody
else's medical record. The tools' own docstrings said what they were —
"attribution plus a fail-closed single-subject compatibility gate; it is not
MCP v2 subject authorization" — and that gate stopped being fail-closed the
moment the resolver was given a name to match.

The fix is one seam rather than sixty edits. `MCPAuthMiddleware` puts the
token's identity into a `ContextVar`; `_mcp_actor_username` reads it; the six
`_mcp_v1_*` helpers every tool already funnels through ask that instead of the
environment. Three states, and the middle one is the point:

- **a username** — the account that stood at the consent screen;
- **an anonymous token** — a credential that authenticated and named nobody, as
  every token minted before this does. Honoured while the installation holds one
  subject, refused once there is a choice to make;
- **nothing at all** — no request: a direct call, a job, a test. Unchanged, and
  deliberately not narrowed, because `.env` names an account and the resolver
  returns that account's own record — a fact about one person rather than an
  assumption about how many exist.

`resolve_legacy_ownership_context` matches a username and never reads `status`,
so a suspended person's connector would have kept working for the rest of the
token's year. It is checked explicitly now, in the seam rather than the
middleware — that is where a session already exists, and where a test can
substitute one.

Thirteen tests, including the one PR-10 names: *token A cannot select subject
B, even with a known row ID or direct tool call.* Eight drive the tools with an
actor set; five drive the middleware itself with a real signed token, because
the first eight prove the tools read the seam and prove nothing about the half
that fills it. Verified by reverting the seam on purpose — six of the eight
fail.

One PostgreSQL-only failure fell out of the run and was pre-existing:
`test_hrt_foreign_cycle_item_is_not_materialized_before_rejection` builds an
item whose subject differs from its cycle's, and
`fk_hrt_cycle_items_cycle_subject` is a composite key that will not store one.
The same shape as the five in `fdc7253`, fixed the same way: the database
refusing is the stronger guarantee, so the test now asserts both — unreachable
on the database that ships, still refused by the reader on the one the fast
suite runs.

This is PR-10's authorization gate, which the roadmap separates from its
protocol gate for exactly this reason: the wire migration to `2026-07-28` waits
on an external SDK, and none of the above did.


### Added — the LLM boundary is proven, not just built (PR-10, in part)

`assemble_context` is what the weekly digest, the daily brief, the doctor's
report and the MCP composition tool all reason over, and `build_prompt`
serializes it whole into what an external model receives. Its subject has been
mandatory since it was written and every read inside it is scoped — the boundary
was there. The proof was not, and a boundary nobody re-checks is one a single
new query can step around: adding an unscoped `select` to a 3000-line assembler
is a two-line mistake no existing test would have noticed.

Seven contract tests now seed a second person with values that could not belong
to the first — `999.9`, `SENTINEL-MEAL-DO-NOT-LEAK` — compose for the first, and
search the serialized prompt for any of them. Absurd values on purpose: a leak
of plausible numbers is invisible in a diff.

They hold four things. Nothing of another person's reaches the model, in either
language. A module the person switched off contributes nothing, which is a
promise about what *leaves* rather than only about what renders. The window
bounds it, so a note from a year before and a result dated after the period both
stay out. And the prompt names nobody: age, sex, height, programme and goals
cross, while display name, username and row ids do not — asserted as an absence,
because that is how it would be lost, to somebody adding a name so the narrative
can say "Timur has been sleeping badly".

Two of the seven exist to keep the other five honest. One composes for the
second person and requires their sentinels to appear, so the isolation
assertions cannot pass on an empty context; another requires the sentinels to be
present with every module on before switching two off. Verified by breaking it
on purpose: a deliberate leak fails three of the seven.


### Changed — the external API token names a record (PR-10, in part)

`VITALS_EXTERNAL_API_TOKEN` was one string for the whole installation, and the
endpoint it opened resolved its subject from whoever `.env` named as the owner.
On a single-user machine that is a per-subject credential by accident. With a
second person in the database it is a credential with no boundary: its holder
reads a record nobody granted them, and nothing about the token says whose data
came back. It was the last `.env`-owner read left on a data path.

Credentials are rows now (revision `0063`). Issued by the record's owner and by
nobody else — not a professional in care, not a platform administrator, because
handing out a long-lived key to somebody's health data is not something a
support grant should be able to do quietly. Labelled, so a list of secrets is
one somebody can revoke from with confidence. Expiring by themselves, revocable,
and kept after revocation: "this dashboard could read my weight until March" is
part of who-saw-what.

**The secret exists once.** Only its SHA-256 is stored, and the page that mints
it is rendered from the POST rather than redirected — a URL ends up in browser
history, in the access log and in the next page's referrer, and a bearer token
is a capability. The rule `consents.issue_invitation` already followed.

**Authentication asks again every time**, and about more than the token: a
suspended owner's credentials stop working on the next request rather than
eventually.

The environment token still works while the installation holds exactly one
subject — the same fail-closed rule as the rest of this migration — and is
refused with `external_api_token_cannot_name_a_record` as soon as it would have
to guess. It used to resolve silently to the `.env` owner, with the holder none
the wiser about whose data they had.


### Added — a browser suite with page objects, roles and scenarios

Seven defects on this branch were found only by opening the product, and each
had a green suite over it. What every one of them has in common is that the
service was right, the route answered 200, and the page showed the wrong thing —
or showed the right thing where nobody could reach it. Nothing below the browser
distinguishes those from working.

`tests/ui/` is that layer, built as a framework rather than a script:

- **It starts its own installation.** A session fixture seeds a throwaway
  database, starts the app on a free port and tears both down. A browser suite
  that needs a README before it works is one nobody runs.
- **Locators live on page objects**, one class per screen, and navigation
  returns the page it lands on — so a flow reads as `console.ask_for(...)
  .open_the_record()` rather than as a pile of selectors.
- **Tests address people, not URLs.** `doctor.record_of("timur")` resolves the
  subject id from the seeded database, because every id changes on a reseed and
  a pasted one becomes a 404 that reads exactly like a regression.
- **Every load is watched.** Console errors, unexpected 4xx/5xx and horizontal
  overflow are collected by a fixture and raised at the end of the test, so a
  scenario cannot pass its own assertions while walking past a 500.
- **A failure is photographed.** The screen and its markup land in
  `.ui-failures/` and the paths are printed, one per role — a two-role flow
  fails on one of them and which one is the question.
- **The installation is left as it was found.** Sharing one seeded app across
  33 scenarios is a fifteen-second saving and a real hazard: a pending support
  request one scenario leaves behind is one the next reasonably concludes is a
  bug. Four tests failed exactly that way before the reset fixture existed.

Skipped when Playwright or a Chromium is absent and excluded from the default
run — `pytest tests/ui -m ui`, 33 scenarios in about ninety seconds.

Building it caught one defect in itself worth recording: the first version
started plain uvicorn against a Redis that was not there. The app did not crash;
it rendered, answered 200, and silently lost a message that had just been
written. A harness that degrades quietly invents defects that look like product
defects, so `tests/ui/_serve.py` substitutes FakeRedis exactly as `run_local.py`
does, and says why.


### Added — support access a patient decides, and can end (PR-12)

The policy engine has understood support grants since PR-02. `_support_allows`
in `vitals/access.py` checks the grantee, the lifecycle, the expiry, the mode
ceiling and the exact scope — and nothing had ever created one, so the branch
was dead code and a platform superadmin's role authorized precisely nothing.
`access_resolution`'s own docstring has claimed since it was written that
support grants are read there; they were not. This makes the sentence true.

**An ask is not a grant, and gets its own table.** `support_access_grants`
cannot hold a pending one, and that is a feature of it: its constraints say a
row there was approved by somebody who is not its grantee and expires strictly
after that approval. A "pending" status would cost both, and those two are most
of what makes a row there mean *authorized*. Revision `0062` adds
`support_access_requests` and its scopes; approving one is the only thing that
ever writes a grant.

**The order is enforced three times.** `open_request` refuses an actor who is
not an active superadmin. `approve_request` refuses an actor who does not own
the subject. The schema refuses a grant whose approver is its grantee. The
superadmin check runs again at approval, not only at the ask — a request can sit
for a week, and a patient's yes must not authorize somebody the platform removed
in the meantime.

**Read only.** `repair` and `export` are refused by name. A mode accepted and
unimplemented would read as approved to the patient and then do nothing, or
something nobody designed; each needs its own review, and the roadmap sequences
them after this.

**The granted record can be opened**, on the same screens a professional uses.
What may be shown is decided by the policy from the grant's exact scopes, so a
grant covering weight and labs renders those and lists the rest as not shared,
and every write affordance is hidden. Three things are said differently: the
banner does not call support a doctor, the withheld-domains line names a grant
rather than a consent, and the record route asks the policy before listing notes
instead of catching the refusal after — which is what turned an ordinary
partial-scope reader into a 500.

**The care-team conversation stays shut.** Being in the room is a participant
row and a grant does not add one, so the thread list is empty for support and a
direct thread URL answers 404.

**Nothing is deleted and the banner is not optional.** Declined and withdrawn
asks stay — "support asked in March and I said no" is a thing a patient is
entitled to find — and a live grant draws a banner on every page with a link
that ends it. Either side may revoke: the patient must not have to find somebody
to change their mind, and the admin should be able to put the access down rather
than wait it out.

Audit events carry no free text. The admin's sentence about why they want to
read somebody's record is shown to that patient and stored on the request; the
audit envelope goes to log sinks read by people with no business seeing it.

Two new callers enter the platform scope and are on the named list with their
reasons: an admin's own console spans every record that answered them, and the
list of records an ask may name is the auditable list `/settings/platform/ai`
already shows rather than a search for a patient by name. Both return frozen
values, so nothing reachable under the open scope leaves the function.

Not in this release, and named rather than implied: operational dashboards, the
repair workflow, export approval, incident retention controls, and the
break-glass path. The roadmap's PR-12 scope lists them and they need their own
review.


### Added — the care-team conversation, with the patient in the room

The safe first communication feature, and the shape carries the decision. A
thread belongs to a health subject; the subject is a participant from the moment
it exists and cannot be removed by anybody, including themselves; every message
in it is one they can read. A hidden doctor-to-trainer channel is a different
product with a different privacy and legal answer, and this schema cannot
express one — there is no thread without a subject and no participant list the
subject is absent from.

**Being in the room is a row, and it is not permission.** A professional joins
because somebody added them, and that row records the care relationship they
joined under — so a conversation read back a year later says which relationship
each person was speaking from. Whether they may still read or send is asked of
the policy on every call, so a paused consent stops the conversation without
deleting it and a revoked one stops it permanently, while the patient keeps every
word.

**Reading and sending are separately revocable.** `care_team.message` is an
operation with two actions: `read` for seeing the thread, `message` for writing
into it. A patient who wants a doctor to be able to look back at what was said
without adding to it can have exactly that. `PolicyAction.MESSAGE` and the
`'message'` action in the `consent_scopes` check constraint had been in the
vocabulary since it was laid down, with no caller; this is the first.

**Nothing is deleted.** A message is corrected in place, keeping its author and
gaining an edit time, and only its author may correct it. A participant who
leaves keeps their row with a `removed_at`. A thread is closed rather than
removed, and reopens. All for the reason a professional's note is never deleted:
a clinical conversation somebody can make disappear is a worse record than one
that stays, and the patient cannot review a history they cannot see.

Revision `0061` adds three tables, each carrying its own `subject_id` with a
composite foreign key back to `(thread, subject)` — so a message filed under
somebody else's thread, which would be invisible to its own patient and visible
to another, cannot exist.

The professional reaches it from the patient's record page; the patient reaches
the *same screens* from `/messages`, which resolves their record from who they
are. Deliberately the same screens: a separate patient-facing view of a clinical
conversation is a place for the two to drift apart, and the argument for a
patient-visible thread is that they cannot.

One defect found the way this feature's own screens were: the thread page
answered 500 because the template asks for `message.author.username`, and a
relationship lazy-loading outside the async driver's greenlet raises rather than
loading. Every service test passed — they read messages back as objects and
none of them renders. The reads eager-load now, and two tests render the page.

Revision `0067` adds one optional private PDF/image attachment per message. Its
metadata repeats `subject_id` and uses composite foreign keys to both the
message and `FileAsset`; bytes live in a dedicated private volume outside the
static mount. The download hangs from the subject/thread path and rechecks live
read consent and participation on every request, so pausing consent stops the
next professional download while the patient keeps their record. Revision
`0068` stores device subscriptions, `0069` adds the subject-isolated outbox, and
`0070` adds definitive provider rejection semantics. The PHI-free transport is
now called by a consent-rechecking, at-most-once scheduler job. New work remains
visible through the durable in-app unread count even when a wakeup is missed;
the service worker renders a generic notification and returns to the inbox.


### Fixed — six defects a browser found and the suites could not

Six, on four screens, and none of them visible to an assertion about a status
code or a substring. They are grouped because they share a cause: the suites
check what a page *answers*, and these are all defects in what it *shows*.

**A reply could render above the message it answered.** `care_messages.created_at`
came from the column default, which is `now()` — in PostgreSQL the instant the
*transaction* began, identical for every row written inside it. Two messages in
one transaction therefore carry the same timestamp, the thread falls back to its
tiebreak, and the tiebreak is a random UUID. `_now` asks for `clock_timestamp()`
on PostgreSQL now, which advances inside a transaction, and uses the process
clock elsewhere; `send_message` stamps the row rather than leaving it to the
default. A clinical conversation that can reorder itself is worse than one with
a coarse clock.

**A refused page was a sentence on a white screen with no way out.** Three
exception handlers answered a browser navigation with
`HTMLResponse(content=detail)`: one unstyled line, no masthead, no navigation,
no link anywhere. It is worst for the account that meets it most — a platform
superadmin on a shared installation can open exactly one address, `/care`, and
on all the others was left on a blank page with the name of the section they
wanted written out in prose and no way to reach it. All three render
`refusal.html` now, with the button chosen from what the account holds. The
access denial keeps the property it was written for: a denial and a miss still
look identical from outside, and a test holds that.

**The labs header said "0 markers" above a table of two.** The first masthead
metric counted the marker *catalog*, which is a different thing with its own
card on the right of the same page. It counts the set the table lists now, and
that `out_of_range` is drawn from, so the summary and the table agree.

**The per-marker chart rendered an empty grid beside a value that was plainly
there.** The demo seeder wrote lab rows straight through the model with the
marker name as typed, and every read path normalizes the name it looks up:
"tsh" was stored, "Tsh" was asked for. Seeded data the app itself could not
have written tests the wrong app. Each marker also gets four dates instead of
one, because a single point draws an empty grid too — indistinguishable, by
eye, from the bug above.

**The nutrition page counted in Russian on an English installation.**
`meal_word` hardcoded the Russian forms and applied the Russian rule, so the
page rendered "Today's meals · 1 приём" — one line speaking the other language.
The catalogue has carried `nutrition.meal_word_*` in both languages the whole
time; this filter never read them, while `days_word` directly beside it always
did.

**And it named one macro out of three on a phone.** Three macro cards across
390px leave each label sharing its row with a percentage, and the label is what
loses: "Protein" and "Carbs" came out as "Pr…" and "Ca…" while "Fat" happened
to fit. Stacked below 767px, where the word has the whole card.


### Fixed — four more tests that could not run on the database that ships

The same shape as the five below, in the two files the earlier integration run
was killed before reaching. All four failed in their own setup on PostgreSQL,
so what they assert was unverified on the only database production uses.

`ck_professional_invitations_positive_ttl` forbids `expires_at <= created_at`,
so simulating a lapsed offer by pulling the expiry back onto the issue time
writes a row the schema rejects; the offer is aged into the past instead.
`_without_indexes` dropped an index, rolled back, and recreated it — which works
on SQLite and cannot on PostgreSQL, where DDL is transactional: the rollback
puts the index back and the CREATE then fails as a duplicate. It asks for the
end state now rather than for the step. And two cases passed `dialect="sqlite"`
literally while running against PostgreSQL, where the SQLite partial-index
predicate compares a boolean column to `1` — not an operator that exists there.

Pre-existing: none of these files has been touched on this branch.


### Known — the same analyte typed two ways becomes two markers

`labs_service.normalize_marker` promises to standardize casing and does so only
for the 62 names in `MARKER_ALIASES`; everything else falls through to
"upper-case the first character, keep the rest". So `TSH`, `tsh` and `tSh` are
three markers with three histories and three charts, and the lab form takes free
text, so a person reaches this by typing.

Not fixed, deliberately. The fix re-keys stored clinical data: changing the
fallback splits every existing installation's history at the moment of the
change unless a migration re-keys the rows first, and re-keying makes two
spellings collide under `uq_lab_markers_subject_name`, which needs a merge
policy rather than a rename. Normalizer, migration and collision policy are one
piece of work, and half of it would be worse than the defect.


### Fixed — five tests that could not run on the database that ships

The fast suite is SQLite and the integration suite is PostgreSQL, and five tests
had only ever been run on the first. On the second they failed in their own
setup, so what they assert was unverified on the only database production uses.

All five construct a state deliberately, to prove the application refuses it —
and PostgreSQL refuses to store it first. `ck_consent_grants_positive_ttl`
forbids `expires_at <= granted_at`, so simulating expiry by setting the two
equal writes a row the schema does not allow; the grant is aged into the past
instead, which keeps the term positive and is what an expired grant actually
looks like. `fk_body_scan_metrics_scan_subject` is a composite key over
`(scan_id, subject_id)`, so a metric whose subject differs from its scan's
cannot exist at all — a stronger guarantee than a reader refusing it, and the
four body-scan cases now assert both: unreachable on the database that ships,
still refused by the reader on the one the fast suite runs.


### Fixed — the patient's consent page answered a bare 404 to a doctor

`/settings/care` is about "who holds *my* record", and a doctor or a trainer
keeps none. It resolves its own subject rather than going through the chrome
adapter, so it missed the answer PR-08 gave every other personal page and
returned a 404 that said nothing. Somebody who holds patients is redirected to
their roster now; anybody else is told plainly that this account has no record
of its own.

Found by opening the seeded shared installation in a browser and walking the
pages as each account, which is how the previous three defects of this shape
were found too.


### Changed — every scheduled job about a record now runs once per record

Eight of the fourteen jobs ran once for the installation, and the four left over
were the last thing PR-09 was waiting on. Both reasons are gone: the Telegram
transport was removed, and `integration_credentials` gave each account its own
credential, token store, session cache and login breaker.

`daily_brief` and `nudges` fan out per subject. The four provider jobs —
`garmin_sync`, `garmin_pulse`, `garmin_weight_export`, `hevy_sync` — fan out per
**connection**, which is a different question: a subject who has not connected a
watch has nothing for them to do, and enumerating them would mean four scheduled
no-ops a day per person. One account's failure is logged and the next account is
still tried, which is the same rule the subject fan-out has and matters more
here — three refused logins used to pause every account for six hours, because
the breaker was one flat Redis key.

**A failure is filed against the record it happened to.** The shared runner
recorded one outcome per tick, through a resolver that asked for "the sole
subject" — right by accident on a one-person installation, and on a two-person
one a refusal the handler swallowed, so a failing sync raised no alert at all
while `/health` stayed green. `record_subject_job_outcome` takes a mandatory
subject and is called once per record by the fan-out; the runner keeps only the
platform-family jobs, which are about the installation's own state and have no
record to be about.

**`subject_id` is mandatory on all four provider jobs**, and on the brief and
the nudges. An omittable one is the shape `vitals/legacy_scope.py` exists to keep
out, and it is exactly what let these jobs mean "the sole subject, or refuse".
The human caller who asked through MCP for "sync my Garmin now" gets its own
entry point, `sync_now_for_actor`, which resolves the record the *actor* owns —
two callers that meant genuinely different things and had been sharing one
argument. What is left on the job is `actor_user_id`: attribution rather than
scope, whose request it was rather than whose record, unset for a scheduled run.

The outbound-weight lock is per connection too. `garmin_weight_export` was one
flat lock name for the installation, so one patient pressing **Send now** made
another's answer "busy" — for an operation against a different Garmin account.

`tests/test_scheduler_fanout.py` used to pin those eight jobs as deliberately
*not* fanned out. It now pins the inverse — every job that is about a record runs
once per record, and the three that are not are named rather than defaulted — so
a job that quietly stops being fanned out is a job that silently serves one
person on an installation holding ten.


### Added — an installation can gain a second person, and a decision about whether it may

Two facts that had been one. `identity_bootstrap` makes the installation's own
owner out of `VITALS_AUTH_USERNAME`, and `scripts/seed_care_demo.py` made
everybody else — which is why the professional features shipped in PR-07 and
PR-08 have never had anybody to be about on a real installation, and why every
shared-installation defect of the last few weeks was found by running that
script rather than by using the product.

It also meant that "registration is closed" was a property of there being
nowhere for an account to come from, rather than a decision anything had made.
That is true and fragile: the day something could create one, nothing would have
stopped it.

**`account_provisioning_service` is the one place a subject is born.** A subject
is not one row — it needs an account that owns it, a member role, the
integration roots every provider path resolves through, and a module map — and a
subject missing any of those fails on the fourth page somebody visits rather
than at creation. It never adopts the environment's Garmin or Hevy credentials:
those are the installation owner's, and a new root that claimed them would hand
one person's watch to everybody provisioned after them.

**`registration_service` is the decision, and it answers `disabled`.** Four
modes from the plan — `disabled`, `invite_only`, `admin_approved`, `open` — and
two switches in front of them. `VITALS_REGISTRATION_UNLOCKED` is a deployment
decision that comes after a security review, deliberately an environment
variable rather than something an administrator can flip from a screen; the
stored mode is the installation's own setting, configurable and reviewable ahead
of the release that makes it mean anything. The two middle modes have no
implementation and refuse with a message that says so, rather than falling
through to the most permissive one.

`authentication.federation` consults it: an unrecognised provider identity becomes
an account only where the installation has said it wants one, and the refusal
when it has not is byte-identical to "no such identity", so a stranger learns
nothing about whether this installation is accepting people. A name somebody
already holds is a refusal too — `newcomer-2` would hand a stranger a name
implying a relationship to an existing account.

`scripts/provision_account.py` is how an operator creates one today. It is
deliberately not registration: no form, no route, no token, and whoever runs it
already has a shell on the host. The demo seeder goes through the same service
now, so a gap in provisioning shows up in the browser check rather than only
after registration opens.


### Fixed — a new installation could not be created

The container's start command is `alembic upgrade head && uvicorn …`. On an
empty PostgreSQL that failed, so the process never started and no new deployment
could be stood up at all.

Revision `0005` seeded five skincare products — one person's regimen, in
Russian — into every new installation. Revision `0049` later made
`skincare_products.subject_id` NOT NULL and refuses while any row has no owner.
On an installation that already existed those five rows were the owner's and the
Stage-3B backfill adopted them; on an empty database there is no owner to adopt
them, because identity bootstrap is an application step that runs after
migrations. The seed could not be removed by a later revision — `0049` comes
first and is what fails — and could not be made conditional, because at that
point in the chain the ownership columns do not exist yet, so nothing there can
tell a fresh database from a historical replay.

So `0005` was edited after having been applied, which this repository otherwise
forbids, and its docstring says why at length. Deleting the insert is a no-op for
any installation that already ran it. The only behaviour that changes is the one
that was broken: **a fresh installation now starts with an empty skincare
catalog**, which is also the more correct default — the ownership inventory has
said for a while that this table is personal despite its "reference" label.

Behind it was a second one, which only became reachable once the first was
fixed. Revision `0058` dropped `signals` and `day_context`, and its `downgrade`
recreated an approximation of them: the raw link named `raw_payload_id` instead
of `raw_id`, four indexes missing, foreign keys left to their default names. On
the way down from head, revisions `0051`, `0050`, `0049`, `0038` and `0037` each
drop those objects by name, so the chain stopped at "index
`ix_signals_connection_batch` does not exist" — halfway through, on a database
somebody is using. The downgrade now reproduces exactly what revision `0057`
left, including the row-security predicate `0050`/`0051` rewrote.

Neither was visible to any test. The migration rehearsals all start from a
synthetic revision-0034 lake with an owner already bootstrapped, which is the
*upgrade* path; the fast suite builds its schema with `create_all` and never runs
a migration. `tests/test_fresh_installation_migrations.py` walks the one path
nobody exercised — the one every new deployment takes.


### Added — a provider account that belongs to one patient

`VITALS_GARMIN_EMAIL`, `VITALS_GARMIN_PASSWORD` and `VITALS_HEVY_API_KEY` are one
watch and one workout account for the whole process. That is the single-user
shape behind the last four scheduled jobs that could not be run per subject:
doing so with those credentials would have written the operator's own watch data
into everybody else's record, which turns an outage into a disclosure.

**`integration_credentials`** (revision `0060`) is where a per-person credential
goes. It holds a Fernet ciphertext of a small JSON object under
`VITALS_CREDENTIAL_KEY` — the installation's key, which is the kind of thing
`.env` is for — and nothing else: no email, no account id, no key suffix, for the
same reason the connection's account discriminator is opaque. Its foreign key is
composite on `(connection, subject)`, so a credential whose two owners disagree
cannot exist. `key_version` has one value and exists so rotating the key is later
a migration rather than an outage. This is the first encrypted-at-rest store
here; everything else is hashed (passwords, which only need comparing) or
plaintext in `.env` (installation-wide accounts).

**The credential was the obvious half.** The quiet half is everything the Garmin
client keeps beside it — the cached token session in Redis, the login breaker's
counters, the `sync:last_success` marker, the token store on disk — all flat,
process-wide keys. Two subjects sharing those means one person's session
resuming as another's, and one person's three failed logins pausing everybody
else's sync for six hours. Every one of them is namespaced by connection now,
including the installation owner's: an exception there would have to be decided
on every lookup from facts that are ambiguous on exactly the installations that
matter. The cost is one credential login for the owner on the first sync after
the upgrade, which is what happens whenever a token expires anyway.

**`legacy_env:` now names the installation's own account and only it.** The
tenancy bootstrap wrote that ref on *every* subject's roots without knowing whose
they were — harmless while nothing resolved it, and a disclosure the moment
something did. Only the boot path reconciling `VITALS_AUTH_USERNAME` writes it
now, and the migration cleared it from every Garmin and Hevy connection it was
never about. If that variable is unset when the migration runs, every such ref is
cleared: the owner re-enters their credentials on the settings card, which is a
form, and that is better than guessing which record the file describes.

Every construction of a Garmin or Hevy client goes through the new resolver, so
the two sync jobs, the pulse, the weight outbox, the two dashboards and the two
"Sync now" buttons all act as the record's own account. Two of them had to be
reordered to do it: they built the client — and asked "is this configured?" —
before anything had said whose record the run was for.

`/settings/garmin` and `/settings/hevy` store against the signed-in record and
stop writing `.env`. The outbound-weight opt-in is gated on that record having an
account, where before a patient with no Garmin passed the check on the strength
of the operator's and their weight would have been pushed to somebody else's
watch. A deployment with no `VITALS_CREDENTIAL_KEY` is told so on the card rather
than accepting a password and failing on save.


### Changed — the profile belongs to a person, not to the installation

Age, sex, height, the programme, the goals and the three nutrition targets lived
in `.env`, which names nobody: one set of them for however many patients an
installation holds. Two defects came out of that fact, and only one of them
looked like a defect.

**The visible half** was the report. Those five fields were printed on every
patient's weekly digest, doctor's report and share link as though they were
theirs. They were omitted outright for a while, which was a placeholder rather
than an answer — it cost the owner five fields and told nobody anything.

**The quiet half** was the Navy formula, which takes a height and a sex. Every
patient's body-fat percentage and lean body mass were computed from the
installation owner's geometry, cached for the process. A wrong number in a
medical record reads exactly like a right one.

They are a subject-scoped setting now (`health_profile_service`), and every
reader takes a subject: the report assembler, the share snapshot, the Navy
estimate, the nutrition goals and their progress bars, the protein nudge, and
the MCP `get_user_profile` tool and `vitals://profile` resource — that last pair
answered with the owner's body regardless of which record the caller was scoped
to.

**Absent is not a default.** A subject who has never filled the card in gets
nulls rather than 190 cm, male, 18. The estimate is skipped instead of computed
from half a profile, the report omits what it does not know, and the form
renders empty boxes instead of pre-filling somebody else's numbers — including a
new blank option on the sex select, without which the first save on that card
would have quietly stored "male". The three nutrition targets are the deliberate
exception: a target is a goal rather than a fact about a body, so a sane default
is honest where inventing a height is not.

`.env` is adopted once, at startup, onto the legacy owner's record — while that
owner is still the only subject, which is the one state in which an
unattributed profile is unambiguously somebody's. It never overwrites, so a
corrected height survives the next deploy. `Config` keeps the eight fields as an
adoption source with a note not to add a reader.

`/settings/profile` writes the record and stops writing `.env`. That also fixes
the timezone field, which had been writing `VITALS_TIMEZONE` since the
per-subject clock landed — the one place nothing reads any more. Changing your
timezone in Settings did nothing at all; it now updates
`health_subjects.timezone`, and an unknown zone is refused rather than stored,
because stored it would raise on every later request that asks what day it is.


### Fixed — a shared installation can save its notification settings

Everything the sole-subject retirement covered so far walks `GET`. `POST` was
never swept, and one of these was live: clicking **Save** on the notification
settings card answered 409 on any installation holding two people, so nobody
could store their own brief time.

The refusal came from `_lock_write_roots`, which demanded exactly one subject
for every caller. What a second person actually invalidates is the shared
`app_settings` mirror — the same distinction `scoped_settings_service` already
draws — and not this person's own subject-scoped row. The cardinality is now
reported rather than enforced: the mirror is skipped, the scoped rows are
written, and the two callers that genuinely need a sole subject (the startup
adoption of the legacy row, and the actorless startup read) refuse on it
themselves.

The second half is the scheduler. `apply_schedule` rebuilds the process-wide
registry from whatever was just saved, which on a shared installation means the
second person's Save re-times the first person's brief. Startup had already
decided this — it keeps the defaults rather than faking a schedule from one
person's row — and the save path now agrees, and says so on the page instead of
reporting a plain "saved" that is true about the row and false about the effect.

`tests/test_shared_installation_pages.py` gained the write half it was missing:
an empty-body sweep of every mutating route for the same no-stack-trace
property, and a stronger one that carries a body each route accepts, so the
request reaches the service rather than stopping at validation. Twenty-one
domain write paths are asserted to work for the record's own owner with somebody
else in the database.

It also gained the third account shape. The file walked the record's own owner
and an account with no record at all; a shared installation is mostly made of
neither. A patient who is not the `.env` owner, keeps their own history and
reaches every page about it now walks both sweeps, and the fixture is explicit
that its integration roots exist only because it created them — today nothing
but the startup bootstrap and the demo seeder does, which is the next thing a
registration page will get wrong.


### Removed — Telegram, signals, and the day context

Three things that were one thing: a chat, the free text it carried, and the
questions it asked. All of them assumed a single person — one bot token and one
chat id in the environment, which cannot belong to more than one patient — and
they were the reason four scheduled jobs could not be run per subject. Web push
replaces the delivery and is per-subscription by construction.

**What went.** The webhook and the Telegram client; the inbound layer that
claimed updates, parsed replies and redrew answered questions; the `signals`
domain and its AI parser; `day_context` and the week template that pre-filled it;
the evening block that asked the questions. Roughly 22,000 lines, and revision
0058 drops the two tables. That deletes data, and the migration's downgrade
recreates the shape only — it says so rather than leaving it to be discovered.

**What stayed, and why.** The historical delivery journal, the morning brief,
the nudges and message composition. The journal's docstring originally claimed
a second channel would add rows there, but its schema still requires a Telegram
channel and a subject-owned integration connection. Revision `0068` records the
correct boundary instead: a browser subscription belongs to an account/device,
not to one patient. The proactive jobs still compose and stay quiet until a
separate outbox/sender lands, which is how the app behaved before the bot
existed.

The layer's master switch was removed rather than left stranded: it *was* the
`signals` module, so with the module gone it would have read as permanently off
and no message could ever be sent again. Its own preferences decide now.

**What could not go.** `Source.TELEGRAM` and the string `"signals"` as a
raw-payload domain. The domain left the enum — nothing writes it — but rows
already stored carry it, and classifying what is already there is precisely what
the raw ownership backfill does.

**What is genuinely lost.** The symptoms section of the doctor's report: what a
patient said in their own words, which is the one thing no device produces. That
is a gap rather than a tidy-up, and the strongest argument for whatever captures
free text next.

### Removed — the settings the three removals left behind

Deleting a feature and deleting its knobs are two jobs, and only the first had
been done. What was still on screen or in the environment after the chat, the
signals domain and the day context went:

- **`VITALS_TELEGRAM_BOT_TOKEN`, `_CHAT_ID`, `_WEBHOOK_PATH`, `_WEBHOOK_SECRET`**
  in `.env.example`, plus the setup section and the environment table in the
  README that told a new installation to fill them in. Nothing had read them
  since the transport went. Every remaining `VITALS_*` in `.env.example` is now
  checked to have a reader.
- **The evening block's time field**, on the settings card and in the stored
  policy. Revision 0059 rewrites the rows, and that is not cosmetic: the policy
  decoder compares a stored row's key set against the code's with `!=` — on
  purpose, because a preference that has drifted is worth failing on — so any
  installation that had ever saved its proactive settings would have come back up
  raising on every read.
- **The week template** — seven rows of dropdowns asking what each weekday is
  usually like. Its inputs stopped being passed to the template when
  `day_context` went, so `/settings` had been rendering a heading, a hint about
  "the evening block", and an empty box beneath them. No test saw it; the page
  still returned 200.
- **The proactive layer's module gate.** `prefs.bot_enabled` read the `signals`
  key out of the enabled-modules map, and `signals` had left the module registry,
  so it could only ever return `False`. Nothing was silently off today only
  because nothing can send at all yet — it would have been the first thing to
  break when web push landed, and it would have broken as silence.
- **The CSRF exemption for `/tg/`**, along with its two stale test-inventory
  entries. A prefix that waves requests past the origin check for a route nobody
  has mounted is a door held open for whatever is mounted there next.
- **42 dead i18n keys** in both languages — the whole `/signals` page vocabulary,
  the week-template labels, the weekday names that only it used. The i18n tests
  check that every referenced key exists, not that every key is referenced, which
  is exactly how they accumulated.

And the strings that were still true-sounding but wrong: the brief card named two
environment variables that no longer exist and offered to send "to Telegram"; the
empty state promised briefs "at 11:00"; the parser model was labelled as parsing
signals. The AI report prompt was the one with teeth — it still described a
`day_context` key in both languages, so the model was being told about data the
context could not carry. A test now asserts neither prompt names it.

### Added — a professional sees the record, not only the notes about it

The patient screen showed notes and plans and nothing else. The policy already
granted the whole record — a doctor and a trainer are given the same domains,
because the kind decides who is writing and not what may be read — so a doctor
opening a patient had permission to see everything and was shown almost nothing.

It now renders the record as per-domain summaries: weight and its trend, flagged
labs, nutrition averages, the active protocol, supplements, and the rest.

- **Consent is applied as a whitelist.** The first attempt filtered by asking the
  report assembler to leave out the domains the patient withheld, which is wrong
  in a way a demo would not show: that assembler forces every *core* section on
  whatever it is handed, weight and labs among them, so a withheld weight would
  still have been rendered. The view is built out of the permitted sections
  instead, so a section this screen has not thought about yet cannot leak either.
- **What the patient withheld is named**, not quietly missing. A clinician
  reasoning from a partial record has to know it is partial; a gap they cannot
  see reads as "nothing there" and gets reasoned from. Sections the patient does
  not use are not named — that is not about this professional.
- **The card is dated.** It shows the same closed period every report here uses,
  completed days only, which means the latest reading can be a day behind the
  patient's own dashboard. Undated staleness is a defect; a named period is a
  report.

### Fixed — a doctor's screens had no navigation at all

Every page added for professionals omitted one variable the base template hides
the entire chrome behind, so the rail, the bottom bar and the sign-out button
vanished on exactly the screens a doctor lives on. A doctor is sent there from
their own dashboard, which meant landing on a page with no way anywhere.

With the chrome back, the second half showed: it offered Today, every module
section, Share and Settings to an account with no health record of its own, and
each of those bounces straight back. They are hidden now. Sign-out stays — a page
with no way out is a trap.

### Changed — every page answers in a shared installation

Twenty-five of twenty-seven pages return 200 with ten patients, two doctors, two
trainers and an operator in the database. At the start of this work eight did.
The two that do not are not gates: `/settings/export` says that backup format v1
describes an installation holding one person and names the per-subject export
that does work, and `/external/summary` says the external API is switched off.

Five compatibility bridges were retired, all with the same correction. Each
demanded a sole health subject whenever it was *asked for*, when what it exists
for is adopting rows that belong to nobody — and only if such a row is actually
there does it matter that there is more than one person it could belong to. Two
questions had been fused into one, and the fused version stopped pages that were
asking nothing of the bridge.

- **Alerts** — opened `/nutrition`, `/supplements`, `/genetics`.
- **The conflict engine**, the largest at seven pages — opened `/labs`, `/hrt`,
  `/skincare`, `/glp1`, `/interactions`. It widens nine predicates and seven of
  them cannot match a row on a current schema at all: they test `subject_id IS
  NULL` on columns revision 0049 made `NOT NULL`. The engine does not know its
  domains, so each domain registers the probe beside the widening it mirrors —
  a probe looser than the predicate it guards would let a row be adopted with
  nobody having decided whose it is.
- **The Garmin weight outbox** — opened `/weight`, `/weight/measures`,
  `/settings`.
- **The weekly digest** and **the share bridge** — opened `/today`, `/reports`,
  `/share`.

Where the correction is load-bearing: for alerts, the effective bridge is
*returned* and every caller uses the returned value, because skipping the proof
while the query still widened is the one combination that could show one person
another's alert. And only the subject *count* turns on the probe — the
owner-lifecycle and actor-ownership checks stay where they were, since they ask
who may use the legacy path at all, which is a permission and does not stop
being one because there is nothing to widen.

The refusals are kept for what they were written for: an unowned row plus two
people still closes every one of these. The way out is unchanged and already
shipped — `scripts/backfill_*_subject_ownership.py`, run while the installation
is still one person, which is exactly when adopting an unowned row into that
person is right. A new installation never had such rows and never pays the proof.

### Fixed — a doctor could not reach their own patients

Found by signing in as one. Every personal page told them the app did not
support several records yet — untrue, and unactionable. A doctor or a trainer
keeps no health record of their own, so a page about "your weight" has nothing
to be about for them.

Behind the message was the real defect: the navigation decided whether to offer
the patient roster from the chrome scope, and that scope is empty for anybody
who owns no record. The roster link was therefore hidden from exactly the people
who have a roster, and a doctor signing in had no way to reach their patients at
all. It is now asked about the signed-in account, which is what the question was
always about. A professional who holds patients lands on their roster; one who
holds nobody is told plainly.

### Fixed — five pages served a stack trace where they meant to say "not yet"

Found by walking the app in a browser against a ten-patient installation, and
now walked by a test that does the same thing.

- `/nutrition`, `/supplements`, `/skincare`, `/genetics` and `/settings`
  answered **500** in a shared installation. Refusing was correct — nothing was
  written, no other person's row was read — but the refusal had nowhere to land:
  four bridge exceptions were never registered on the handler that turns them
  into a clean 409. A refusal that arrives as a crash sends whoever meets it
  looking for a bug that is not there.
- The cause was two lists that drifted. One tuple named the bridge refusals at
  startup, a stack of decorators named them again per request, and a type added
  to one was missing from the other. There is now one tuple, registration loops
  over it, and the log names *which bridge* refused as well as the route.
- `GET /settings/export` crashed the same way for a different reason: backup
  format v1 describes an installation holding one person, and this one holds
  several. That is a limit of the format, not a fault, so it is now a 409 that
  names the export which does work — `/settings/export-subject` carries one
  record and restores on its own. The refusal got its own type,
  `MultiSubjectBackupError`, because a router cannot tell it apart from a
  malformed-file error by reading a translated string.
- A subject with no notification-policy rows was read as *corrupt* rather than
  *unconfigured*. Only the legacy owner's rows are seeded at startup, so
  `/settings` crashed for everybody else. Missing partitions now fall back to
  the defaults on the human read; the write paths still require all three,
  because there a missing one means a half-written split.

`tests/test_shared_installation_pages.py` is the durable version of that browser
minute: it walks every page against a two-subject database and asserts none
answers 500, and separately pins exactly which pages still refuse — in both
directions, so the backlog cannot go stale while nobody is looking.

### Changed — the unowned-alert bridge asks what it is for, not how many people there are

- The bridge widens exactly one predicate, `subject_id IS NULL AND
  integration_connection_id IS NULL`, and demanded a sole health subject
  whenever it was requested — including when no such row existed anywhere. Two
  questions had been fused. *Is there an alert nobody owns* is what the bridge
  exists to answer; only if that is yes does *is this installation one person*
  have to hold, because with two people nothing can say whose it is.
- `_prepare_context` now answers the first under the governance lock it already
  takes, and **returns the bridge that actually applies** — `REJECT` when there
  is nothing to adopt. Every caller uses the returned value. That is the
  load-bearing part: skipping the proof while the query still widened would be
  the one combination that could show one person another's alert. The predicate
  and the proof cannot disagree, so an unowned row committed a microsecond later
  simply is not selected.
- The refusal is kept for the case it was written for: an unowned row plus two
  people still closes the bridge. The way out is unchanged and already shipped —
  `scripts/backfill_system_alert_subject_ownership.py`, run while the
  installation is still one person, which is exactly when adopting an unowned
  row into that person is right.
- Opened `/nutrition`, `/supplements` and `/genetics`; `/garmin` and `/hevy`
  opened too, once seeded subjects got the integration roots the app creates
  only for the legacy owner.

### Fixed — the settings gate stops closing the app, and starts guarding what it meant

First of the 36 sole-subject gates, and the one behind the module map — which
half the pages read before they render.

- Every setting here has two representations: a scoped row belonging to one
  user, subject or connection, and one global `app_settings` key belonging to
  the installation. **Only the second stops meaning anything when there are two
  people.** With two subjects "the module map" is not a thing that exists, and
  the shared row is nobody's in particular.
- The bridge was refusing the whole operation. It now stops the half that
  actually needs a sole subject: reads fall through to the caller's default
  rather than to a shared value that is not theirs, writes land in the scoped
  row and skip the mirror, and adopting the shared value into one scope stops
  entirely. Mirroring one person's choice into the global key would hand it to
  everybody still reading the fallback — that is the property the old refusal
  was really protecting, and it is now asserted directly.
- `scripts/seed_care_demo.py` builds a populated shared installation to look at:
  an operator, two doctors, two trainers, ten patients, and care in every state
  the screens draw — open, awaiting consent, paused, revoked, ended, and an
  offer nobody has taken up. It prints a signed session cookie per account,
  because the password login authenticates exactly one username from `.env` and
  inventing a second way in for testing would be worse than printing a cookie in
  a dev script. Refuses to run against anything but a local SQLite file.

### Fixed — the app could not be run at all by the installations PR-07 was for

Found by opening a browser, which no test in this repository does: every web
test runs against a database holding exactly one health subject, so none of
this was reachable from the suite.

- **The process refused to start.** The startup lifespan reconciles the `.env`
  credential with the durable identity, materializes the owner's module map and
  seeds their hormone panel — all through sole-owner bridges that fail closed on
  a second health subject. Correct while that state was impossible; PR-07 made a
  second subject the point, and the result was that the professional features
  could not be deployed by anybody who used them. Both steps now skip with a
  warning: there is nothing to reconcile in a shared installation and nothing to
  lose by not trying.
- **Then every page answered 409.** `/today`, `/weight`, `/labs`, `/settings`,
  `/more` — the whole existing app. `resolve_legacy_ownership_context` asked
  whether the database held exactly one subject, when the check right below it
  already required the actor to *own* the subject; the count was standing in for
  a relationship it could have asked about directly. It now selects the actor's
  own record, which is both narrower and structural — a stranger matches no row
  rather than loading one and being compared against it.
- That is one gate. **There are 36 more**, across 14 service modules, each its
  own sole-subject bridge with its own reasoning about what it guards. They are
  being retired one at a time rather than in a sweep, and the list is in
  `web/main.py` beside the handler that turns them into a 409 rather than a 500.
  A shared installation today reaches `/care` and `/settings/care`, and little
  else.
- Verified in the browser both ways: a single-subject installation — the one
  actually deployed — serves every page with no console errors, and a
  two-subject one now boots and serves the care pages.

### Fixed — the browser was keeping a record it is no longer allowed to see



- **htmx caches a snapshot of every boosted page in `localStorage`**, under
  `htmx-history-cache`. Nothing configured it, so on a shared machine a
  professional's patient list — or a patient's own record — sat in the browser
  after the session allowed to see it had ended. The care and consent pages now
  carry `hx-history="false"`: one extra request to re-fetch, against a copy that
  outlives the session.
- The login page drops `htmx-history-cache` and the diagnostics buffer. Anybody
  looking at that page is by definition not in a session, which covers logging
  out, a session expiring, and somebody else sitting down — three routes to the
  same page and one thing to do about them.
- The render-diagnostics ring buffer recorded `location.pathname`, which on a
  care page is a patient's id. It exists to diagnose render stalls and does not
  need to know who was opened, so the path is redacted before it is stored.

### Fixed — the page chrome stops breaking when a second person exists

- The nav rail, the language and the status card resolved their subject through
  `resolve_legacy_ownership_context`, which is deliberately fail-closed on
  "exactly one health subject in the database". Correct for a write path, wrong
  for chrome: every request in a two-person installation logged an exception and
  rendered the defaults.
- They now resolve the **signed-in account's own record**, which is what they
  always meant. A doctor reading a patient's notes still has their own modules
  switched on and their own language; the patient's settings are the patient's.
- **This removed protection that was there by accident.** A foreign supplement
  toggle used to answer 404 because the broken chrome made every module read as
  off, so the request died at the module gate before the route saw the
  client-controlled id. With the chrome working, the refusal now comes from
  where it should — the route's own adapter — and a new handler turns it into a
  409 saying the page does not yet support more than one record, rather than the
  500 an unhandled exception produced. Logged at warning: a route reaching that
  handler is one still to be ported to `resolve_access_context`.

### Added — the patient's side of the same pair (PR-08)

- `/settings/care` is where a patient sees who holds their record, what each of
  those people can see, and how to stop it: pause, resume, withdraw, end, and
  invite. Withdrawing takes effect on the professional's next request.
- The subject is resolved from *who the patient is* rather than from the path —
  the mirror image of the professional routes, and the same rule underneath:
  the subject comes from whichever source cannot go stale. A patient has one
  record and nothing to select.
- **The invitation link never enters a URL the browser keeps.** The page is
  rendered straight from the POST rather than redirected to with the token in a
  query string: a URL ends up in history, in the access log and in the next
  page's referrer, and an invitation link is a capability. It is shown once, and
  there is no page that can show it again because only its hash is stored.
- `GET /care/accept/{token}` asks; `POST` accepts. A one-time link must not be
  spent by a browser prefetch, a link preview or a mail scanner — none of which
  involve anybody having decided anything, and any of which would burn an
  invitation the intended person could then never use.
- Accepting establishes care and opens nothing. Being in care and having agreed
  to show something are the patient's two separate decisions, and accepting on
  their behalf would be making the second one for them.

### Added — the professional's side, and the patient it is about (PR-08)

- `/care` lists the records a professional currently holds; `/care/{subject_id}`
  opens one, with their own notes and plans.
- **The selected patient travels in the URL, never in the session.** That is the
  design rather than a URL style, and the reason is a failure that cannot be
  tested away: a professional opens patient A, leaves the tab, selects patient B
  elsewhere, comes back and submits the form still on screen. With the selection
  held server-side that write lands on B — silently, with A's data in it,
  nothing about the request looking wrong, in a record whose owner was never
  involved. With it in the path the stale tab posts to the patient it was
  rendered for, and if consent has since been revoked it is refused instead. A
  test drives exactly that sequence.
- Nothing is cached across requests. The relationship, the consent and the
  policy decision are resolved per request, which is what makes a revocation
  take effect on the next one rather than on the next login.
- The page says why it is open. Somebody looking at another person's medical
  record can see on what grounds without asking anybody — a screen that cannot
  say why it is open is one nobody can audit by looking at it.
- Uniform refusals: a subject that does not exist, one the caller holds no
  relationship with, one whose consent is paused and one whose consent lapsed
  are four different facts and one 404. Told apart, a professional could ask
  "is this person a patient here?" one id at a time. A caller with no session at
  all still gets the login redirect — that rule is about telling *authenticated*
  callers apart, and answering 404 there would only hide the login page.
- The roster shows a paused relationship as paused rather than omitting it:
  "gone" and "on hold" are different things to tell a professional. It is listed
  and named, and it is not a link, because the record is genuinely closed.

### Fixed — validating a lake against the schema it actually has

- The Stage-4 whole-lake validation and two rehearsals derive their inventory
  from `Base.metadata`, which describes the schema at *head*, and run against a
  lake at the pre-contract revision. Every table PR-07 adds is created after
  that point, so the validation started querying relations that were not there
  yet and the audit CLI reported `internal_error` where the contract says
  `dependency_error`.
- All three now filter to the tables the database in front of them actually
  holds. That is the right rule rather than a workaround: a table introduced
  after the contract is created with its ownership mandatory from the first row,
  so it has no unowned history to prove.

### Added — where a professional's contribution goes (PR-07)

- `professional_notes` and `care_plans`, plus
  `care.records`. A doctor's reading of a lab panel is not the
  lab panel: keeping them apart is partly about the record — a year later the
  two would be indistinguishable — and partly about permission, because a
  professional's thinking living inside the patient's measurements would mean a
  professional needs to be able to write into them.
- Three rules run through it. **Only somebody in live care may write**, which
  means the same pair — relationship and consent, both live — that everything
  else needs; a patient who narrowed a consent to reading has not agreed to be
  written about. **Only the author may change what they wrote**, and the author
  condition is in the query, so another professional's note is not found rather
  than refused. **Nothing is deleted**: a clinical note that can disappear is a
  worse record than one that stays and is superseded, and a plan that can vanish
  is one the patient cannot hold anybody to. A test asserts that structurally
  rather than trusting the paragraph saying so.
- Reading is shared; editing is not. A second professional joining a case sees
  what the first concluded, and the patient sees everything written about them.
- Consent defaults separate the two kinds of *resource*, which is the line that
  does the work: patient facts stay read-only for both kinds of professional,
  while the professional's own artifacts carry create and update, because that
  is where the read-only rule sends them. Delete is absent from both.
- Revision `0057` adds the two tables with row security. Each row carries three
  references: `subject_id` (whose record it sits in, which row security reads),
  `actor_user_id` (who wrote it, which is what stops a second professional
  editing it), and `relationship_id` (the care it was written under, which makes
  it reviewable). The relationship is `RESTRICT` and reads are by subject: care
  ends and the note does not, and a record that became unreadable when care
  ended is exactly the record a patient needs afterwards.

### Added — care relationships and versioned consent (PR-07)

- `care_relationships` says a professional is in care for a patient;
  `consent_grants` says what that patient agreed they may see. **Access needs
  both, live, at the moment of the request.** `resolve_access_context` now loads
  the pair into the `RelationshipGrant` the policy engine has been able to
  evaluate since PR-04 and has never been given.
- The asymmetry is deliberate. A relationship with no live consent is an
  ordinary, correct state — somebody the patient agreed to work with and has not
  yet, or no longer, agreed to show anything to.
- **Consent is versioned rather than edited.** Narrowing what somebody may read
  is a new version superseding the old, so "what was this professional allowed
  to see on the day they read it" stays answerable; an updated row cannot answer
  that, and it is the question any later dispute is about. Exactly one version
  per relationship may be live, enforced by a partial unique index rather than a
  convention, because two live versions would mean the wider silently wins.
- **Both kinds are offered the whole record.** The separation between a doctor
  and a trainer is not what each may look at — it is that they are two different
  people, with two relationships and two sets of their own notes. Splitting the
  domains by kind would make the narrower choice the one a patient has to know
  to ask for, and the patient already chose whom to invite. The offered set is
  derived from `Domain` rather than written out, so a module added later is not
  silently invisible; `SYSTEM` is excluded because it is the installation's own
  operational state rather than anything about the patient.
- **And the kind is a fact rather than a label.** A professional whose profile
  says doctor cannot be taken on as somebody's trainer. Without that check the
  kind on a relationship is only what the patient happened to type into the
  invitation. It applies where there is something to check against: a
  professional who has filled in no profile has claimed no kind. Requiring a
  profile — or a verified one — before care can start is the natural next step
  and a decision about onboarding order rather than a technical one, since it
  would hold every new professional at the door until an operator reached them.
- **Every default is read-only.** A patient's record is theirs; a professional's
  contribution belongs in their own note rather than inside somebody else's
  measurement. A test pins that no default carries create, update, delete, share
  or export for either kind.
- Pause and revoke are different operations. A pause is the patient taking a
  break and resuming must not cost a new invitation; a revocation does not come
  back. Ending the relationship revokes every consent under it, because a live
  consent behind an ended relationship is a permission waiting for somebody to
  re-establish the pair.
- Revocation takes effect on the next decision — not the next login, not a cache
  expiry — because the grant is loaded per context and the policy re-checks the
  actor, lifecycle, expiry and exact scope every time.
- Every relationship is resolved *inside* the actor's scope — the ownership
  condition is in the `WHERE`, not a check after the read — so somebody else's
  relationship does not exist rather than being refused. Told apart, "not yours"
  and "no such thing" would let a caller enumerate relationships by trying ids.
  The bare-key ratchet in `vitals/legacy_scope.py` is what caught the first
  version of this, which fetched by id and asked afterwards.
- Revision `0056` adds the three tables, each with row security carrying both
  clauses. The consent tables repeat `subject_id` rather than reaching it
  through a join: a row reachable only by joining is a row outside the policy
  protecting everything else of that patient's.

### Added — one link, one professional, one record (PR-07)

- `professional_invitations` plus `care.invitations`: a patient offers one
  professional a way into their record, as a token in a link. Most of the design
  is a list of things a link must not be.
- **Not a bearer capability that outlives its purpose** — it expires (14 days by
  default) and is one-time.
- **Not usable by whoever it was forwarded to** — it is bound to an address, and
  the address must be a *verified* claim presented by the accepting session. An
  unverified address is somebody asserting they own a mailbox, which is exactly
  what the binding stops. The verified address is a parameter rather than a
  column read: after the federated cutover an address is a claim the provider
  makes at sign-in, and a service reading a stored one would be trusting
  whichever half of the system wrote it last.
- **Not reconstructible from a database copy** — only the token's SHA-256 is
  stored, so a dump of the table is not a set of working invitations.
- **Not informative when it refuses.** Spent, expired, revoked, addressed to
  somebody else, and never-existed all raise the same thing with the same
  message — a test pins that the five produce exactly one string. Told apart,
  they would answer "is this address being treated here?" one address at a time.
- Accepting grants nothing. It creates the relationship half of the pair;
  consent is the other, and a test pins that an acceptor with no consent is
  still refused every action on the record.
- Revision `0055` adds the table with row security carrying both clauses.
  Accepting runs in the platform scope — the professional is not bound to this
  subject yet, and the token is what authorizes reading the row at all — so
  `care.invitations.accept` joins the enumerated allowlist.

### Added — a professional's claim, and an operator deciding about it (PR-07)

- `professional_profiles` holds what somebody claims about themselves: a name, a
  licence number, a kind. `care.professionals` is the operator workflow that
  decides whether the claim checks out.
- **None of it grants access to anything, and that is the design.** Verification
  answers a question about the world outside this installation — is this person
  a doctor at all. Consent answers a question about one patient's record. If
  verification implied access, one operator approving one licence would admit
  that person to every record in the installation and no patient would ever have
  been asked. The table therefore has no `subject_id` and no row security: there
  is nothing here that belongs to a patient.
- Two states are constrained rather than merely recorded. `verified` requires
  both a timestamp and a reviewer, so a claim cannot verify itself, and nobody
  reviews their own. `rejected` and `suspended` require a note — the
  professional needs to know what to fix, and the next operator needs to see
  what the last one concluded.
- Suspending is reachable from `verified` and withdraws the stamp. A licence can
  lapse after the fact; deleting the profile instead would erase the trail of it
  ever having been approved, which is what an audit of a withdrawn licence needs
  most.
- Revision `0054` adds the table.

### Changed — private files stop being addressed by their path (PR-06)

- **`GET /files/{opaque_key}`** replaces `/static/uploads/{key}`. The key is
  `FileAsset.opaque_key`: a UUID with no relationship to the bytes, unique
  across the installation, and rotatable — replacing it revokes every link that
  ever leaked, without touching the row it belongs to or the file on disk.
- The asset is the authority. It names the subject and the lifecycle state, and
  the subject is part of the lookup rather than a check afterwards, so a key
  belonging to somebody else is *not found* rather than refused. Malformed key,
  unknown key, another subject's key, deleted, purged, and bytes missing from
  disk are six different facts and one identical 404 — telling them apart is
  exactly the oracle somebody holding a guessed URL would use.
- Two checks the old route needed are gone with what they defended against. A
  `labs/` prefix used to imply a purpose, so metadata claiming a different one
  was inconsistent with where the bytes sat; there is no prefix left to
  contradict. And `uploads/labs/x` and `labs/x` were one file with two
  spellings, so two metadata rows could claim it and disagree about whether it
  was deleted; a key names a row, not a path. The alias is still refused on
  deletion, where two rows pointing at one file is still a real problem.
- A malicious file name no longer reaches the page at all. `uploads/synthetic-
  ');window.photo_pwned=1;('-.png` was rendered into an Alpine expression's data
  attributes, where escaping was the only thing between an uploaded file name
  and a same-origin script. The name is not in the HTML now.
- `/static/uploads/{key:path}` remains as a seal: 404 for everybody, with no
  session dependency, so the static mount can never reach the private tree. A
  test pins that it is registered ahead of the mount. The bytes still live under
  `web/static/uploads`; moving them to a private root would make the guarantee
  structural rather than a matter of route ordering, and is follow-up work.

### Added — restoring one record without touching anybody else's (PR-06)

- `POST /settings/import-subject` deletes and reloads exactly the caller's
  subject. `/settings/import` empties every portable table and is correct only
  for a whole-database backup; running it per person would take the installation
  down to restore one record.
- **Primary keys are not preserved, and that is forced rather than chosen.** All
  39 portable tables number their rows with an integer sequence, so one
  subject's row 5 and another's row 5 both exist. Carrying ids across would
  collide with rows the operation is not allowed to touch. Rows are inserted
  fresh and the 17 references between them are rewritten through a map built as
  each parent lands — which is why the walk is in foreign-key order.
- **A reference can point out of the file.** The installation's shared catalog
  lives under a NULL subject and a personal export does not carry it: the
  receiving installation seeded its own and numbered it its own way, so the
  integer would either dangle or land on an unrelated row holding that number.
  Those references travel as the target's natural key instead. One that does not
  resolve is refused — a dose that quietly forgot which compound it recorded is
  worse than an import that did not happen.
- A travelling name always came from the catalog, so the catalog is asked first;
  a personal row of the same name is the fallback, for the cross-installation
  case where the receiver does not have that entry and the person recreated it.
- The reference map is derived from the schema rather than listed, so a
  reference added later cannot silently become one installation's id pasted into
  another. A contract test pins that every table a reference can point *out* to
  has a portable name.
- Ownership is assigned by the boundary and never read from the file — a file
  that names a subject outright still lands in the caller's own record.
- Each import refuses the other's file: a full backup loaded per-subject would be
  silently truncated to one subject's worth of itself and look like a successful
  restore.

### Fixed — the settings page stops offering a sign-in it no longer owns

- After the OIDC cutover every route behind the sign-in card answers 404, but
  the card itself still rendered: a password form and a second-factor enrolment
  that could not happen.
- Worse, the page read the 2FA state unconditionally. A half-finished enrolment
  from before the cutover still reads as `pending`, so its live TOTP secret and
  QR code were painted onto a page whose buttons could no longer act on them.
  The state is not read at all now when the provider owns sign-in.
- The card is replaced by one line saying where to change these things.

### Added — a personal export, separate from the installation's backup (PR-06)

- `GET /settings/export-subject` answers "what is mine". The existing
  `/settings/export` answers "what is in this installation" and is an operator's
  file; conflating the two is how a personal download ends up carrying things
  that are not personal.
- Three things it deliberately leaves behind. `app_settings` is the deployment's
  configuration — the scheduler's timezone, which modules are on — so carrying
  it would make a personal file a way to reconfigure whatever imported it. Rows
  with a NULL subject are the installation's curated catalog, including the
  conflict rules, and the receiving installation seeds its own; shipping a copy
  would let one person's file overwrite another installation's safety rules.
  Ownership and private-storage columns are suppressed exactly as the full
  backup suppresses them, because those are assigned by a trusted boundary on
  the way in and never read from a file.
- The two kinds are now named in the envelope, and `import_full` refuses a
  personal export. It replaces every portable table for everybody, and a
  personal export is valid JSON with the same envelope and overlapping table
  names — so without the check it would load, emptying the database and putting
  one person back into the hole.
- Still to come: the matching subject-scoped *import*. `import_full` deletes
  each portable table unqualified, which is safe today only because the
  compatibility resolver refuses a second subject. Scoping the wipe is not the
  hard part; every portable table has an integer primary key, so a per-subject
  restore cannot preserve ids the way the full one does and needs an id remap
  across the 16 references between those tables.

### Security — a restore and a restart are operator work (PR-06)

- **`/settings/import` had no authorization beyond holding a session.** Its
  paired `/settings/export` was policy-decided; the half that *replaces the
  database* was not. `/settings/restart` had none either — any account could
  stop the process.
- Both now go through `require_installation_operator`, which asks a question the
  subject-scoped policy engine cannot: these operations have no subject. A
  platform superadmin is an operator; while the installation holds exactly one
  subject, that subject's owner is one, because on a self-hosted install they
  are; and a second subject closes the operations until somebody holds the role.
- Refusing is deliberate rather than a gap. A full restore wipes portable tables
  for everybody, so it cannot be made safe per-subject, and guessing which half
  of the database the caller meant would be worse than saying no.
- The shape is the point. Passing the caller's own subject into `is_allowed`
  would have read as a check while being unconditionally true — self-ownership
  authorizes everything on one's own subject — so every account would have
  inherited the restore button. That trap is pinned as a test.
- Today the second-subject clause is not yet what closes these routes:
  `resolve_legacy_ownership_context` refuses a second subject first, so they
  currently fail with a resolution error. That is an accident of the
  compatibility resolver, and it disappears when `resolve_access_context`
  replaces it — at which point the answer is still no, for a stated reason.

### Fixed — work that belongs to nobody in particular

- **Every published doctor link would have answered "not found".** Revision 0050
  put row security on `shared_reports`, and the visitor who opens a published
  link has no account: nothing binds `vitals.subject_id`, the policy's
  comparison is NULL, and the report the token names is invisible — reading
  exactly like a revoked link. Four housekeeping jobs had the same shape, each
  sweeping across every subject with no person to act as.
- Revision 0053 rewrites all 51 policies to admit a second, explicit scope:
  `vitals.platform_scope`. It is transaction-local like the subject binding, it
  has to be asked for by name through `enter_platform_scope`, and only the exact
  string `on` opens it — a stray or truncated value closes.
- Five call sites use it, and a contract test enumerates them, so a sixth is
  something a reviewer sees rather than something that accumulates. Reaching for
  it to make an empty page non-empty is the misuse it guards against: for a path
  that merely forgot to resolve its subject, seeing nothing is correct and the
  fix is to bind.
- `tests/test_scheduled_job_scope.py` is the guard against the next one. Every
  registered job is classified as either binding a subject or acting for the
  installation, and a new job fails the test until somebody decides which — the
  decision that was never explicitly made for the three that broke. It matters
  more for jobs than for routes because a job that reads nothing does not error:
  it finishes, commits, and reports success while the scheduler stays green.
- Worth naming why the suite was silent about this. Both suites build the schema
  with `create_all`, which knows about columns and constraints and nothing about
  policies — so 4555 passing tests said nothing at all about row security.
  `tests/test_row_level_security.py` is the exception, and the only place the
  boundary is actually exercised: schema from the migrations, connection from a
  role with no `BYPASSRLS`.

### Added — federated authentication (PR-05)

- **Vitals stops authenticating anybody.** `vitals/services/authentication/oidc.py` verifies
  what a provider hands back: the issuer verbatim in the discovery document, the
  token and the authorization response (RFC 9207); the audience and `azp`; the
  nonce; the state; PKCE with S256 only; token times; and `auth_time` when an
  operation demands freshness. Signing keys are fetched with the same HTTP
  client as everything else, so a provider that hangs fails a login rather than
  hanging it.
- Email and display name are read for display and are never lookup keys. A
  provider may let somebody claim an address later, and matching on it would
  hand over the whole record.
- Session cookies gain a version 2 carrying the local user id, the session
  version and the provider's `auth_time`. `authentication.sessions` confirms each
  against the account on every request, so bumping one row revokes every session
  that account holds — with no server-side store to grow and go stale.
- Provisioning is closed: a valid login by somebody with no account is a
  refusal. The one exception is a one-time binding for an installation that
  predates federated login, driven by a subject an operator reads from the
  provider's own console.
- **The cutover is switched by `VITALS_OIDC_ISSUER`, not by deploying.** While
  it is unset the existing login works; setting it turns `/login` into a
  redirect to the provider and makes the password and TOTP paths 404. Second
  factors become the provider's business, which is where password hashing,
  reset, recovery codes and rotation already live.
- Revision 0052 adds `user_federated_identities` and makes `users.password_hash`
  nullable.
- CSRF gains a third barrier. The `Origin` check had a real gap — a request
  carrying no `Origin` at all passes it, because "absent" and "same-origin" look
  the same from the server. `Sec-Fetch-Site` names the relationship instead of
  leaving it inferred, is sent by every current browser, and cannot be set by a
  script, so where the two disagree the unforgeable one decides. Reads stay
  unaffected, which is what lets the provider's cross-site redirect reach the
  callback.
- ZITADEL runs behind a compose profile, with its own database and volume:
  bringing it up is the cutover, and that has to be a decision rather than a
  side effect of a routine restart. `docs/OIDC_SETUP.md` is the order to do it
  in, including how to get back in if the provider is down — and a test checks
  the document against the variables the code actually reads.


### Removed

- Ten public service functions with no caller anywhere: a digest-owner alias, an
  owned Garmin daily reader, the three quarantined legacy weight-export hooks,
  a genetics update path the router never used, a Telegram chat-id helper, a
  preferences alias, an upload-connection guard, and a progress-photo reader.
  Each had been superseded rather than removed when its replacement landed.
- Two dead translation keys per language. Both were defined twice in the same
  dictionary, so the second silently won and the first had never been seen.

### Fixed

- `web/main.py` referenced `HTMLResponse` without importing it, so a browser
  refused by the policy engine would have crashed instead of receiving a 403.
  The test only exercised the JSON branch; it now covers both.
- The MCP `get_measurements` tool never imported the service it calls and would
  have raised `NameError` on any invocation.
- `web/routers/oauth.py` imported `SESSION_COOKIE` from two modules, the second
  shadowing the first.
- `vitals/services/proactive/prefs.py` and `upload_ownership_service.py` listed
  names in `__all__` that no longer exist, breaking `import *`.

### Added — tooling

- `ruff.toml` selects the correctness rules — undefined names, unused imports,
  `__all__` entries with nothing behind them, duplicate dictionary keys — and
  deliberately does not enforce formatting. `tests/test_lint_contract.py` runs it
  as part of the suite, so a defect it can see fails a test run rather than
  waiting for somebody to remember the command.


### Added — scoped services (PR-04)

- **The bare-key ratchet reached zero too.** Every subject-owned row is now
  resolved inside the caller's scope rather than by a primary key, which proves
  nothing about who it belongs to. Both inventories assert equality rather than
  a bound, so reopening either means deleting an assertion.
- `app_settings` was classified `MIXED` — a claim the schema cannot satisfy,
  since it has no subject column. Corrected to `NONE`, which is what a
  string-keyed installation-wide store is.
- `alerts_service.override_alert` is deleted (no caller; both live surfaces use
  the scoped `legacy_subject_alerts.override`) and `resolve_alert` takes a
  mandatory subject.
- `hrt_service.set_compound_active` sees only curated compounds. A bare key
  could reach a subject's own custom compound and flip a global flag on a row
  that is not global.
- Six `RawPayload` reads across labs, body composition and the Telegram inbound
  path are scoped. Each was on the way to stamping `processed_at`, which is a
  write — a payload outside the caller's subject is now missing rather than
  mutated.

- **Revision 0051 finishes row security.** The ten tables with a nullable
  subject get a policy too, in two groups: the catalogs and the platform's own
  rows share (*mine or the installation's*), the inherited children hide (*mine*
  only, so a row the backfill has not reached stays invisible). Every table with
  a `subject_id` column is now covered by one revision or the other, and a test
  asserts there is no third state.
- Which group a table joins follows from the ownership registry — and for an
  inherited child, from its parent's classification.
  `hrt_compound_components` shares rather than hides because its parent is a
  mixed catalog, so a component of a curated compound belongs to nobody.

- **The policy engine has a caller.** `vitals/access.py` had been complete and
  unused since PR-02; `access_resolution.resolve_access_context` now builds a
  real `AccessContext` and `require_access` decides one exact resource and
  action through `is_allowed`.
- A second person's record becomes ordinary denied access instead of an error
  about the database's cardinality: the resolver selects the subject by
  ownership rather than by there being only one.
- Resolving a context authorizes nothing, and entering the database scope is a
  separate step — asking whether you may reach somebody's record must not
  require entering it first.
- The export routes are the first decided by the engine rather than by being
  logged in. `AccessDeniedError` maps to 403 and reveals nothing about whose
  record was reached for or whether it exists.

- The ownership cutover is now proven as one operation. A revision-0034 lake
  with real data goes through all twenty backfill phases, the contract
  migration and the row policies in a single rehearsal — previously each third
  was proven alone, and the last two only against an empty or hand-stamped
  database.
- `vitals/ownership_deploy.py` names the phase order in the application rather
  than inside a test, `docs/OWNERSHIP_CUTOVER_RUNBOOK.md` is generated from it,
  and a test checks the document against the tuple.

- **Revision 0050 enforces subject isolation in the database.** Forty-one
  tables get `FORCE ROW LEVEL SECURITY` and a policy comparing `subject_id` to
  the `vitals.subject_id` session setting. An unbound session sees nothing
  rather than everything; a stranger's id opens nothing; `WITH CHECK` refuses a
  write addressed outside the bound scope.
- `rls_session.bind_session_subject` sets that value with
  `set_config(..., is_local => true)`, so it is discarded at commit and cannot
  ride a pooled connection into the next request. The subject is remembered on
  the session and re-applied when a new transaction begins, so a service that
  commits mid-work does not carry on against a policy that matches nothing.
  Rebinding to a different subject is refused.
- The binding happens in `resolve_legacy_ownership_context`, right after the
  subject is read and before the first read of a protected table.
- `system_alerts`, `conflict_rules` and the inherited children are deliberately
  excluded: they need "mine or the installation's" rather than "mine", which is
  a different predicate and needs its own review.

- **Revision 0049 makes ownership mandatory.** Thirty-nine `REQUIRED`
  references become `NOT NULL` in the database and in the models together, so
  the schema the fast suite builds and the one a real installation runs finally
  agree about who owns a row. This is the precondition for FORCE RLS: a policy
  comparing `subject_id` to the session's subject excludes a null instead of
  protecting it.
- The migration refuses before it alters anything, raising
  `OwnershipBackfillIncompleteError` with each table that is behind and by how
  much. `PRE_OWNERSHIP_CONTRACT_REVISION` records the deploy order in code:
  migrate to 0048, finish the backfill, then migrate to head.
- On PostgreSQL each column is proven with a `NOT VALID` check that is validated
  before `SET NOT NULL`, so the constraint lands without an ACCESS EXCLUSIVE
  table scan.
- `@pytest.mark.pre_ownership_contract` builds the older schema for the two
  kinds of test that must still write an ownerless row: the backfill services,
  whose input is an unstamped row, and the legacy-bridge readers that pin what a
  scoped reader does mid-backfill.

- **No production path writes an ownerless row any more.** Garmin's unscoped
  `sync`, `pulse`, `ingest_daily`, `ingest_intraday`, `ingest_activities`,
  `ingest_health_auto_export` and both reparse paths are deleted, along with
  Hevy's `sync` / `reparse_from_raw` / `reparse_pending` / `_upsert_workout` and
  `raw_payload_service.upsert_raw_payload` itself. Every one had an owned twin
  the live callers were already using.
- `pulse_job` loses its zero-subject arm: a pulse writes a day of somebody's
  watch data, and without a subject there is nobody to write it for.
- `genetics_service.store_raw_vcf` becomes a refusal. It already declined every
  scoped caller; underneath was the zero-subject arm storing an uploaded VCF as
  a payload belonging to nobody.
- The Garmin weight outbox refuses to write without a destination account. An
  outbox row is an intent to send a weight *to* one; the old bridge wrote a row
  addressed to nowhere and keyed on a bare date.

### Fixed

- `hevy_service.latest_notes` took no subject while both its siblings on the
  Hevy page did, so it called `_exercise_sessions` without the argument that had
  become mandatory — selecting any exercise raised `TypeError`.

- `PENDING_OWNERSHIP_CONTRACT_COLUMNS` starts the Stage-6 ratchet: the
  thirty-nine `REQUIRED` ownership columns still nullable, recomputed from the
  models by a contract test that fails in either direction. Reaching empty is
  the condition for the `NOT NULL` contract migration and FORCE RLS.
- Writing that migration first surfaced why it cannot ship yet: the unscoped
  `garmin_service.ingest_daily` / `ingest_intraday` / `ingest_activities` and
  `raw_payload_service.upsert_raw_payload` still create rows without the
  reference, so a `NOT NULL` would break the next Garmin and Hevy sync rather
  than protect anything. Those writers are retired first — see
  `docs/COMMERCIAL_OWNERSHIP_INVENTORY.md`, Stage 6D.
- Test fixtures for the owner's provider connections and private-file root
  (`garmin_connection_id`, `hevy_connection_id`, `legacy_connection_ids`,
  `legacy_file_asset_id`), so a test seeding vendor data or a progress photo
  names the connection and the file it means.

- **The conflict engine has no unscoped path left.** The seven
  `legacy_resolver=` registrations are gone, and with them the engine's second
  resolver arm, the `evaluate`/`enforce`/`enforce_day_end` entry points, and the
  seven unscoped domain readers they existed to call. A rule is now evaluated
  for one person or not at all.
- `register_domain_resolver` refuses a reader that does not take a keyword-only
  `scope` with no default. A resolver that would happily answer without a
  subject can no longer be registered, so the arm cannot grow back by accident.
- Unstamped rows stay reachable through `ConflictScope`'s explicit
  `FULLY_UNOWNED` bridge, which needs a subject to bridge *from* and which
  `evaluate_scoped` proves is still the installation's only one. That is the
  backfill's bridge and outlives PR-04.
- The writer inventory now asserts the functions are absent, not merely
  uncalled: a zero-call-site count would still pass with `enforce` sitting there
  waiting for its next caller.

- **Every legacy scope bridge is closed.** The registry that started PR-04 at
  168 bridged functions is empty, and its contract test now asserts equality
  rather than a bound: no service can accept an omittable subject again without
  deleting that line. This is the precondition for the `NOT NULL` ownership
  contract and for FORCE RLS.
- The last seven went together. `ai_gateway_service._ensure_nonoverlapping_period`
  now takes a mandatory subject paired with its model and refuses the two
  mismatches outright — a subject period without a subject, and a platform
  period with one. `hrt_service.set_compound_active` and `prefs.bot_enabled`
  keep `None` as a legal value but no longer as a default, so a caller has to
  say out loud that it means the installation-wide flag.
- `scoped_settings_service` swaps three optional ids for one mandatory
  `scope_id`. The old spelling let a caller reach any of the four entry points
  with every id left out and learn about it only at runtime; worse, it accepted
  two at once and picked. `expected_subject_id` keeps its own name because it is
  not a scope — it cross-checks which person a connection belongs to, and is
  refused outside connection-scoped keys.

- Closed the supplements catalog, the week template, the alert reader and the
  Garmin daily readers — nine bridges. `alerts_service.list_active` and both
  Garmin day readers now demand a scope instead of defaulting to "everything in
  the database": a report built for one person could quietly carry another
  person's warnings and another person's watch.
- `alerts_service.list_active(subject_id=...)` is mandatory and has exactly two
  readings — a person's id returns their alerts, `None` returns the platform's
  own installation-level alerts. The old default returned every row.
- `garmin_service.latest_daily` / `list_daily_between` take a required
  `subject_id`. A day the backfill has not stamped yet is simply not that
  person's day, so the range comes back short rather than borrowed.
- The Garmin silence nudge no longer has a zero-subject arm: "whose watch went
  quiet" is not a question the newest row in the database can answer. Its
  once-per-episode check already lives on the owned send path.

- Closed the module state, the daily brief, the Today screen and the HRT
  reminders together — thirteen bridges. Which modules are on is one person's
  preference, and the brief and the Today screen are assembled from that
  person's domains, so none of them has a meaning without a subject.
- The module cache is keyed per person, like the chart cache before it: a shared
  Redis key would serve one subject's module state to the next request from
  another.
- `brief.generate_brief` is retired to a refusal. It was the zero-subject
  injected-client path and already declined whenever any subject existed; with
  every domain it reads now closed, there is no context to assemble without one.
  The refusal itself stays so a caller reaching for that spelling fails loudly.
- `prefs.bot_enabled` reads the installation-wide module row directly when there
  is no subject. That arm is the zero-subject delivery gate and goes with it; a
  database with no subjects has no per-person state to consult.

- Closed the weight domain — thirty-nine bridges, the largest in the codebase
  and the one every other domain leans on: weigh-ins, body measurements, noise
  markers, progress photos, the chart series, and the Garmin export outbox
  behind them. With it, **the last legacy `enforce()` call site in the codebase
  is gone**: every conflict-gated write now names its subject and the decision
  that authorised it.
- Adoption on write is gone everywhere it survived — a note, an edit, or a
  delete no longer claims an ownerless weigh-in on the way past. The two
  remaining `_adopt_weight_provenance` calls are the dedupe path, where the row
  is already in scope and only its connection and raw link are being filled in.
- The integrity validators stop scanning rows that belong to nobody. A
  half-migrated graph was worth reporting while the bridge could reach it; now
  such a row is outside every scope, and what these validators still guard is a
  row that names *this* subject and then cites provenance that does not.
- A Garmin weigh-in inside a daily bundle is projected on the owned ingest path
  only. A legacy ingest keeps the Garmin row and stops there, because without a
  subject there is nobody for the reading to belong to.
- `handle_active_weight_deleted` clears the outbox link itself rather than
  depending on `ON DELETE SET NULL`. The caller already knows which id it
  deleted, and the two dialects enforce the constraint differently.
- Progress photos close with the rest: every upload domain now refuses a write
  that does not name a subject, which is what `test_upload_dual_write` asserts
  in place of the permissive path it used to describe.

- Closed the labs domain — twenty-four bridges, the largest leaf in the codebase
  and the one with the deepest provenance chain: a result, its marker catalog
  row, the raw payload it was parsed from, the document behind that, and the
  paid AI invocation that read it. The generic replay trio goes with the bridge,
  as it did for body composition; `reparse_owned_pending` is the only sweep left
  and decides adoption per raw from the raw's own roots.
- `ingest_extracted` now insists on the raw payload the boundary created. That
  was already true in practice — the unowned arm was the only path that built
  one itself — so the signature now says it.
- A garbled row costs that row, not the document. The batch preflight used to
  raise on an implausible value while the ingest loop skipped it; both skip now,
  which is what the function has always claimed to do.
- A legacy raw behind an owned parsed fact is valid provenance, because the
  nightly replay deliberately produces that shape: it owns the normalized facts
  and leaves a raw with no provider or file roots to adopt alone. An MCP fact
  gets no such latitude — it is written raw-first and must cite a raw of its own.
- `hrt_reminders.seed_hormone_panel` closed with it, since it exists to write
  into one person's marker catalog.

- Closed the GLP-1 domain. Twenty bridges — the largest single leaf so far, and
  the first with an alert of its own. Every read takes the subject, every write
  takes the subject with its conflict decision, and two more legacy `enforce()`
  call sites went with them: five remain across the codebase.
- Dose-phase bookkeeping is now unconditionally serialized. Closing an older
  open phase used to take the row lock only on the scoped path and adopt an
  unowned phase while it was there; both were consequences of the bridge. A
  phase belongs to one person, so the lock is always taken and nothing is
  claimed.
- The plateau evaluator names the subject whose dose and whose weight trend it
  is reading. It still hands `weight_service` the compatibility flag, because
  weight is the next domain to close, not this one.

- Closed the timeline domain, both halves of it: the manual annotations a person
  writes and the derived feed re-shaped from ten other domains' rows. Ten
  bridges. `_fully_legacy_row_scope` is gone with them — the selector that
  decided whether an ownerless row was "genuinely legacy" by inspecting its
  actor, file and raw links, including the Stage-3A provider-connection rules.
  A row's own subject is now the whole answer, which is what that hundred-line
  predicate was standing in for.
- Whose payload a row cites does not decide whose row it is. An unowned row
  whose raw is also unowned used to be admitted; an exact Stage-3A raw naming
  the subject used to lend it one. Neither does now.
- Closed the custom-charts domain. Six bridges, including the shared Redis key:
  one cache entry per person, because a global key would serve one subject's
  chart list to the next request from another. The `app_settings` write branch
  is gone — the scoped setting read already falls back to that row on its own,
  so pre-backfill installations keep their charts without a bridge here.
- `today_service.build` was the last entry on the composition-reader allowlist
  in the timeline contract test; that allowlist is now empty.

- Closed the nutrition domain. Eight bridges. Every read takes the subject and
  every write takes the subject with its conflict decision; the day's running
  total is one person's total, which is what the GLP-1 low-intake rules compare
  against. Adoption on write is gone: a meal belonging to nobody is out of scope
  rather than claimable, and the last `enforce()` call in the module went with
  it — seven legacy enforce sites left across the codebase.
- The nav rail's status card now names the subject whose day it reports, which
  meant scoping `nutrition_service.daily_summary` behind it; the ownership
  resolution sits inside the card's fail-safe guard, because chrome must draw
  nothing rather than raise.
- The protein nudge refuses to run without ownership — being behind on protein
  is something a person is, not an installation.

- Closed the signals domain — what a person types to the bot, and the per-day
  context they answer with a tap. Fourteen bridges. Every read takes the
  subject; every write takes the identity, and a captured message also names the
  channel it arrived through, because a signal without a recipient connection is
  a message from nowhere.
- The day-context upsert no longer adopts an unowned row. One answered day per
  person means the subject *is* the lookup: a row that belongs to nobody is
  nobody's day to answer, so it is not found and not claimed. The case that used
  to need an explicit refusal — a partial row with an actor and no subject — is
  now simply unreachable.
- The signal replay writes on behalf of each raw's own roots rather than a
  caller-supplied flag, the same shape body composition settled on.
- Closed `day_plan.resolve` and `day_plan.record_answer` with it, since both are
  thin wrappers over the day context: `resolve` takes the subject, `record_answer`
  takes the identity and the connection the tap came in on. `handle_text` and the
  nightly signal recovery now refuse to run without proactive ownership — an
  incoming message belongs to somebody.

- Closed the body-composition domain, the deepest graph so far: a scan, its
  metric sheet, the raw payload it was parsed from, the file that payload came
  out of, and the weigh-in it bridges into the weight domain. Fifteen bridges
  closed. Every read takes the subject; every write takes the subject together
  with the Weight capability, because a scan that changes today's weight has to
  take the Weight lock order to do it.
- Retired the generic replay path (`ingest_extracted`, `reparse_from_raw`,
  `reparse_pending`) and the disabled singleton resolver with it. `reparse_owned_pending`
  is now the only sweep, and it is the one reader body_comp keeps that can see a
  payload belonging to nobody — adopting that payload into the subject's history
  is the whole point of the sweep. It decides that per raw, from the raw's own
  roots, instead of from a caller-supplied flag: a fully-unowned payload and a
  Stage-3A parser payload adopt, everything else is judged exactly.
- A migrated manual scan keeps its unknown actor null, and the closed reader
  accepts that — the Stage-3B backfill stamped subjects without inventing
  actors. Any *other* user's id on that row is still refused as a forged
  attribution.
- An ownerless scan is now simply outside every scope rather than adoptable on
  read: owning the raw it points at does not pull it into the owner's history.
  Mid-backfill this means the reader shows exactly the rows already stamped,
  which is what the PostgreSQL stop/resume rehearsal now pins.
- Scoped the chart builder along the way — `build_catalog`, `series_for` and
  `resolve_chart_series` name the subject whose charts they offer — and with it
  the three Hevy readers those charts consult (`exercise_catalog`,
  `working_weight_series`, `progression_for_exercise`), which had been reading
  across the whole installation.

- Closed the three HRT domains together — doses and side effects, cycles and
  their items, and the templates behind them — because a cycle read is a graph
  read and closing one without the others would have left the reader half
  scoped. Forty-nine bridges across the three, and two more legacy `enforce`
  call sites with them. A cycle whose items are not yet owned is now refused
  rather than half-read: the scoped reader reports the graph instead of
  returning the parent and dropping the children.
- Left the HRT catalog's `active` flag frozen rather than scoping it. It is a
  per-person preference stored on a global catalog row, so scoping it needs a
  reviewed SubjectSetting mapping; passing a subject is refused rather than
  silently writing one person's choice onto everybody's catalog. That refusal
  is the one deliberate exception in the module and is documented as such.
- Closed the genetics domain, the first one whose facts carry raw provenance.
  Every read takes the subject, every write takes the subject with its conflict
  decision, and `get_variant` no longer falls back to a bare primary-key fetch —
  a key proves nothing about whose genome it is. A variant with an actor but no
  subject is reported as broken provenance rather than passed over, and the
  bridge that adopted an unowned variant into a subject is gone along with the
  four tests that described it. The scoped conflict resolver keeps the engine's
  fully-unowned arm, and what it proves for a row that belongs to nobody is that
  the raw it cites belongs to nobody either: a manual fact citing any raw, or a
  VCF fact citing a raw with an actor, is refused there exactly as it is on the
  owned path. The demo VCF seed writes directly, like the skincare one.
- Closed the skincare domain. Every read takes the subject and every write takes
  the subject with its conflict decision; the routine, the observations and the
  product shelf are one person's or they are nobody's. All sixteen bridges are
  closed. One reader deliberately survives: the conflict engine still offers its
  callers a fully-unowned bridge, and a resolver has to honour the scope it is
  handed, so the scoped skincare resolver is the last place in the module that
  can see a row with no subject. It goes when that bridge does.
- Closed the goal domain. `milestones_service` takes the subject on every read
  and the subject with its conflict decision on every write; progress refuses a
  goal that belongs to somebody else rather than computing it from this
  subject's weight. A goal without a subject is nobody's: it is not listed, not
  adopted, and not counted. All thirteen of the module's bridges are closed.
  What did not go with them is the guard that reports a goal carrying an actor
  but no subject — that is broken provenance, not merely another person's row.
- Closed the first leaf domain. `supplements_service` now demands the subject on
  every read and the subject together with its conflict decision on every write;
  `include_legacy_unowned` is gone from all of them, as is the branch that
  adopted an unowned row on the way past. A regimen without a person is not a
  thing, so a supplement with no subject is no longer anybody's: the page does
  not list it, the report does not carry it, and no write path will claim it.
  Nine of the module's eleven bridges are closed and three of the fourteen
  legacy `enforce` call sites went with them; the two that remain are the
  conflict-engine resolver and the scope helper it shares. The share report's
  block builders take the subject they compose for, which is what made closing
  the leaf possible at all.

- Threaded a mandatory subject through the composition layer. `assemble_context`
  is what the weekly digest, the daily brief, the doctor's report and the MCP
  composition tool all reason over, and it read the whole installation: a single
  unscoped query there would put one person's numbers into another person's
  document. It now takes the subject it composes for and every one of its
  thirty-odd domain reads is scoped by it, as are `build_snapshot`,
  `today_service.build`, and the brief's `build_context`. A subject also finally
  sees their own custom HRT compounds instead of only the curated catalog, which
  the scoped read made visible as a gap. Alerts are read per subject, so the
  platform's own diagnostics no longer reach anyone's report.
- Removed the two zero-subject generators. `digest_service.generate_digest` and
  the brief's compatibility wrapper both refused to run once a subject existed,
  which in a commercial installation is always, so neither had a production
  caller; only tests kept them alive. What those tests actually asserted —
  whether a day is empty, what the header prints, that the protocol never
  reaches the brief — is asserted against the context and the header directly,
  where it belongs, and the AI path they incidentally exercised is already
  covered by the gateway suites.

- Added the ratchet that makes PR-04 measurable. Stage 2 gave every core service
  an optional scope — pass `subject_id` (or an `identity`/`context`, and
  sometimes `include_legacy_unowned`) and the call is scoped; omit it and it
  reads or writes across the whole installation. That optionality is what kept
  the migration reversible while ownership was being backfilled, and it is now
  the last thing standing between this schema and a second person: a scoped
  unique key over a nullable column, a policy engine no service consults, and
  row-level security applied under an application that still issues unscoped
  reads are each worth nothing on their own. `vitals/legacy_scope.py` inventories
  what remains — 266 bridged functions across 25 modules, and 14 modules that
  still fetch a subject-owned row by bare primary key, which proves nothing
  about who it belongs to. A contract test recomputes both inventories from the
  source and fails in either direction, so a new bridge cannot appear unnoticed
  and closed ones cannot be left recorded as outstanding. Reaching zero is the
  condition for making the compatibility columns `NOT NULL` and enabling FORCE
  RLS.

### Added — commercial multi-user foundation

- Added the first isolated commercial-fork schema slice: stable users, additive
  member/doctor/trainer/platform-superadmin roles, self-owned health subjects,
  explicit time-limited support grants and scopes, and append-only audit events.
  The superadmin role alone grants no health-data access; a support investigation
  must be bound to one subject, reason, expiry, mode, approver, and concrete scope.
- Added reversible migration `0035`, synthetic constraint/security tests, and a
  durable PR-by-PR implementation and validation roadmap in
  `docs/COMMERCIAL_MULTI_USER_ROADMAP.md`. The environment-backed login remains
  active through a compatibility layer; registration stays closed until the
  subject-isolation and authorization gates are complete.
- Added an idempotent, fail-closed runtime bootstrap for the existing owner. It
  copies the configured bcrypt hash verbatim, assigns `member` and
  `platform_superadmin`, creates the self-owned health subject, serializes
  concurrent PostgreSQL startup with an advisory lock, and records only bounded
  operational audit metadata. A config/identity/hash mismatch now stops startup
  instead of silently creating or rewriting an administrator.
- Added Stage 3A, the first resumable subject-ownership data phase over
  historical raw payloads. Schema-only
  revision `0045` stores a payload-free, subject-bound checkpoint;
  the fixed `stage3.raw_payloads.v1` operator command defaults to read-only
  status/preflight and requires `--apply` for independently committed bounded
  batches inside one raw-writer maintenance window. The final transition
  keyset-rehashes the complete frozen snapshot and refuses cross-batch payload,
  count, or ownership drift. Its JSON output excludes subject and raw/checkpoint IDs, payloads,
  paths, credentials, DB URLs, and exception text. A throwaway PostgreSQL 15
  gate passed the real migration chain through revision `0034` and then to head,
  batch-size-2 process stop/resume, unchanged data/link/frozen-output hashes,
  idempotent completion, and populated-checkpoint downgrade refusal. Remaining
  ownership phases and whole-lake validation are still required before
  registration can open.
- Added Stage 3B for the fixed catalog of 17 actor-optional normalized tables
  whose historical subject can be proven without inventing connector, raw,
  file, or control-plane provenance. Each table has an independent resumable
  checkpoint and bounded PK scan; historical rows gain only the sole subject,
  never a fabricated actor. The operator requires completed Stage 3A, rejects
  partial/foreign ownership, unreviewed domain/source values, unsafe HRT parent
  graphs, and future scoped-key duplicates, and rehashes the frozen data and
  ownership snapshot before completion. Backup v1 atomically re-bases all 17
  checkpoints to its incoming snapshots. The CLI exposes only fixed table
  names, counts, result codes, and checksums, with no health values or identity
  IDs; all covered writers must remain paused for the multi-batch maintenance
  window.
- Added Stage 3C for the two HRT children whose subject is inherited solely from
  the already-reviewed Stage 3B cycle or template parent. The fixed
  `stage3.inherited_children.hrt.v1` operator backfills only
  `hrt_cycle_items` and `hrt_cycle_template_items`, never invents actor,
  connection, file, or raw provenance, and rejects foreign parents or unsafe
  compound references. Independent checkpoints, final locked checksum
  verification, strict child-scope tests, and an atomic backup-v1 checkpoint reset
  preserve bounded stop/resume behavior. Body-scan, Hevy, and compound children
  remain in later provenance-aware phases.
- Added Stage 3D for the fixed raw-linked provider catalog:
  `garmin_daily`, `garmin_activities`, `garmin_intraday`, and `hevy_workouts`.
  Historical rows gain only S and the connection already proven by their exact
  Stage-3A raw link; actor provenance is validated but never rewritten. The
  operator rejects partial/foreign roots, mismatched natural keys, unreviewed
  connection lifecycles, and unsafe Hevy child graphs, and finalizes only after
  locked data/ownership verification. Non-empty backup-v1 restores become
  terminal `RESTORE_BLOCKED` because stripped connection provenance cannot be
  guessed; empty provider snapshots complete safely.
- Added Stage 3E for Hevy's inherited exercise/set tree. The fixed
  `stage3.inherited_children.hevy.v1` operator copies only the exact S/C of the
  reviewed workout/exercise parent chain, rejects partial or foreign roots, and
  preserves every child fact and timestamp. Because owned Hevy refreshes replace
  both child tables wholesale, completion freezes the migration snapshot while
  later status validates the current strict graph. Non-empty backup-v1 child
  snapshots become `RESTORE_BLOCKED`; empty snapshots complete.
- Added Stage 3F for the mixed HRT compound catalog. The fixed
  `stage3.mixed_catalog.hrt.v1` operation classifies exact checked-in system
  compounds as global while assigning the sole subject only to reviewed
  historical manual/MCP compounds and their components. It preserves every
  actor and medical/catalog field, validates linked dose and cycle snapshots,
  uses fixed keyset pages, freezes custom ownership evidence across later
  catalog reseeds, and permits backup-v1 stop/resume because the retained
  source/key marker proves the system-versus-custom split. Catalog
  synchronization now refuses a custom row that collides with a checked-in key
  before changing any definition or component. Catalog rows are locked before
  that decision, custom keys must be canonical lowercase slugs, and scoped
  curated reads require exact HRT/system provenance.
- Added Stage 3G for mixed global and subject-owned conflict rules. The fixed
  `stage3.mixed_catalog.conflict_rules.v1` operation keeps exact checked-in YAML
  definitions global, assigns only the sole subject to reviewed historical
  custom rules, preserves the legacy `active` toggle and every rule field, and
  freezes durable ownership evidence only for custom rows so curated catalog
  reseeds remain safe. Catalog synchronization now shares identity governance,
  locks matching rows in stable order, and refuses subject-owned code collisions
  before refreshing any definition. Backup v1 resets the bounded checkpoint from
  its retained subject marker; the activation-preference bridge remains a later
  cutover gate.
- Added Stage 3H for legacy progress-photo file ownership. The fixed
  `stage3.file_backed.progress_photos.v1` operation preserves unknown historical
  actors, creates metadata-only legacy FileAsset placeholders without touching
  medical bytes, rejects duplicate/unsafe/aliased file graphs, and keeps strict
  live uploads on exact S+A+F. Supported photo deletion retires the asset and is
  compatible with completed current-graph validation. Because backup v1 carries
  neither bytes nor trustworthy actor/file roots, nonempty restores are recorded
  as `RESTORE_BLOCKED` and create no placeholders; empty snapshots complete.
- Added Stage 3I for channel-optional day-context ownership. The fixed
  `stage3.channel_optional.day_context.v1` operation assigns only the sole
  subject to reviewed fully-unowned history, preserves every answer, plan,
  timestamp, source, and valid optional actor/channel root, and never infers
  stripped provenance. Completed checks validate the current mutable context
  graph, while backup v1 resets nonempty snapshots for bounded recompletion and
  completes empty snapshots exactly.
- Added Stage 3J for channel-optional signal ownership. The fixed
  `stage3.channel_optional.signals.v1` operation assigns only the sole subject
  to reviewed fully-unowned history while preserving signal facts, batch/raw
  links, timestamps, and every valid optional actor/Telegram-recipient root.
  It validates raw and batch provenance without inferring stripped A/C, uses
  canonical root-before-fact locks, and revalidates the current volatile graph
  after completion. Backup v1 resets nonempty signal snapshots for bounded
  recompletion after Stage 3I and completes empty snapshots exactly.
- Added Stage 3K for retained shared-report ownership. The fixed
  `stage3.retained_artifact.shared_reports.v1` operation assigns only the sole
  subject to reviewed fully-unowned reports while preserving creator/revoker
  gaps and every public token, password hash, frozen snapshot, lifecycle field,
  counter, and timestamp. A checkpoint-bounded consumer boundary distinguishes
  migrated history, the unprocessed frozen tail, and strict live reports.
  Backup v1 neither exports nor replaces published reports, so import prepares
  or preserves the retained checkpoint without trusting bounds from the file.
- Added Stage 5D: revision `0048` drops the twelve legacy global keys the scoped
  keys replaced. This is the point at which two people may share a weigh-in
  date, a lab-marker name, an rsID, and a day, and two accounts of one provider
  may share an external id — which is exactly what a second subject needs and
  what the global keys made impossible. Every temporary bridge that stood in for
  a surviving global key is removed with it, and the tests that asserted a typed
  cutover error now assert the coexistence the cutover bought: a real
  PostgreSQL rehearsal has two subjects write the same weigh-in date, marker
  name, rsID, and provider external id from two concurrent transactions, while
  one subject still cannot hold the same key twice. What replaces each bridge is
  a real invariant rather than nothing: an active alert whose ownership shape
  matches no class is still refused, a genetics rename still cannot land on an
  rsID this subject already holds, and the catalogs simply cannot see a
  subject's own compound or rule. A supporting `(alert_key, entity_ref)` index
  replaces the dropped global alert key for dismissal-history reads, which the
  unresolved-only scoped keys cannot serve. Downgrade recreates every dropped
  key, but only while the data still satisfies it: once a second subject has
  written a duplicate of a legacy global key, this revision is a one-way
  boundary and recovery is a verified backup plus a forward fix.
- Added Stage 5C, which switches every key-based write path to the scoped key.
  A path used to look its natural key up across the whole installation and then
  check afterwards whether the row it found happened to belong to the caller;
  it now looks the key up *inside* the caller's scope, so a row outside it is
  never read into the write path or mutated. A Garmin day and activity, a Hevy
  workout, and a weight-export intent resolve inside their connection; a day
  context resolves inside its subject; the compound and rule catalogs read only
  the platform half of their key, so a subject's own compound or rule is no
  longer something catalog startup can even see; and an active alert resolves
  inside the root its class belongs to — the connection for a provider alert,
  the subject for a health alert, the installation for a platform alert. The
  weight, body-measurement, lab-marker, genetics, and HRT-cycle paths already
  worked this way. Because the legacy global keys are still installed, each
  path also carries one narrowly scoped bridge that reports a row outside the
  scope as a typed cutover error instead of letting the surviving global key
  raise a bare integrity error; every bridge is marked to be removed with the
  key it stands in for.
- Added Stage 5B: revision `0047` installs the sixteen scoped unique keys. Each
  is installed *beside* the legacy global key it will eventually replace, never
  instead of it — a scoped key is strictly weaker than the global key it
  narrows, so installing it can reject nothing the lake already holds and every
  legacy reader and writer keeps working unchanged. On PostgreSQL the indexes
  are built `CONCURRENTLY` so the migration never holds a write lock on a
  populated health table, and `IF NOT EXISTS` makes a re-run after an
  interrupted build safe; downgrade drops them transactionally, so a refused
  downgrade further down the chain rolls the whole attempt back rather than
  leaving the lake half-cut-over. A schema-contract test pins the migration to
  the reviewed catalog so the two cannot drift, and rehearsals now derive the
  migration head from the chain itself instead of naming a revision, so adding
  one no longer edits twenty files. Dropping the global keys, and switching
  every key-based write path to the scoped key, remain the separately reviewed
  cutover.
- Added Stage 5A, the audit that gates the scoped-key cutover. `vitals/scoped_keys.py`
  is the machine-readable inventory of the change: twelve legacy global keys and
  the sixteen scoped indexes that replace them, each naming the column its scope
  comes from. The fixed `stage5.scoped_key_audit.v1` operation proves, read-only,
  that no row would collide under a proposed scoped key and — the check the audit
  mainly exists for — that no row is missing the scope the key depends on, since
  a scoped unique index over a null scope column silently degenerates into the
  global key it was replacing. A provider row with no connection passes Stage 4,
  because its ownership never leaves the reviewed roots, and is refused here.
  The audit requires Stage 4 to have proved *this* lake rather than merely to
  have run: stale evidence blocks it exactly as missing evidence does. It
  creates, drops, and rewrites nothing but its own checkpoint, and its operator
  command exposes no table, key, phase, reset, or database selector and emits
  only counts, result codes, and checksums. `skincare_logs` and `supplements`
  are deliberately out of scope: they carry no global uniqueness today, so they
  never blocked a second subject, and adding uniqueness where the application
  allows duplicates is a product decision rather than an isolation one.
- Added Stage 4, the whole-lake ownership gate that closes the PR-03 backfill
  sequence. Revision `0046` installs six parent/child subject-equality foreign
  keys `NOT VALID` on PostgreSQL, so the migration installs the rule without
  scanning a lake whose ownership is not proved yet; the fixed
  `stage4.whole_lake_validation.v1` operation then proves the lake and makes
  them valid. The check inventory is derived from the schema metadata and the
  machine-readable ownership registry rather than a hand-kept list: a table that
  is persisted but unclassified fails the run, and a newly added ownership
  reference is validated the moment it exists. One pass proves that every
  required subject is present, that no row reaches a subject, actor, connection,
  file asset, or raw payload outside the reviewed roots, that every child agrees
  with its parent and every normalized fact with its raw payload, that a scoped
  read returns exactly what the legacy unscoped read returns, and that exactly
  one health subject still exists. A curated catalog parent carries no subject
  and its inherited components carry none either; what is proved there is
  equality with the parent, not the presence of a subject. Every Stage-3 phase
  must be terminal first, and the recorded evidence is a chained digest of the
  whole graph, so data written after a run invalidates it and the operator has
  to record it again rather than inheriting a stale proof. The operator command
  is read-only by default, exposes no table, phase, reset, or database selector,
  and emits only counts, result codes, and checksums. A throwaway PostgreSQL 15
  rehearsal drove the real migration chain from revision `0034`, the complete
  twenty-command Stage-3 chain, the unvalidated-to-valid constraint promotion,
  idempotent re-recording, ordinary write-path rejection of a crossed parent, a
  boundary broken behind the constraints being refused without recording, and
  the populated-checkpoint downgrade refusal.
- Added Stage 3T for subject-optional system-alert ownership, completing the
  PR-03 backfill catalogue. The fixed `stage3.subject_optional.system_alerts.v1`
  operation classifies every historical alert through the writer's own reviewed
  key allowlist and adds only what the class proves: a health or conflict alert
  gains the sole subject, a provider alert additionally gains the exact reviewed
  legacy connection for its provider, and an installation-wide platform alert
  keeps neither root. An unclassified key fails closed rather than being folded
  into any class. Severity, message, key, entity reference, and the override and
  resolution history are untouched, and lifecycle actors must be the owner or
  null. Backup v1 rebinds the subject but strips the connection, so a restored
  provider alert is completed again rather than left half-migrated.
- Added Stage 3S for retained notification ownership. A delivered message only
  means something together with the person it went to and the channel that
  carried it, so the fixed `stage3.delivery_artifact.notifications.v1` operation
  gives a reviewed fully-unowned row the sole subject, the reviewed owner as
  recipient, and the exact reviewed legacy Telegram root together; the
  originating actor stays null and the sent time, category, dedupe key, channel,
  external message id, and payload are untouched. A rotated or additional
  recipient fails the read-only preflight while adoption is pending, and a reply
  or echo linking a platform invocation must belong to the subject and have
  succeeded. `notifications` also becomes retained rather than portable: backup
  v1 transports neither recipient nor channel, so a restored address-less row
  would violate the reviewed dedupe shape and resurrect keys that no longer
  scope to anything. `delivery_intent_id` joins the suppressed plumbing columns.
- Added Stage 3R for retained weekly-digest ownership. The fixed
  `stage3.retained_artifact.weekly_digests.v1` operation gives a reviewed
  fully-unowned artifact only the sole subject; the authoring actor, the
  historical subject OpenRouter connection, the platform invocation link, the
  narrative, the context it was built from, the model, and both timestamps stay
  exactly as persisted. Subject-funded and platform-funded provenance are proved
  mutually exclusive, a linked invocation must belong to the subject, match the
  digest kind, and have succeeded, and a digest created above the frozen
  watermark must carry reviewed AI funding. Backup v1 neither exports nor
  replaces digests, so import prepares or preserves the retained checkpoint
  without trusting bounds from the file.
- Added Stage 3Q for Garmin weight-outbox ownership. The fixed
  `stage3.provider_outbox.garmin_weight_exports.v1` operation gives a reviewed
  fully-unowned row the sole subject plus the exact reviewed legacy Garmin
  account it was queued for; the requesting actor, dispatch markers, retry
  counters, remote sample identity, and every timestamp stay as persisted. The
  destination is never guessed: a missing, rotated, or non-legacy account fails
  the read-only preflight while adoption is still pending. Backup v1 cannot
  carry a required destination, so a nonempty restored snapshot is recorded as
  `RESTORE_BLOCKED` and the operator command refuses to advance.
- Added Stage 3P for inherited body-scan metric ownership. The fixed
  `stage3.inherited_children.body_scan_metrics.v1` operation copies only the
  reviewed parent scan's subject down to its metrics; the metric key, printed
  label, value, unit, reference range, segment, category, and both timestamps are
  untouched. A child never leads its parent: a metric whose scan is still
  unowned fails closed, and a live metric requires the strict parent graph.
  Parents are locked before children so a concurrent scan adoption cannot slip
  between validation and the child update. The body-scan reader now also accepts
  a migrated manual scan whose unknown actor stays null. Backup v1 rebinds the
  child subject and resets the exact checkpoint after Stage 3O.
- Added Stage 3O for file-backed body-scan ownership. The fixed
  `stage3.file_backed.body_scans.v1` operation gives a reviewed fully-unowned
  scan the sole subject and, when it kept a sheet, a metadata-only FileAsset
  root; historical actors and the placeholder uploader stay null because the old
  route does not prove who uploaded a sheet. Device, file key, raw link, note,
  and both timestamps are untouched, and no file byte is read, moved, or hashed.
  Manual scans may claim neither file nor raw provenance, structured MCP scans
  stay file-free, and a parsed scan's vision provenance is validated read-only on
  its raw payload. The body-scan reader now recognises that reviewed placeholder,
  so migrated sheet history stays legible instead of failing its own file check.
  Backup v1 carries neither sheet bytes nor trustworthy actor/file roots, so a
  nonempty restored snapshot is recorded as `RESTORE_BLOCKED`.
- Added Stage 3N for raw-linked genetic-variant ownership. The fixed
  `stage3.raw_linked_facts.genetic_variants.v1` operation gives reviewed
  fully-unowned historical variants only the sole subject; actor attribution,
  the VCF batch link, gene, rsID, genotype, marker, impact, interpretation, and
  both timestamps are preserved exactly. Manual and MCP variants must stay
  rawless, an imported variant must retain its durable VCF batch, and that batch
  must have null provider-connection and file roots because a VCF upload is
  streamed. The phase also proves the one-variant-per-rsID invariant that the
  later `(S, rsid)` cutover must satisfy. Backup v1 rebinds S, strips the actor,
  and resets the exact checkpoint after Stage 3M.
- Added Stage 3M for raw-linked lab-result ownership. The fixed
  `stage3.raw_linked_facts.lab_results.v1` operation gives reviewed fully-unowned
  historical results only the sole subject; actor attribution, the raw link,
  marker, value, unit, reference-range snapshot, flag, lab name, note, and both
  timestamps are preserved exactly. Manual, MCP, and parsed provenance are
  validated against the linked raw payload read-only: a subject-funded gateway
  parse may not also claim a platform invocation, a platform parse must present
  a same-subject lab document whose storage reference matches the raw external
  ID, and a fileless or restored parser raw is accepted as history without
  letting it forge a parser claim. Backup v1 rebinds S, strips the actor, and
  resets the exact checkpoint after Stage 3L.
- Added Stage 3L for optional-channel weight ownership. The fixed
  `stage3.channel_optional.weight_logs.v1` operation gives reviewed fully-unowned
  historical weights only the sole subject: actor attribution, the Garmin or
  body-scan connection, the raw link, mass, note, supersession, and both
  timestamps are preserved exactly, and a provider connection is never copied
  down from the raw payload onto the fact. Manual/MCP, Garmin, and body-scan
  provenance shapes are validated against their reviewed raw domain/source and
  parser-invocation exclusivity, and the one-active-weight-per-date invariant is
  proved independently of the legacy global index. Backup v1 rebinds S, strips
  actor/connection, and resets the exact checkpoint after Stage 3K to RUNNING
  for a nonempty snapshot or COMPLETED for an empty one.
- Split the mixed proactive settings aggregate into a subject schedule/nudge
  policy, Telegram-recipient quiet-hours/budget policy, and Garmin-connection
  sync/pulse/export policy. Startup materializes complete scoped rows before
  scheduler registration; strict runtime reads never fall back to global
  defaults, human reads/writes require the active owner actor, and the legacy
  row is mirrored only while governance still proves exactly one subject.
  A shared guarded zero-subject transaction (`BEGIN IMMEDIATE` on SQLite and the
  identity advisory lock on PostgreSQL) keeps pre-bootstrap compatibility from
  racing identity creation. Per-subject timezone dispatch remains a separate
  PR-09 gate.
- Added a durable, subject-owned Telegram delivery intent protocol. Every owned
  new-message path commits a payload-free `PENDING` claim, freshly revalidates
  the subject, recipient, current Telegram connection, module state, quiet hours,
  and budget before committing `DISPATCHING`, performs exactly one network call
  without an open database transaction, and atomically records `SENT` plus the
  matching Notification journal. Uncertain transport outcomes become terminal
  `AMBIGUOUS` claims and are never retried; stale claims are reconciled without
  provider I/O. A stale raw-backed reply/echo that provably never acquired a
  dispatch lease may be re-armed from deterministic domain state; an uncertain
  or policy-cancelled claim cannot. Scoped occurrence keys, raw/category
  uniqueness, and conservative
  cooldown/budget accounting prevent duplicate sends across workers and channel
  rotation. The intent stores no text, buttons, chat identifier, credential, or
  free-form provider error—only a bounded lifecycle code. Existing journal
  content, callback edits/withdrawals, and the
  exact-one environment-backed Telegram transport remain explicit PR-09 follow-up
  gates rather than being presented as multi-recipient or PHI-free delivery.
- Added framework-independent immutable access-policy values. Ownership permits
  access to the selected subject; doctor/trainer access requires an exact live
  relationship-consent scope, and superadmin support access requires an exact
  live support grant and scope. Roles alone never expose another subject's PHI,
  wildcard scopes and implicit support-mode expansion are denied, and policy
  evaluation never imports the web or database layers.
- OpenRouter configuration is now platform control-plane state: only an active
  `platform_superadmin` sees the AI settings card or may update its global key,
  endpoint, and model choices. Authorization is locked against concurrent role
  changes, and the audit event records field names only—never secret or model
  values. This role gate grants no prompt, artifact, or subject-data access.
- Added a dedicated, no-PHI platform AI control page for creating, rotating,
  enabling, and disabling the global gateway and configuring aligned half-open
  platform/opaque-subject quota periods. Configuration changes are versioned,
  a commit error after an environment write clears the credential and requires
  explicit reconciliation rather than guessing an ambiguous database outcome,
  and only migrated platform consumers (currently Weekly Digest, Daily Brief,
  Signals parsing, Telegram question replies, Labs recognition, and Body Scan
  recognition) are governed; legacy subject OpenRouter roots remain
  readable only as validated historical provenance during their cutover.
- Weekly digests now use that centrally funded gateway without granting the
  superadmin health-data access. Web, MCP, and scheduler generation reserve a
  subject-owned `AIInvocation`, commit before exactly one OpenRouter call, then
  atomically finalize sanitized usage and an S/A-scoped digest linked to the
  invocation; new rows never pretend the platform gateway is a subject provider.
  Platform and per-subject quotas are hard-ledgered, terminal failures advance
  through three bounded idempotent attempts, completed/in-flight product keys
  survive gateway or quota rotation without another paid call, incompatible
  PREPARED reservations are released before retry, and a 15-minute platform
  recovery job releases abandoned reservations or conservatively closes stale
  paid calls.
  Backup v1 leaves generated digests and their accounting provenance in place
  rather than exporting broken ownership links; the curated LLM export continues
  to include their narrative content.
- Daily Brief now uses the same centrally funded invocation gateway at web-build,
  web test-send, and actorless scheduler boundaries. Each opaque form token (or
  deterministic scheduler date key) has a stable product identity across model
  and prompt-policy changes and can dispatch at most once; an incompatible
  prepared model is cancelled to a linked header instead of buying a replacement.
  Database locks never span OpenRouter, failed or ambiguous attempts retain
  conservative charges and produce a linked deterministic header, and missing
  platform capacity still produces an invocation-free header. Telegram test and
  reply delivery use prepare/network/journal phases; their existing concurrent
  outbound-claim gap remains tracked separately. Legacy subject-OpenRouter brief
  rows stay readable.
- Telegram Signals parsing and scheduled raw recovery now use the centrally
  funded platform gateway without requiring a subject-owned OpenRouter
  connection. Each raw message receives up to three subject-bound invocation
  attempts with sanitized usage accounting; reservation and charge transactions
  close before the single bounded model call, while successful normalization and
  terminal accounting commit atomically. Parser alerts are S-scoped with C null,
  may reference the exact failed/ambiguous invocation, and retire legacy alerts
  without inventing missing ownership roots. Recovery skips exhausted head rows,
  preserves the 04:00 health-day boundary, and accepts only the exact-one
  fully-null historical bridge. Echo delivery records the invocation and
  revalidates Telegram edits before and after transport so a late stale echo is
  suppressed or neutralized; the general durable outbound-intent cutover remains
  tracked under PR-09. Keyset recovery also scans past malformed or oversized
  immutable raws, so they remain available for audit without starving later
  valid messages or aborting unrelated parser-alert reconciliation.
- Telegram questions now use one raw-bound, subject-authorized invocation of the
  centrally funded gateway. Raw classification and reservation commit together;
  start/charge commits before exactly one usage-aware OpenRouter call, and
  terminal accounting commits before Telegram delivery. Duplicate webhooks and
  the scheduled recovery worker never buy another attempt, while a cursor scans
  beyond ordinary-message head rows and invocation-backed gaps remain directly
  recoverable from PostgreSQL. The generated answer exists only in redacted,
  non-pickleable memory: Telegram receives it, but the Notification journal keeps
  only the raw/invocation provenance marker. A post-send check withdraws an
  answer after a concurrent edit, module disable, owner change, or recipient-
  connection retirement. New inbound raws retain the owner's current message
  but reduce Telegram's nested replied-to/callback bot message to operational
  IDs, so Telegram cannot copy a memory-only AI answer back into raw history.
  The remaining check/send/journal race still requires
  PR-09's durable outbound intent before registration opens.
- Lab-document recognition now uses the centrally funded platform gateway and
  no longer requires or writes a subject-owned OpenRouter connection. The
  upload stores private bytes first, then commits exact S+A+F and a C-null raw
  placeholder with a raw-bound WEB invocation; local PDF conversion completes
  before start/charge, then one usage-aware vision call runs without a database
  transaction. Sanitized accounting plus the verbatim validated extraction
  replace the placeholder atomically, with the same paid in-memory completion
  retried after a transient finalization rollback. Failed or ambiguous calls keep
  the file/raw roots for audit without exposing provider errors, and the nightly
  replay skips every platform raw unless its exact Labs invocation succeeded.
  Confirmation and replay continue to accept validated historical C-backed
  uploads, while the dashboard now reports redacted platform-root/quota
  readiness instead of environment-key presence.
- Body-scan document recognition now uses the same centrally funded platform
  gateway and no longer creates or requires a subject-owned OpenRouter
  connection. The upload commits exact S+A+F, a C-null raw placeholder, and one
  raw-bound WEB invocation before local image/PDF preprocessing and charge;
  exactly one usage-aware vision call then runs without a database transaction.
  Sanitized accounting and a strictly validated verbatim extraction finalize
  atomically, while confirmation remains a separate editable Weight transaction.
  Replay and derived Weight accept only an exact successful platform invocation
  or the validated historical subject-C chain, reject mixed provenance, and keep
  retained Weight readable after the source document is monotonically retired.
  The dashboard now projects redacted platform-root/quota readiness rather than
  environment-key presence.
- Browser sessions now use a strict versioned envelope while accepting existing
  signed bare-username cookies for their normal TTL. The public auth dependency
  remains compatible, and cookies contain no roles, subject IDs, grants, or PHI.
  Password changes update both the environment compatibility credential and the
  durable user using compare-and-swap, increment `session_version`, and restore
  the old environment hash if the database commit fails. Registration remains
  disabled; database-backed session enforcement arrives at the auth cutover.
- Ordinary backup/export and restore now leave identity, role, health-subject,
  support-grant, audit, and published-link control-plane tables untouched. This
  prevents the new durable password hash or access metadata from entering a user
  backup and prevents a legacy or forged import from replacing its authorizing
  owner, planting privileges, erasing audit history, or reviving a shared link.
  A non-empty v1 full restore cannot prove the A/C/F roots it strips, so it
  atomically records `stage3.raw_payloads.v1` as `RESTORE_BLOCKED`; ordinary
  apply cannot guess or clear that state, and future backup-v2 or reviewed manual
  remap is required. Empty restores record an empty completed checkpoint, while
  retained AI-invocation or durable-delivery references make raw replacement
  refuse before mutation. The checkpoint itself grants no authority over S-only
  raw history.
- Added the reversible PR-03 ownership expansion: subject-bound integration and
  private-file roots, scoped setting stores, nullable subject/actor/connection/
  file references on every classified health-data table and directly queried
  child, and supporting subject-aware indexes. Legacy readers, paths, global
  uniqueness, and registration behavior remain unchanged until dual-write,
  backfill, and scoped-key validation are complete.
- Backup v1 never transports tenant UUIDs or private resource locators. It binds
  required rows to the sole authoritative local health subject and uses only a
  boolean marker to preserve global-versus-subject semantics for mixed catalogs
  and optional alerts. Forged ownership fields are ignored, malformed markers
  fail before mutation, and a multi-subject database cannot use the legacy
  whole-database format.
- Persisted progress photos, lab documents, and body-scan documents now dual-
  write subject, human actor, private-file metadata, and OpenRouter provenance
  where applicable. Upload confirmation validates the subject/raw/file chain,
  legacy download and delete paths enforce subject scope and file lifecycle, and
  failed pre-commit writes remove orphan bytes while ambiguous commits preserve
  them for reconciliation rather than risking medical-file loss.
- UI language, enabled modules, and custom chart definitions now read their
  user/subject setting first and dual-write the temporary legacy setting in the
  same transaction. Their Redis cache keys include the owning UUID, collection
  updates are serialized on PostgreSQL, and web writes publish cache state only
  after the database commit succeeds.
- Garmin and Hevy ingestion now resolves the sole legacy subject and provider
  connection at each web, MCP, scheduler, import, and reparse boundary. Raw and
  normalized rows carry matching subject/connection provenance, human-triggered
  runs retain their actor, children inherit their parent roots, and cross-subject
  or retired-root updates fail closed before replacing history.
- Direct Timeline, Supplements, Signals, DayContext, and proactive-message web/MCP
  paths are subject-scoped. Human/MCP writes retain actor and source provenance,
  Telegram capture uses a channel-neutral delivery context, brief narratives cite
  OpenRouter rather than the notification channel, and legacy NULL history is
  visible only through the verified single-subject bridge. Telegram accepts only
  the configured private sender, durably claims the complete upstream update,
  supersedes edited facts without deleting raw history, and can replay failed
  actions. A foreign notification dedupe collision is rejected before any
  network send.
- The week template now uses its subject-scoped setting with atomic partial MCP
  updates and legacy dual-write. Custom-chart Timeline overlays, notification
  budgets/dedupe across connection rotation, and the Signals module gate use the
  resolved subject. Registration remains closed while global conflict/alert,
  composition/export, provider credential, and scoped-unique cutovers remain.
- Nutrition web/MCP writes now bind the authenticated legacy owner to an opaque
  conflict capability before locking a meal. Create and update checks evaluate
  the projected subject-day macro total, updates replace the prior day aggregate
  without double counting, deletes share the same subject lock, and the day-end
  job reconciles actorless subject alerts. Direct Nutrition summaries and search
  are subject-scoped, while pre-backfill rows require the exact-one compatibility
  bridge and partial-root rows remain hidden.
- Skincare checklist, observation, and personal-product web paths now stamp and
  filter the selected subject, and direct MCP Skincare reads/writes preserve MCP
  source provenance. Checklist replacement is serialized under the prepared
  conflict capability and excludes the prior subject-day state before evaluating
  retinoid/peel rules; note and delete paths reject foreign or partial legacy
  rows. The destructive historical Skincare and full demo seed scripts now refuse
  to run once commercial identity bootstrap exists.
- GLP-1 injection, dose-phase, and side-effect web/MCP paths now stamp and filter
  the selected subject while preserving manual versus MCP provenance. Injection
  edits merge only after a scoped row lock, phase creation evaluates the proposed
  active dose before closing an older phase, and note/delete paths reject foreign
  or partially owned legacy rows. Plateau detection now reads only the subject's
  phase, Weight facts, and noise ranges, and reconciles an actorless subject alert;
  the scheduled check no-ops when that subject has GLP-1 disabled.
- Labs manual, upload-confirmation, and MCP writes now use the selected subject's
  transaction-bound conflict capability. Single and batch MCP inputs persist an
  owned raw payload with MCP provenance before normalization; parser results keep
  their validated uploader, OpenRouter connection, and private-file chain. Direct
  Labs reads, edits, notes, deferrals, and deletes reject foreign or partial roots,
  while the nightly replay and startup hormone-marker seed use actorless scoped
  boundaries. Out-of-range and retest alerts reconcile only within that subject.
- HRT dose, side-effect, cycle, cycle-item, and cycle-template web/MCP paths now
  stamp and filter the selected subject while preserving manual versus MCP
  provenance. Dose corrections replace only the edited administration in safety
  evaluation; cycle/template children inherit and validate their parent's scope,
  legacy partial graphs fail closed, and template import/export remains parse-only.
  Protocol and Labs-due reminders now reconcile actorless subject alerts from
  subject-scoped cycle, dose, compound-catalog, and Labs reads. The global compound
  activation flag is frozen on scoped paths until it receives a reviewed
  `SubjectSetting` mapping.
- Direct WeightLog web/MCP writes and the Garmin/body-scan bridges now acquire one
  transaction-bound capability in governance -> active-weight advisory -> subject
  order. Provider-derived weights require a matching owned raw payload and
  connection, direct reads/notes reject foreign or partial roots, and deleting or
  moving an active row never silently reactivates a safety-blocked historical
  value. The global active-date and Garmin outbox-date keys and cross-domain
  chart/share/export readers remain registration blockers.
- BodyMeasurement and NoiseMarker direct web/MCP paths now use the selected
  subject and transaction-bound conflict capability before any target-row read.
  New rows retain manual versus MCP source and human actor provenance; edits,
  notes, date moves, and legacy adoption preserve their original attribution.
  Partial NULL roots fail closed, foreign IDs are non-enumerating, the Weight page
  composes its Weight/measurement/noise/GLP-1/Timeline series inside one subject,
  and the noisy-period alert is an actorless typed health alert. The global
  BodyMeasurement date key, BodyScan provenance, and whole-lake composition remain
  explicit registration blockers.
- BodyScan upload confirmation, structured MCP writes, direct reads, notes,
  deletes, alert reconciliation, and nightly replay now use the same selected
  subject and transaction-bound Weight capability. Upload graphs validate the
  uploader, private file, historical OpenRouter connection, raw payload, scan,
  and every metric as one S/A/C/F chain; partial or foreign roots fail closed.
  MCP scans persist their complete Source.MCP payload before normalization, keep
  Source.MCP on the scan, and link any derived Source.BODY_SCAN Weight fact to
  that exact raw record. Conflict evaluation is subject-scoped, visceral/phase
  alerts are actorless health alerts, and owned replay is isolated per raw row.
  Composite ownership FKs/backfill, raw natural-key uniqueness, and the remaining
  chart/share/digest/overview/export composition cutover still block registration.
- Genetics web, MCP, and local CLI imports now bind the selected owner before any
  catalog mutation. VCF imports persist a content-addressed S+A raw revision
  with null provider/file roots before linking curated variants; manual and MCP
  facts retain their own provenance, corrections preserve the original
  actor/source/raw roots, and a risk-to-reference re-import clears obsolete
  conflict markers. Direct reads and deletes are subject-scoped, partial or
  malformed raw graphs fail closed, and nightly replay can complete a partially
  normalized VCF batch without letting older pending evidence replace a newer
  fact. Versioned truncated raws now hash both the retained first-50k sample and
  the canonical curated tail evidence, so a changed tail creates a new revision
  and replay can rebuild every catalog fact without rewriting disappeared older
  evidence. Malformed v2 evidence and partial legacy raw candidates are rejected
  before adoption or mutation, including corruption outside a requested result
  limit. Lossless whole-VCF chunking, scoped rsID/raw uniqueness, composite
  ownership FKs, and whole-lake composition remain registration blockers.
- ProgressPhoto upload, gallery, Timeline markers, protected downloads, and
  deletes now accept only an exact subject/owner/private-file graph. New photos
  derive their storage key from one exclusive locked FileAsset; the compatibility
  bridge accepts only fully-null S/A/F history, while partial, cross-subject,
  wrong-purpose, retired, duplicated, or key-mismatched graphs fail closed.
  Deletion atomically removes the fact and marks its file metadata DELETED before
  bytes are unlinked, then records PURGED in a fresh transaction, so rollback and
  ambiguous commits cannot silently discard intimate medical images.
- Milestone web/MCP creates now stamp the selected subject and human actor;
  status changes, partial updates, and deletes lock and mutate only that subject
  while retaining the original actor. Goal lists, Timeline/Today cards, external
  glance cards, and live Weight/body-composition progress propagate the same
  exact-one subject scope. Whole-lake MCP snapshot/export/overview tools and
  manual, MCP, or scheduled weekly-digest generation now close at that same
  cardinality gate before serialization or an LLM request. Only fully-null
  historical S/A rows may use the compatibility bridge, partial roots fail
  closed, and a second subject closes the legacy surfaces instead of exposing
  another goal.
- The Garmin Weight export outbox now projects only the prepared subject and Garmin
  account, validates every linked Weight fact before using remote-delete authority,
  and records the human requester while scheduled work remains actorless. Settings,
  manual/MCP Weight writes, Garmin ingestion, body-scan derivation, and the scheduler
  use the scoped connection setting and transaction-bound outbox capability. Each
  vendor mutation revalidates live account roots after durable intent commits; a
  disabled or retired Garmin account stops new network activity without blocking a
  local health correction. The global outbox date key, provider credentials, and
  Redis/provider scheduling namespaces still block multi-subject registration.
- System alerts now have typed health-subject, provider-connection, and platform
  contexts backed by an exhaustive key/domain registry. The legacy owner sees
  health alerts plus current and retired provider alerts through one fail-closed
  aggregate, while maintenance alerts remain platform-only; web/MCP lifecycle
  actions retain the human actor. Garmin/Hevy operational alerts and every
  scheduler failure are stamped into an explicitly reviewed scope. Conflict-rule
  reads and all seven domain resolvers use one subject and evaluation date,
  distinguish curated global definitions from subject/custom rows, and serialize
  the exact-one legacy bridge against concurrent subject creation. Registration
  remains closed while the remaining alert writers, conflict mutations, global
  uniqueness, and subject-aware composition are migrated.
- The scheduled empty-day brief alert now reconciles as an actorless subject
  alert, adopting only the matching fully-null legacy row through the exact-one
  bridge. Brief context, OpenRouter rendering, durable digest storage, Telegram
  delivery, its journal, and alert bookkeeping use separate caller-owned phases,
  so no database transaction or ownership lock spans provider network latency.
- The Signals parser-outage warning now belongs to the exact subject and
  OpenRouter AI-gateway connection, with actorless raise and recovery. Telegram
  commits the complete raw update before parsing; live and replay parsing release
  ownership transactions before OpenRouter, persist raw/Signal outcomes first,
  and reconcile alerts best-effort afterward. Recovery on a replacement gateway
  can clear the same-subject warning on its validated historical gateway, while
  partial, foreign, pending, and ambiguous roots remain fail-closed. Outbound
  echo intent and subject-aware composition remain later commercial cutovers.
- Frozen doctor-report rows now belong to the exact legacy subject and creator;
  owner list, download, revoke, and delete actions validate that scope, and a
  human revoke records its revoker without inventing a missing historical
  creator. Public links remain opaque password-protected capabilities: corrupt
  roots look identical to dead links, successful opens revalidate and lock the
  token before counting, and access cookies bind both row id and token so id
  reuse cannot reopen another report. Password verification holds no database
  or identity-governance transaction, while an already-unlocked document renders
  before governance is released so a concurrent revoke cannot leak one final
  page. Scheduled expiry cleanup stays actorless. Snapshot assembly is still an
  exact-one whole-lake compatibility read; subject-aware report composition
  remains a later commercial cutover.
- Platform-only scheduler diagnostics are now excluded from the transitional
  Today and digest/brief composition readers. Operational exception text can no
  longer appear in a health card or be forwarded to the external LLM while the
  full subject-aware composition cutover is still in progress.
- Conflict-rule activation now belongs to the selected health subject while the
  curated definitions remain global. UI, MCP, and conflict evaluation use the
  same fail-closed activation state, with an exact-one legacy mirror during the
  closed-registration rollout. Supplements create, partial update, and activation
  now use a transaction-bound scoped writer proof, lock and refresh update targets,
  exclude the replaced row from safety evaluation, and attribute human overrides
  to the authenticated owner without exposing internal ownership identifiers.

### Fixed — durable inbound diagnostics

- **A failed Telegram update no longer hides why it failed** — the scoped
  durable delivery cutover re-raised `DurableInboundProcessingError` with
  `from None`, discarding the original exception. The cause is chained again,
  so a post-capture failure reports the underlying ownership or delivery error
  instead of only the wrapper. The web webhook still acknowledges the update
  without a traceback, so no message content reaches the logs.
- Four PostgreSQL race tests around concurrent and edited Telegram updates were
  left on the pre-cutover contract, where the passed-in notifier performed the
  send. They now supply a bound notifier and a `notifier_resolver`, matching how
  delivery resolves its endpoint at dispatch. The duplicate-webhook test no
  longer asserts an exactly-once parse through the injected parser seam: a plain
  callable leaves no reservation row, while the paid provider path still
  reserves one before dispatching and is covered separately.

### Fixed — pre-identity Garmin weight outbox

- **A paused Garmin request no longer blocks local weight writes.** The
  pre-identity branch of the weight exporter re-proved its zero-subject
  compatibility right after releasing the short outbox lease, but kept the
  transaction-scoped identity-governance advisory lock while the GET or POST was
  on the wire. Every concurrent local save and delete hook queued behind it for
  the duration of the vendor round trip. The legacy branch now proves the same
  fact in its own short transaction and commits it, exactly as the scoped branch
  already did, and fails closed if the database was bootstrapped mid-flight.
- **Legacy weight writes take identity governance in the canonical order.**
  `log_weight`, `update_weight_log`, and `delete_weight_log` reached identity
  governance only in the closing outbox hook — after the outbox advisory and
  every row lock, the inversion the canonical order exists to prevent. The
  compatibility guard fails closed on that inversion, so a save on a session that
  already held an open read transaction silently skipped its outbox projection: a
  local delete did not cancel a queued export, and a correction did not update
  one. The legacy branch now establishes that root before the outbox advisory,
  where `prepare_weight_write` puts it for a scoped write.
- Identity compatibility now has two explicit entry points instead of one:
  `authorize_pre_identity_compatibility_transaction` remains the boundary API
  that insists on a fresh guarded root, and `require_pre_identity_compatibility`
  adopts the transaction a service hook is already inside. Both hold the same
  governance lock and run the same zero-subject probe.

### Fixed — AI period-report context

- **Stored Garmin and treatment data now reaches the report** — context schema v2 includes bounded Garmin activities and expanded daily metrics, every same-day Hevy session, GLP-1 phases/injections/effects, and HRT plans/actual doses/effects. Garmin and Hevy remain separate sources so a synchronized session is not counted twice.
- **The report sees the rest of the relevant lake too** — body-measurement and BIA history with deltas, every lab result measured in the period plus saved retest metadata, complete nutrition macros, skincare applications/products, supplement notes/contraindications, curated genetics, signals, resolved day context, milestones, and non-duplicating timeline events. Raw payloads, paths, intraday samples, raw VCF data, and unbounded workout-set trees stay out.
- **Historical slices are actually historical** — one validated window bounds every dated query, today's scheduled report covers closed days while the one-day morning brief is explicit, and future rows cannot leak into an older report. Optional modules are gated before querying; per-domain coverage reports disabled/empty state, row dates, freshness, sample counts, and truncation so the model does not mistake hidden or partial data for missing data.
- Russian and English prompts now describe the emitted schema symmetrically, require stored lab follow-up cadence, and preserve compatibility for scheduled digests, morning briefs, doctor/share reports, and MCP snapshots. Public report/MCP windows remain capped at 90 days; the existing doctor-report choices retain their 180-day ceiling.

### Added — outbound Garmin weight sync

- **Explicit opt-in and live controls in Settings** — Vitals can send the latest direct local weight (manual, MCP, or body-composition scan) to Garmin Connect. The export interval (15 minutes by default) and freshness window (up to a hard 30-day ceiling) apply on the running scheduler without a restart, and **Send now** runs an explicit reconciliation. Newly saved Garmin credentials also take effect in the current process; the background job quietly no-ops while credentials are absent instead of burning retries and alerts.
- **Transactional outbox** (`GarminWeightExport`, migrations `0033`–`0034`) — local saves remain independent of Garmin availability. The safety upgrade quarantines pre-release rows whose old POST/ownership outcome cannot be proven, so they are neither repeated nor used to delete Garmin data. Ordinary transport failures retry with exponential backoff, while Settings distinguishes queued, checking, owned-and-sent, externally matched, conflict, unverified, and deletion states and surfaces the last error and next retry.
- **Empty-day write rule** — every POST is preceded by a fresh read and is allowed only when Garmin has no weigh-in for that day. One equal pre-existing entry is recorded as an external match, never as Vitals-owned; a different value, multiple entries, or an incomplete Garmin response becomes a visible conflict without adding another value or deleting someone else's data.
- **Ambiguous POSTs are never repeated** — Vitals commits a durable `unverified` dispatch marker before the non-idempotent request. Ownership requires either a `samplePk` returned by that POST or one sole read-back record matching its reserved millisecond timestamp, `MANUAL` source, and exact weight; equality by weight alone never authorizes deletion. Scheduled runs and **Send now** only force another safe reconciliation and never repeat an unverified POST.
- **Owned deletion and monotonic no-backfill cursor** — deleting a local weight queues cleanup only for the exact Garmin `samplePk` previously established as Vitals-owned. A durable cursor remembers the newest local date already observed, so deletion, disabling, or re-enabling cannot expose an older measurement as a new export candidate.
- This uses the same pinned, **unofficial** `garminconnect` web session as inbound sync. Garmin can change that private endpoint; the feature is off by default and has no official API guarantee.

### Added — Optional two-factor sign-in (TOTP)

- **Two-step login** (`web/auth.py`) — with 2FA on, a correct password no longer completes anything: it hands the browser a short-lived pending handle that grants no access, and the session is minted only at `/login/2fa` after a valid code. The handle is signed with its own salt (`vitals-2fa`), alongside the session and MCP salts, so it can never be presented where a real session is expected. The code field auto-submits at six digits.
- **Codes are stdlib** (`vitals/services/authentication/legacy_two_factor.py`) — RFC 6238 is an HMAC-SHA1 over a 30-second counter plus dynamic truncation, so there is no authenticator library. ±1 step for clock drift, constant-time compare per candidate step, and the matched step is burned in Redis so the same six digits can't be replayed by whoever read them over your shoulder. Conformance is pinned against the published RFC test vectors.
- **Enrolment in Settings**, off by default — a QR (inline SVG, `segno`) for a second device, the key in text with a copy button, and an `otpauth://` link for an authenticator on the machine showing the page. A freshly minted secret is stored **unconfirmed** and grants nothing until a code from it is typed back, so a key that never reached the app can't lock the owner out. Turning 2FA off requires a current code — otherwise a stolen session cookie could switch off the very factor that makes the cookie insufficient.
- **Backup symmetry** (`vitals/services/data_portability_service.py`) — the exporter already dropped `app_settings` keys that look like a credential; the importer now mirrors that rule and neither deletes nor accepts them. Without the mirror, restoring any legitimate backup silently switched 2FA off (the file never carries the key, and the restore wipes before it reloads), and an uploaded file could plant a chosen secret without presenting a code.
- No new environment variable and no migration: the state is one row in `app_settings`, and the key name keeps it out of every downloaded backup. Restoring onto a fresh server therefore leaves 2FA off — deliberately, and it fails toward "the password still works" rather than locking the owner out.

### Added — Signals & the proactive layer (15th module, `signals`)

- **Signals** (`Signal`, `DayContext`, migration `0029_signals`) — the capture domain for everything that happens "in the moment" and has no shape ("headache", "coffee at 22:00"). Free text arrives over Telegram, lands in `raw_payloads` **before** any parsing, and is split into `Signal` rows of three kinds (`state` / `symptom` / `exposure`) sharing a `batch_id` — the unit the echo's "wrong" button undoes. Keys stay free text during the shake-out period and are folded to canonical names **on read** (`KEY_ALIASES`), so consolidating the vocabulary later is a dict edit rather than a migration.
- **Telegram channel** (`web/routers/telegram.py`, `vitals/services/proactive/`) — a webhook on a secret path (`/tg/<path>`), verified by the `X-Telegram-Bot-Api-Secret-Token` header with `compare_digest`, rate-limited, listening to exactly **one** chat id and failing closed (401) when unconfigured. Idempotent by `update_id` keyed into `raw_payloads`. The channel sits behind a `Notifier` protocol — nothing above it imports `httpx` or knows the word "telegram".
- **Delivery gate** (`NotificationLog`, migration `0030_notifications`) — one place that decides whether a message may leave: module off, dedupe key, quiet hours (nudges only) and a daily budget across the three self-initiated categories. Replies to the owner are exempt from the budget on purpose.
- **Morning brief** (migration `0031_digest_kind`) — deterministic blocks assembled by code from the same cross-domain context the weekly digest uses; the model contributes exactly one interpretation paragraph, and an LLM failure drops that block only. An empty day sends nothing and raises a passive `info` alert instead. Stored in `weekly_digests` with `kind='daily_brief'`, visible on `/reports` alongside "build" and "send a test" buttons.
- **Evening block & week template** — a 23:45 message (deliberately not midnight) that sums the day up and asks about tomorrow; the week template pre-fills what a weekday can predict, and every button carries its own date so a tap after midnight still answers the right day. What was guessed (`planned`) is stored next to what was answered (`answers`).
- **Nudges** — a registry of specs (condition, text, cooldown, category toggle) walked by one engine, hourly at :05. Three categories: activity, nutrition, data freshness. Every condition checks the clock itself and stays silent on missing data.
- **Settings card** (`/settings` → Proactive layer) — brief and evening times, quiet hours, daily budget, nudge categories, Garmin poll interval and light-pulse window, plus the week template. Stored in `app_settings`, and saving **re-registers the jobs on the running scheduler** — no container restart.
- **`/signals` page** — the capture feed, a key-frequency table showing the real phrasings behind each key (material for the future key registry), and per-row deletion of misparsed entries. Read-and-delete only: capture belongs to the bot.
- **Second pass at unparsed messages** — messages the parser choked on are retried by the morning brief (one week back, up to 20), and a successful parse finally marks the raw row processed.
- **Signals reach the models** — `assemble_context` now carries signals (with the hour attached) and `day_context`, so both the weekly digest and the brief see the circumstances behind the numbers; both system prompts describe the blocks. Rows tapped "wrong" are excluded.
- **MCP** — `get_signals`, `log_signal`, `get_day_context`.
- Optional module, **off by default**, and it doubles as the master switch: `signals` off silences the bot entirely.
- Config: `VITALS_TELEGRAM_BOT_TOKEN`, `VITALS_TELEGRAM_CHAT_ID`, `VITALS_TELEGRAM_WEBHOOK_PATH`, `VITALS_TELEGRAM_WEBHOOK_SECRET`, `VITALS_LLM_MODEL_BRIEF` (empty → the digest model).

### Added — data lake

- **Nightly re-parse sweep** (`raw_payload_sweep`, 03:30) — `upsert_raw_payload` has always reset `processed_at` on refresh, but only signals ever read it back; labs and body composition now join Garmin and Hevy in a single shared job, each domain committing independently.
- **Source VCF kept** — genetics imports store the recognized VCF rows in `raw_payloads` (up to 50k per import), so extending the interpretation dictionary re-reads the old file instead of asking for a re-upload.
- **Whole Garmin row in the LLM export** — the `garmin_daily` / `garmin_activities` export blocks dumped a hand-picked dozen of ~45 fields; they now dump every mapped column minus plumbing, so new metrics join automatically (the tall intraday sample table stays out).
- Import summaries now label `signals`, `day_context`, `body_scans`, `milestones` and `noise_markers` instead of counting them as "and N more rows".

### Added — MCP layer (**75 tools**: 33 read + 40 write + 2 sync)

- **Closing the loop, not just opening it** — the connector could see work but not finish it. Now: `resolve_alert` / `override_alert`, `update_lab_result` (recomputes the out-of-range flag, refreshes alerts), `update_event`, `log_day_context` (routed through the evening block's `record_answer`, so the template's guess is kept next to the answer), `mark_signal_misparse`.
- **Domains brought to parity** — HRT gains `update_hrt_dose`, `log_hrt_side_effect`, `close_hrt_cycle`; genetics gains `upsert_genetic_variant` and a gene/rsid filter on `get_genetics_snps`, which previously returned the first 100 alphabetically with no way to ask for one variant; the proactive layer gains `get_proactive_state` and `set_week_template`. Bot configuration stays read-only on purpose — the connector records facts about a life, it does not retune the bot.
- **On-demand sync** — `sync_garmin(days)` and `sync_hevy` let the connector refill a gap it can see (`get_data_overview` says the last two days are empty) instead of reporting stale numbers and waiting for the next scheduled poll. Both are capped at **3 calls a day** each, counted per calendar day in Redis: a sync is an outbound call to someone else's API, Garmin throttles logins, and the scheduler already polls both several times a day. Over the cap the tool returns an error without going anywhere; the scheduled sync is unaffected. `sync_hevy` honours the module toggle.
- **`Source.MCP`** — records written through the connector carry their own provenance instead of masquerading as manual entry. In the weight source priority `mcp` ranks equal to `manual`, so "manual beats Garmin" still holds and recency decides between equals. Existing rows are not relabelled.

### Changed — MCP surface

- **12 `delete_*` tools → one `delete_record(domain, record_id)`** — every deletion service shares the signature `(session, id) -> bool`, so the twelve near-identical tools collapse into a domain map. Reconnect the connector to pick up the new tool list.
- **`export_everything(domains, since)`** — the default call now returns the **last 90 days** instead of the entire history; the full record is one explicit `since` away. The web export endpoint is unchanged and still returns everything.
- **The module toggle is enforced at one shared entry point**, not on 3 tools out of ~40 — a write into a disabled optional module is now refused everywhere, and a new tool inherits the check instead of having to remember it.
- `_parse_date` reports which argument was wrong and what shape it expected (`on_date must be a YYYY-MM-DD date, got 'вчера'`) instead of surfacing a raw parser error.
- **A response pays only for what was asked** — three places were spending the conversation's context on data no question had needed.
  - **`get_garmin_metrics(sleep_detail=False)`** — the per-minute sleep-stage timeline and breathing events are ~70% of a Garmin daily row and used to ride along on every read of the last hundred nights. They now fold to a count plus a hint (`"28 entries — call again with sleep_detail=True"`) rather than disappearing: silence would read as "this night has no stages". The switch is separate from `intraday`, so asking about the shape of one night doesn't pull every curve in the window. A 100-day read drops from ~96k to ~24k tokens; a night that was never measured still says nothing at all.
  - **`serialize_row` drops bookkeeping and unset fields** — `domain`, `created_at`, `updated_at` and `raw_payload_id` are columns no tool accepts back, and an absent key reads the same as a `null` while costing nothing. `id`, `date` and `source` stay, so edits, deletes and weight provenance are unaffected. Rows shrink 39–59%.
  - **A switched-off module's tools are no longer listed** — they already refused the call, so listing them only spent budget on schemas for domains the owner does not track (75 tools / ~13k tokens → 33 / ~6k with every optional module off). Resolved per request, so a toggle takes effect on the connector's next reconnect; if the module state can't be read, the full surface is listed rather than an empty one.

### Fixed — MCP data loss

- **The edit tools were destroying data.** `update_meal` / `update_glp1` / `update_supplement` replaced the whole row while every argument but one defaulted to `None`, and `on_date` defaulted to *today* — so renaming a meal blanked its calories and moved it to the current date, and renaming a supplement re-enabled a disabled one and wiped its dose. All `update_*` tools now merge: a field left out keeps its stored value, and an omitted date keeps the record's own date. The web forms are unaffected — there, clearing a field is still meant to clear the column.
- **`get_data_overview` under-reported the lake** — the "what do I even have" tool did not know about signals, day context or any of HRT, so a model that honestly started by orienting itself concluded those domains did not exist. A guard test now fails when a domain is added without a matching overview entry.
- **Lab results bypassed the conflict engine** — `add_result` was the only writing service without the gate, despite 31 curated lab rules and a registered resolver. It now runs `enforce` like every other domain, with the same `override` path.

### Security

- **PKCE is mandatory on `/oauth/authorize`.** An authorization request without a `code_challenge` used to skip verification entirely; it is now rejected. The metadata already advertised `S256` only and `verify_pkce` already refused `plain` — this closes the last gap.
- **`/.well-known/oauth-protected-resource`** (RFC 9728), and a `401` from the MCP endpoint now answers with `WWW-Authenticate: Bearer resource_metadata="..."` instead of a bare `Bearer`, so a client can discover where to authorize.

### Added — conflict engine

- Two new rule families (**116 curated rules** total): GLP-1 × labs and HRT × skincare.

### Changed — Garmin

- The daily sync now also pulls the **whole-day heart-rate curve** (`get_heart_rates` → the `heart_rate` intraday series), and the overview chart draws it alongside stress and Body Battery on its own right-hand bpm axis. Available through `get_garmin_metrics(intraday=True)` too. Only days synced from here on carry it — the curve was never fetched before, so it isn't in the stored payloads to reparse.
- Credential logins are **rationed** (3 per 24h, then a 6h pause, both in Redis) — Garmin rate-limits per account and every retry extends the block, so the breaker fails closed. Resuming a token session can no longer silently escalate into a credential login. MFA detection works again (`return_on_mfa`), and a throttled login is reported apart from bad credentials.
- The token store is backed up: `backup.sh` archives the `vitals_garmin_session` volume next to the SQL dump, same rotation. A lost session can be impossible to log back in; the database can always be re-synced.
- `garmin.garth.dumps/dump` had been dead since a library upgrade (swallowed by a bare `except`) — rewritten against the current API, and token-store failures now raise a `warn` alert.
- The poll schedule moved out of the code into the settings card, and a **light pulse** (today's steps, one request, no login) runs between full syncs inside a configurable active window.

### Changed — dependencies & transport

- **Python 3.13** base image; safe upgrades across the Python dependency set.
- `fastmcp` 2.2.0 → **3.4.5**, and the MCP server moved from SSE to **streamable HTTP** at `/mcp/` (the mounted app's lifespan is now entered explicitly, without which every request failed with "manager not initialized").
- `garminconnect` 0.3.2 → **0.3.7**, still pinned: the image resolves requirements on every rebuild, so an open range means an unattended deploy could swap the login machinery under a working token.
- Frontend vendor bundles: Alpine.js 3.15.12, Chart.js 4.5.1 (+ annotation plugin).
- `docs/known-good-deps.txt` — a snapshot of what prod actually runs, as a reference point after upgrades.

### Fixed

- **Scheduler keepalive never ran** — the one always-on liveness signal was registered as a lambda returning a coroutine, which APScheduler called synchronously and threw away. The heartbeat had not been stamped since startup.
- **Digest narratives were being truncated** — `max_tokens=6000` is shared with a reasoning model's thinking tokens, so the visible narrative hit the ceiling and was persisted cut off mid-word. Raised to 16000, and a `finish_reason == "length"` now logs a warning.
- A signals-parser outage alert now clears as soon as the model answers again.
- Saves no longer jump the page to the top: they re-fetch over htmx and swap only the guts of `<main>`, holding the scroll offset across every swap that lands on the current page.
- The bottom fade on a capped list only appears when the list actually overflows (bound to a `scroll(self)` timeline), instead of smudging the bottom edge of a short table.
- Proactive settings that get clamped on save now say so instead of reporting a plain "saved" while the number was quietly changed.
- Contrast: `--violet` lightened to `#BCA4DC`, and `.v-chip.bad` uses `--bad-strong` (plain `--bad` on `--bad-soft` measured 3.58:1).
- `touch-action: manipulation` on tap targets removes the ~300ms click delay.

---

### Added — HRT / TRT

- **HRT / TRT** (new Optional module, `hrt`) — harm-reduction tracker for hormone/TRT and anabolic-steroid cycles: testosterone esters, ancillaries (AI/SERM/HCG), cycle compounds (tren/EQ/mast/primo/orals) and GH/IGF-1/peptides. Tracking only — no dosing advice.
- Curated **compound catalog** (`vitals/data/hrt_compounds.yaml`, 73 molecules across 15 classes) with ester, route, half-life and active-hormone mass fraction; seeded idempotently on startup by `hrt_catalog.sync_catalog` (keyed on a stable `key` slug, like the conflict-rule catalog). Multi-ester blends (Sustanon) carry a per-ester breakdown.
- **Dose log** with ml→mg computation (volume × concentration) and grey-market provenance fields (brand / lab / batch / measured concentration) on each administration; HRT-specific injection-site rotation grid; side-effect log graded 1-5.
- Conflict-engine resolver (`hrt_service.resolve_active`) exposing recently-dosed compounds so cross-domain rules can reference the current protocol.
- Optional module, default OFF; migration `0024_hrt` creates the tables.

### Added — HRT cycles, release model & bloodwork

- **Cycles** (`HrtCycle`/`HrtCycleItem`, migration `0025_hrt_cycles`) — protocol plans by kind, each with a per-compound **schedule engine**: segment lists (flat or a linear ramp) expanded off a fixed grid anchored at the cycle start, supporting fractional intervals (E3.5D) and titration.
- **Active-release model** — a server-rendered curve estimating active-hormone mg in the body over time (sum of each administration's exponential decay by half-life × active fraction), over actual doses plus the active cycle's projected plan.
- **Protocol-aware reminders** (daily scheduler job `hrt_reminders`) — bloodwork-due while on cycle (cadence by kind) and missed-injection nags off the fixed grid; both idempotent passive alerts. Seeds a hormone/safety **bloodwork panel** into the Labs catalog with retest intervals.
- **Cross-domain safety rules** (soft_warn, never block) — oral 17-aa + high ALT/AST, active testosterone + high hematocrit, 19-nor + high prolactin.
- **MCP tools** — `log_hrt_dose`, `get_hrt_logs`, `add_hrt_cycle`, `add_hrt_cycle_item`, `get_hrt_cycles`.

### Added — HRT week-staggered courses & shareable cycle templates

- **Per-compound start offset** (`start_offset_days` on `HrtCycleItem`, migration `0026`) — a cycle item's schedule grid can now anchor at `cycle start + N days` instead of day 0, enabling real multi-compound week-anchored protocols (e.g. an oral from week 5, ancillaries weeks 5–9, PCT weeks 11–13). The web form takes a friendly "start week" field; planned doses, the release curve and injection reminders all respect the offset.
- **Cycle templates** (`HrtCycleTemplate`/`HrtCycleTemplateItem`, migration `0027`) — save an active cycle's plan as a **date-free, reusable template** and later materialize it into a new cycle at any start date (kind, per-compound offsets and schedules carry over; the usual open-cycle auto-close applies).
- **Template sharing** — export any template as portable JSON (`vitals.hrt_cycle_template` v1, copyable share-code block or `.json` download) and import someone else's by pasting it; portable across self-hosted instances because items reference the shared compound catalog by slug. Imports are strictly validated (envelope/version, cycle kind, units, offsets, compound keys against the local catalog, schedule shape) and never half-import.
- **Schedule validation hardened** — all cycle-item write paths (form, MCP, template import) now funnel through a single `validate_schedule` normalizer that rejects malformed segments and strips unknown keys.
- Active-cycle card now shows the kind's bloodwork cadence, so cycle kinds visibly differ beyond the label.
- **Cycle kinds collapsed to two** (migration `0028`): `course` (any exogenous-hormone protocol — TRT/blast/cruise nuance goes in the cycle name) and `pct` (its own tighter bloodwork cadence, 30 vs 90 days). The old five kinds only differed by label; `add_cycle` now validates the kind.
- **Inline plan-item editing** — a cycle item's dose/interval/duration/start week can be edited in place (no more delete + re-add); multi-segment/ramp schedules keep their shape and only expose the start week in the form.
- **Import duplicate handling** — pasting the same share code twice is rejected as a duplicate; a name clash with different content gets a numbered name (`X (2)`) instead of silently shadowing.

---

## [1.2.0] — 2026-07-12

### Changed — Timeline

- Cross-domain event feed now draws from every domain instead of 5: added supplement start/stop, skincare product added/removed, GLP-1 side effects (severity ≥ 3), full milestone lifecycle (set/achieved/missed, not just achieved), genetics import batches, and progress photos (rendered inline as a thumbnail — BIA/InBody scan sheets get the same thumbnail treatment for free)
- Lab-draw events now reflect the actual result: tone follows the worst flag in that day's batch (critical/out-of-range/normal) instead of always rendering neutral, and flagged marker names appear in the event detail
- Fixed a rendering bug where `warn`-tone events (illness/travel annotations, noisy-weight periods) were visually indistinguishable from `bad`-tone ones — they now use separate colors

---

## [1.1.0] — 2026-07-09

### Added — Timeline

- **Timeline** (13th module) — cross-domain event feed: manual annotations (life events, illness, travel, protocol changes) merged with events derived live from other domains' own rows (GLP-1 dose changes, lab draws, BIA scans, achieved milestones, noisy weight periods)
- Manual annotation flags rendered as Chart.js overlays on the weight chart and any custom chart whose series touch an annotated domain
- MCP: `get_timeline` (read) and `log_event` (write) — 37 tools total (22 read + 15 write)
- Optional module (`timeline`), toggleable in Settings; migration `0018_timeline_annotations` seeds it ON
- `export_llm` gained a `timeline_annotations` block; full backup/restore picks up the new `annotations` table automatically

---

## [1.0.0] — 2026-06-27

### Initial public release

**Core infrastructure**
- FastAPI application with Jinja2 + HTMX + Alpine.js frontend
- PostgreSQL 15 + SQLAlchemy 2 async ORM + Alembic migrations
- Redis for scheduler locks and Garmin session caching
- Docker Compose setup with loopback-only port binding (`127.0.0.1:8000`)
- APScheduler for background jobs
- Atomic database backup & restore

**Authentication & Security**
- Single-user bcrypt password authentication
- Signed session cookies (itsdangerous)
- CSRF protection via Origin header validation
- CSP headers
- MCP OAuth 2.0 + PKCE for Claude.ai integration

**Health Domains (12 modules)**
1. **Weight & Body Composition** — WeightLog, BodyMeasurement, ProgressPhoto; US Navy body fat formula; 7-day moving average; linear regression + goal projection; Garmin import with manual override
2. **GLP-1 Protocol** — Injection log (Semaglutide / Tirzepatide); dose phase overlays; plateau detection (>14 days, <100g/week trend)
3. **Garmin Connect** — Auto-sync via garth: HRV, sleep, resting HR, stress, Body Battery, Training Readiness; Health Auto Export JSON fallback
4. **Hevy Workouts** — API sync: exercises, sets, reps, weight; cross-reference with Garmin recovery
5. **Nutrition** — Meal logging with calories + macros; configurable daily targets; included in AI digests
6. **Supplements Catalog** — Evidence-tier catalog (A/B/C); Conflict Engine integration
7. **Skincare Log** — Morning/evening routine; skin status; acid + retinoid conflict warnings
8. **Lab Results & OCR** — PDF/image upload → LLM extraction; out-of-range flagging; history charts
9. **Genetics (VCF)** — VCF parser → health-relevant SNPs; feeds Conflict Engine
10. **Milestones & Goals** — Numeric targets + deadlines; real-time progress %
11. **Weekly AI Digests** — LLM narrative via OpenRouter; configurable model; cross-domain correlations
12. **MCP Integration** — 25 FastMCP tools (14 read + 11 write) for Claude.ai via OAuth 2.0 + PKCE

**Architecture**
- `vitals/` core layer: zero web dependencies, importable in scripts and tests
- `web/` delivery layer: FastAPI, auth, CSRF, Jinja2; zero business logic
- `InsightsMixin` shared interface across all 12 domain models
- `raw_payloads` JSONB table: all API responses preserved for future re-parsing
- Conflict Engine: soft/hard warnings with override audit trail

**Developer experience**
- `python run_local.py` — SQLite + FakeRedis, no Docker needed
- 20 test modules, 100+ tests
- Integration test suite against real Postgres (`scripts/test_postgres.sh`)
- `.env.example` with full documentation
- PWA: installable on iOS/Android Home Screen
