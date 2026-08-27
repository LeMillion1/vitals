"""Aggregate the settings delivery routers and stable import seams.

Business and persistence workflows live in :mod:`web.settings.routes`; this
module keeps the historic router import and the small set of handler/helper
names used by tests and extensions.
"""

from fastapi import APIRouter

from web.settings import platform as platform_settings
from web.settings.routes import (
    common,
    portability,
    preferences,
    profile,
    providers,
    security,
)

router = APIRouter(tags=["settings"])
router.include_router(platform_settings.router, prefix="/settings")
router.include_router(profile.router, prefix="/settings")
router.include_router(providers.router, prefix="/settings")
router.include_router(security.router, prefix="/settings")
router.include_router(preferences.router, prefix="/settings")
router.include_router(portability.router, prefix="/settings")

_blank_if_none = common.blank_if_none
_is_known_timezone = common.is_known_timezone
_number = common.number
_redirect = common.redirect

# Platform-control compatibility exports.
configure_platform_ai_quota = platform_settings.configure_platform_ai_quota
disable_platform_ai = platform_settings.disable_platform_ai
enable_platform_ai = platform_settings.enable_platform_ai
platform_ai_page = platform_settings.platform_ai_page
platform_settings_page = platform_settings.platform_settings_page
save_ai = platform_settings.save_ai
save_mcp = platform_settings.save_mcp

# Page/profile compatibility exports.
login_breaker_state = profile.login_breaker_state
_connector_rows = profile._connector_rows
_external_token_rows = profile._external_token_rows
_page = profile._page
save_profile = profile.save_profile
settings_page = profile.settings_page

# Provider compatibility exports.
_garmin_weight_control = providers._garmin_weight_control
_subject_garmin_account = providers._subject_garmin_account
revoke_connector = providers.revoke_connector
save_garmin = providers.save_garmin
save_hevy = providers.save_hevy
send_garmin_weight_now = providers.send_garmin_weight_now
toggle_garmin_weight_export = providers.toggle_garmin_weight_export

# Security compatibility exports.
change_password = security.change_password
confirm_twofa = security.confirm_twofa
disable_twofa = security.disable_twofa
issue_external_api_token = security.issue_external_api_token
revoke_external_api_token = security.revoke_external_api_token
start_twofa = security.start_twofa

# Preferences/scheduler compatibility exports.
apply_schedule = preferences.apply_schedule
load_process_mode = preferences.load_process_mode
save_language = preferences.save_language
save_proactive = preferences.save_proactive
signal_schedule_reload = preferences.signal_schedule_reload
toggle_module = preferences.toggle_module

# Portability/operation compatibility exports.
require_installation_operator_user = portability.require_installation_operator_user
_authorize_export = portability._authorize_export
_authorize_installation_operation = portability._authorize_installation_operation
export_backup = portability.export_backup
export_llm = portability.export_llm
export_subject_backup = portability.export_subject_backup
import_backup = portability.import_backup
import_subject_record = portability.import_subject_record
restart_container = portability.restart_container
