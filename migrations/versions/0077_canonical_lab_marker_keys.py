"""Give lab markers a lossless, subject-scoped canonical identity.

Revision ID: 0077
Revises: 0076
Create Date: 2026-08-25

Display spelling was the lab natural key.  Unknown names only had their first
character capitalized, so TSH, Tsh and TSh split one analyte into independent
histories and alerts.  This revision adds a conservative normalized key while
retaining every colliding catalog row and every original result spelling.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from typing import Any, Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0077"
down_revision: Union[str, None] = "0076"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ALIASES = {
    "определение иммунореактивного инсулина": "Инсулин",
    "определение тиреотропина, тиротропина, тиреоидного гормона (ттг)": "ТТГ",
    "тиреотропный гормон (ттг)": "ТТГ",
    "определение свободного тироксина (т4)": "Т4 свободный",
    "исследование антител к тиреоглобулину (ат-тг)": "АТ-ТГ",
    "исследование антител к тиреоидной пероксидазе (ат-тпо)": "АТ-ТПО",
    "определение холестерина общего": "Холестерин общий",
    "холестерин": "Холестерин общий",
    "определение триглицеридов общих": "Триглицериды",
    "определение липопротеинов высокой плотности (лпвп-альфа)": "Холестерин-ЛПВП",
    "холестерин липопротеидов низкой плотности (лпнп, ldl)": "Холестерин-ЛПНП",
    "холестерин-лпнп": "Холестерин-ЛПНП",
    "определение липопротеинов низкой плотности (лпнп-бета)": "Холестерин-ЛПНП",
    "холестерин-лпонп": "Холестерин-ЛПОНП",
    "определение липопротеинов очень низкой плотности (лпонп), пребета-лп": "Холестерин-ЛПОНП",
    "определение аланинаминотрансферазы (алт)": "АЛТ",
    "аланинаминотрансфераза (алт)": "АЛТ",
    "определение аспартатаминотрансферазы (аст)": "АСТ",
    "аспартатаминотрансфераза (аст)": "АСТ",
    "определение глюкозы": "Глюкоза",
    "глюкоза плазмы": "Глюкоза",
    "глюкоза полуколичественно": "Глюкоза",
    "определение гемоглобина a1c (гликированный гемоглобин)": "Гликированный гемоглобин (HbA1c)",
    "hba1c (гликированный гемоглобин)": "Гликированный гемоглобин (HbA1c)",
    "гемоглобин общий": "Гемоглобин",
    "количество эритроцитов": "Эритроциты",
    "средний объем эритроцита": "Средний объем эритроцитов",
    "средний объем эритроцитов (mcv)": "Средний объем эритроцитов",
    "среднее содержание hb в эритроците": "Среднее содержание гемоглобина в эритроците",
    "среднее содержание гемоглобина в эритроците": "Среднее содержание гемоглобина в эритроците",
    "среднее содержание гемоглобина в эритроците (mch)": "Среднее содержание гемоглобина в эритроците",
    "средняя концентрация гемоглобина в эритроците": "Средняя концентрация гемоглобина в эритроците",
    "средняя концентрация hb в эритроците (mchc)": "Средняя концентрация гемоглобина в эритроците",
    "ширина распределения эритроцитов по объему": "Гетерогенность эритроцитов по объему",
    "гетерогенность эритроцитов по объёму": "Гетерогенность эритроцитов по объему",
    "количество тромбоцитов": "Тромбоциты",
    "средний объем тромбоцитов в крови": "Средний объем тромбоцитов",
    "средний объем тромбоцитов (mpv)": "Средний объем тромбоцитов",
    "ширина распределения тромбоцитов по объему": "Гетерогенность тромбоцитов по объему",
    "гетерогенность тромбоцитов по объёму": "Гетерогенность тромбоцитов по объему",
    "отн.ширина распред.тромбоцитов по объему (pdw)": "Гетерогенность тромбоцитов по объему",
    "общий объем тромбоцитов в крови (тромбокрит, pct)": "Тромбокрит",
    "тромбокрит (pct)": "Тромбокрит",
    "количество лейкоцитов": "Лейкоциты",
    "абсолютное количество нейтрофилов": "Нейтрофилы",
    "нейтрофилы сегментоядерные": "Нейтрофилы",
    "нейтрофилы (общее число), %": "Нейтрофилы %",
    "абсолютное количество эозинофилов": "Эозинофилы",
    "эозинофилы %": "Эозинофилы %",
    "абсолютное количество базофилов": "Базофилы",
    "базофилы %": "Базофилы %",
    "абсолютное количество моноцитов": "Моноциты",
    "моноциты %": "Моноциты %",
    "абсолютное количество лимфоцитов": "Лимфоциты",
    "лимфоциты (общее число), %": "Лимфоциты %",
    "лимфоциты %": "Лимфоциты %",
    "скорость оседания эритроцитов (по вестергрену)": "СОЭ",
    "определение кальция общего": "Кальций общий",
    "определение альбумина": "Альбумин",
    "определение кортизола": "Кортизол",
    "исследование пролактина (прл)": "Пролактин",
    "25-он витамин d, ихла, суммарный (кальциферол)": "25-ОН витамин D",
}
_WHITESPACE = re.compile(r"\s+")
_BATCH = 1_000


def _clean(value: str) -> str:
    return _WHITESPACE.sub(" ", unicodedata.normalize("NFKC", value).strip())


def _base_key(value: str) -> str:
    return _clean(value).casefold().replace("ё", "е")


def _key(value: str) -> str:
    cleaned = _clean(value)
    alias = _ALIASES.get(_base_key(cleaned), cleaned)
    return _base_key(alias)


def _winner(rows: list[dict[str, Any]]) -> dict[str, Any]:
    # A human-authored row beats an actorless seed.  Within that class the most
    # recently edited complete row reflects the latest personal catalog choice;
    # id makes an exact timestamp tie deterministic.
    return max(
        rows,
        key=lambda row: (
            row["actor_user_id"] is not None,
            row["updated_at"],
            -row["id"],
        ),
    )


def _rewrite_result_alert(
    bind: Any,
    *,
    subject_id: Any,
    result_id: int,
    old_name: str,
    canonical_name: str,
) -> None:
    if old_name == canonical_name:
        return
    for alert_key in ("labs.out_of_range", "labs.retest_due"):
        bind.execute(
            sa.text(
                "UPDATE system_alerts SET entity_ref = :new "
                "WHERE subject_id = :subject_id AND alert_key = :alert_key "
                "AND entity_ref = :old"
            ),
            {
                "new": f"{canonical_name}:{result_id}",
                "old": f"{old_name}:{result_id}",
                "subject_id": subject_id,
                "alert_key": alert_key,
            },
        )


def upgrade() -> None:
    with op.batch_alter_table("lab_markers") as batch:
        batch.add_column(sa.Column("normalized_name", sa.String(256), nullable=True))
        batch.add_column(
            sa.Column(
                "is_canonical",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("true"),
            )
        )
    with op.batch_alter_table("lab_results") as batch:
        batch.add_column(sa.Column("marker_key", sa.String(256), nullable=True))
        batch.add_column(sa.Column("marker_original", sa.String(128), nullable=True))

    bind = op.get_bind()
    groups: dict[tuple[Any, str], list[dict[str, Any]]] = defaultdict(list)
    marker_rows = bind.execute(
        sa.text(
            "SELECT id, subject_id, actor_user_id, name, updated_at "
            "FROM lab_markers ORDER BY id"
        )
    ).mappings()
    for row in marker_rows:
        key = _key(row["name"])
        if not key or len(key) > 256:
            raise RuntimeError(f"invalid normalized lab marker id={row['id']}")
        materialized = dict(row)
        materialized["normalized_name"] = key
        groups[(row["subject_id"], key)].append(materialized)

    canonical: dict[tuple[Any, str], str] = {}
    for group_key, rows in groups.items():
        winner = _winner(rows)
        canonical[group_key] = winner["name"]
        for row in rows:
            bind.execute(
                sa.text(
                    "UPDATE lab_markers SET normalized_name = :key, "
                    "is_canonical = :canonical WHERE id = :id"
                ),
                {
                    "key": group_key[1],
                    "canonical": row["id"] == winner["id"],
                    "id": row["id"],
                },
            )

    cursor = 0
    while True:
        rows = list(
            bind.execute(
                sa.text(
                    "SELECT id, subject_id, marker FROM lab_results "
                    "WHERE id > :cursor ORDER BY id LIMIT :limit"
                ),
                {"cursor": cursor, "limit": _BATCH},
            ).mappings()
        )
        if not rows:
            break
        for row in rows:
            key = _key(row["marker"])
            if not key or len(key) > 256:
                raise RuntimeError(f"invalid normalized lab result id={row['id']}")
            canonical_name = canonical.setdefault(
                (row["subject_id"], key), row["marker"]
            )
            bind.execute(
                sa.text(
                    "UPDATE lab_results SET marker_original = :original, "
                    "marker_key = :key, marker = :canonical WHERE id = :id"
                ),
                {
                    "original": row["marker"],
                    "key": key,
                    "canonical": canonical_name,
                    "id": row["id"],
                },
            )
            _rewrite_result_alert(
                bind,
                subject_id=row["subject_id"],
                result_id=row["id"],
                old_name=row["marker"],
                canonical_name=canonical_name,
            )
        cursor = rows[-1]["id"]

    with op.batch_alter_table("lab_markers") as batch:
        batch.alter_column("normalized_name", nullable=False)
    with op.batch_alter_table("lab_results") as batch:
        batch.alter_column("marker_key", nullable=False)
        batch.alter_column("marker_original", nullable=False)

    op.create_index(
        "ix_lab_markers_subject_normalized_name",
        "lab_markers",
        ["subject_id", "normalized_name"],
    )
    op.create_index(
        "uq_lab_markers_subject_normalized_canonical",
        "lab_markers",
        ["subject_id", "normalized_name"],
        unique=True,
        postgresql_where=sa.text("is_canonical = true"),
        sqlite_where=sa.text("is_canonical = 1"),
    )
    op.create_index(
        "ix_lab_results_subject_marker_key_date",
        "lab_results",
        ["subject_id", "marker_key", "date"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    cursor = 0
    while True:
        rows = list(
            bind.execute(
                sa.text(
                    "SELECT id, subject_id, marker, marker_original FROM lab_results "
                    "WHERE id > :cursor ORDER BY id LIMIT :limit"
                ),
                {"cursor": cursor, "limit": _BATCH},
            ).mappings()
        )
        if not rows:
            break
        for row in rows:
            _rewrite_result_alert(
                bind,
                subject_id=row["subject_id"],
                result_id=row["id"],
                old_name=row["marker"],
                canonical_name=row["marker_original"],
            )
            bind.execute(
                sa.text("UPDATE lab_results SET marker = :marker WHERE id = :id"),
                {"marker": row["marker_original"], "id": row["id"]},
            )
        cursor = rows[-1]["id"]

    op.drop_index(
        "ix_lab_results_subject_marker_key_date", table_name="lab_results"
    )
    op.drop_index(
        "uq_lab_markers_subject_normalized_canonical", table_name="lab_markers"
    )
    op.drop_index(
        "ix_lab_markers_subject_normalized_name", table_name="lab_markers"
    )
    with op.batch_alter_table("lab_results") as batch:
        batch.drop_column("marker_original")
        batch.drop_column("marker_key")
    with op.batch_alter_table("lab_markers") as batch:
        batch.drop_column("is_canonical")
        batch.drop_column("normalized_name")
