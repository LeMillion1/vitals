"""Weight MCP tool registration without a reverse dependency on the router."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from vitals.enums import Source
from vitals.services.conflicts.engine import ConflictBlocked
from vitals.services.weight import logs as weight_logs
from vitals.services.weight import measurements as weight_measurements
from vitals.services.weight import noise as weight_noise
from vitals.services.weight import writes as weight_writes
from vitals.utils.timeutils import today_local


@dataclass(frozen=True)
class WeightToolDependencies:
    """Router-owned identity, capability, and serialization seams."""

    get_session_factory: Callable[[], Any]
    parse_date: Callable[..., Any]
    conflict_scope: Callable[[Any], Awaitable[Any]]
    weight_write: Callable[..., Awaitable[Any]]
    auxiliary_weight_write: Callable[..., Awaitable[Any]]
    conflict_payload: Callable[[ConflictBlocked], dict]
    serialize_row: Callable[[Any], dict]
    serialize_written: Callable[[Any, Any], Awaitable[dict]]


@dataclass(frozen=True)
class RegisteredWeightReadTools:
    get_weight_logs: Callable[..., Awaitable[dict]]


@dataclass(frozen=True)
class RegisteredWeightWriteTools:
    log_weight: Callable[..., Awaitable[dict]]


@dataclass(frozen=True)
class RegisteredMeasurementTools:
    log_measurement: Callable[..., Awaitable[dict]]
    get_measurements: Callable[..., Awaitable[list[dict]]]


@dataclass(frozen=True)
class RegisteredMeasurementUpdateTools:
    update_measurement: Callable[..., Awaitable[dict]]


@dataclass(frozen=True)
class RegisteredNoiseTools:
    add_noise_marker: Callable[..., Awaitable[dict]]


def register_weight_read_tools(
    server: Any,
    deps: WeightToolDependencies,
) -> RegisteredWeightReadTools:
    """Register the combined Weight read tool at its frozen registry position."""

    @server.tool()
    async def get_weight_logs(
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: int = 100,
    ) -> dict:
        """Retrieves active weight logs, body measurements, and noise markers for a
        date range (YYYY-MM-DD). Weights/measurements default to the most recent 100."""
        session_factory = deps.get_session_factory()
        start = deps.parse_date(start_date, field="start_date")
        end = deps.parse_date(end_date, field="end_date")

        async with session_factory() as session:
            scope = await deps.conflict_scope(session)
            weights = await weight_logs.list_active_weights(
                session,
                start=start,
                end=end,
                subject_id=scope.subject_id,
            )
            weights = sorted(weights, key=lambda row: row.date, reverse=True)[:limit]

            measurements = await weight_measurements.list_body_measurements(
                session,
                subject_id=scope.subject_id,
                start=start,
                end=end,
            )
            measurements = sorted(
                measurements,
                key=lambda row: row.date,
                reverse=True,
            )[:limit]

            noise = await weight_noise.list_noise_markers(
                session,
                subject_id=scope.subject_id,
                start=start,
                end=end,
            )
            noise = sorted(noise, key=lambda row: row.start_date, reverse=True)

            return {
                "weights": [deps.serialize_row(row) for row in weights],
                "measurements": [deps.serialize_row(row) for row in measurements],
                "noise_markers": [deps.serialize_row(row) for row in noise],
            }

    return RegisteredWeightReadTools(get_weight_logs=get_weight_logs)


def register_weight_write_tools(
    server: Any,
    deps: WeightToolDependencies,
) -> RegisteredWeightWriteTools:
    """Register the Weight write tool at its frozen registry position."""

    @server.tool()
    async def log_weight(
        weight_kg: float,
        on_date: Optional[str] = None,
        note: Optional[str] = None,
        override: bool = False,
    ) -> dict:
        """Records a manual weight entry (kg). One active weight per date — manual
        entries override Garmin imports. WRITE tool — saved immediately. If a hard
        conflict rule blocks the save, returns ``{"blocked": true, ...}``; call again
        with ``override=True`` to save anyway."""
        session_factory = deps.get_session_factory()
        parsed_date = deps.parse_date(on_date, today_local(), field="on_date")

        async with session_factory() as session:
            try:
                conflict_context, prepared = await deps.weight_write(
                    session,
                    evaluation_date=parsed_date,
                )
                row = await weight_writes.log_weight(
                    session,
                    on_date=parsed_date,
                    weight_kg=weight_kg,
                    note=note,
                    source=Source.MCP.value,
                    override=override,
                    identity=conflict_context.identity,
                    prepared_weight_write=prepared,
                )
            except ConflictBlocked as exc:
                await session.rollback()
                return deps.conflict_payload(exc)
            except ValueError as exc:
                await session.rollback()
                return {"error": str(exc)}
            await session.commit()
            return await deps.serialize_written(session, row)

    return RegisteredWeightWriteTools(log_weight=log_weight)


def register_measurement_tools(
    server: Any,
    deps: WeightToolDependencies,
) -> RegisteredMeasurementTools:
    """Register Weight measurement tools at their frozen registry position."""

    @server.tool()
    async def log_measurement(
        on_date: Optional[str] = None,
        neck_cm: Optional[float] = None,
        waist_cm: Optional[float] = None,
        hips_cm: Optional[float] = None,
        note: Optional[str] = None,
        override: bool = False,
    ) -> dict:
        """Records body circumference measurements (neck, waist, hips in cm). Upserts
        per date. Auto-computes Navy body-fat % and LBM if weight exists for the date.
        WRITE tool — saved immediately. If a hard conflict rule blocks the save,
        returns ``{"blocked": true, ...}``; call again with ``override=True``."""
        session_factory = deps.get_session_factory()
        parsed_date = deps.parse_date(on_date, today_local(), field="on_date")

        async with session_factory() as session:
            conflict_context, prepared = await deps.auxiliary_weight_write(
                session,
                evaluation_date=parsed_date,
            )
            try:
                row = await weight_measurements.upsert_body_measurement(
                    session,
                    on_date=parsed_date,
                    neck_cm=neck_cm,
                    waist_cm=waist_cm,
                    hips_cm=hips_cm,
                    note=note,
                    source=Source.MCP.value,
                    override=override,
                    identity=conflict_context.identity,
                    prepared_conflict_write=prepared,
                )
            except ConflictBlocked as exc:
                await session.rollback()
                return deps.conflict_payload(exc)
            except ValueError as exc:
                await session.rollback()
                return {"error": str(exc)}
            await session.commit()
            return await deps.serialize_written(session, row)

    @server.tool()
    async def get_measurements(
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict]:
        """Retrieves body measurements (neck, waist, hips, body-fat %, LBM) for a date
        range. Defaults to the most recent 100 rows."""
        session_factory = deps.get_session_factory()
        start = deps.parse_date(start_date, field="start_date")
        end = deps.parse_date(end_date, field="end_date")

        async with session_factory() as session:
            scope = await deps.conflict_scope(session)
            rows = await weight_measurements.list_body_measurements(
                session,
                subject_id=scope.subject_id,
                start=start,
                end=end,
            )
            rows = sorted(rows, key=lambda row: row.date, reverse=True)[:limit]
            return [deps.serialize_row(row) for row in rows]

    return RegisteredMeasurementTools(
        log_measurement=log_measurement,
        get_measurements=get_measurements,
    )


def register_noise_tools(
    server: Any,
    deps: WeightToolDependencies,
) -> RegisteredNoiseTools:
    """Register the Weight noise write tool at its frozen registry position."""

    @server.tool()
    async def add_noise_marker(
        start_date: str,
        reason: str,
        end_date: Optional[str] = None,
        direction: Optional[str] = None,
    ) -> dict:
        """Marks a date range as noisy so it's excluded from the weight moving average
        and trend (e.g. "sick week", "creatine loading"). ``direction`` is up (scale
        inflated), down (scale deflated), or neutral. Omit ``end_date`` for an open
        period. WRITE tool — the weight trend recomputes without this range."""
        session_factory = deps.get_session_factory()
        parsed_start = deps.parse_date(start_date, field="start_date")
        parsed_end = deps.parse_date(end_date, field="end_date")
        async with session_factory() as session:
            conflict_context, prepared = await deps.auxiliary_weight_write(
                session,
                evaluation_date=today_local(),
            )
            try:
                row = await weight_noise.add_noise_marker(
                    session,
                    start_date=parsed_start,
                    end_date=parsed_end,
                    reason=reason,
                    direction=direction,
                    source=Source.MCP.value,
                    identity=conflict_context.identity,
                    prepared_conflict_write=prepared,
                )
            except ValueError as exc:
                await session.rollback()
                return {"error": str(exc)}
            await session.commit()
            return await deps.serialize_written(session, row)

    return RegisteredNoiseTools(add_noise_marker=add_noise_marker)


def register_measurement_update_tools(
    server: Any,
    deps: WeightToolDependencies,
) -> RegisteredMeasurementUpdateTools:
    """Register the measurement edit tool at its frozen registry position."""

    @server.tool()
    async def update_measurement(
        measurement_id: int,
        on_date: str,
        neck_cm: Optional[float] = None,
        waist_cm: Optional[float] = None,
        hips_cm: Optional[float] = None,
        note: Optional[str] = None,
        override: bool = False,
    ) -> dict:
        """Edits a body-measurement row by ID (recomputes Navy body-fat % / LBM). On a
        hard block returns ``{"blocked": true, ...}``; retry with ``override=True``.
        WRITE tool."""
        session_factory = deps.get_session_factory()
        parsed_date = deps.parse_date(on_date, field="on_date")
        async with session_factory() as session:
            conflict_context, prepared = await deps.auxiliary_weight_write(
                session,
                evaluation_date=parsed_date,
            )
            try:
                row = await weight_measurements.update_body_measurement(
                    session,
                    measurement_id,
                    on_date=parsed_date,
                    neck_cm=neck_cm,
                    waist_cm=waist_cm,
                    hips_cm=hips_cm,
                    note=note,
                    override=override,
                    identity=conflict_context.identity,
                    prepared_conflict_write=prepared,
                )
            except ConflictBlocked as exc:
                await session.rollback()
                return deps.conflict_payload(exc)
            except ValueError as exc:
                await session.rollback()
                return {"error": str(exc)}
            if row is None:
                return {"error": f"Measurement {measurement_id} not found"}
            await session.commit()
            return await deps.serialize_written(session, row)

    return RegisteredMeasurementUpdateTools(update_measurement=update_measurement)


__all__ = [
    "RegisteredMeasurementTools",
    "RegisteredMeasurementUpdateTools",
    "RegisteredNoiseTools",
    "RegisteredWeightReadTools",
    "RegisteredWeightWriteTools",
    "WeightToolDependencies",
    "register_measurement_tools",
    "register_measurement_update_tools",
    "register_noise_tools",
    "register_weight_read_tools",
    "register_weight_write_tools",
]
