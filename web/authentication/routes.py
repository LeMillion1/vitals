"""Aggregate browser authentication routes for the application shell."""

from fastapi import APIRouter

from web.authentication.federated import router as federated_router
from web.authentication.legacy import router as legacy_router

router = APIRouter()
router.include_router(federated_router)
router.include_router(legacy_router)

__all__ = ["router"]
