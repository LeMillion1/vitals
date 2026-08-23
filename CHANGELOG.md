# Changelog

All notable changes to Vitals are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [Unreleased]

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
  `professional_record_service`. A doctor's reading of a lab panel is not the
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
- Consent defaults now separate the two kinds of resource. Patient facts stay
  read-only for both kinds; the professional's own artifacts carry create and
  update, because that is where the read-only rule sends them. Delete is absent
  from both.
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
- **Doctors and trainers differ by domain**, and the split is about what the
  work needs rather than seniority: a trainer planning sessions needs load,
  bodyweight and recovery, not a genome, a hormone schedule or a lab panel.
  Defaulting them in would make the narrower choice the one a patient has to
  know to ask for. The kind lives on the relationship, not on the profile, so an
  account that is both cannot take the wider of the two.
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

- `professional_invitations` plus `invitation_service`: a patient offers one
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
  `invitation_service.accept` joins the enumerated allowlist.

### Added — a professional's claim, and an operator deciding about it (PR-07)

- `professional_profiles` holds what somebody claims about themselves: a name, a
  licence number, a kind. `professional_service` is the operator workflow that
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

- **Vitals stops authenticating anybody.** `vitals/services/oidc.py` verifies
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
  version and the provider's `auth_time`. `session_service` confirms each
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
- **Codes are stdlib** (`vitals/services/twofa_service.py`) — RFC 6238 is an HMAC-SHA1 over a 30-second counter plus dynamic truncation, so there is no authenticator library. ±1 step for clock drift, constant-time compare per candidate step, and the matched step is burned in Redis so the same six digits can't be replayed by whoever read them over your shoulder. Conformance is pinned against the published RFC test vectors.
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
