"""Model Context Protocol (MCP) server integration for Vitals.

Exposes access to all health domains using FastMCP and standard SQLAlchemy
preloading patterns. Read tools cover every domain; write tools let Claude
record and edit meals, weight, GLP-1, skincare, supplements, measurements,
body scans, labs, goals, timeline events and notes directly from the
conversation. Two resources (``vitals://profile``, ``vitals://digest/latest``)
and a ``weekly_review`` prompt round out the surface.

Response conventions (a stable contract the model can rely on):
  * Success — the tool's normal payload (a dict, or a list of dicts).
  * A recoverable problem (bad id, unknown key, missing dependency) — a dict
    ``{"error": "<human message>"}`` (list-returning tools wrap it: ``[{"error": ...}]``).
  * A hard conflict block on a write — a dict ``{"blocked": true, "violations":
    [...], "message": ..., "hint": ...}`` (see ``_conflict_payload``); the model
    can retry the same call with ``override=True``.
  * A delete — ``{"deleted": <bool>, "domain": <str>, "record_id": <id>}``
    (one ``delete_record`` tool serves every domain; see ``_DELETE_TARGETS``).
  * A write to a switched-off optional domain — ``{"error": "module '<key>' is
    disabled"}``; ``get_modules`` says which are on.
  * An unexpected execution failure — a safe ``{"error": ..., "code": ...,
    "error_id": ..., "retryable": ...}`` result. The id correlates with a
    payload-free server log entry; arbitrary exception text is never exposed.
"""
from __future__ import annotations

from web.config import get_web_config
from web.deps import get_redis_client, get_session_factory
from web.mcp.access import (
    PROMPT_ACCESS,  # noqa: F401 - transitional compatibility re-export
    RECORD_DOMAIN_KEYS as _RECORD_DOMAIN_KEYS,  # noqa: F401 - compat export
    RESOURCE_ACCESS,  # noqa: F401 - transitional compatibility re-export
    TOOL_ACCESS,  # noqa: F401 - transitional compatibility re-export
)
from web.mcp.arguments import (
    McpArgumentError,  # noqa: F401 - transitional compatibility re-export
    _parse_date,
    _parse_time,
)
from web.mcp.conflicts import (
    auxiliary_weight_write as _mcp_v1_aux_weight_write,
    composition_scope as _mcp_v1_composition_scope,
    conflict_scope as _mcp_v1_conflict_scope,
    conflict_write_context as _mcp_v1_conflict_write_context,
    weight_write as _mcp_v1_weight_write,
)
from web.mcp.errors import (
    McpActorUnresolved,  # noqa: F401 - compatibility exception identity
)
from web.mcp.identity import (
    ANONYMOUS_TOKEN,  # noqa: F401 - compatibility identity sentinel
    MCP_ACTOR as _MCP_ACTOR,  # noqa: F401 - compatibility request-state seam
    current_actor as _current_actor,  # noqa: F401 - compatibility identity seam
    current_grant_binding as _current_grant_binding,  # noqa: F401 - compat seam
)
from web.mcp.ownership import (
    actor_username as _mcp_actor_username,
    legacy_alert_owner as _mcp_v1_legacy_alert_owner,
    legacy_owner as _mcp_v1_legacy_owner,
)
from web.mcp.record_catalog import DELETE_TARGETS, NOTE_MODELS
from web.mcp.resources import ResourceDependencies, register_resources
from web.mcp.modules import module_enabled as _subject_module_enabled
from web.mcp.modules import module_gate as _module_gate
from web.mcp.serialization import (
    _ROW_NOISE,  # noqa: F401 - transitional compatibility re-export
    _conflict_payload,
    serialize_row,
    serialize_written,
)
from web.mcp.server import (
    MCP_SERVER_VERSION,  # noqa: F401 - compatibility server contract
    TOOL_MODULES,
    ConnectorTokenVerifier,
    VitalsMCPServer,  # noqa: F401 - compatibility server type
    build_server,
    described_for_a_model as _described_for_a_model,  # noqa: F401 - compat
)
from web.mcp.tools.alerts import AlertToolDependencies, register_alert_tools
from web.mcp.tools.conflicts import ConflictToolDependencies, register_conflict_tools
from web.mcp.tools.hrt import HrtToolDependencies, register_hrt_tools
from web.mcp.tools.genetics import GeneticsToolDependencies, register_genetics_tools
from web.mcp.tools.glp1 import (
    Glp1ToolDependencies,
    register_glp1_injection_tools,
    register_glp1_maintenance_tools,
    register_glp1_read_tools,
)
from web.mcp.tools.labs import LabsToolDependencies, register_labs_tools
from web.mcp.tools.records import (
    RecordToolDependencies,
    register_delete_tools,
    register_note_tools,
)
from web.mcp.tools.digest import (
    DigestToolDependencies,
    register_digest_read_tools,
    register_digest_tools,
)
from web.mcp.tools.milestones import (
    MilestoneToolDependencies,
    register_milestone_tools,
)
from web.mcp.tools.module_settings import (
    ModuleSettingsToolDependencies,
    register_module_settings_tools,
)
from web.mcp.tools.proactive import ProactiveToolDependencies, register_proactive_tools
from web.mcp.tools.providers import (
    INTRADAY_POINT_CAP as _DEFAULT_INTRADAY_POINT_CAP,
    SYNC_DAILY_LIMIT as _DEFAULT_SYNC_DAILY_LIMIT,
    ProviderToolDependencies,
    fold_sleep_detail,
    register_garmin_read_tools,
    register_garmin_sync_tools,
    register_hevy_read_tools,
    register_hevy_sync_tools,
)
from web.mcp.tools.reporting import (
    ReportingToolDependencies,
    register_reporting_tools,
    register_trend_tools,
)
from web.mcp.tools.skincare import (
    SkincareToolDependencies,
    register_skincare_observation_tools,
    register_skincare_read_tools,
    register_skincare_routine_tools,
)
from web.mcp.tools.supplements import (
    SupplementsToolDependencies,
    register_supplements_conflict_tools,
    register_supplements_read_tools,
    register_supplements_write_tools,
)
from web.mcp.tools.body_composition import (
    BodyCompositionToolDependencies,
    register_body_composition_tools,
    serialize_scan as _serialize_scan,  # noqa: F401 - compatibility export
)
from web.mcp.tools.weight import (
    WeightToolDependencies,
    register_measurement_update_tools,
    register_measurement_tools,
    register_noise_tools,
    register_weight_read_tools,
    register_weight_write_tools,
)
from web.mcp.tools.nutrition import (
    NutritionToolDependencies,
    register_nutrition_tools,
)
from web.mcp.tools.profile import ProfileToolDependencies, register_profile_tool
from web.mcp.tools.timeline import TimelineToolDependencies, register_timeline_tools
from web.mcp.transport import build_transport as _build_mcp_transport

class _ConnectorTokenVerifier(ConnectorTokenVerifier):
    """Compatibility constructor using this adapter's patchable factory seam."""

    def __init__(self):
        super().__init__(session_factory_provider=lambda: get_session_factory())


def _build_server() -> VitalsMCPServer:
    return build_server(session_factory_provider=lambda: get_session_factory())


mcp = _build_server()

# Compatibility catalogs for direct callers and frozen cross-surface tests.
_DELETE_TARGETS = DELETE_TARGETS
_NOTE_MODELS = NOTE_MODELS


async def _module_enabled(session, key: str) -> bool:
    return await _subject_module_enabled(
        session, key, owner_resolver=_mcp_v1_legacy_owner
    )


def gated(module_key: str):
    return _module_gate(
        module_key,
        session_factory_provider=lambda: get_session_factory(),
        owner_resolver=_mcp_v1_legacy_owner,
    )


# ── Tool Definitions ─────────────────────────────────────────────────────────

get_user_profile = register_profile_tool(
    mcp,
    ProfileToolDependencies(
        get_session_factory=lambda: get_session_factory(),
        conflict_scope=_mcp_v1_conflict_scope,
    ),
)


_weight_tool_dependencies = WeightToolDependencies(
    get_session_factory=lambda: get_session_factory(),
    parse_date=_parse_date,
    conflict_scope=_mcp_v1_conflict_scope,
    weight_write=lambda *args, **kwargs: _mcp_v1_weight_write(*args, **kwargs),
    auxiliary_weight_write=lambda *args, **kwargs: _mcp_v1_aux_weight_write(
        *args,
        **kwargs,
    ),
    conflict_payload=_conflict_payload,
    serialize_row=serialize_row,
    serialize_written=serialize_written,
)
_weight_read_tools = register_weight_read_tools(mcp, _weight_tool_dependencies)
get_weight_logs = _weight_read_tools.get_weight_logs


_glp1_tool_dependencies = Glp1ToolDependencies(
    get_session_factory=lambda: get_session_factory(),
    parse_date=_parse_date,
    conflict_scope=_mcp_v1_conflict_scope,
    conflict_write_context=_mcp_v1_conflict_write_context,
    conflict_payload=_conflict_payload,
    serialize_row=serialize_row,
    serialize_written=serialize_written,
    gated=gated,
)
_glp1_read_tools = register_glp1_read_tools(mcp, _glp1_tool_dependencies)
get_glp1_logs = _glp1_read_tools.get_glp1_logs


INTRADAY_POINT_CAP = _DEFAULT_INTRADAY_POINT_CAP
_fold_sleep_detail = fold_sleep_detail

_provider_tool_dependencies = ProviderToolDependencies(
    get_session_factory=lambda: get_session_factory(),
    get_redis_client=lambda: get_redis_client(),
    parse_date=_parse_date,
    conflict_scope=_mcp_v1_conflict_scope,
    actor_username=_mcp_actor_username,
    serialize_row=serialize_row,
    gated=gated,
    intraday_point_cap=lambda: INTRADAY_POINT_CAP,
    sync_daily_limit=lambda: SYNC_DAILY_LIMIT,
)
_garmin_read_tools = register_garmin_read_tools(
    mcp,
    _provider_tool_dependencies,
)
get_garmin_metrics = _garmin_read_tools.get_garmin_metrics

_hevy_read_tools = register_hevy_read_tools(
    mcp,
    _provider_tool_dependencies,
)
get_hevy_workouts = _hevy_read_tools.get_hevy_workouts


_supplements_tool_dependencies = SupplementsToolDependencies(
    get_session_factory=lambda: get_session_factory(),
    legacy_owner=_mcp_v1_legacy_owner,
    conflict_scope=_mcp_v1_conflict_scope,
    conflict_write_context=_mcp_v1_conflict_write_context,
    conflict_payload=_conflict_payload,
    serialize_row=serialize_row,
    serialize_written=serialize_written,
    gated=gated,
)
_supplements_read_tools = register_supplements_read_tools(
    mcp,
    _supplements_tool_dependencies,
)
get_supplements_catalog = _supplements_read_tools.get_supplements_catalog

_skincare_tool_dependencies = SkincareToolDependencies(
    get_session_factory=lambda: get_session_factory(),
    parse_date=_parse_date,
    conflict_scope=_mcp_v1_conflict_scope,
    conflict_write_context=_mcp_v1_conflict_write_context,
    conflict_payload=_conflict_payload,
    serialize_row=serialize_row,
    serialize_written=serialize_written,
    gated=gated,
)
_skincare_read_tools = register_skincare_read_tools(
    mcp,
    _skincare_tool_dependencies,
)
get_skincare_logs = _skincare_read_tools.get_skincare_logs

_genetics_tool_dependencies = GeneticsToolDependencies(
    get_session_factory=lambda: get_session_factory(),
    conflict_scope=_mcp_v1_conflict_scope,
    conflict_write_context=_mcp_v1_conflict_write_context,
    serialize_row=serialize_row,
    serialize_written=serialize_written,
    gated=gated,
)
_genetics_tools = register_genetics_tools(mcp, _genetics_tool_dependencies)
get_genetics_snps = _genetics_tools.get_genetics_snps
upsert_genetic_variant = _genetics_tools.upsert_genetic_variant
_alert_tool_dependencies = AlertToolDependencies(
    get_session_factory=lambda: get_session_factory(),
    legacy_alert_owner=_mcp_v1_legacy_alert_owner,
    serialize_row=serialize_row,
    serialize_written=serialize_written,
)
_alert_tools = register_alert_tools(mcp, _alert_tool_dependencies)
get_active_alerts = _alert_tools.get_active_alerts
resolve_alert = _alert_tools.resolve_alert
override_alert = _alert_tools.override_alert
_digest_tool_dependencies = DigestToolDependencies(
    get_session_factory=lambda: get_session_factory(),
    actor_username=_mcp_actor_username,
    serialize_row=serialize_row,
    serialize_written=serialize_written,
)
_digest_read_tools = register_digest_read_tools(
    mcp,
    _digest_tool_dependencies,
)
get_weekly_digests = _digest_read_tools.get_weekly_digests


_supplements_conflict_tools = register_supplements_conflict_tools(
    mcp,
    _supplements_tool_dependencies,
)
check_supplement_conflicts = (
    _supplements_conflict_tools.check_supplement_conflicts
)


_conflict_tool_dependencies = ConflictToolDependencies(
    get_session_factory=lambda: get_session_factory(),
    conflict_scope=_mcp_v1_conflict_scope,
    serialize_row=serialize_row,
)
_conflict_tools = register_conflict_tools(mcp, _conflict_tool_dependencies)
list_conflict_rules = _conflict_tools.list_conflict_rules
check_conflicts = _conflict_tools.check_conflicts


# ── Nutrition tools ──────────────────────────────────────────────────────────
_nutrition_tools = register_nutrition_tools(
    mcp,
    NutritionToolDependencies(
        get_session_factory=lambda: get_session_factory(),
        parse_date=_parse_date,
        parse_time=_parse_time,
        conflict_scope=_mcp_v1_conflict_scope,
        conflict_write_context=_mcp_v1_conflict_write_context,
        conflict_payload=_conflict_payload,
        serialize_row=serialize_row,
        serialize_written=serialize_written,
        gated=gated,
    ),
)
log_meal = _nutrition_tools.log_meal
get_nutrition_summary = _nutrition_tools.get_nutrition_summary
update_meal = _nutrition_tools.update_meal
search_meals = _nutrition_tools.search_meals


# ── Weight tools ────────────────────────────────────────────────────────────

_weight_write_tools = register_weight_write_tools(mcp, _weight_tool_dependencies)
log_weight = _weight_write_tools.log_weight


# ── GLP-1 tools ─────────────────────────────────────────────────────────────

_glp1_injection_tools = register_glp1_injection_tools(
    mcp,
    _glp1_tool_dependencies,
)
log_glp1 = _glp1_injection_tools.log_glp1


# ── HRT / TRT tools ─────────────────────────────────────────────────────────

_hrt_tool_dependencies = HrtToolDependencies(
    get_session_factory=lambda: get_session_factory(),
    parse_date=_parse_date,
    conflict_scope=_mcp_v1_conflict_scope,
    conflict_write_context=_mcp_v1_conflict_write_context,
    conflict_payload=_conflict_payload,
    serialize_row=serialize_row,
    serialize_written=serialize_written,
    gated=gated,
)
_hrt_tools = register_hrt_tools(mcp, _hrt_tool_dependencies)
get_hrt_logs = _hrt_tools.get_hrt_logs
log_hrt_dose = _hrt_tools.log_hrt_dose
add_hrt_cycle = _hrt_tools.add_hrt_cycle
add_hrt_cycle_item = _hrt_tools.add_hrt_cycle_item
update_hrt_dose = _hrt_tools.update_hrt_dose
log_hrt_side_effect = _hrt_tools.log_hrt_side_effect
close_hrt_cycle = _hrt_tools.close_hrt_cycle
get_hrt_cycles = _hrt_tools.get_hrt_cycles


# ── Skincare tools ──────────────────────────────────────────────────────────

_skincare_routine_tools = register_skincare_routine_tools(
    mcp,
    _skincare_tool_dependencies,
)
log_skincare = _skincare_routine_tools.log_skincare


# ── Body measurement tools ──────────────────────────────────────────────────

_measurement_tools = register_measurement_tools(mcp, _weight_tool_dependencies)
log_measurement = _measurement_tools.log_measurement
get_measurements = _measurement_tools.get_measurements


# ── Notes tools ─────────────────────────────────────────────────────────────

_record_tool_dependencies = RecordToolDependencies(
    get_session_factory=lambda: get_session_factory(),
    parse_date=_parse_date,
    module_enabled=_module_enabled,
    conflict_scope=_mcp_v1_conflict_scope,
    conflict_write_context=_mcp_v1_conflict_write_context,
    weight_write=lambda *args, **kwargs: _mcp_v1_weight_write(*args, **kwargs),
    auxiliary_weight_write=lambda *args, **kwargs: _mcp_v1_aux_weight_write(
        *args,
        **kwargs,
    ),
    legacy_owner=_mcp_v1_legacy_owner,
    serialize_row=serialize_row,
    serialize_written=serialize_written,
)
_note_tools = register_note_tools(mcp, _record_tool_dependencies)
log_note = _note_tools.log_note
get_notes = _note_tools.get_notes


# ── Deletion (one tool, every domain) ─────────────────────────────────────────

_delete_tools = register_delete_tools(mcp, _record_tool_dependencies)
delete_record = _delete_tools.delete_record


# ── Body composition tools (InBody / МедАсс — optional module) ────────────────
_body_composition_tool_dependencies = BodyCompositionToolDependencies(
    get_session_factory=lambda: get_session_factory(),
    parse_date=_parse_date,
    module_enabled=_module_enabled,
    conflict_scope=_mcp_v1_conflict_scope,
    weight_write=lambda *args, **kwargs: _mcp_v1_weight_write(*args, **kwargs),
    conflict_payload=_conflict_payload,
    gated=gated,
)
_body_composition_tools = register_body_composition_tools(
    mcp,
    _body_composition_tool_dependencies,
)
get_body_scans = _body_composition_tools.get_body_scans
get_body_scan = _body_composition_tools.get_body_scan
get_body_metric_history = _body_composition_tools.get_body_metric_history
log_body_scan = _body_composition_tools.log_body_scan


# ── Labs tools ──────────────────────────────────────────────────────────────
_labs_tool_dependencies = LabsToolDependencies(
    # Keep the router's direct-test monkeypatch seam dynamic.
    get_session_factory=lambda: get_session_factory(),
    parse_date=_parse_date,
    conflict_scope=_mcp_v1_conflict_scope,
    conflict_write_context=_mcp_v1_conflict_write_context,
    conflict_payload=_conflict_payload,
    serialize_row=serialize_row,
    serialize_written=serialize_written,
)
_labs_tools = register_labs_tools(
    mcp,
    _labs_tool_dependencies,
)
get_lab_results = _labs_tools.get_lab_results
log_lab_result = _labs_tools.log_lab_result
update_lab_result = _labs_tools.update_lab_result
log_lab_results = _labs_tools.log_lab_results

# ── Timeline tools ───────────────────────────────────────────────────────────
_timeline_tools = register_timeline_tools(
    mcp,
    TimelineToolDependencies(
        get_session_factory=lambda: get_session_factory(),
        parse_date=_parse_date,
        conflict_scope=_mcp_v1_conflict_scope,
        legacy_owner=_mcp_v1_legacy_owner,
        serialize_written=serialize_written,
        gated=gated,
    ),
)
get_timeline = _timeline_tools.get_timeline
log_event = _timeline_tools.log_event
update_event = _timeline_tools.update_event


# ── Cross-domain + whole-lake tools ──────────────────────────────────────────
_reporting_tool_dependencies = ReportingToolDependencies(
    get_session_factory=lambda: get_session_factory(),
    parse_date=_parse_date,
    composition_scope=_mcp_v1_composition_scope,
    conflict_scope=_mcp_v1_conflict_scope,
)
_reporting_tools = register_reporting_tools(
    mcp,
    _reporting_tool_dependencies,
)
get_full_snapshot = _reporting_tools.get_full_snapshot
export_everything = _reporting_tools.export_everything
get_data_overview = _reporting_tools.get_data_overview


# ── Milestones / goals tools ──────────────────────────────────────────────────
_milestone_tool_dependencies = MilestoneToolDependencies(
    get_session_factory=lambda: get_session_factory(),
    parse_date=_parse_date,
    conflict_scope=_mcp_v1_conflict_scope,
    conflict_write_context=_mcp_v1_conflict_write_context,
    serialize_written=serialize_written,
)
_milestone_tools = register_milestone_tools(
    mcp,
    _milestone_tool_dependencies,
)
get_milestones = _milestone_tools.get_milestones
create_milestone = _milestone_tools.create_milestone
update_milestone = _milestone_tools.update_milestone


# ── GLP-1 write completeness (edit/delete injection, side effects, phases) ────
_glp1_maintenance_tools = register_glp1_maintenance_tools(
    mcp,
    _glp1_tool_dependencies,
)
update_glp1 = _glp1_maintenance_tools.update_glp1
log_side_effect = _glp1_maintenance_tools.log_side_effect
add_dose_phase = _glp1_maintenance_tools.add_dose_phase


# ── Skincare observations ─────────────────────────────────────────────────────
_skincare_observation_tools = register_skincare_observation_tools(
    mcp,
    _skincare_tool_dependencies,
)
log_skincare_observation = (
    _skincare_observation_tools.log_skincare_observation
)


# ── Supplements catalog CRUD ──────────────────────────────────────────────────
_supplements_write_tools = register_supplements_write_tools(
    mcp,
    _supplements_tool_dependencies,
)
add_supplement = _supplements_write_tools.add_supplement
update_supplement = _supplements_write_tools.update_supplement
set_supplement_active = _supplements_write_tools.set_supplement_active


# ── Body measurement edit/delete + noise markers ──────────────────────────────
_measurement_update_tools = register_measurement_update_tools(
    mcp,
    _weight_tool_dependencies,
)
update_measurement = _measurement_update_tools.update_measurement


_noise_tools = register_noise_tools(mcp, _weight_tool_dependencies)
add_noise_marker = _noise_tools.add_noise_marker


# ── Modules (optional-domain toggles) ─────────────────────────────────────────
_module_settings_tool_dependencies = ModuleSettingsToolDependencies(
    get_session_factory=lambda: get_session_factory(),
    legacy_owner=_mcp_v1_legacy_owner,
    get_redis_client=lambda: get_redis_client(),
)
_module_settings_tools = register_module_settings_tools(
    mcp,
    _module_settings_tool_dependencies,
)
get_modules = _module_settings_tools.get_modules
set_module = _module_settings_tools.set_module


# ── Weekly digest generation ──────────────────────────────────────────────────
_digest_tools = register_digest_tools(mcp, _digest_tool_dependencies)
generate_digest_now = _digest_tools.generate_digest_now


# ── Trend analytics ───────────────────────────────────────────────────────────
_trend_tools = register_trend_tools(mcp, _reporting_tool_dependencies)
get_trend = _trend_tools.get_trend


_proactive_tool_dependencies = ProactiveToolDependencies(
    get_session_factory=lambda: get_session_factory(),
    actor_username=_mcp_actor_username,
    serialize_row=serialize_row,
)
_proactive_tools = register_proactive_tools(
    mcp,
    _proactive_tool_dependencies,
)
get_proactive_state = _proactive_tools.get_proactive_state


# ── Sync tools (pull from Garmin / Hevy on demand) ────────────────────────────
SYNC_DAILY_LIMIT = _DEFAULT_SYNC_DAILY_LIMIT

_garmin_sync_tools = register_garmin_sync_tools(
    mcp,
    _provider_tool_dependencies,
)
sync_garmin = _garmin_sync_tools.sync_garmin

_hevy_sync_tools = register_hevy_sync_tools(
    mcp,
    _provider_tool_dependencies,
)
sync_hevy = _hevy_sync_tools.sync_hevy


# ── Resources & prompts ───────────────────────────────────────────────────────
_resources = register_resources(
    mcp,
    ResourceDependencies(
        get_session_factory=lambda: get_session_factory(),
        actor_username=_mcp_actor_username,
        get_user_profile=lambda: get_user_profile(),
    ),
)
profile_resource = _resources.profile_resource
latest_digest_resource = _resources.latest_digest_resource
weekly_review = _resources.weekly_review


# The read side of the same map (the writes registered themselves via ``gated``).
# ``tests/test_mcp_tool_budget.py`` checks every name here is a real tool, so a
# rename can't quietly leave a domain's reads visible forever.
TOOL_MODULES.update({
    "get_glp1_logs": "glp1",
    "get_hevy_workouts": "hevy",
    "get_supplements_catalog": "supplements",
    "get_skincare_logs": "skincare",
    "get_genetics_snps": "genetics",
    "get_hrt_logs": "hrt",
    "get_hrt_cycles": "hrt",
    "get_nutrition_summary": "nutrition",
    "search_meals": "nutrition",
    "get_body_scans": "body_comp",
    "get_body_scan": "body_comp",
    "get_body_metric_history": "body_comp",
    "get_timeline": "timeline",
})


def get_mcp_app() -> tuple[object, object]:
    """Return the stateless MCP app and its mount-owned lifespan."""

    return _build_mcp_transport(mcp, public_url=get_web_config().public_url)
