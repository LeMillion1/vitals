"""Static package boundaries for the staged MCP router extraction."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MCP_PACKAGE = ROOT / "web" / "mcp"
MCP_TOOLS_PACKAGE = MCP_PACKAGE / "tools"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


def test_mcp_common_leaves_do_not_reverse_import_router_or_package() -> None:
    for name in (
        "access.py",
        "arguments.py",
        "conflicts.py",
        "errors.py",
        "identity.py",
        "modules.py",
        "ownership.py",
        "resources.py",
        "serialization.py",
        "server.py",
        "transport.py",
    ):
        imports = _imports(MCP_PACKAGE / name)
        assert "web.routers.mcp" not in imports
        assert "web.mcp" not in imports


def test_mcp_common_leaves_do_not_import_domain_services() -> None:
    for name in (
        "access.py",
        "arguments.py",
        "errors.py",
        "identity.py",
        "serialization.py",
        "transport.py",
    ):
        imports = _imports(MCP_PACKAGE / name)
        assert not {
            imported for imported in imports if imported.startswith("vitals.services")
        }


def test_mcp_package_init_has_no_aggregate_exports() -> None:
    assert not _imports(MCP_PACKAGE / "__init__.py")
    assert not _imports(MCP_TOOLS_PACKAGE / "__init__.py")


def test_mcp_labs_tool_leaf_does_not_reverse_import_router() -> None:
    imports = _imports(MCP_TOOLS_PACKAGE / "labs.py")
    assert "web.routers.mcp" not in imports
    assert not {
        imported for imported in imports if imported.startswith("web.routers")
    }


def test_mcp_hrt_tool_leaf_does_not_reverse_import_router() -> None:
    imports = _imports(MCP_TOOLS_PACKAGE / "hrt.py")
    assert "web.routers.mcp" not in imports
    assert not {
        imported for imported in imports if imported.startswith("web.routers")
    }


def test_mcp_supplements_skincare_and_genetics_leaves_stay_out_of_router_and_orm(
) -> None:
    for name in ("supplements.py", "skincare.py", "genetics.py"):
        imports = _imports(MCP_TOOLS_PACKAGE / name)
        assert not {
            imported for imported in imports if imported.startswith("web.routers")
        }
        assert not {
            imported for imported in imports if imported.startswith("vitals.models")
        }


def test_mcp_glp1_tool_leaf_stays_out_of_router_and_orm() -> None:
    imports = _imports(MCP_TOOLS_PACKAGE / "glp1.py")
    assert not {imported for imported in imports if imported.startswith("web.routers")}
    assert not {imported for imported in imports if imported.startswith("vitals.models")}


def test_mcp_alert_and_conflict_tool_leaves_stay_out_of_router_and_orm() -> None:
    for name in ("alerts.py", "conflicts.py"):
        imports = _imports(MCP_TOOLS_PACKAGE / name)
        assert not {
            imported for imported in imports if imported.startswith("web.routers")
        }
        assert not {
            imported for imported in imports if imported.startswith("vitals.models")
        }


def test_mcp_records_leaf_uses_explicit_services_not_generic_orm_crud() -> None:
    path = MCP_TOOLS_PACKAGE / "records.py"
    imports = _imports(path)
    source = path.read_text(encoding="utf-8")
    assert not {imported for imported in imports if imported.startswith("web.routers")}
    assert not {imported for imported in imports if imported.startswith("vitals.models")}
    assert "session.get(" not in source
    assert "session.delete(" not in source
    assert "select(" not in source


def test_mcp_provider_tool_leaf_uses_query_apis_not_orm() -> None:
    path = MCP_TOOLS_PACKAGE / "providers.py"
    imports = _imports(path)
    source = path.read_text(encoding="utf-8")
    assert not {imported for imported in imports if imported.startswith("web.routers")}
    assert not {imported for imported in imports if imported.startswith("vitals.models")}
    assert not {imported for imported in imports if imported.startswith("sqlalchemy")}
    assert "session.execute(" not in source
    assert "select(" not in source


def test_mcp_router_contains_no_direct_tool_registrations() -> None:
    path = ROOT / "web" / "routers" / "mcp.py"
    source = path.read_text(encoding="utf-8")
    imports = _imports(path)
    assert "@mcp.tool" not in source
    assert not {imported for imported in imports if imported.startswith("sqlalchemy")}
    assert not {
        imported for imported in imports if imported.startswith("vitals.models")
    }


def test_mcp_router_reexports_registered_provider_callables(monkeypatch) -> None:
    from web.mcp import conflicts, ownership
    from web.routers import mcp as router

    dependencies = router._provider_tool_dependencies
    assert dependencies.parse_date is router._parse_date
    assert dependencies.conflict_scope is conflicts.conflict_scope
    assert dependencies.actor_username is ownership.actor_username
    assert dependencies.serialize_row is router.serialize_row
    assert dependencies.gated is router.gated
    sentinel_factory = object()
    sentinel_redis = object()
    monkeypatch.setattr(router, "get_session_factory", lambda: sentinel_factory)
    monkeypatch.setattr(router, "get_redis_client", lambda: sentinel_redis)
    assert dependencies.get_session_factory() is sentinel_factory
    assert dependencies.get_redis_client() is sentinel_redis
    monkeypatch.setattr(router, "INTRADAY_POINT_CAP", 17)
    monkeypatch.setattr(router, "SYNC_DAILY_LIMIT", 5)
    assert dependencies.intraday_point_cap() == 17
    assert dependencies.sync_daily_limit() == 5
    assert router.get_garmin_metrics is router._garmin_read_tools.get_garmin_metrics
    assert router.get_hevy_workouts is router._hevy_read_tools.get_hevy_workouts
    assert router.sync_garmin is router._garmin_sync_tools.sync_garmin
    assert router.sync_hevy is router._hevy_sync_tools.sync_hevy
    assert {
        router.get_garmin_metrics.__module__,
        router.get_hevy_workouts.__module__,
        router.sync_garmin.__module__,
        router.sync_hevy.__module__,
    } == {"web.mcp.tools.providers"}


def test_mcp_record_catalog_is_compatibility_metadata_only() -> None:
    imports = _imports(MCP_PACKAGE / "record_catalog.py")
    source = (MCP_PACKAGE / "record_catalog.py").read_text(encoding="utf-8")
    assert imports == {"__future__", "typing", "vitals.models"}
    assert "session." not in source
    assert "vitals.services" not in source


def test_mcp_router_reexports_registered_record_hubs_and_catalogs(monkeypatch) -> None:
    from web.mcp import conflicts, ownership, record_catalog
    from web.routers import mcp as router

    dependencies = router._record_tool_dependencies
    assert dependencies.parse_date is router._parse_date
    assert dependencies.module_enabled is router._module_enabled
    assert dependencies.conflict_scope is conflicts.conflict_scope
    assert dependencies.conflict_write_context is conflicts.conflict_write_context
    assert dependencies.legacy_owner is ownership.legacy_owner
    assert dependencies.serialize_row is router.serialize_row
    assert dependencies.serialize_written is router.serialize_written
    assert callable(dependencies.weight_write)
    assert callable(dependencies.auxiliary_weight_write)
    sentinel_factory = object()
    monkeypatch.setattr(router, "get_session_factory", lambda: sentinel_factory)
    assert dependencies.get_session_factory() is sentinel_factory
    assert router._NOTE_MODELS is record_catalog.NOTE_MODELS
    assert router._DELETE_TARGETS is record_catalog.DELETE_TARGETS
    assert router.log_note is router._note_tools.log_note
    assert router.get_notes is router._note_tools.get_notes
    assert router.delete_record is router._delete_tools.delete_record
    assert {
        router.log_note.__module__,
        router.get_notes.__module__,
        router.delete_record.__module__,
    } == {"web.mcp.tools.records"}


def test_mcp_router_reexports_registered_alert_callables(monkeypatch) -> None:
    from web.mcp import ownership
    from web.routers import mcp as router

    dependencies = router._alert_tool_dependencies
    assert dependencies.legacy_alert_owner is ownership.legacy_alert_owner
    assert dependencies.serialize_row is router.serialize_row
    assert dependencies.serialize_written is router.serialize_written
    sentinel_factory = object()
    monkeypatch.setattr(router, "get_session_factory", lambda: sentinel_factory)
    assert dependencies.get_session_factory() is sentinel_factory
    assert router.get_active_alerts is router._alert_tools.get_active_alerts
    assert router.resolve_alert is router._alert_tools.resolve_alert
    assert router.override_alert is router._alert_tools.override_alert
    assert {
        router.get_active_alerts.__module__,
        router.resolve_alert.__module__,
        router.override_alert.__module__,
    } == {"web.mcp.tools.alerts"}


def test_mcp_router_reexports_registered_conflict_callables(monkeypatch) -> None:
    from web.mcp import conflicts
    from web.routers import mcp as router

    dependencies = router._conflict_tool_dependencies
    assert dependencies.conflict_scope is conflicts.conflict_scope
    assert dependencies.serialize_row is router.serialize_row
    sentinel_factory = object()
    monkeypatch.setattr(router, "get_session_factory", lambda: sentinel_factory)
    assert dependencies.get_session_factory() is sentinel_factory
    assert router.list_conflict_rules is router._conflict_tools.list_conflict_rules
    assert router.check_conflicts is router._conflict_tools.check_conflicts
    assert {
        router.list_conflict_rules.__module__,
        router.check_conflicts.__module__,
    } == {"web.mcp.tools.conflicts"}


def test_mcp_router_reexports_registered_glp1_callables(monkeypatch) -> None:
    from web.mcp import conflicts
    from web.routers import mcp as router

    dependencies = router._glp1_tool_dependencies
    assert dependencies.conflict_scope is conflicts.conflict_scope
    assert dependencies.conflict_write_context is conflicts.conflict_write_context
    assert dependencies.parse_date is router._parse_date
    assert dependencies.conflict_payload is router._conflict_payload
    assert dependencies.serialize_row is router.serialize_row
    assert dependencies.serialize_written is router.serialize_written
    assert dependencies.gated is router.gated
    sentinel_factory = object()
    monkeypatch.setattr(router, "get_session_factory", lambda: sentinel_factory)
    assert dependencies.get_session_factory() is sentinel_factory
    assert router.get_glp1_logs is router._glp1_read_tools.get_glp1_logs
    assert router.log_glp1 is router._glp1_injection_tools.log_glp1
    assert router.update_glp1 is router._glp1_maintenance_tools.update_glp1
    assert router.log_side_effect is router._glp1_maintenance_tools.log_side_effect
    assert router.add_dose_phase is router._glp1_maintenance_tools.add_dose_phase
    assert {
        router.get_glp1_logs.__module__,
        router.log_glp1.__module__,
        router.update_glp1.__module__,
        router.log_side_effect.__module__,
        router.add_dose_phase.__module__,
    } == {"web.mcp.tools.glp1"}


def test_mcp_router_reexports_registered_supplements_callables(monkeypatch) -> None:
    from web.mcp import conflicts, ownership
    from web.routers import mcp as router

    dependencies = router._supplements_tool_dependencies
    assert dependencies.legacy_owner is ownership.legacy_owner
    assert dependencies.conflict_scope is conflicts.conflict_scope
    assert dependencies.conflict_write_context is conflicts.conflict_write_context
    assert dependencies.conflict_payload is router._conflict_payload
    assert dependencies.serialize_row is router.serialize_row
    assert dependencies.serialize_written is router.serialize_written
    assert dependencies.gated is router.gated
    sentinel_factory = object()
    monkeypatch.setattr(router, "get_session_factory", lambda: sentinel_factory)
    assert dependencies.get_session_factory() is sentinel_factory
    assert (
        router.get_supplements_catalog
        is router._supplements_read_tools.get_supplements_catalog
    )
    assert (
        router.check_supplement_conflicts
        is router._supplements_conflict_tools.check_supplement_conflicts
    )
    assert router.add_supplement is router._supplements_write_tools.add_supplement
    assert (
        router.update_supplement
        is router._supplements_write_tools.update_supplement
    )
    assert (
        router.set_supplement_active
        is router._supplements_write_tools.set_supplement_active
    )
    assert {
        router.get_supplements_catalog.__module__,
        router.check_supplement_conflicts.__module__,
        router.add_supplement.__module__,
        router.update_supplement.__module__,
        router.set_supplement_active.__module__,
    } == {"web.mcp.tools.supplements"}


def test_mcp_router_reexports_registered_skincare_callables(monkeypatch) -> None:
    from web.mcp import conflicts
    from web.routers import mcp as router

    dependencies = router._skincare_tool_dependencies
    assert dependencies.conflict_scope is conflicts.conflict_scope
    assert dependencies.conflict_write_context is conflicts.conflict_write_context
    assert dependencies.parse_date is router._parse_date
    assert dependencies.conflict_payload is router._conflict_payload
    assert dependencies.serialize_row is router.serialize_row
    assert dependencies.serialize_written is router.serialize_written
    assert dependencies.gated is router.gated
    sentinel_factory = object()
    monkeypatch.setattr(router, "get_session_factory", lambda: sentinel_factory)
    assert dependencies.get_session_factory() is sentinel_factory
    assert router.get_skincare_logs is router._skincare_read_tools.get_skincare_logs
    assert router.log_skincare is router._skincare_routine_tools.log_skincare
    assert (
        router.log_skincare_observation
        is router._skincare_observation_tools.log_skincare_observation
    )
    assert {
        router.get_skincare_logs.__module__,
        router.log_skincare.__module__,
        router.log_skincare_observation.__module__,
    } == {"web.mcp.tools.skincare"}


def test_mcp_router_reexports_registered_genetics_callables(monkeypatch) -> None:
    from web.mcp import conflicts
    from web.routers import mcp as router

    dependencies = router._genetics_tool_dependencies
    assert dependencies.conflict_scope is conflicts.conflict_scope
    assert dependencies.conflict_write_context is conflicts.conflict_write_context
    assert dependencies.serialize_row is router.serialize_row
    assert dependencies.serialize_written is router.serialize_written
    assert dependencies.gated is router.gated
    sentinel_factory = object()
    monkeypatch.setattr(router, "get_session_factory", lambda: sentinel_factory)
    assert dependencies.get_session_factory() is sentinel_factory
    assert router.get_genetics_snps is router._genetics_tools.get_genetics_snps
    assert (
        router.upsert_genetic_variant
        is router._genetics_tools.upsert_genetic_variant
    )
    assert {
        router.get_genetics_snps.__module__,
        router.upsert_genetic_variant.__module__,
    } == {"web.mcp.tools.genetics"}


def test_mcp_nutrition_tool_leaf_stays_out_of_router_and_orm() -> None:
    imports = _imports(MCP_TOOLS_PACKAGE / "nutrition.py")
    assert not {imported for imported in imports if imported.startswith("web.routers")}
    assert not {imported for imported in imports if imported.startswith("vitals.models")}


def test_mcp_timeline_tool_leaf_stays_out_of_router_and_orm() -> None:
    imports = _imports(MCP_TOOLS_PACKAGE / "timeline.py")
    assert not {imported for imported in imports if imported.startswith("web.routers")}
    assert not {imported for imported in imports if imported.startswith("vitals.models")}


def test_mcp_profile_tool_leaf_stays_out_of_router_and_orm() -> None:
    imports = _imports(MCP_TOOLS_PACKAGE / "profile.py")
    assert not {imported for imported in imports if imported.startswith("web.routers")}
    assert not {imported for imported in imports if imported.startswith("vitals.models")}


def test_mcp_router_reexports_registered_nutrition_callables() -> None:
    from web.routers import mcp as router

    assert router.log_meal is router._nutrition_tools.log_meal
    assert router.get_nutrition_summary is router._nutrition_tools.get_nutrition_summary
    assert router.update_meal is router._nutrition_tools.update_meal
    assert router.search_meals is router._nutrition_tools.search_meals


def test_mcp_router_reexports_registered_timeline_callables() -> None:
    from web.routers import mcp as router

    assert router.get_timeline is router._timeline_tools.get_timeline
    assert router.log_event is router._timeline_tools.log_event
    assert router.update_event is router._timeline_tools.update_event


def test_mcp_router_reexports_registered_resources_and_prompt() -> None:
    from web.routers import mcp as router

    assert router.profile_resource is router._resources.profile_resource
    assert router.latest_digest_resource is router._resources.latest_digest_resource
    assert router.weekly_review is router._resources.weekly_review


def test_mcp_profile_tool_is_owned_by_its_adapter() -> None:
    from web.routers import mcp as router

    assert router.get_user_profile.__module__ == "web.mcp.tools.profile"


def test_mcp_weight_and_body_composition_leaves_stay_out_of_router_and_orm() -> None:
    for name in ("weight.py", "body_composition.py"):
        imports = _imports(MCP_TOOLS_PACKAGE / name)
        assert not {
            imported for imported in imports if imported.startswith("web.routers")
        }
        assert not {
            imported for imported in imports if imported.startswith("vitals.models")
        }


def test_mcp_router_reexports_registered_weight_callables(monkeypatch) -> None:
    from web.mcp import conflicts
    from web.routers import mcp as router

    dependencies = router._weight_tool_dependencies
    assert dependencies.conflict_scope is conflicts.conflict_scope
    assert callable(dependencies.weight_write)
    assert callable(dependencies.auxiliary_weight_write)
    assert dependencies.parse_date is router._parse_date
    assert dependencies.conflict_payload is router._conflict_payload
    assert dependencies.serialize_row is router.serialize_row
    assert dependencies.serialize_written is router.serialize_written
    sentinel_factory = object()
    monkeypatch.setattr(router, "get_session_factory", lambda: sentinel_factory)
    assert dependencies.get_session_factory() is sentinel_factory
    assert router.get_weight_logs is router._weight_read_tools.get_weight_logs
    assert router.log_weight is router._weight_write_tools.log_weight
    assert router.log_measurement is router._measurement_tools.log_measurement
    assert router.get_measurements is router._measurement_tools.get_measurements
    assert (
        router.update_measurement
        is router._measurement_update_tools.update_measurement
    )
    assert router.add_noise_marker is router._noise_tools.add_noise_marker
    assert {
        router.get_weight_logs.__module__,
        router.log_weight.__module__,
        router.log_measurement.__module__,
        router.get_measurements.__module__,
        router.update_measurement.__module__,
        router.add_noise_marker.__module__,
    } == {"web.mcp.tools.weight"}


def test_mcp_router_reexports_registered_body_composition_callables(
    monkeypatch,
) -> None:
    from web.mcp import conflicts
    from web.mcp.tools import body_composition
    from web.routers import mcp as router

    dependencies = router._body_composition_tool_dependencies
    assert dependencies.conflict_scope is conflicts.conflict_scope
    assert callable(dependencies.weight_write)
    assert dependencies.parse_date is router._parse_date
    assert dependencies.module_enabled is router._module_enabled
    assert dependencies.conflict_payload is router._conflict_payload
    assert router._serialize_scan is body_composition.serialize_scan
    assert dependencies.gated is router.gated
    sentinel_factory = object()
    monkeypatch.setattr(router, "get_session_factory", lambda: sentinel_factory)
    assert dependencies.get_session_factory() is sentinel_factory
    assert router.get_body_scans is router._body_composition_tools.get_body_scans
    assert router.get_body_scan is router._body_composition_tools.get_body_scan
    assert (
        router.get_body_metric_history
        is router._body_composition_tools.get_body_metric_history
    )
    assert router.log_body_scan is router._body_composition_tools.log_body_scan
    assert {
        router.get_body_scans.__module__,
        router.get_body_scan.__module__,
        router.get_body_metric_history.__module__,
        router.log_body_scan.__module__,
    } == {"web.mcp.tools.body_composition"}


def test_mcp_milestone_and_module_settings_leaves_stay_out_of_router_and_orm() -> None:
    for name in ("milestones.py", "module_settings.py"):
        imports = _imports(MCP_TOOLS_PACKAGE / name)
        assert not {
            imported for imported in imports if imported.startswith("web.routers")
        }
        assert not {
            imported for imported in imports if imported.startswith("vitals.models")
        }


def test_mcp_router_reexports_registered_milestone_callables(monkeypatch) -> None:
    from web.mcp import conflicts
    from web.routers import mcp as router

    dependencies = router._milestone_tool_dependencies
    assert dependencies.conflict_scope is conflicts.conflict_scope
    assert dependencies.conflict_write_context is conflicts.conflict_write_context
    assert dependencies.parse_date is router._parse_date
    assert dependencies.serialize_written is router.serialize_written
    sentinel_factory = object()
    monkeypatch.setattr(router, "get_session_factory", lambda: sentinel_factory)
    assert dependencies.get_session_factory() is sentinel_factory
    assert router.get_milestones is router._milestone_tools.get_milestones
    assert router.create_milestone is router._milestone_tools.create_milestone
    assert router.update_milestone is router._milestone_tools.update_milestone
    assert {
        router.get_milestones.__module__,
        router.create_milestone.__module__,
        router.update_milestone.__module__,
    } == {"web.mcp.tools.milestones"}


def test_mcp_router_reexports_registered_module_settings_callables(
    monkeypatch,
) -> None:
    from web.routers import mcp as router

    dependencies = router._module_settings_tool_dependencies
    assert dependencies.legacy_owner is router._mcp_v1_legacy_owner
    sentinel_factory = object()
    sentinel_redis = object()
    monkeypatch.setattr(router, "get_session_factory", lambda: sentinel_factory)
    monkeypatch.setattr(router, "get_redis_client", lambda: sentinel_redis)
    assert dependencies.get_session_factory() is sentinel_factory
    assert dependencies.get_redis_client() is sentinel_redis
    assert router.get_modules is router._module_settings_tools.get_modules
    assert router.set_module is router._module_settings_tools.set_module
    assert {
        router.get_modules.__module__,
        router.set_module.__module__,
    } == {"web.mcp.tools.module_settings"}


def test_mcp_reporting_digest_and_proactive_leaves_do_not_reverse_import_router() -> None:
    for name in ("reporting.py", "digest.py", "proactive.py"):
        imports = _imports(MCP_TOOLS_PACKAGE / name)
        assert not {
            imported for imported in imports if imported.startswith("web.routers")
        }
        assert not {
            imported for imported in imports if imported.startswith("vitals.models")
        }


def test_data_overview_projection_is_core_owned() -> None:
    projection = ROOT / "vitals" / "services" / "projections" / "data_overview.py"
    projection_imports = _imports(projection)
    assert not {
        imported for imported in projection_imports if imported.startswith("web")
    }

    reporting_imports = _imports(MCP_TOOLS_PACKAGE / "reporting.py")
    assert "vitals.services.projections.data_overview" in reporting_imports

    router = ROOT / "web" / "routers" / "mcp.py"
    router_tree = ast.parse(router.read_text(encoding="utf-8"), filename=str(router))
    router_functions = {
        node.name
        for node in router_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "_data_overview" not in router_functions


def test_mcp_router_reexports_registered_reporting_callables(monkeypatch) -> None:
    from web.mcp import conflicts
    from web.routers import mcp as router

    dependencies = router._reporting_tool_dependencies
    assert dependencies.composition_scope is conflicts.composition_scope
    assert dependencies.conflict_scope is conflicts.conflict_scope
    assert dependencies.parse_date is router._parse_date
    sentinel_factory = object()
    monkeypatch.setattr(router, "get_session_factory", lambda: sentinel_factory)
    assert dependencies.get_session_factory() is sentinel_factory
    assert router.get_full_snapshot is router._reporting_tools.get_full_snapshot
    assert router.export_everything is router._reporting_tools.export_everything
    assert router.get_data_overview is router._reporting_tools.get_data_overview
    assert router.get_trend is router._trend_tools.get_trend
    assert {
        router.get_full_snapshot.__module__,
        router.export_everything.__module__,
        router.get_data_overview.__module__,
        router.get_trend.__module__,
    } == {"web.mcp.tools.reporting"}


def test_mcp_router_reexports_registered_digest_and_proactive_callables(
    monkeypatch,
) -> None:
    from web.routers import mcp as router

    sentinel_factory = object()
    monkeypatch.setattr(router, "get_session_factory", lambda: sentinel_factory)

    digest_dependencies = router._digest_tool_dependencies
    assert digest_dependencies.actor_username is router._mcp_actor_username
    assert digest_dependencies.serialize_row is router.serialize_row
    assert digest_dependencies.serialize_written is router.serialize_written
    assert digest_dependencies.get_session_factory() is sentinel_factory
    assert (
        router.get_weekly_digests
        is router._digest_read_tools.get_weekly_digests
    )
    assert router.generate_digest_now is router._digest_tools.generate_digest_now

    proactive_dependencies = router._proactive_tool_dependencies
    assert proactive_dependencies.actor_username is router._mcp_actor_username
    assert proactive_dependencies.serialize_row is router.serialize_row
    assert proactive_dependencies.get_session_factory() is sentinel_factory
    assert router.get_proactive_state is router._proactive_tools.get_proactive_state

    assert {
        router.get_weekly_digests.__module__,
        router.generate_digest_now.__module__,
    } == {"web.mcp.tools.digest"}
    assert router.get_proactive_state.__module__ == "web.mcp.tools.proactive"


def test_mcp_router_reexports_registered_hrt_callables(monkeypatch) -> None:
    from web.mcp import conflicts
    from web.routers import mcp as router

    dependencies = router._hrt_tool_dependencies
    assert dependencies.conflict_scope is conflicts.conflict_scope
    assert dependencies.conflict_write_context is conflicts.conflict_write_context
    assert dependencies.parse_date is router._parse_date
    assert dependencies.conflict_payload is router._conflict_payload
    assert dependencies.serialize_row is router.serialize_row
    assert dependencies.serialize_written is router.serialize_written
    assert dependencies.gated is router.gated
    sentinel_factory = object()
    monkeypatch.setattr(router, "get_session_factory", lambda: sentinel_factory)
    assert dependencies.get_session_factory() is sentinel_factory
    assert router.get_hrt_logs is router._hrt_tools.get_hrt_logs
    assert router.log_hrt_dose is router._hrt_tools.log_hrt_dose
    assert router.add_hrt_cycle is router._hrt_tools.add_hrt_cycle
    assert router.add_hrt_cycle_item is router._hrt_tools.add_hrt_cycle_item
    assert router.update_hrt_dose is router._hrt_tools.update_hrt_dose
    assert router.log_hrt_side_effect is router._hrt_tools.log_hrt_side_effect
    assert router.close_hrt_cycle is router._hrt_tools.close_hrt_cycle
    assert router.get_hrt_cycles is router._hrt_tools.get_hrt_cycles
    assert {
        router.get_hrt_logs.__module__,
        router.log_hrt_dose.__module__,
        router.add_hrt_cycle.__module__,
        router.add_hrt_cycle_item.__module__,
        router.update_hrt_dose.__module__,
        router.log_hrt_side_effect.__module__,
        router.close_hrt_cycle.__module__,
        router.get_hrt_cycles.__module__,
    } == {"web.mcp.tools.hrt"}


def test_mcp_router_reexports_registered_labs_callables(monkeypatch) -> None:
    from web.mcp import conflicts
    from web.routers import mcp as router

    dependencies = router._labs_tool_dependencies
    assert dependencies.conflict_scope is conflicts.conflict_scope
    assert dependencies.conflict_write_context is conflicts.conflict_write_context
    assert dependencies.parse_date is router._parse_date
    assert dependencies.conflict_payload is router._conflict_payload
    assert dependencies.serialize_row is router.serialize_row
    assert dependencies.serialize_written is router.serialize_written
    sentinel_factory = object()
    monkeypatch.setattr(router, "get_session_factory", lambda: sentinel_factory)
    assert dependencies.get_session_factory() is sentinel_factory
    assert router.get_lab_results is router._labs_tools.get_lab_results
    assert router.log_lab_result is router._labs_tools.log_lab_result
    assert router.update_lab_result is router._labs_tools.update_lab_result
    assert router.log_lab_results is router._labs_tools.log_lab_results
    assert {
        router.get_lab_results.__module__,
        router.log_lab_result.__module__,
        router.update_lab_result.__module__,
        router.log_lab_results.__module__,
    } == {"web.mcp.tools.labs"}


def test_mcp_router_reexports_the_extracted_common_helpers() -> None:
    from web.mcp import (
        access,
        arguments,
        conflicts,
        errors,
        identity,
        ownership,
        serialization,
        server,
    )
    from web.routers import mcp as router

    assert router.TOOL_ACCESS is access.TOOL_ACCESS
    assert router.RESOURCE_ACCESS is access.RESOURCE_ACCESS
    assert router.PROMPT_ACCESS is access.PROMPT_ACCESS
    assert router.McpArgumentError is arguments.McpArgumentError
    assert router.McpActorUnresolved is errors.McpActorUnresolved
    assert router._MCP_ACTOR is identity.MCP_ACTOR
    assert router.ANONYMOUS_TOKEN == identity.ANONYMOUS_TOKEN
    assert router._current_actor is identity.current_actor
    assert router._current_grant_binding is identity.current_grant_binding
    assert router._mcp_actor_username is ownership.actor_username
    assert router._mcp_v1_legacy_owner is ownership.legacy_owner
    assert router._mcp_v1_legacy_alert_owner is ownership.legacy_alert_owner
    assert router._mcp_v1_conflict_scope is conflicts.conflict_scope
    assert router._mcp_v1_composition_scope is conflicts.composition_scope
    assert router._mcp_v1_conflict_write_context is conflicts.conflict_write_context
    assert router._mcp_v1_weight_write is conflicts.weight_write
    assert router._mcp_v1_aux_weight_write is conflicts.auxiliary_weight_write
    assert issubclass(router._ConnectorTokenVerifier, server.ConnectorTokenVerifier)
    assert router.VitalsMCPServer is server.VitalsMCPServer
    assert router.MCP_SERVER_VERSION == server.MCP_SERVER_VERSION
    assert router.TOOL_MODULES is server.TOOL_MODULES
    assert router._described_for_a_model is server.described_for_a_model
    assert router._parse_date is arguments._parse_date
    assert router._parse_time is arguments._parse_time
    assert router._ROW_NOISE is serialization._ROW_NOISE
    assert router._conflict_payload is serialization._conflict_payload
    assert router.serialize_row is serialization.serialize_row
    assert router.serialize_written is serialization.serialize_written


def test_mcp_private_output_policy_matches_portability_contract() -> None:
    from vitals.services.portability.v1_contract import (
        GENERIC_OUTPUT_SUPPRESSED_COLUMNS,
    )
    from web.mcp.serialization import _GENERIC_OUTPUT_SUPPRESSED_COLUMNS

    assert _GENERIC_OUTPUT_SUPPRESSED_COLUMNS == GENERIC_OUTPUT_SUPPRESSED_COLUMNS
