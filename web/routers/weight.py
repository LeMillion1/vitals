"""Composition root for Weight HTTP delivery.

Route implementations live in ``weight_routes`` and share explicit delivery
helpers from ``weight_routes.common``. This module preserves the established
handler imports without making either child router depend back on its parent.
"""

from fastapi import APIRouter

from web.routers.weight_routes import common
from web.routers.weight_routes.body_composition import (
    BodyScanConfirm,
    BodyScanMetricIn,
    body_scan_confirm,
    body_scan_upload,
    delete_body_scan_entry,
    router as body_composition_router,
)
from web.routers.weight_routes.records import (
    add_noise_entry,
    add_photo_entry,
    delete_measurement_entry,
    delete_noise_marker_entry,
    delete_photo_entry,
    delete_weight_entry,
    log_measurement_entry,
    log_weight_entry,
    router as weight_records_router,
    weight_dashboard,
    weight_measures,
)

router = APIRouter(tags=["weight"])

# Compatibility exports for existing callers. Child routers depend directly
# on ``common``; these aliases are not part of their dependency graph.
STATIC_DIR = common.STATIC_DIR
_back = common._back
_prepare_aux_write = common._prepare_aux_write
_prepare_weight_write = common._prepare_weight_write
_section_context = common._section_context

__all__ = [
    "BodyScanConfirm",
    "BodyScanMetricIn",
    "STATIC_DIR",
    "_back",
    "_prepare_aux_write",
    "_prepare_weight_write",
    "_section_context",
    "add_noise_entry",
    "add_photo_entry",
    "body_scan_confirm",
    "body_scan_upload",
    "delete_body_scan_entry",
    "delete_measurement_entry",
    "delete_noise_marker_entry",
    "delete_photo_entry",
    "delete_weight_entry",
    "log_measurement_entry",
    "log_weight_entry",
    "router",
    "weight_dashboard",
    "weight_measures",
]

# Preserve the historical route order. FastAPI resolves routes in order.
router.routes.extend(weight_records_router.routes[:6])
router.routes.extend(body_composition_router.routes)
router.routes.extend(weight_records_router.routes[6:])
