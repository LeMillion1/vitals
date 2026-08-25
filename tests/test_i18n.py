"""Tests for the i18n translation system, plurals, digest prompts, and localized routing."""
from __future__ import annotations

import pathlib
import re

import pytest
from vitals.i18n import t, plural, current_lang
from vitals.services.digest_service import build_prompt


def test_translation_basic():
    # Test translations in EN
    current_lang.set("en")
    assert t("nav.weight") == "Weight"
    assert t("settings.sex_male") == "Male"

    # Test translations in RU
    current_lang.set("ru")
    assert t("nav.weight") == "Вес"
    assert t("settings.sex_male") == "Мужской"


def test_translation_fallback():
    # If key doesn't exist in current language but exists in default (EN), it should fall back to EN
    current_lang.set("ru")
    # For testing fallback, let's look up a key that is defined in both, or test behavior when a key is absent
    assert t("non_existent_key_xyz") == "non_existent_key_xyz"


def test_support_console_explains_the_three_separate_approvals():
    from vitals.i18n import STRINGS

    assert STRINGS["en"]["support.console_description"] == (
        "Record reading, a one-time portability export, and the fixed bounded "
        "repair each require a separate patient approval. Broader repairs remain "
        "unavailable."
    )
    assert STRINGS["ru"]["support.console_description"] == (
        "Чтение записи, одноразовая выгрузка и фиксированное ограниченное "
        "исправление требуют отдельных одобрений пациента. Более широкие "
        "исправления недоступны."
    )


def test_plurals():
    # English plurals: 2 forms (1 vs other)
    current_lang.set("en")
    assert plural(1, "session", "sessions") == "session"
    assert plural(2, "session", "sessions") == "sessions"
    assert plural(0, "session", "sessions") == "sessions"
    assert plural(5, "session", "sessions") == "sessions"

    # Russian plurals: 3 forms (1 vs 2-4 vs many)
    current_lang.set("ru")
    assert plural(1, "сессия", "сессии", "сессий") == "сессия"
    assert plural(21, "сессия", "сессии", "сессий") == "сессия"
    assert plural(2, "сессия", "сессии", "сессий") == "сессии"
    assert plural(4, "сессия", "сессии", "сессий") == "сессии"
    assert plural(0, "сессия", "сессии", "сессий") == "сессий"
    assert plural(5, "сессия", "сессии", "сессий") == "сессий"
    assert plural(11, "сессия", "сессии", "сессий") == "сессий"


def test_body_metrics_count_plural():
    # body-composition scan chip: "{n} metric(s)" — RU needs the 3-form split
    current_lang.set("ru")
    words = (t("body.metric"), t("body.metrics_234"), t("body.metrics_many"))
    assert plural(1, *words) == "метрика"
    assert plural(2, *words) == "метрики"
    assert plural(5, *words) == "метрик"

    current_lang.set("en")
    words = (t("body.metric"), t("body.metrics_234"), t("body.metrics_many"))
    assert plural(1, *words) == "metric"
    assert plural(5, *words) == "metrics"


def test_days_word_agrees_with_the_count():
    # The doctor document counts days out loud ("Еда записана за 42 дня"), and
    # 42 takes a different form from 41 and from 45.
    from web.templating import days_word

    current_lang.set("ru")
    assert days_word(1) == "день"
    assert days_word(42) == "дня"
    assert days_word(5) == "дней"
    assert days_word(11) == "дней"

    current_lang.set("en")
    assert days_word(1) == "day"
    assert days_word(42) == "days"


def test_meal_word_agrees_with_the_count_and_the_language():
    """The nutrition page counts meals out loud, next to English headings.

    The filter hardcoded the Russian forms and the Russian rule, so an English
    installation rendered "Today's meals · 1 приём" — the page speaking two
    languages on one line. The catalogue carried both all along; only this
    filter never read them. Found by photographing the page, which is the only
    place a single wrong word next to a right one is obvious.
    """

    from web.templating import meal_word

    current_lang.set("ru")
    assert meal_word(1) == "приём"
    assert meal_word(2) == "приёма"
    assert meal_word(5) == "приёмов"
    assert meal_word(11) == "приёмов"

    current_lang.set("en")
    assert meal_word(1) == "meal"
    assert meal_word(2) == "meals"
    assert meal_word(5) == "meals"


def test_digest_build_prompt():
    context = {"test": "data"}

    # RU prompt
    prompt_ru = build_prompt(context, lang="ru")
    assert "Структурный срез данных за период" in prompt_ru
    assert "Напиши аналитический разбор" in prompt_ru

    # EN prompt
    prompt_en = build_prompt(context, lang="en")
    assert "Structured data snapshot for the period" in prompt_en
    assert "Write an analytical digest" in prompt_en


@pytest.mark.asyncio
async def test_oauth_page_renders_localized(auth_client, db_session, redis):
    # Set language to RU
    response = await auth_client.post("/settings/language", data={"language": "ru"})
    assert response.status_code == 303

    # Query authorization view
    r = await auth_client.get("/oauth/authorize?response_type=code&client_id=test-id&redirect_uri=http://localhost&state=123", headers={"Accept": "text/html"})
    assert r.status_code == 200
    assert "Разрешение доступа" in r.text
    assert "Неверный client_id" in r.text

    # Set language to EN
    response = await auth_client.post("/settings/language", data={"language": "en"})
    assert response.status_code == 303

    # Query authorization view again
    r = await auth_client.get("/oauth/authorize?response_type=code&client_id=test-id&redirect_uri=http://localhost&state=123", headers={"Accept": "text/html"})
    assert r.status_code == 200
    assert "Access Authorization" in r.text
    assert "Invalid client_id" in r.text


def test_i18n_key_parity():
    from vitals.i18n import STRINGS

    en_keys = set(STRINGS["en"].keys())
    ru_keys = set(STRINGS["ru"].keys())

    missing_in_ru = en_keys - ru_keys
    missing_in_en = ru_keys - en_keys

    assert not missing_in_ru, f"Translation keys missing in RU: {missing_in_ru}"
    assert not missing_in_en, f"Translation keys missing in EN: {missing_in_en}"


# Only fully-literal keys: the string must be immediately followed by `,` or `)`,
# so dynamic keys like t("nav." + spec.key) or t("enum.site." + s) are skipped
# (they can't be statically resolved and aren't the bug this guards against).
_TPL_KEY_RE = re.compile(r"""[^\w.]t\(\s*["']([a-z0-9_.]+)["']\s*[,)]""")
_JS_KEY_RE = re.compile(r"""window\.t\(\s*["']([a-z0-9_.]+)["']\s*\)""")
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_referenced_keys_exist_in_dictionaries():
    """Every literal key referenced by a Jinja ``t("…")`` or a JS ``window.t("…")``
    must exist in the dictionary it resolves against — templates against the full
    ``_EN`` map, JS against the ``js.*`` slice (which ``window.t`` sees). Guards the
    class of bug where a template ships a raw key like ``glp1.no_records`` or JS
    calls ``window.t("body.error.no_file")`` for a key that only lives in the
    template namespace. Complements ``test_i18n_key_parity`` (EN⇄RU symmetry):
    parity says both dicts agree, this says the code actually references real keys.
    """
    from vitals.i18n import _EN

    en_keys = set(_EN.keys())
    js_keys = {k[len("js."):] for k in en_keys if k.startswith("js.")}

    missing_tpl: set[str] = set()
    for path in (_REPO_ROOT / "web" / "templates").rglob("*.html"):
        for key in _TPL_KEY_RE.findall(path.read_text(encoding="utf-8")):
            if key not in en_keys:
                missing_tpl.add(key)

    # window.t() is called from inline <script> blocks too (base.html, settings,
    # weight), and those calls resolve against the same js.* slice. _TPL_KEY_RE
    # can't see them — its `[^\w.]` lead-in excludes the dot in `window.t(` — so
    # scanning only web/static left ~24 inline calls guarded by nothing.
    js_sources = list((_REPO_ROOT / "web" / "static").glob("*.js"))
    js_sources += list((_REPO_ROOT / "web" / "templates").rglob("*.html"))

    missing_js: set[str] = set()
    for path in js_sources:
        for key in _JS_KEY_RE.findall(path.read_text(encoding="utf-8")):
            if key not in js_keys:
                missing_js.add(key)

    assert not missing_tpl, f"Template t() keys missing from dictionary: {sorted(missing_tpl)}"
    assert not missing_js, f"JS window.t() keys missing from js.* namespace: {sorted(missing_js)}"


# ``t("enum.domain." ~ key)`` is built at render time, so the scanner above —
# which only sees literal keys — cannot cover any of it. That is not a
# theoretical hole: two of ``Domain``'s fourteen members shipped without a
# translation and the support console rendered them as ``enum.domain.system``
# to whoever asked for a section by name. What makes these safe to check
# exhaustively is that the member list *is* the key list: every value the
# template can interpolate is a member of one enumeration.
_ENUM_NAMESPACES = {
    "annotation_kind": "AnnotationKind",
    "cycle_kind": "CycleKind",
    "domain": "Domain",
    "drug": "Drug",
    "flag": "LabFlag",
    "site": "InjectionSite",
    "status": "MilestoneStatus",
}


@pytest.mark.parametrize("namespace,enum_name", sorted(_ENUM_NAMESPACES.items()))
def test_every_enum_member_can_be_named_in_both_languages(namespace, enum_name):
    from vitals import enums
    from vitals.i18n import STRINGS

    members = [member.value for member in getattr(enums, enum_name)]
    for lang in ("en", "ru"):
        missing = [
            value
            for value in members
            if f"enum.{namespace}.{value}" not in STRINGS[lang]
        ]
        assert not missing, (
            f"{enum_name} members with no {lang.upper()} name: {missing} — "
            f"a template interpolating one shows the raw key"
        )


def test_every_compound_class_in_the_catalog_can_be_named():
    """The HRT catalogue is data rather than an enumeration, so a new file adds
    a class without anything in the code changing. The dose picker groups by it."""

    from vitals.i18n import STRINGS
    from vitals.services.hrt_catalog import load_compound_catalog

    classes = {entry["compound_class"] for _key, entry in load_compound_catalog()}
    for lang in ("en", "ru"):
        missing = sorted(
            cls for cls in classes if f"enum.compound_class.{cls}" not in STRINGS[lang]
        )
        assert not missing, f"compound classes with no {lang.upper()} name: {missing}"
