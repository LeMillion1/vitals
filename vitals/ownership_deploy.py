"""The order the ownership backfill phases have to run in, named once.

Nineteen phases stamp the lake, and they are not independent: a child cannot
inherit a subject its parent does not have yet, and a normalized fact cannot
take its provenance from a raw payload that is still unowned. The order below is
that dependency graph flattened, and it is the same order an operator follows.

It lives in the application rather than in a test because it is an operational
fact, not a test fixture. :data:`vitals.ownership.PRE_OWNERSHIP_CONTRACT_REVISION`
says where a lake must be before these run; this says what runs there; and the
contract revision after them is what refuses to proceed if any of it was skipped.

A paired contract test checks the sequence against the scripts on disk, so a new
phase cannot ship without taking its place in the order.
"""

from __future__ import annotations

from dataclasses import dataclass

from vitals.operations.ownership.body_scan_metric import (
    BODY_SCAN_METRIC_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.operations.ownership.body_scan import (
    BODY_SCAN_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.operations.ownership.conflict_rule import (
    CONFLICT_RULE_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.operations.ownership.garmin_weight_export import (
    GARMIN_WEIGHT_EXPORT_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.operations.ownership.genetic_variant import (
    GENETIC_VARIANT_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.operations.ownership.hevy_child import (
    HEVY_CHILD_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.operations.ownership.hrt_child import (
    HRT_CHILD_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.operations.ownership.hrt_compound import (
    HRT_COMPOUND_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.operations.ownership.lab_result import (
    LAB_RESULT_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.operations.ownership.normalized import (
    NORMALIZED_MANUAL_BACKFILL_PHASE,
)
from vitals.operations.ownership.notification import (
    NOTIFICATION_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.operations.ownership.progress_photo import (
    PROGRESS_PHOTO_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.operations.ownership.provider_raw import (
    PROVIDER_RAW_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.operations.ownership.raw import (
    RAW_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.operations.ownership.retired_signals import (
    RETIRED_SIGNAL_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.operations.ownership.shared_report import (
    SHARED_REPORT_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.operations.ownership.system_alert import (
    SYSTEM_ALERT_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.operations.ownership.weekly_digest import (
    WEEKLY_DIGEST_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.operations.ownership.weight_log import (
    WEIGHT_LOG_OWNERSHIP_BACKFILL_PHASE,
)


@dataclass(frozen=True, slots=True)
class OwnershipBackfillStep:
    """One phase: the operator command, and the checkpoint it reports under."""

    script: str
    phase: str


#: The nineteen phases in dependency order. Raw payloads first, because every
#: provenance-bearing fact resolves its owner through one; then the normalized
#: facts; then the children, which inherit from parents the earlier phases have
#: already stamped; then the artifacts — reports, digests, notifications, alerts
#: — which reference all of the above.
OWNERSHIP_BACKFILL_SEQUENCE: tuple[OwnershipBackfillStep, ...] = (
    OwnershipBackfillStep(
        "backfill_subject_ownership.py", RAW_OWNERSHIP_BACKFILL_PHASE
    ),
    OwnershipBackfillStep(
        "backfill_normalized_subject_ownership.py", NORMALIZED_MANUAL_BACKFILL_PHASE
    ),
    OwnershipBackfillStep(
        "backfill_hrt_child_subject_ownership.py", HRT_CHILD_OWNERSHIP_BACKFILL_PHASE
    ),
    OwnershipBackfillStep(
        "backfill_provider_raw_subject_ownership.py",
        PROVIDER_RAW_OWNERSHIP_BACKFILL_PHASE,
    ),
    OwnershipBackfillStep(
        "backfill_hevy_child_subject_ownership.py", HEVY_CHILD_OWNERSHIP_BACKFILL_PHASE
    ),
    OwnershipBackfillStep(
        "backfill_hrt_compound_subject_ownership.py",
        HRT_COMPOUND_OWNERSHIP_BACKFILL_PHASE,
    ),
    OwnershipBackfillStep(
        "backfill_conflict_rule_subject_ownership.py",
        CONFLICT_RULE_OWNERSHIP_BACKFILL_PHASE,
    ),
    OwnershipBackfillStep(
        "backfill_progress_photo_subject_ownership.py",
        PROGRESS_PHOTO_OWNERSHIP_BACKFILL_PHASE,
    ),
    OwnershipBackfillStep(
        "backfill_shared_report_subject_ownership.py",
        SHARED_REPORT_OWNERSHIP_BACKFILL_PHASE,
    ),
    OwnershipBackfillStep(
        "backfill_weight_log_subject_ownership.py",
        WEIGHT_LOG_OWNERSHIP_BACKFILL_PHASE,
    ),
    OwnershipBackfillStep(
        "backfill_lab_result_subject_ownership.py",
        LAB_RESULT_OWNERSHIP_BACKFILL_PHASE,
    ),
    OwnershipBackfillStep(
        "backfill_genetic_variant_subject_ownership.py",
        GENETIC_VARIANT_OWNERSHIP_BACKFILL_PHASE,
    ),
    OwnershipBackfillStep(
        "backfill_body_scan_subject_ownership.py", BODY_SCAN_OWNERSHIP_BACKFILL_PHASE
    ),
    OwnershipBackfillStep(
        "backfill_body_scan_metric_subject_ownership.py",
        BODY_SCAN_METRIC_OWNERSHIP_BACKFILL_PHASE,
    ),
    OwnershipBackfillStep(
        "backfill_garmin_weight_export_subject_ownership.py",
        GARMIN_WEIGHT_EXPORT_OWNERSHIP_BACKFILL_PHASE,
    ),
    OwnershipBackfillStep(
        "backfill_weekly_digest_subject_ownership.py",
        WEEKLY_DIGEST_OWNERSHIP_BACKFILL_PHASE,
    ),
    OwnershipBackfillStep(
        "backfill_retired_signal_ownership.py",
        RETIRED_SIGNAL_OWNERSHIP_BACKFILL_PHASE,
    ),
    OwnershipBackfillStep(
        "backfill_notification_subject_ownership.py",
        NOTIFICATION_OWNERSHIP_BACKFILL_PHASE,
    ),
    OwnershipBackfillStep(
        "backfill_system_alert_subject_ownership.py",
        SYSTEM_ALERT_OWNERSHIP_BACKFILL_PHASE,
    ),
)


__all__ = ["OWNERSHIP_BACKFILL_SEQUENCE", "OwnershipBackfillStep"]
