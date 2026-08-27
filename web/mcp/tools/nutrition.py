"""Nutrition MCP tool registration without a router dependency."""

from __future__ import annotations

from vitals.services.nutrition import analytics as nutrition_analytics
from vitals.services.nutrition import queries as nutrition_queries
from vitals.services.nutrition import writes as nutrition_writes

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from vitals.enums import Source

from vitals.services.conflicts import engine
from vitals.services.conflicts.engine import ConflictBlocked
from vitals.utils.timeutils import today_local


@dataclass(frozen=True)
class NutritionToolDependencies:
    get_session_factory: Callable[[], Any]
    parse_date: Callable[..., Any]
    parse_time: Callable[..., Any]
    conflict_scope: Callable[[Any], Awaitable[Any]]
    conflict_write_context: Callable[..., Awaitable[Any]]
    conflict_payload: Callable[[ConflictBlocked], dict]
    serialize_row: Callable[[Any], dict]
    serialize_written: Callable[[Any, Any], Awaitable[dict]]
    gated: Callable[[str], Callable[[Callable[..., Any]], Callable[..., Any]]]


@dataclass(frozen=True)
class RegisteredNutritionTools:
    log_meal: Callable[..., Awaitable[dict]]
    get_nutrition_summary: Callable[..., Awaitable[dict]]
    update_meal: Callable[..., Awaitable[dict]]
    search_meals: Callable[..., Awaitable[list[dict]]]


def register_nutrition_tools(
    server: Any,
    deps: NutritionToolDependencies,
) -> RegisteredNutritionTools:
    """Register the four frozen Nutrition tools in their existing order."""

    @server.tool()
    @deps.gated("nutrition")
    async def log_meal(
        name: str,
        calories: Optional[float] = None,
        protein_g: Optional[float] = None,
        fat_g: Optional[float] = None,
        carbs_g: Optional[float] = None,
        eaten_at: Optional[str] = None,
        note: Optional[str] = None,
        on_date: Optional[str] = None,
        override: bool = False,
    ) -> dict:
        """Records a meal or snack with optional macros (KCAL, protein, fat, carbs).

        This is a WRITE tool — the meal is saved to the database immediately.
        Defaults: on_date = today, eaten_at = current time. If a hard conflict rule
        blocks the save, returns ``{"blocked": true, "violations": [...]}`` instead
        of saving; call again with ``override=True`` to save anyway.
        """
        session_factory = deps.get_session_factory()
        parsed_date = deps.parse_date(on_date, today_local(), field="on_date")
        parsed_time = deps.parse_time(eaten_at, field="eaten_at")

        async with session_factory() as session:
            context = await deps.conflict_write_context(
                session,
                evaluation_date=parsed_date,
            )
            prepared = await engine.prepare_scoped_write(session, context=context)
            try:
                row = await nutrition_writes.log_meal(
                    session,
                    on_date=parsed_date,
                    name=name,
                    eaten_at=parsed_time,
                    calories=calories,
                    protein_g=protein_g,
                    fat_g=fat_g,
                    carbs_g=carbs_g,
                    note=note,
                    source=Source.MCP.value,
                    override=override,
                    identity=context.identity,
                    prepared_conflict_write=prepared,
                )
            except ConflictBlocked as exc:
                await session.rollback()
                return deps.conflict_payload(exc)
            await session.commit()
            return await deps.serialize_written(session, row)

    @server.tool()
    @deps.gated("nutrition")
    async def get_nutrition_summary(
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> dict:
        """Returns a nutrition summary with total KCAL/protein/fat/carbs, meal counts,
        per-day breakdown, and goal tracking. Defaults to today if no dates given."""
        session_factory = deps.get_session_factory()
        today = today_local()
        start = deps.parse_date(start_date, today, field="start_date")
        end = deps.parse_date(end_date, today, field="end_date")

        async with session_factory() as session:
            scope = await deps.conflict_scope(session)
            if start == end:
                return await nutrition_analytics.daily_summary(
                    session,
                    start,
                    subject_id=scope.subject_id,
                )
            return await nutrition_analytics.nutrition_summary(
                session,
                start,
                end,
                subject_id=scope.subject_id,
            )

    @server.tool()
    @deps.gated("nutrition")
    async def update_meal(
        meal_id: int,
        name: Optional[str] = None,
        calories: Optional[float] = None,
        protein_g: Optional[float] = None,
        fat_g: Optional[float] = None,
        carbs_g: Optional[float] = None,
        eaten_at: Optional[str] = None,
        note: Optional[str] = None,
        on_date: Optional[str] = None,
        override: bool = False,
    ) -> dict:
        """Updates an existing meal by ID. Returns the updated meal or an error.

        Only the fields you pass are changed — anything left out keeps its stored
        value, including ``on_date``, which stays the meal's own date rather than
        moving the meal to today. WRITE tool — changes are saved immediately.
        """
        session_factory = deps.get_session_factory()
        parsed_date = deps.parse_date(on_date, field="on_date")
        parsed_time = deps.parse_time(eaten_at, field="eaten_at")

        async with session_factory() as session:
            context = await deps.conflict_write_context(
                session,
                evaluation_date=parsed_date or today_local(),
            )
            prepared = await engine.prepare_scoped_write(session, context=context)
            current = await nutrition_queries.get_meal_for_update(
                session,
                meal_id,
                identity=context.identity,
                prepared_conflict_write=prepared,
            )
            if current is None:
                return {"error": f"Meal {meal_id} not found"}
            final_date = current.date if parsed_date is None else parsed_date
            if context.evaluation_date != final_date:
                context = engine.ConflictWriteContext(
                    identity=context.identity,
                    evaluation_date=final_date,
                    legacy_bridge=context.legacy_bridge,
                )
                prepared = await engine.prepare_scoped_write(session, context=context)
            merged = {
                "name": current.name if name is None else name,
                "eaten_at": current.eaten_at if eaten_at is None else parsed_time,
                "calories": current.calories if calories is None else calories,
                "protein_g": current.protein_g if protein_g is None else protein_g,
                "fat_g": current.fat_g if fat_g is None else fat_g,
                "carbs_g": current.carbs_g if carbs_g is None else carbs_g,
                "note": current.note if note is None else note,
            }
            try:
                row = await nutrition_writes.update_meal(
                    session,
                    meal_id,
                    on_date=final_date,
                    override=override,
                    identity=context.identity,
                    prepared_conflict_write=prepared,
                    **merged,
                )
            except ConflictBlocked as exc:
                await session.rollback()
                return deps.conflict_payload(exc)
            await session.commit()
            return await deps.serialize_written(session, row)

    @server.tool()
    @deps.gated("nutrition")
    async def search_meals(
        query: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]:
        """Searches meals by name substring and/or date range. Returns matching meals
        ordered by date descending."""
        session_factory = deps.get_session_factory()
        start = deps.parse_date(start_date, field="start_date")
        end = deps.parse_date(end_date, field="end_date")

        async with session_factory() as session:
            scope = await deps.conflict_scope(session)
            rows = await nutrition_queries.list_meals(
                session,
                start=start,
                end=end,
                subject_id=scope.subject_id,
                name_query=query,
                limit=limit,
            )
            return [deps.serialize_row(row) for row in rows]

    return RegisteredNutritionTools(
        log_meal=log_meal,
        get_nutrition_summary=get_nutrition_summary,
        update_meal=update_meal,
        search_meals=search_meals,
    )


__all__ = [
    "NutritionToolDependencies",
    "RegisteredNutritionTools",
    "register_nutrition_tools",
]
