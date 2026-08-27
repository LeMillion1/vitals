"""FastAPI composition root for the Vitals panel."""

from __future__ import annotations

import logging

from fastapi import Depends, FastAPI

from web.app_lifecycle import (
    _LEGACY_BOOTSTRAP_CLOSED,
    _bootstrap_legacy_identity,
    _load_oidc_identity_state,
    lifespan,
)
from web.authentication.routes import router as auth_router
from web.csrf import add_csrf_origin_check, add_security_headers
from web.deps import (
    load_care_consent_task,
    load_care_unread_count,
    load_enabled_modules,
    load_language,
    load_nav_status,
    load_subject_timezone,
    load_support_banner,
    require_module,
)
from web.error_handlers import (
    access_denied_handler,
    auth_exception_handler,
    http_exception_handler,
    legacy_ownership_handler,
    module_disabled_handler,
    no_personal_record_handler,
    recent_authentication_handler,
    register_error_handlers,
)
from web.system_routes import (
    UPLOADS_DIR,
    health,
    register_system_routes,
    root,
    serve_upload,
    service_worker,
)

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Vitals Health OS",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    # No anonymous schema: it would enumerate every installed health module.
    openapi_url=None,
    dependencies=[
        Depends(load_language),
        Depends(load_subject_timezone),
        Depends(load_enabled_modules),
        Depends(load_nav_status),
        Depends(load_support_banner),
        Depends(load_care_consent_task),
        Depends(load_care_unread_count),
    ],
)

add_csrf_origin_check(app)
add_security_headers(app)

# Registration order is security-sensitive: the private upload blocker is
# installed before the public static mount inside register_system_routes.
register_system_routes(app)
register_error_handlers(app)

app.include_router(auth_router)

# Import route owners only at the composition boundary. The explicit order is
# part of the public delivery contract and avoids ambiguous path matches.
from web.routers.alerts import router as alerts_router  # noqa: E402
from web.routers.break_glass import (  # noqa: E402
    admin_router as break_glass_admin_router,
    patient_router as break_glass_patient_router,
)
from web.routers.care import router as care_router  # noqa: E402
from web.routers.charts import router as charts_router  # noqa: E402
from web.routers.consents import router as consents_router  # noqa: E402
from web.routers.external_api import router as external_api_router  # noqa: E402
from web.routers.files import router as files_router  # noqa: E402
from web.routers.garmin import router as garmin_router  # noqa: E402
from web.routers.genetics import router as genetics_router  # noqa: E402
from web.routers.glp1 import router as glp1_router  # noqa: E402
from web.routers.hevy import router as hevy_router  # noqa: E402
from web.routers.hrt import router as hrt_router  # noqa: E402
from web.routers.interactions import router as interactions_router  # noqa: E402
from web.routers.labs import router as labs_router  # noqa: E402
from web.routers.messages import router as messages_router  # noqa: E402
from web.routers.more import router as more_router  # noqa: E402
from web.routers.nutrition import router as nutrition_router  # noqa: E402
from web.routers.portability_v2 import router as portability_v2_router  # noqa: E402
from web.routers.professional_reviews import (  # noqa: E402
    router as professional_reviews_router,
)
from web.routers.public_report import router as public_report_router  # noqa: E402
from web.routers.registration import router as registration_router  # noqa: E402
from web.routers.registration_operator import (  # noqa: E402
    router as registration_operator_router,
)
from web.routers.reports import router as reports_router  # noqa: E402
from web.routers.settings import router as settings_router  # noqa: E402
from web.routers.share import router as share_router  # noqa: E402
from web.routers.skincare import router as skincare_router  # noqa: E402
from web.routers.supplements import router as supplements_router  # noqa: E402
from web.routers.support_access import (  # noqa: E402
    admin_router as support_admin_router,
    patient_router as support_patient_router,
)
from web.routers.timeline import router as timeline_router  # noqa: E402
from web.routers.today import router as today_router  # noqa: E402
from web.routers.web_push import router as web_push_router  # noqa: E402
from web.routers.weight import router as weight_router  # noqa: E402

# Core account and record surfaces.
app.include_router(today_router)
app.include_router(more_router)
app.include_router(alerts_router)
app.include_router(weight_router)
app.include_router(files_router)
app.include_router(care_router)
app.include_router(consents_router)
app.include_router(messages_router)
app.include_router(web_push_router)
app.include_router(support_admin_router)
app.include_router(break_glass_admin_router)
app.include_router(professional_reviews_router)
app.include_router(registration_router)
app.include_router(registration_operator_router)
app.include_router(support_patient_router)
app.include_router(break_glass_patient_router)
app.include_router(garmin_router)
app.include_router(labs_router)
app.include_router(reports_router)
app.include_router(share_router)
app.include_router(portability_v2_router)
app.include_router(settings_router)
app.include_router(charts_router)
app.include_router(external_api_router)
app.include_router(public_report_router)

# Optional domains remain consistently gated at the delivery boundary.
app.include_router(glp1_router, dependencies=[Depends(require_module("glp1"))])
app.include_router(hevy_router, dependencies=[Depends(require_module("hevy"))])
app.include_router(
    supplements_router,
    dependencies=[Depends(require_module("supplements"))],
)
app.include_router(hrt_router, dependencies=[Depends(require_module("hrt"))])
app.include_router(
    genetics_router,
    dependencies=[Depends(require_module("genetics"))],
)
app.include_router(
    skincare_router,
    dependencies=[Depends(require_module("skincare"))],
)
app.include_router(
    nutrition_router,
    dependencies=[Depends(require_module("nutrition"))],
)
app.include_router(
    interactions_router,
    dependencies=[Depends(require_module("interactions"))],
)
app.include_router(
    timeline_router,
    dependencies=[Depends(require_module("timeline"))],
)

try:
    from web.routers.mcp import get_mcp_app  # noqa: E402
    from web.routers.oauth import router as oauth_router  # noqa: E402

    app.include_router(oauth_router)
    mcp_app, mcp_lifespan = get_mcp_app()
    app.mount("/mcp", mcp_app)
    app.state.mcp_lifespan = mcp_lifespan
except ImportError:
    logger.warning("MCP/OAuth disabled (fastmcp not available)")


__all__ = [
    "UPLOADS_DIR",
    "_LEGACY_BOOTSTRAP_CLOSED",
    "_bootstrap_legacy_identity",
    "_load_oidc_identity_state",
    "access_denied_handler",
    "app",
    "auth_exception_handler",
    "health",
    "http_exception_handler",
    "legacy_ownership_handler",
    "lifespan",
    "module_disabled_handler",
    "no_personal_record_handler",
    "recent_authentication_handler",
    "root",
    "serve_upload",
    "service_worker",
]
