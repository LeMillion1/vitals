# Changelog

All notable changes to Vitals are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [Unreleased]

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
