"""Machine-readable inventory of the legacy unscoped service bridges.

Stage 2 gave every core service an optional scope: pass ``subject_id`` (or an
``identity``/``context``, and sometimes ``include_legacy_unowned``) and the call
is scoped; omit it and the call reads or writes across the whole installation
exactly as the single-user application always did.  That optionality is what
kept the migration reversible while ownership was being backfilled.

It is also the last thing standing between this schema and a second person.  A
scoped unique key over a nullable column, a policy engine no service consults,
and row-level security applied under an application that still issues unscoped
reads are each worth nothing on their own.  PR-04 closes these bridges service
by service; this registry is what makes "closed" measurable.

Every entry names a function that still accepts an omittable scope.  The paired
contract test recomputes the inventory from the source and fails when a module
grows a bridge that is not listed here, so the set can only shrink.  When it is
empty, ``AccessContext`` is mandatory everywhere and the compatibility columns
can become ``NOT NULL``.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType


# module -> functions that still accept an omittable subject/identity/context,
# or an ``include_legacy_unowned`` escape hatch.
LEGACY_SCOPE_BRIDGES: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        'vitals.services.ai_gateway_service': frozenset(
            {
                '_ensure_nonoverlapping_period',
            }
        ),
        'vitals.services.body_scan_service': frozenset(
            {
                '_lock_scan_for_update',
                '_require_legacy_bridge',
                '_subject_scope',
                '_validate_persisted_scan',
                'available_metrics',
                'bia_chart_points',
                'delete_scan',
                'get_scan',
                'latest_scan',
                'list_scans',
                'metric_history',
                'refresh_alerts',
                'reparse_owned_pending',
                'save_scan',
                'update_scan_note',
            }
        ),
        'vitals.services.custom_charts_service': frozenset(
            {
                'cache_key',
                'create_chart',
                'delete_chart',
                'get_chart',
                'list_charts',
                'prime_cache',
            }
        ),
        'vitals.services.genetics_service': frozenset(
            {
                '_lock_by_rsid',
                '_lock_scoped_variant',
                '_replace_vcf_rows',
                '_require_legacy_bridge',
                '_subject_scope',
                '_validate_raw_owner',
                '_validate_variant_graph',
                'add_variant',
                'delete_variant',
                'get_variant',
                'ingest_vcf_batch',
                'list_variants',
                'reparse_owned_pending',
                'store_raw_vcf',
                'update_variant',
                'upsert_by_rsid',
            }
        ),
        'vitals.services.glp1_service': frozenset(
            {
                '_owned_row_for_update',
                '_require_legacy_bridge',
                '_subject_scope',
                'active_dose_phase',
                'add_dose_phase',
                'delete_dose_phase',
                'delete_injection',
                'delete_side_effect',
                'dose_phase_overlays',
                'get_injection_for_update',
                'last_injection',
                'list_dose_phases',
                'list_injections',
                'list_side_effects',
                'log_injection',
                'log_side_effect',
                'refresh_plateau_alert',
                'update_injection',
                'update_injection_note',
            }
        ),
        'vitals.services.hrt_cycle_service': frozenset(
            {
                '_actual_contributions',
                '_lock_cycle_graph',
                '_lock_cycle_graph_for_item',
                '_planned_contributions',
                '_require_scoped_prepared_write',
                '_resolve_scoped_compound',
                '_row_in_scope',
                '_subject_scope',
                '_validate_cycle_graph',
                'active_cycle',
                'add_cycle',
                'add_cycle_item',
                'close_cycle',
                'delete_cycle',
                'delete_cycle_item',
                'list_cycles',
                'planned_administrations',
                'release_series',
                'update_cycle_item',
            }
        ),
        'vitals.services.hrt_reminders': frozenset(
            {
                'refresh_all',
                'refresh_injection_due',
                'refresh_labs_due',
                'seed_hormone_panel',
            }
        ),
        'vitals.services.hrt_service': frozenset(
            {
                '_owned_row_for_update',
                '_require_legacy_bridge',
                '_subject_scope',
                'delete_dose',
                'delete_side_effect',
                'get_compound',
                'get_dose_for_update',
                'last_dose',
                'list_compounds',
                'list_doses',
                'list_side_effects',
                'log_dose',
                'log_side_effect',
                'set_compound_active',
                'update_dose',
                'update_side_effect',
            }
        ),
        'vitals.services.hrt_template_service': frozenset(
            {
                '_lock_template_graph',
                '_require_scoped_prepared_write',
                '_row_in_scope',
                '_subject_scope',
                '_validate_export_scope',
                '_validate_template_graph',
                'create_cycle_from_template',
                'delete_template',
                'export_template',
                'export_template_json',
                'get_template',
                'import_template',
                'list_templates',
                'save_cycle_as_template',
            }
        ),
        'vitals.services.labs_service': frozenset(
            {
                '_ensure_marker',
                '_get_result_for_update',
                '_lock_result_provenance_before_row',
                '_marker_for_update',
                '_require_legacy_bridge',
                '_result_by_id_stmt',
                '_result_exists',
                '_subject_scope',
                'add_result',
                'confirm_extracted',
                'defer_retest',
                'delete_result',
                'ensure_marker_catalog_entry',
                'get_marker',
                'get_result_for_update',
                'ingest_extracted',
                'latest_per_marker',
                'list_markers',
                'list_results',
                'marker_history',
                'refresh_alerts',
                'reparse_owned_pending',
                'update_result',
                'update_result_note',
            }
        ),
        'vitals.services.milestones_service': frozenset(
            {
                '_current_body_fat',
                '_current_weight',
                '_lock_milestone_for_write',
                '_require_prepared_write',
                '_require_progress_scope',
                '_subject_scope',
                'create_milestone',
                'dashboard_cards',
                'delete_milestone',
                'list_milestones',
                'progress',
                'set_status',
                'update_milestone',
            }
        ),
        'vitals.services.modules_service': frozenset(
            {
                'cache_key',
                'get_enabled_modules',
                'prime_cache',
                'set_module_enabled',
            }
        ),
        'vitals.services.nutrition_service': frozenset(
            {
                'daily_summary',
                'delete_meal',
                'list_meals',
                'list_meals_for_date',
                'log_meal',
                'nutrition_summary',
                'resolve_today',
                'update_meal',
            }
        ),
        'vitals.services.proactive.brief': frozenset(
            {
                '_prepare_brief',
                '_signals_since_yesterday',
                'build_context',
                'generate_brief',
            }
        ),
        'vitals.services.proactive.day_plan': frozenset(
            {
                'get_week_template',
                'record_answer',
                'resolve',
                'set_week_template',
            }
        ),
        'vitals.services.proactive.inbound': frozenset(
            {
                'known_keys',
            }
        ),
        'vitals.services.proactive.prefs': frozenset(
            {
                'bot_enabled',
            }
        ),
        'vitals.services.proactive.signal_ai_service': frozenset(
            {
                '_known_keys',
            }
        ),
        'vitals.services.scoped_settings_service': frozenset(
            {
                'get_scoped_setting',
                'mirror_legacy_setting',
                'set_scoped_setting',
                'update_scoped_setting',
            }
        ),
        'vitals.services.signals_service': frozenset(
            {
                '_raw_scope',
                '_signal_scope',
                'create_signals',
                'delete_signal',
                'get_day_context',
                'ingest_stored_text',
                'ingest_text',
                'key_frequency',
                'list_day_contexts',
                'list_signals',
                'mark_misparse',
                'reparse_unparsed',
                'set_day_context',
                'store_raw_text',
            }
        ),
        'vitals.services.skincare_service': frozenset(
            {
                '_get_log',
                '_get_owned_row_for_update',
                '_require_legacy_bridge',
                '_subject_scope',
                'add_observation',
                'add_product',
                'delete_log',
                'delete_observation',
                'delete_product',
                'get_log',
                'list_logs',
                'list_observations',
                'list_products',
                'update_log_note',
                'update_product',
                'upsert_log',
            }
        ),
        'vitals.services.supplements_service': frozenset(
            {
                '_get_supplement_for_update',
                '_supplement_by_id_stmt',
                '_supplement_subject_scope',
                'add_supplement',
                'delete_supplement',
                'get_supplement',
                'get_supplement_for_update',
                'list_supplements',
                'resolve_active',
                'set_active',
                'update_supplement',
            }
        ),
        'vitals.services.timeline_service': frozenset(
            {
                '_annotation_subject_scope',
                '_derived_events',
                '_fully_legacy_row_scope',
                'create_annotation',
                'delete_annotation',
                'get_annotation',
                'list_annotations',
                'list_events',
                'overlays_for',
                'update_annotation',
            }
        ),
        'vitals.services.today_service': frozenset(
            {
                '_goal',
                'build',
            }
        ),
        'vitals.services.weight_service': frozenset(
            {
                '_apply_body_measurement_values',
                '_assert_weight_scope_integrity',
                '_body_measurement_scope_condition',
                '_get_body_measurement_for_date_update',
                '_get_body_measurement_for_update',
                '_get_noise_marker_for_update',
                '_get_weight_log_date_in_scope',
                '_get_weight_log_for_update',
                '_glp1_phase_overlays',
                '_noise_marker_scope_condition',
                '_noise_ranges',
                '_progress_photo_scope_rows',
                '_recompute_lbm_for_date',
                '_recompute_lbm_for_date_null',
                '_require_legacy_bridge',
                '_validate_persisted_weight_provenance',
                '_weight_scope_condition',
                'add_noise_marker',
                'add_progress_photo',
                'chart_series',
                'delete_body_measurement',
                'delete_noise_marker',
                'delete_progress_photo',
                'delete_weight_log',
                'get_active_weight',
                'get_progress_photo',
                'get_progress_photo_by_file_key',
                'list_active_weights',
                'list_body_measurements',
                'list_noise_markers',
                'list_progress_photos',
                'list_weight_notes',
                'log_weight',
                'refresh_noise_alert',
                'update_body_measurement',
                'update_body_measurement_note',
                'update_weight_log',
                'update_weight_note',
                'upsert_body_measurement',
            }
        ),
    }
)


def bridge_total() -> int:
    """Return how many bridged functions remain."""

    return sum(len(names) for names in LEGACY_SCOPE_BRIDGES.values())


# module -> subject-owned models still fetched by bare primary key.  A
# ``session.get(Model, id)`` proves nothing about who the row belongs to: the
# caller has to have established that separately, and every one of these is a
# place where PR-04 has to make the scope explicit instead.
LEGACY_BARE_ID_READS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "vitals.services.alerts_service": frozenset({"SystemAlert"}),
        "vitals.services.body_scan_service": frozenset({"RawPayload"}),
        "vitals.services.custom_charts_service": frozenset({"AppSetting"}),
        "vitals.services.garmin_service": frozenset({"RawPayload"}),
        "vitals.services.garmin_weight_service": frozenset({"AppSetting"}),
        "vitals.services.genetics_service": frozenset({"GeneticVariant"}),
        "vitals.services.hrt_service": frozenset({"HrtCompound"}),
        "vitals.services.labs_service": frozenset({"RawPayload"}),
        "vitals.services.language_service": frozenset({"AppSetting"}),
        "vitals.services.modules_service": frozenset({"AppSetting"}),
        "vitals.services.proactive.day_plan": frozenset({"AppSetting"}),
        "vitals.services.proactive.inbound": frozenset({"RawPayload"}),
        "vitals.services.twofa_service": frozenset({"AppSetting"}),
        "vitals.services.weight_service": frozenset({"ProgressPhoto"}),
    }
)


def bare_id_read_total() -> int:
    """Return how many module/model bare-key reads remain."""

    return sum(len(models) for models in LEGACY_BARE_ID_READS.values())


__all__ = [
    "LEGACY_BARE_ID_READS",
    "LEGACY_SCOPE_BRIDGES",
    "bare_id_read_total",
    "bridge_total",
]
