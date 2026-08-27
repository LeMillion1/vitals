"""Shared transaction-safe coordinator for paid medical-document extraction."""

from __future__ import annotations

from vitals.services.ai_gateway import contracts as ai_gateway_service_contracts

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import AIInvocationStatus, FileStorageBackend


class MedicalAIUploadReason(StrEnum):
    SUCCEEDED = "succeeded"
    PENDING = "pending"
    QUOTA = "quota"
    NOT_CONFIGURED = "not_configured"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class MedicalAIUploadOutcome:
    reason: MedicalAIUploadReason
    extracted: dict[str, Any] | None = None
    raw_payload_id: int | None = None


async def run_medical_ai_upload(
    session: AsyncSession,
    *,
    label: str,
    logger: logging.Logger,
    file_bytes: bytes,
    storage_ref: str,
    private_root: str,
    static_dir: str,
    write_file: Callable[[str, str, bytes], Any],
    remove_file: Callable[..., Any],
    run_in_threadpool: Callable[..., Awaitable[Any]],
    prepare: Callable[[], Awaitable[Any]],
    prepare_content: Callable[[Any, bytes], Any],
    validation_error: type[Exception],
    cancel: Callable[[Any], Awaitable[Any]],
    start: Callable[[Any, Any], Awaitable[Any]],
    render: Callable[[Any, Any, bytes, Any], Awaitable[Any]],
    persist: Callable[[Any, Any], Awaitable[Any]],
) -> MedicalAIUploadOutcome:
    """Persist bytes, reserve/dispatch AI, and durably finalize one extraction.

    The caller supplies domain operations and retains HTTP response mapping. This
    coordinator owns the delivery transaction boundary and preserves uploaded
    bytes whenever a commit acknowledgement is ambiguous.
    """

    file_written = False
    prepared = None
    try:
        await run_in_threadpool(write_file, private_root, storage_ref, file_bytes)
        file_written = True
        prepared = await prepare()
    except (
        ai_gateway_service_contracts.AIGatewayConfigurationError,
        ai_gateway_service_contracts.AIQuotaExceededError,
    ) as exc:
        await _rollback_and_remove(
            session,
            label=label,
            logger=logger,
            storage_ref=storage_ref,
            private_root=private_root,
            static_dir=static_dir,
            file_written=file_written,
            remove_file=remove_file,
            run_in_threadpool=run_in_threadpool,
        )
        reason = (
            MedicalAIUploadReason.QUOTA
            if isinstance(exc, ai_gateway_service_contracts.AIQuotaExceededError)
            else MedicalAIUploadReason.NOT_CONFIGURED
        )
        return MedicalAIUploadOutcome(reason=reason)
    except BaseException:
        await _rollback_and_remove(
            session,
            label=label,
            logger=logger,
            storage_ref=storage_ref,
            private_root=private_root,
            static_dir=static_dir,
            file_written=file_written,
            remove_file=remove_file,
            run_in_threadpool=run_in_threadpool,
        )
        raise

    try:
        await session.commit()
    except BaseException:
        await _reset_after_ambiguous_commit(session, label=label, logger=logger)
        raise

    assert prepared is not None
    raw_payload_id = prepared.raw_payload_id
    if prepared.reservation_status is AIInvocationStatus.SUCCEEDED:
        extracted = prepared.existing_extracted
    elif not prepared.dispatchable:
        return MedicalAIUploadOutcome(
            reason=MedicalAIUploadReason.PENDING,
            raw_payload_id=raw_payload_id,
        )
    else:
        try:
            content = prepare_content(prepared, file_bytes)
        except validation_error:
            await _cancel_without_network(
                session,
                prepared=prepared,
                cancel=cancel,
                label=label,
                logger=logger,
                phase="locally invalid",
            )
            return MedicalAIUploadOutcome(reason=MedicalAIUploadReason.ERROR)

        try:
            lease = await start(prepared, content)
            await session.commit()
        except (
            ai_gateway_service_contracts.AIGatewayConfigurationError,
            ai_gateway_service_contracts.AIQuotaExceededError,
        ) as exc:
            await session.rollback()
            await _cancel_without_network(
                session,
                prepared=prepared,
                cancel=cancel,
                label=label,
                logger=logger,
                phase="zero-network",
            )
            reason = (
                MedicalAIUploadReason.QUOTA
                if isinstance(exc, ai_gateway_service_contracts.AIQuotaExceededError)
                else MedicalAIUploadReason.NOT_CONFIGURED
            )
            return MedicalAIUploadOutcome(reason=reason)

        completion = await render(prepared, lease, file_bytes, content)
        result = None
        for attempt in range(2):
            try:
                result = await persist(prepared, completion)
                break
            except Exception:
                await session.rollback()
                if attempt == 0:
                    logger.warning(
                        "Retrying transient %s AI finalization with the same paid completion",
                        label,
                    )
                    continue
                logger.exception("%s AI finalization failed after internal retry", label)
                raise
        assert result is not None
        try:
            await session.commit()
        except BaseException:
            await _reset_after_ambiguous_finalization(
                session,
                label=label,
                logger=logger,
            )
            raise
        if result.status is not AIInvocationStatus.SUCCEEDED:
            logger.warning(
                "%s AI extraction ended with status %s",
                label,
                result.status.value,
            )
            return MedicalAIUploadOutcome(reason=MedicalAIUploadReason.ERROR)
        extracted = result.extracted

    if not isinstance(extracted, dict):
        return MedicalAIUploadOutcome(reason=MedicalAIUploadReason.ERROR)
    return MedicalAIUploadOutcome(
        reason=MedicalAIUploadReason.SUCCEEDED,
        extracted=extracted,
        raw_payload_id=raw_payload_id,
    )


async def _rollback_and_remove(
    session: AsyncSession,
    *,
    label: str,
    logger: logging.Logger,
    storage_ref: str,
    private_root: str,
    static_dir: str,
    file_written: bool,
    remove_file: Callable[..., Any],
    run_in_threadpool: Callable[..., Awaitable[Any]],
) -> None:
    try:
        await session.rollback()
    except BaseException:
        logger.exception("Could not roll back failed %s transaction", label)
    if not file_written:
        return
    try:
        await run_in_threadpool(
            remove_file,
            storage_backend=FileStorageBackend.PRIVATE_LOCAL.value,
            storage_ref=storage_ref,
            static_dir=static_dir,
            private_root=private_root,
        )
    except OSError as exc:
        logger.warning("Could not clean up failed %s: %s", label, exc)


async def _cancel_without_network(
    session: AsyncSession,
    *,
    prepared: Any,
    cancel: Callable[[Any], Awaitable[Any]],
    label: str,
    logger: logging.Logger,
    phase: str,
) -> None:
    try:
        await cancel(prepared)
        await session.commit()
    except Exception:
        await session.rollback()
        logger.warning("Could not release a %s %s AI reservation", phase, label)


async def _reset_after_ambiguous_commit(
    session: AsyncSession,
    *,
    label: str,
    logger: logging.Logger,
) -> None:
    try:
        await session.rollback()
    except BaseException:
        logger.exception("Could not reset session after %s commit failure", label)
    logger.exception("%s preparation commit is ambiguous; preserved uploaded bytes", label)


async def _reset_after_ambiguous_finalization(
    session: AsyncSession,
    *,
    label: str,
    logger: logging.Logger,
) -> None:
    try:
        await session.rollback()
    except BaseException:
        logger.exception("Could not reset failed %s AI finalization", label)
    logger.exception("%s AI finalization commit outcome is ambiguous", label)


__all__ = [
    "MedicalAIUploadOutcome",
    "MedicalAIUploadReason",
    "run_medical_ai_upload",
]
