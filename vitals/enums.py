"""Domain enums — single source of truth for the status/domain/source strings
that the Insights Layer relies on (``InsightsMixin.domain``/``.source``,
``system_alerts.severity``, ``conflict_rules.rule_type``).

``StrEnum`` members *are* their string value, so they store directly in the
``VARCHAR`` columns and compare equal to plain strings — no migration coupling.
"""
from __future__ import annotations

from enum import StrEnum


class UserStatus(StrEnum):
    """Lifecycle state of an application identity."""

    PENDING = "pending"
    ACTIVE = "active"
    SUSPENDED = "suspended"


class UserRoleName(StrEnum):
    """Additive application roles.

    Roles describe product capabilities only. In particular,
    ``PLATFORM_SUPERADMIN`` never grants access to a health subject by itself;
    support access additionally requires a live, explicitly scoped grant.
    """

    MEMBER = "member"
    DOCTOR = "doctor"
    TRAINER = "trainer"
    PLATFORM_SUPERADMIN = "platform_superadmin"


class RegistrationAccountKind(StrEnum):
    """Non-privileged account shape requested during public admission."""

    MEMBER = "member"
    DOCTOR = "doctor"
    TRAINER = "trainer"


class RegistrationInvitationStatus(StrEnum):
    """Lifecycle of one account-admission invitation."""

    PENDING = "pending"
    CONSUMED = "consumed"
    REVOKED = "revoked"
    EXPIRED = "expired"


class RegistrationRequestStatus(StrEnum):
    """Lifecycle of one operator-reviewed account-admission request."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class SupportAccessMode(StrEnum):
    """Maximum purpose approved for one time-limited support grant."""

    READ = "read"
    REPAIR = "repair"
    EXPORT = "export"


class SupportAccessStatus(StrEnum):
    """Persisted lifecycle marker for a support-access grant."""

    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"
    CONSUMED = "consumed"


class ExternalApiTokenStatus(StrEnum):
    """Lifecycle of one subject-scoped external API credential."""

    ACTIVE = "active"
    REVOKED = "revoked"


class SupportAccessRequestStatus(StrEnum):
    """Lifecycle of an *ask* for support access, which is not access.

    Separate from :class:`SupportAccessStatus` because the two describe
    different objects. A row in ``support_access_grants`` is authorization that
    somebody already approved — its constraints say so: an approver who is not
    the grantee, and an expiry strictly after the approval. There is no state of
    that row meaning "nobody has agreed yet", and adding one would cost exactly
    those two guarantees. So the ask lives in its own table and ends by
    producing a grant, or by not producing one.
    """

    PENDING = "pending"
    APPROVED = "approved"
    DECLINED = "declined"
    WITHDRAWN = "withdrawn"
    EXPIRED = "expired"


class SupportRepairStatus(StrEnum):
    """Lifecycle of one separately reviewed, exact support repair."""

    PROPOSED = "proposed"
    APPROVED = "approved"
    DECLINED = "declined"
    EXECUTED = "executed"
    STALE = "stale"
    REVERTED = "reverted"


class BreakGlassStatus(StrEnum):
    """Lifecycle stored for one independently approved emergency session."""

    PENDING = "pending"
    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"


class ProfessionalKind(StrEnum):
    """What a professional is, from the patient's point of view.

    Deliberately the same vocabulary as the roles rather than a richer taxonomy.
    A person may hold both; the relationship names which one it is, because the
    defaults a doctor gets and the defaults a trainer gets differ by domain and
    a single account holding both must not silently take the wider of the two.
    """

    DOCTOR = "doctor"
    TRAINER = "trainer"


class ProfessionalVerificationStatus(StrEnum):
    """How far an operator has got with checking who this person claims to be.

    Only ``VERIFIED`` is ever a basis for anything. The rest exist so the state
    a profile is in has a name — an unverified profile is not a broken one, it
    is one nobody has looked at yet, and telling those apart is what makes the
    queue workable.
    """

    UNVERIFIED = "unverified"
    PENDING = "pending"
    VERIFIED = "verified"
    REJECTED = "rejected"
    SUSPENDED = "suspended"


class ProfessionalInvitationStatus(StrEnum):
    """Lifecycle of one offer to enter into care for a patient.

    ``PENDING`` is the only state a token opens. The others exist so that a
    refused acceptance can say *nothing* while the record still knows which of
    them it was — a caller must not be able to tell a spent invitation from an
    expired one from one that never existed, because those three answers
    together are a map of who is treating whom.
    """

    PENDING = "pending"
    ACCEPTED = "accepted"
    REVOKED = "revoked"
    EXPIRED = "expired"


class CareRelationshipStatus(StrEnum):
    """Whether a professional is currently in care for this patient.

    ``PAUSED`` and ``ENDED`` are kept apart deliberately. A pause is the patient
    stepping back — a treatment break, a second opinion, a holiday — and resuming
    it must not need a new invitation. An end is an end, and re-entering care is
    a fresh offer the patient has to make again.
    """

    ACTIVE = "active"
    PAUSED = "paused"
    ENDED = "ended"


class ConsentStatus(StrEnum):
    """Lifecycle of one version of what a patient agreed to.

    Consent is versioned rather than edited. Narrowing what somebody may see is
    a new version superseding the old, so "what was this professional allowed to
    read on the day they read it" stays answerable — which is the question any
    later dispute is actually about.
    """

    ACTIVE = "active"
    PAUSED = "paused"
    SUPERSEDED = "superseded"
    REVOKED = "revoked"
    EXPIRED = "expired"


class CarePlanStatus(StrEnum):
    """Whether a plan is being written, being followed, or is over.

    A plan is never deleted. What somebody was told to do last spring is part of
    the record of their care, and a plan that can vanish is one the patient
    cannot hold anybody to.
    """

    DRAFT = "draft"
    ACTIVE = "active"
    ARCHIVED = "archived"


class CareThreadStatus(StrEnum):
    """Whether a care-team conversation is still being had.

    A closed thread is read-only and stays where it is. Deleting one would take
    away a history the patient can currently read, which is the opposite of what
    a patient-visible channel is for.
    """

    OPEN = "open"
    CLOSED = "closed"


class CarePushDeliveryStatus(StrEnum):
    """At-most-once lifecycle of one care-message device notification."""

    PENDING = "pending"
    DISPATCHING = "dispatching"
    SENT = "sent"
    AMBIGUOUS = "ambiguous"
    CANCELLED = "cancelled"


class CarePushDeliveryErrorCode(StrEnum):
    """PHI-free, allowlisted outcomes for care push delivery."""

    ACCESS_REVOKED = "access_revoked"
    ACCOUNT_INACTIVE = "account_inactive"
    SUBSCRIPTION_REVOKED = "subscription_revoked"
    STALE_PENDING = "stale_pending"
    PROVIDER_GONE = "provider_gone"
    PROVIDER_REJECTED = "provider_rejected"
    TRANSPORT_ERROR = "transport_error"
    INVALID_RESPONSE = "invalid_response"
    STALE_DISPATCH = "stale_dispatch"
    INTERNAL_ERROR = "internal_error"


class SupportScopeResourceType(StrEnum):
    """Kind of resource named by an explicit support-access scope."""

    DOMAIN = "domain"
    ARTIFACT = "artifact"
    OPERATION = "operation"


class AuditOutcome(StrEnum):
    """Result recorded by an immutable audit event."""

    SUCCESS = "success"
    DENIED = "denied"
    FAILED = "failed"


class IntegrationProvider(StrEnum):
    """External systems represented by a subject or platform connection root."""

    GARMIN = "garmin"
    HEVY = "hevy"
    OPENROUTER = "openrouter"
    TELEGRAM = "telegram"


class IntegrationConnectionType(StrEnum):
    """Stable purpose of an integration connection.

    Sync/run state is deliberately not represented here. One account can later
    have several independently scheduled operations without changing identity.
    """

    ACCOUNT = "account"
    IMPORT = "import"
    AI_GATEWAY = "ai_gateway"
    RECIPIENT = "recipient"


class IntegrationConnectionStatus(StrEnum):
    """Lifecycle of an integration connection, not transient sync health."""

    LEGACY = "legacy"
    PENDING = "pending"
    ACTIVE = "active"
    DISABLED = "disabled"
    RETIRED = "retired"


class AIInvocationPurpose(StrEnum):
    """Bounded product purpose for one paid platform AI operation."""

    WEEKLY_DIGEST = "weekly_digest"
    DAILY_BRIEF = "daily_brief"
    LAB_DOCUMENT_PARSE = "lab_document_parse"
    BODY_SCAN_PARSE = "body_scan_parse"
    SIGNAL_PARSE = "signal_parse"
    QUESTION_REPLY = "question_reply"


class AIInvocationSource(StrEnum):
    """Authenticated surface or system boundary that initiated an AI call."""

    WEB = "web"
    MCP = "mcp"
    SCHEDULER = "scheduler"
    TELEGRAM = "telegram"


class AIInvocationStatus(StrEnum):
    """Durable lifecycle of one idempotent, potentially paid AI attempt."""

    PREPARED = "prepared"
    DISPATCHING = "dispatching"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    AMBIGUOUS = "ambiguous"
    CANCELLED = "cancelled"


class AIInvocationErrorCode(StrEnum):
    """Allowlisted operational failure codes; never free-form provider detail."""

    PROVIDER_UNCONFIGURED = "provider_unconfigured"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    TIMEOUT = "timeout"
    INVALID_RESPONSE = "invalid_response"
    CANCELLED_BY_POLICY = "cancelled_by_policy"
    QUOTA_EXCEEDED = "quota_exceeded"
    INTERNAL_ERROR = "internal_error"


class NotificationDeliveryStatus(StrEnum):
    """Durable lifecycle of one at-most-once outbound delivery attempt."""

    PENDING = "pending"
    DISPATCHING = "dispatching"
    SENT = "sent"
    AMBIGUOUS = "ambiguous"
    CANCELLED = "cancelled"


class NotificationDeliveryErrorCode(StrEnum):
    """Allowlisted delivery outcomes; never free-form transport detail."""

    TRANSPORT_ERROR = "transport_error"
    INVALID_RESPONSE = "invalid_response"
    STALE_DISPATCH = "stale_dispatch"
    CANCELLED_BY_POLICY = "cancelled_by_policy"
    STALE_PENDING = "stale_pending"
    SCOPE_INVALID = "scope_invalid"
    INTERNAL_ERROR = "internal_error"


class FileStorageBackend(StrEnum):
    """Physical storage class for a private file asset."""

    LEGACY_LOCAL = "legacy_local"
    PRIVATE_LOCAL = "private_local"
    OBJECT_STORE = "object_store"


class FileAssetStatus(StrEnum):
    """Lifecycle of owned file metadata and its referenced bytes."""

    LEGACY_PLACEHOLDER = "legacy_placeholder"
    PENDING = "pending"
    ACTIVE = "active"
    DELETED = "deleted"
    PURGED = "purged"


class FileAssetPurpose(StrEnum):
    """Persisted medical-file purposes that exist in Vitals today."""

    PROGRESS_PHOTO = "progress_photo"
    LAB_DOCUMENT = "lab_document"
    BODY_SCAN_DOCUMENT = "body_scan_document"
    CARE_MESSAGE_ATTACHMENT = "care_message_attachment"


class Severity(StrEnum):
    """system_alerts ladder (see services/alerts_service.py).

    - ``NOTE``  — an *interpretation*, never a failure: the app read the data and
      has something to say about it (recovery is low, the dose has plateaued).
      Nothing is broken, nothing needs dismissing, and it never blocks a save.
      Split out of ``WARN`` because painting a reading of the numbers in the same
      amber as "Garmin needs MFA" taught the owner to ignore both.
    - ``INFO``  — passive UI badge (noisy-weight period active, goal deadline near).
    - ``WARN``  — non-intrusive UI status only, never popups/modals (Garmin MFA
      needed, an integration stopped syncing).
    - ``BLOCK`` — raised as a pre-save validation error; overridable via the
      conflict-engine flow.

    The column is a plain ``VARCHAR(16)`` with no CHECK constraint, so adding a
    member needs no migration.
    """

    NOTE = "note"
    INFO = "info"
    WARN = "warn"
    BLOCK = "block"


class RuleType(StrEnum):
    """conflict_rules kinds (data-driven cross-domain rules)."""

    HARD_BLOCK = "hard_block"          # block save unless overridden
    SOFT_WARN = "soft_warn"           # write an alert, never block
    TIMING_SEPARATION = "timing_separation"  # e.g. separate two items by N hours


class Domain(StrEnum):
    """Every log/metric row carries one of these in ``InsightsMixin.domain``.

    One per module (plus ``SYSTEM`` for infra/non-domain alerts), so mass export
    and analytical filtering are uniform across the data lake.
    """

    WEIGHT = "weight"
    BODY_COMPOSITION = "body_comp"  # InBody / МедАсс BIA scans (lives under /weight)
    GLP1 = "glp1"
    SUPPLEMENTS = "supplements"
    GENETICS = "genetics"
    SKINCARE = "skincare"
    WORKOUTS = "workouts"   # Hevy
    GARMIN = "garmin"       # activity & recovery
    LABS = "labs"
    NUTRITION = "nutrition"
    HRT = "hrt"  # hormone/TRT/AAS cycles, estrogen control, GH/IGF-1/peptides
    MILESTONES = "milestones"
    TIMELINE = "timeline"  # global annotations shown across every domain's chart
    SYSTEM = "system"


#: The domains that name a section of somebody's record, which is not all of
#: them. ``SYSTEM`` stamps infrastructure alerts that belong to no module and
#: to no person; offering it beside Labs and Nutrition on the support console
#: asks a patient to approve reading a section of their record that does not
#: exist. Anything a person is asked to consent to comes from here.
RECORD_SECTIONS: tuple[Domain, ...] = tuple(
    domain for domain in Domain if domain is not Domain.SYSTEM
)


class Evidence(StrEnum):
    """Strength-of-evidence tier for a supplement (catalog reference)."""

    A = "A"  # strong (meta-analyses / RCTs)
    B = "B"  # moderate
    C = "C"  # weak / anecdotal


class Drug(StrEnum):
    """GLP-1 receptor agonists tracked in the injection log / dose phases."""

    SEMAGLUTIDE = "semaglutide"
    TIRZEPATIDE = "tirzepatide"


class InjectionSite(StrEnum):
    """Subcutaneous injection sites for the body-map rotation grid. The user
    rotates sites to avoid lipohypertrophy; the grid surfaces the last-used one."""

    ABDOMEN_LEFT = "abdomen_left"
    ABDOMEN_RIGHT = "abdomen_right"
    THIGH_LEFT = "thigh_left"
    THIGH_RIGHT = "thigh_right"
    ARM_LEFT = "arm_left"
    ARM_RIGHT = "arm_right"


class Route(StrEnum):
    """Administration route for an HRT compound / dose (vitals.models.hrt)."""

    INTRAMUSCULAR = "intramuscular"
    SUBCUTANEOUS = "subcutaneous"
    ORAL = "oral"
    TRANSDERMAL = "transdermal"


class DoseUnit(StrEnum):
    """Unit a dose is measured in. Injectable AAS/esters are mg; growth hormone
    and gonadotropins are IU; most peptides and IGF-1 analogs are mcg."""

    MG = "mg"
    IU = "iu"
    MCG = "mcg"


class HrtInjectionSite(StrEnum):
    """Intramuscular/subcutaneous sites for the HRT body-map rotation grid — the
    deeper IM depots used for oil-based esters, distinct from the GLP-1 subcut
    grid (``InjectionSite``). The user rotates sites to avoid scar tissue/PIP."""

    GLUTE_LEFT = "glute_left"
    GLUTE_RIGHT = "glute_right"
    VENTROGLUTE_LEFT = "ventroglute_left"
    VENTROGLUTE_RIGHT = "ventroglute_right"
    DELT_LEFT = "delt_left"
    DELT_RIGHT = "delt_right"
    QUAD_LEFT = "quad_left"
    QUAD_RIGHT = "quad_right"
    VGL_LEFT = "vastus_lateralis_left"
    VGL_RIGHT = "vastus_lateralis_right"


class CycleKind(StrEnum):
    """Kind of an HRT cycle (vitals.models.hrt.HrtCycle) — shapes the lab-check
    cadence. Deliberately just two: the app only *behaves* differently for
    "on hormones" vs "restarting natural production", so pretending to five
    kinds (TRT/blast/cruise/bridge...) was labeling, not function — use the
    cycle's free-text name for that nuance. (Collapsed in migration 0028.)"""

    COURSE = "course"  # any exogenous-hormone protocol (TRT, blast, cruise...)
    PCT = "pct"        # post-cycle therapy (SERM/HCG restart)


class LabFlag(StrEnum):
    """Out-of-range classification for a lab result (computed from value vs ref).

    ``CRITICAL_*`` is raised when the value is far outside the range (see
    ``labs_service.compute_flag``) and escalates the alert."""

    NORMAL = "normal"
    LOW = "low"
    HIGH = "high"
    CRITICAL_LOW = "critical_low"
    CRITICAL_HIGH = "critical_high"


class NoiseDirection(StrEnum):
    """Expected weight distortion direction during a noise period.

    - ``UP``      — noise pushed scale weight *up* vs real trend (creatine
                    loading, high-sodium day, menstrual water retention).
                    Real fat-loss trend is *better* than the raw numbers show.
    - ``DOWN``    — noise pushed scale weight *down* vs real trend (dehydration,
                    post-illness).  Real situation is *worse* than numbers show.
    - ``NEUTRAL`` — direction unknown / not relevant (use when unsure).
    """

    UP = "up"
    DOWN = "down"
    NEUTRAL = "neutral"


class AnnotationKind(StrEnum):
    """Timeline annotation categories — flags the owner drops on the calendar
    (trip, illness, protocol change) that have no natural home in any single
    domain table."""

    LIFE_EVENT = "life_event"
    ILLNESS = "illness"
    TRAVEL = "travel"
    PROTOCOL_CHANGE = "protocol_change"
    NOTE = "note"


class MilestoneStatus(StrEnum):
    """Lifecycle of a goal card."""

    ACTIVE = "active"
    ACHIEVED = "achieved"
    MISSED = "missed"
    PAUSED = "paused"


class DigestKind(StrEnum):
    """Which narrative a ``weekly_digests`` row holds.

    All three are the same artifact — text plus the context it was built from — so
    they share one table and one page instead of growing a second store per
    cadence. ``WEEKLY`` is the historical default, which is why existing rows
    backfill to it.
    """

    WEEKLY = "weekly"
    DAILY_BRIEF = "daily_brief"
    EVENING = "evening"


class Source(StrEnum):
    """Provenance of a row — where the data came from."""

    MANUAL = "manual"
    # Written through the MCP connector (a conversation with Claude). Still the
    # owner talking, so it ranks with MANUAL wherever provenance decides who wins
    # — it only says which surface he used.
    MCP = "mcp"
    GARMIN_API = "garmin_api"
    HEALTH_AUTO_EXPORT = "health_auto_export"  # Garmin backup channel (uploaded JSON)
    HEVY_API = "hevy_api"
    LAB_PARSER = "lab_parser"
    BODY_SCAN = "body_scan"  # InBody / МедАсс body-composition scan (vision-parsed or manual)
    VCF_IMPORT = "vcf_import"
    # The bot is gone. Historical ``raw_payloads`` still carry this source,
    # so the value stays to keep those rows readable.
    TELEGRAM = "telegram"  # historical: captured by the Telegram bot
    # The week template *guessed* this day's context (vs. MANUAL — the owner
    # actually answered). Deliberately reusing MANUAL for "user" rather than
    # adding a second word for the same provenance.
    TEMPLATE = "template"
    SCHEDULER = "scheduler"
    SYSTEM = "system"
