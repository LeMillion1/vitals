"""Typed inversion seam for the legacy full-backup restore coordinator."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

AsyncTransition = Callable[..., Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class PortabilityV1OwnershipHooks:
    """Operations supplied to the generic portable-data replacement workflow."""

    table_groups: Mapping[str, tuple[str, ...]]
    raw_error: type[Exception]
    normalized_error: type[Exception]
    hrt_child_error: type[Exception]
    provider_raw_error: type[Exception]
    hevy_child_error: type[Exception]
    hrt_compound_error: type[Exception]
    conflict_rule_error: type[Exception]
    progress_photo_error: type[Exception]
    shared_report_error: type[Exception]
    weight_log_error: type[Exception]
    lab_result_error: type[Exception]
    genetic_variant_error: type[Exception]
    body_scan_error: type[Exception]
    body_scan_metric_error: type[Exception]
    garmin_weight_export_error: type[Exception]
    weekly_digest_error: type[Exception]
    notification_error: type[Exception]
    system_alert_error: type[Exception]
    block_raw: AsyncTransition
    reset_normalized: AsyncTransition
    reset_hrt_child: AsyncTransition
    block_provider_raw: AsyncTransition
    block_hevy_child: AsyncTransition
    reset_hrt_compound: AsyncTransition
    reset_conflict_rule: AsyncTransition
    block_progress_photo: AsyncTransition
    prepare_shared_report: AsyncTransition
    reset_weight_log: AsyncTransition
    reset_lab_result: AsyncTransition
    reset_genetic_variant: AsyncTransition
    block_body_scan: AsyncTransition
    reset_body_scan_metric: AsyncTransition
    block_garmin_weight_export: AsyncTransition
    prepare_weekly_digest: AsyncTransition
    prepare_notification: AsyncTransition
    reset_system_alert: AsyncTransition
    preflight_conflict_rule: AsyncTransition
    preflight_progress_photo: AsyncTransition
    preflight_shared_report: AsyncTransition
    preflight_weight_log: AsyncTransition
    preflight_lab_result: AsyncTransition
    preflight_genetic_variant: AsyncTransition
    preflight_body_scan: AsyncTransition
    preflight_body_scan_metric: AsyncTransition
    preflight_garmin_weight_export: AsyncTransition
    preflight_weekly_digest: AsyncTransition
    preflight_notification: AsyncTransition
    preflight_system_alert: AsyncTransition

__all__ = ["PortabilityV1OwnershipHooks"]
