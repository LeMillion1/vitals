"""Operational coordinator for destructive full-v1 portability restores."""

from __future__ import annotations

from types import MappingProxyType
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from vitals.operations.ownership.body_scan import (
    BODY_SCAN_OWNERSHIP_BACKFILL_TABLES,
    BodyScanOwnershipBackfillError,
    block_body_scan_ownership_backfill_for_portability_v1_restore,
    preflight_body_scan_ownership_backfill,
)
from vitals.operations.ownership.body_scan_metric import (
    BODY_SCAN_METRIC_OWNERSHIP_BACKFILL_TABLES,
    BodyScanMetricOwnershipBackfillError,
    preflight_body_scan_metric_ownership_backfill,
    reset_body_scan_metric_ownership_backfill_for_portability_v1_restore,
)
from vitals.operations.ownership.conflict_rule import (
    CONFLICT_RULE_OWNERSHIP_BACKFILL_TABLES,
    ConflictRuleOwnershipBackfillError,
    preflight_conflict_rule_ownership_backfill,
    reset_conflict_rule_backfill_for_portability_v1_restore,
)
from vitals.operations.ownership.garmin_weight_export import (
    GARMIN_WEIGHT_EXPORT_OWNERSHIP_BACKFILL_TABLES,
    GarminWeightExportOwnershipBackfillError,
    block_garmin_weight_export_ownership_backfill_for_portability_v1_restore,
    preflight_garmin_weight_export_ownership_backfill,
)
from vitals.operations.ownership.genetic_variant import (
    GENETIC_VARIANT_OWNERSHIP_BACKFILL_TABLES,
    GeneticVariantOwnershipBackfillError,
    preflight_genetic_variant_ownership_backfill,
    reset_genetic_variant_ownership_backfill_for_portability_v1_restore,
)
from vitals.operations.ownership.hevy_child import (
    HEVY_CHILD_OWNERSHIP_BACKFILL_TABLES,
    HevyChildOwnershipBackfillError,
    block_hevy_child_ownership_backfill_for_portability_v1_restore,
)
from vitals.operations.ownership.hrt_child import (
    HRT_CHILD_OWNERSHIP_BACKFILL_TABLES,
    HrtChildOwnershipBackfillError,
    reset_hrt_child_backfill_for_portability_v1_restore,
)
from vitals.operations.ownership.hrt_compound import (
    HRT_COMPOUND_OWNERSHIP_BACKFILL_TABLES,
    HrtCompoundOwnershipBackfillError,
    reset_hrt_compound_backfill_for_portability_v1_restore,
)
from vitals.operations.ownership.lab_result import (
    LAB_RESULT_OWNERSHIP_BACKFILL_TABLES,
    LabResultOwnershipBackfillError,
    preflight_lab_result_ownership_backfill,
    reset_lab_result_ownership_backfill_for_portability_v1_restore,
)
from vitals.operations.ownership.normalized import (
    NORMALIZED_MANUAL_TABLES,
    NormalizedOwnershipBackfillError,
    reset_normalized_manual_backfill_for_portability_v1_restore,
)
from vitals.operations.ownership.notification import (
    NotificationOwnershipBackfillError,
    preflight_notification_ownership_backfill,
    prepare_notification_ownership_backfill_for_portability_v1_restore,
)
from vitals.operations.ownership.progress_photo import (
    PROGRESS_PHOTO_OWNERSHIP_BACKFILL_TABLES,
    ProgressPhotoOwnershipBackfillError,
    block_progress_photo_ownership_backfill_for_portability_v1_restore,
    preflight_progress_photo_ownership_backfill,
)
from vitals.operations.ownership.provider_raw import (
    PROVIDER_RAW_OWNERSHIP_BACKFILL_TABLES,
    ProviderRawOwnershipBackfillError,
    block_provider_raw_ownership_backfill_for_portability_v1_restore,
)
from vitals.operations.ownership.raw import (
    RawOwnershipBackfillError,
    block_raw_ownership_backfill_for_portability_v1_restore,
)
from vitals.operations.ownership.shared_report import (
    SharedReportOwnershipBackfillError,
    preflight_shared_report_ownership_backfill,
    prepare_shared_report_ownership_backfill_for_portability_v1_restore,
)
from vitals.operations.ownership.system_alert import (
    SYSTEM_ALERT_OWNERSHIP_BACKFILL_TABLES,
    SystemAlertOwnershipBackfillError,
    preflight_system_alert_ownership_backfill,
    reset_system_alert_ownership_backfill_for_portability_v1_restore,
)
from vitals.operations.ownership.weekly_digest import (
    WeeklyDigestOwnershipBackfillError,
    preflight_weekly_digest_ownership_backfill,
    prepare_weekly_digest_ownership_backfill_for_portability_v1_restore,
)
from vitals.operations.ownership.weight_log import (
    WEIGHT_LOG_OWNERSHIP_BACKFILL_TABLES,
    WeightLogOwnershipBackfillError,
    preflight_weight_log_ownership_backfill,
    reset_weight_log_ownership_backfill_for_portability_v1_restore,
)
from vitals.ownership_transition.portability_v1 import PortabilityV1OwnershipHooks
from vitals.services import data_portability_service as _portability


def _hooks() -> PortabilityV1OwnershipHooks:
    """Bind current operation symbols so tests and operators share one seam."""

    return PortabilityV1OwnershipHooks(
        table_groups=MappingProxyType(
            {
                "normalized": NORMALIZED_MANUAL_TABLES,
                "hrt_child": HRT_CHILD_OWNERSHIP_BACKFILL_TABLES,
                "provider_raw": PROVIDER_RAW_OWNERSHIP_BACKFILL_TABLES,
                "hevy_child": HEVY_CHILD_OWNERSHIP_BACKFILL_TABLES,
                "hrt_compound": HRT_COMPOUND_OWNERSHIP_BACKFILL_TABLES,
                "conflict_rule": CONFLICT_RULE_OWNERSHIP_BACKFILL_TABLES,
                "progress_photo": PROGRESS_PHOTO_OWNERSHIP_BACKFILL_TABLES,
                "weight_log": WEIGHT_LOG_OWNERSHIP_BACKFILL_TABLES,
                "lab_result": LAB_RESULT_OWNERSHIP_BACKFILL_TABLES,
                "genetic_variant": GENETIC_VARIANT_OWNERSHIP_BACKFILL_TABLES,
                "body_scan": BODY_SCAN_OWNERSHIP_BACKFILL_TABLES,
                "body_scan_metric": BODY_SCAN_METRIC_OWNERSHIP_BACKFILL_TABLES,
                "garmin_weight_export": (
                    GARMIN_WEIGHT_EXPORT_OWNERSHIP_BACKFILL_TABLES
                ),
                "system_alert": SYSTEM_ALERT_OWNERSHIP_BACKFILL_TABLES,
            }
        ),
        raw_error=RawOwnershipBackfillError,
        normalized_error=NormalizedOwnershipBackfillError,
        hrt_child_error=HrtChildOwnershipBackfillError,
        provider_raw_error=ProviderRawOwnershipBackfillError,
        hevy_child_error=HevyChildOwnershipBackfillError,
        hrt_compound_error=HrtCompoundOwnershipBackfillError,
        conflict_rule_error=ConflictRuleOwnershipBackfillError,
        progress_photo_error=ProgressPhotoOwnershipBackfillError,
        shared_report_error=SharedReportOwnershipBackfillError,
        weight_log_error=WeightLogOwnershipBackfillError,
        lab_result_error=LabResultOwnershipBackfillError,
        genetic_variant_error=GeneticVariantOwnershipBackfillError,
        body_scan_error=BodyScanOwnershipBackfillError,
        body_scan_metric_error=BodyScanMetricOwnershipBackfillError,
        garmin_weight_export_error=GarminWeightExportOwnershipBackfillError,
        weekly_digest_error=WeeklyDigestOwnershipBackfillError,
        notification_error=NotificationOwnershipBackfillError,
        system_alert_error=SystemAlertOwnershipBackfillError,
        block_raw=block_raw_ownership_backfill_for_portability_v1_restore,
        reset_normalized=(
            reset_normalized_manual_backfill_for_portability_v1_restore
        ),
        reset_hrt_child=reset_hrt_child_backfill_for_portability_v1_restore,
        block_provider_raw=(
            block_provider_raw_ownership_backfill_for_portability_v1_restore
        ),
        block_hevy_child=(
            block_hevy_child_ownership_backfill_for_portability_v1_restore
        ),
        reset_hrt_compound=(
            reset_hrt_compound_backfill_for_portability_v1_restore
        ),
        reset_conflict_rule=(
            reset_conflict_rule_backfill_for_portability_v1_restore
        ),
        block_progress_photo=(
            block_progress_photo_ownership_backfill_for_portability_v1_restore
        ),
        prepare_shared_report=(
            prepare_shared_report_ownership_backfill_for_portability_v1_restore
        ),
        reset_weight_log=(
            reset_weight_log_ownership_backfill_for_portability_v1_restore
        ),
        reset_lab_result=(
            reset_lab_result_ownership_backfill_for_portability_v1_restore
        ),
        reset_genetic_variant=(
            reset_genetic_variant_ownership_backfill_for_portability_v1_restore
        ),
        block_body_scan=(
            block_body_scan_ownership_backfill_for_portability_v1_restore
        ),
        reset_body_scan_metric=(
            reset_body_scan_metric_ownership_backfill_for_portability_v1_restore
        ),
        block_garmin_weight_export=(
            block_garmin_weight_export_ownership_backfill_for_portability_v1_restore
        ),
        prepare_weekly_digest=(
            prepare_weekly_digest_ownership_backfill_for_portability_v1_restore
        ),
        prepare_notification=(
            prepare_notification_ownership_backfill_for_portability_v1_restore
        ),
        reset_system_alert=(
            reset_system_alert_ownership_backfill_for_portability_v1_restore
        ),
        preflight_conflict_rule=preflight_conflict_rule_ownership_backfill,
        preflight_progress_photo=preflight_progress_photo_ownership_backfill,
        preflight_shared_report=preflight_shared_report_ownership_backfill,
        preflight_weight_log=preflight_weight_log_ownership_backfill,
        preflight_lab_result=preflight_lab_result_ownership_backfill,
        preflight_genetic_variant=preflight_genetic_variant_ownership_backfill,
        preflight_body_scan=preflight_body_scan_ownership_backfill,
        preflight_body_scan_metric=preflight_body_scan_metric_ownership_backfill,
        preflight_garmin_weight_export=(
            preflight_garmin_weight_export_ownership_backfill
        ),
        preflight_weekly_digest=preflight_weekly_digest_ownership_backfill,
        preflight_notification=preflight_notification_ownership_backfill,
        preflight_system_alert=preflight_system_alert_ownership_backfill,
    )


async def import_full(session: AsyncSession, payload: Any) -> _portability.ImportStats:
    """Run the destructive full-v1 restore with explicit ownership operations."""

    live_schema = await _portability._live_schema_columns(session)
    return await _portability._import_full_with_ownership_hooks(
        session,
        payload,
        hooks=_hooks(),
        live_schema=live_schema,
    )


__all__ = ["import_full"]
