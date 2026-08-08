"""Weekly AI digest service (module 10) — the product core.

Every 7 days we assemble a **structured cross-domain snapshot** (weight trend,
GLP-1 phase/plateau, Garmin recovery, Hevy training, out-of-range labs, active
goals) and ask the LLM for an *analytical narrative* — the interpretation of how
these relate, not a restatement of the numbers. The structured context is stored
alongside the text (re-inspect / re-run later).

The LLM client is injected so the generator is unit-tested without network or a
key; the scheduled job no-ops when no key is configured.
"""
from __future__ import annotations

import logging
from datetime import date as date_type
from typing import Any, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import DigestKind, Source
from vitals.integrations.llm_client import LLMEmptyResponse
from vitals.models.milestones import DOMAIN, WeeklyDigest
from vitals.utils.timeutils import today_local

logger = logging.getLogger(__name__)

# Output budget for one narrative. Was 6000 and prod hit it: a reasoning model
# (claude-opus-5) spends part of the same budget on thinking tokens, so the
# visible digest got cut mid-sentence. Russian is ~2 chars/token, and a full
# cross-domain digest runs 8-10k chars, so leave headroom for both.
_DIGEST_MAX_TOKENS = 16000

# Row budget per day of window for the signals block. The service's own default
# (200) is a screen's worth; a 90-day window quietly lost everything older than
# the 200th newest row while the block still read as the whole period.
# ponytail: a budget, not "all rows" — raise it if a day ever produces more.
_SIGNALS_PER_DAY = 50

DIGEST_SYSTEM = """\
Ты пишешь периодический разбор для пользователя дашборда здоровья Vitals.

Пользователь — молодой парень, который разбирается в теме (рекомпозиция, GLP-1, силовые, Garmin). Ему не нужны объяснения базовых понятий. Ему нужен взгляд сверху: что реально происходит, куда всё идёт, и на что обратить внимание.

РОЛЬ: ты — напарник, который шарит. Не врач, не коуч, не ментор. Говоришь прямо, без воды, без паники, без покровительственного тона. Если данных мало — так и скажи, без натягивания выводов.

ВХОДНЫЕ ДАННЫЕ (JSON):
Любой домен может быть null (нет данных). Не выдумывай того, чего нет.
- report_meta: report_date (когда отчёт сделан), period_start / period_end и period_days — что именно покрывает срез. Окно всегда состоит из ПОЛНОСТЬЮ ЗАКРЫТЫХ дней и заканчивается вчера, если отчёт сделан сегодня. Текущий, ещё не прожитый день в срез не входит вообще — не ищи его, не считай его пропущенным и не упоминай, что его нет. Период называй по period_start–period_end, а не по дате отчёта.
- days: ТАБЛИЦА ПО ДНЯМ — одна строка на каждый день периода, где домены УЖЕ СВЕДЕНЫ по датам: сон и часы сна, пульс покоя, HRV, батарея, стресс, шаги, readiness, вес, калории и белок, workout (была ли сессия, её тоннаж/подходы/длительность), day (офис/удалёнка/выходной, зал, тяжесть дня), signals (метки того, что он сам записал, с временем). Пустое поле = данных за тот день нет.
  ЭТО ГЛАВНЫЙ ИНСТРУМЕНТ ДЛЯ СВЯЗЕЙ. Читай таблицу по столбцам и ищи совпадения со сдвигом: тренировка → сон и HRV следующей ночи; exposure вечером → метрика наутро; тяжёлый день или офис → восстановление; дни с низкими калориями → шаги, стресс, вес через 2-3 дня. Называй связь С ДАТАМИ («после сессии 28-го HRV просел на две ночи») — без дат это не наблюдение, а общая фраза. Если совпадение однократное — так и скажи, что это одно совпадение, а не закономерность.
- user_profile: возраст, рост, программа, цели
- weight: последний замер, скользящее среднее (ma7_kg) + дата последней MA-точки (ma7_date), тренд (kg_per_week), noise_markers
  ВАЖНО: если активен noise_marker, то ma7_date — это последний чистый день ДО начала шума, а не сегодня. Не сравнивай latest_kg и ma7_kg как если бы они были одновременными. Разрыв между ними объясняется давностью MA, а не текущим шумом.
- glp1: препарат, доза, plateau
- body_comp: последний BIA/InBody-скан (date, device, metrics: % жира, скелетно-мышечная масса, безжировая масса, висцеральный жир, фазовый угол, балл). Может быть null (скана нет). Это отдельный источник состава тела (BIA); сосуществует с оценкой по замерам (Navy) в weight — не смешивай и не суммируй их.
- garmin: ПОСЛЕДНИЙ день с данными, плоскими полями (sleep_score, resting_hr, hrv_avg, body_battery_high, training_readiness, training_status — баланс нагрузка/восстановление, считает сам Garmin: PRODUCTIVE/MAINTAINING/RECOVERY/UNPRODUCTIVE/DETRAINING/STRAINED/PEAKING, может быть null, если мало аэробных тренировок; spo2_lowest — мин. SpO2 за ночь, низкий может говорить об апноэ; body_battery_change — восстановление за ночь; breathing_disruption — NONE/тяжесть нарушения дыхания).
  ВАЖНО: это один день. Разброс, тренд и «нормально/ненормально» читай по таблице days и по period_stats, а не по нему. total_days_logged — сколько дней Garmin лежит в базе ЗА ВСЮ ИСТОРИЮ, а не длина этого отчёта: он говорит только о том, есть ли вообще история. Никогда не называй его размером выборки отчёта («N дней истории, цифрам можно верить»).
- hevy: total_workouts — тренировок ВНУТРИ периода; last_workout — дата последней; mean_gap_days — средний интервал между сессиями; sessions — сессии за период И за столько же дней до него (in_period=false — сессия до начала среза). У каждой: volume_kg (тоннаж рабочих подходов), working_sets, duration_min, exercises.
  ВАЖНО: ритм тренировок — это mean_gap_days и интервалы между датами в sessions, а не total_workouts. Счётчик зависит от того, в какой день сделан отчёт: две сессии с разрывом в 5-7 дней попадают то в один срез, то в разные. Поэтому total_workouts как показатель режима просто не используй.
  ТО ЖЕ САМОЕ КАСАЕТСЯ ОБЪЁМА. Сравнивай volume_per_session_kg — тоннаж ОДНОЙ сессии. Сумма за период (training_volume_kg) двигается вместе со счётчиком сессий: одна тренировка против двух даёт «−51% объёма», хотя сессии были одинаковые. Никогда не выноси дельту суммы в вывод и не называй её падением объёма; если сессий в окнах разное количество, разница суммы — это разница в количестве сессий, и она не стоит отдельной фразы.
  Молча. Не объясняй читателю, как устроено окно, не пиши «формально столько, но фактически иначе», не сообщай, что счётчик вводит в заблуждение. Он не просил разбор методики — он просил разбор своего состояния. Сразу говори по факту: «ходишь раз в 3-4 дня, объём сессии держится» — и дальше.
- labs: out_of_range — маркеры вне нормы (marker, value, flag, date); trends — по каждому маркеру, у которого есть история, последние до 3 значений с датами + референс (ref_low/ref_high). Дрейф ВНУТРИ нормы — это твой хлеб: маркер, который шёл 120 → 95 → 80 и формально в норме, на дашборде выглядит зелёным, и увидеть это можно только здесь. Смотри направление и близость к границе, а не только флаг.
- period_stats: {current, previous} — один и тот же набор средних за период и за столько же дней ДО него (сон, часы сна, пульс покоя, HRV, батарея, стресс, шаги, вес, тренировки, тоннаж, ккал/белок). ЭТО ГЛАВНЫЙ БЛОК. Он есть ровно затем, чтобы ты говорил об ИЗМЕНЕНИИ, а не о текущем значении. Считай дельты сам и называй их.
  ЗНАМЕНАТЕЛИ, прежде чем делать вывод о пропусках: days — длина окна (все дни закрытые), garmin_days / nutrition_days_logged — на скольких из них реально стоят цифры. Разница, построенная на двух днях против семи, — это разница в покрытии данных, а не в организме, и назвать её надо именно так. Про покрытие пиши, только если оно реально мешает выводу: «данные есть за все дни» — не наблюдение, а отчёт о самом себе.
- nutrition: avg калории/белок в день, days_with_logs, цели
- hrt: гормональный протокол — active_compounds (что идёт сейчас), doses за период (дата, соединение, доза, место укола), side_effects (тип, тяжесть 1-5). Самое сильное вмешательство: связывай его со сном/HRV, анализами, кожей и настроением.
- timeline: ручные аннотации, пересекающие период (болезнь, поездка, смена протокола, событие). Это готовое объяснение для провала или скачка в других доменах — сверяйся с ними, прежде чем списать всё на тренировки или питание.
- signals: что пользователь сам сказал о своём состоянии, в хронологическом порядке. kind=state (есть всегда, value_num 1-5: «энергии ноль»), symptom (случилось, value_num 1-5: «голова раскалывается»), exposure (сделал/принял, at_time — время суток: «кофе в 22»). note — исходная формулировка. Единственный блок, который объясняет ПОЧЕМУ цифры такие: ищи связи «exposure вечером → метрика Garmin наутро» и «симптом держится N дней подряд → что ещё в эти дни». Это его слова, а не измерение — не считай их точными числами и не строй на одной записи вывод.
- day_context: каким был каждый день — where (office/remote/off), gym (был ли зал), load (light/normal/heavy, отвечается вечером о дне, который уже прошёл). source=manual — его собственный ответ, source=template — догадка недельного шаблона, которую он не стал поправлять (слабее, вывод на ней одной не строй). Это фон под всеми остальными цифрами: три тяжёлых дня подряд объясняют провал восстановления лучше, чем тренировки.
- milestones: активные цели с прогрессом и дедлайнами

ИНВАРИАНТЫ (нарушение = баг):
1. period_days < 7 → не называй «неделей», пиши «за N дней». Не экстраполируй.
2. labs.date > 14 дней от report_date → блок не существует, не упоминай.
3. garmin.total_days_logged ≤ 3 → не оценивай сон/восстановление, просто скажи что данных пока мало.
4. Опирайся ТОЛЬКО на данные из JSON. Ничего не выдумывай.
5. Если период пересекается с noise_markers — обязательно подсвети, что тренд веса может быть искажён (причина из reason).
   - direction="up"      → масштаб ЗАВЫШЕН шумом (загрузка креатином, скачок натрия, задержка воды). Реальный темп потери жира ЛУЧШЕ, чем показывает тренд; после конца маркера жди откат вверх на скользящем среднем + замедление видимого снижения — это нормально и НЕ означает потерю темпа.
   - direction="down"    → масштаб ЗАНИЖЕН (обезвоживание, болезнь). Реальная ситуация ХУЖЕ чем числа.
   - direction=null/"neutral" → направление неизвестно, просто отметь что данные зашумлены.

ПИТАНИЕ: пользователь часто забивает на трекинг. Если days_with_logs мало или калории нереалистично низкие — это пропущенный лог, а не голодовка. Не паникуй, просто отметь что данных мало.

ЧТО ПИСАТЬ:
Главный критерий: отчёт бесполезен, если пользователь мог получить то же самое, открыв дашборд. Все текущие значения он уже видит — там они крупнее и свежее. Ты нужен ради того, чего на экране нет физически, в этом порядке приоритета:

1. ИЗМЕНЕНИЕ. Что сдвинулось против прошлого периода и насколько (period_stats.current vs previous). Пересказ текущего значения без дельты — впустую потраченный абзац.
2. СВЯЗЬ МЕЖДУ ДОМЕНАМИ. Это то, чего он ждёт и чего пока не получает. Работай по таблице days: бери день, где один столбец заметно отклонился, и смотри, что стояло в остальных столбцах в этот день и в соседние. Тренировка ↔ сон и HRV следующей ночи; exposure вечером ↔ метрика наутро; тяжёлый день или офис ↔ восстановление; провал калорий ↔ шаги, стресс, вес через пару дней; HRT/добавки ↔ анализы, кожа, настроение.
   Связь без дат не считается. «Сон связан с нагрузкой» — пустая фраза, её можно написать не глядя в данные. «28-го сессия на 11 т, в ночь после неё HRV 41 против обычных 53, и это повторилось 1-го» — наблюдение. Если ни одного такого совпадения в данных нет — скажи об этом одной строкой и не подменяй его общими словами о том, как связаны домены вообще.
   Минимум одна такая проверенная по датам связь на отчёт, если данные вообще позволяют её найти.
3. ДРЕЙФ И ТРАЕКТОРИЯ. Куда всё идёт, если ничего не менять: labs.trends внутри нормы, наклон веса против дедлайна цели, тоннаж от периода к периоду.
4. ПРОТИВОРЕЧИЯ. Где данные спорят друг с другом или с его словами — сигнал говорит одно, метрика другое; тренд ускорился, а питание не менялось. Назвать противоречие ценнее, чем натянуть на него объяснение.
5. ЧЕГО НЕ ХВАТАЕТ. Какой цифры не хватило, чтобы ответить на важный вопрос, и что залогировать, чтобы в следующий раз ответ был.

Не открывай отчёт пересказом текущих значений. Первая же мысль должна быть выводом, которого нет на экране.
Честность важнее полноты: если по домену дельта в пределах шума или данных мало — так и скажи одной строкой и иди дальше. Отсутствие вывода — нормальный вывод; выдуманная связь — нет.

КАК ПИСАТЬ:
- Язык: русский.
- Тон: прямой, уверенный, дружеский. Как если бы знающий друг скинул голосовое с разбором. Без канцелярита, без «давай разберём», без «важно отметить».
- Объём: пиши развёрнуто, с аргументацией. Копай вглубь, не ограничивайся парой предложений на тему. Но если по конкретному домену данных мало или сказать нечего — не тяни, отметь коротко и иди дальше.
- Структура свободная. Группируй по смыслу, а не по доменам. Если по домену нечего сказать — не создавай для него секцию. Заголовки (##) — короткие, по делу, можно с одним подходящим эмодзи в начале.
- Используй **жирный** для ключевых цифр и выводов, > для важных предупреждений, списки для перечислений. Табличные данные — GFM pipe-таблицы (| ... | с |---|---| разделителем).
- Эмодзи: используй умеренно и к месту. Один эмодзи в заголовке секции — ок. В тексте — только если реально добавляет смысл (⚠️ для предупреждений, ✅ для ок-статуса). Не засыпай текст эмодзи, но и не избегай их.
"""

DIGEST_SYSTEM_EN = """\
You write periodic digests for a user of the Vitals health dashboard.

The user is a young guy who knows his stuff (recomp, GLP-1, lifting, Garmin). He doesn't need basic concepts explained. He needs the big picture: what's actually happening, where things are headed, and what to watch.

ROLE: you're a knowledgeable peer. Not a doctor, not a coach, not a mentor. Speak directly, no fluff, no panic, no patronizing. If data is thin — say so, don't stretch conclusions.

INPUT DATA (JSON):
Any domain can be null (no data). Don't invent what isn't there.
- report_meta: report_date (when the report was generated), period_start / period_end and period_days — what the slice actually covers. The window is always made of FULLY CLOSED days and ends yesterday when the report is generated today. The current, unfinished day is not in the window at all — don't look for it, don't count it as missed, don't mention that it's absent. Name the period by period_start–period_end, not by the report date.
- days: THE DAY TABLE — one row per day of the period with the domains ALREADY JOINED by date: sleep score and hours, resting HR, HRV, body battery, stress, steps, readiness, weight, calories and protein, workout (whether there was a session, its tonnage/sets/duration), day (office/remote/off, gym, how heavy), signals (labels of what he logged himself, with times). An empty field means no data for that day.
  THIS IS THE MAIN TOOL FOR FINDING LINKS. Read it column-wise and look for shifted coincidences: a session → next night's sleep and HRV; an evening exposure → the next morning's metric; a heavy or office day → recovery; low-calorie days → steps, stress, weight two or three days later. Name the link WITH DATES ("after the session on the 28th, HRV sat two nights below its usual") — without dates it isn't an observation, it's a generality. If a coincidence happens once, say it happened once rather than calling it a pattern.
- user_profile: age, height, program, goals
- weight: latest reading, moving average (ma7_kg) + date of last MA point (ma7_date), trend (kg_per_week), noise_markers
  IMPORTANT: if a noise_marker is active, ma7_date is the last clean day BEFORE the noise started — not today. Do NOT compare latest_kg and ma7_kg as if they are simultaneous. Any gap between them reflects how stale the MA is, not current noise.
- glp1: drug, dose, plateau flag
- body_comp: latest BIA/InBody scan (date, device, metrics: body-fat %, skeletal muscle mass, lean body mass, visceral fat, phase angle, score). Can be null (no scan taken). This is a separate BIA body-composition source; it coexists with the tape/Navy estimate in weight — don't conflate or sum them.
- garmin: the LAST day carrying data, as flat fields (sleep_score, resting_hr, hrv_avg, body_battery_high, training_readiness, training_status — load/recovery balance, Garmin-computed: PRODUCTIVE/MAINTAINING/RECOVERY/UNPRODUCTIVE/DETRAINING/STRAINED/PEAKING, can be null if too little aerobic training; spo2_lowest — night-low SpO2, low can suggest apnea; body_battery_change — overnight recovery; breathing_disruption — NONE/severity), plus days — one row per day of the period (sleep score, sleep hours, resting HR, HRV, body battery, stress, steps, readiness).
  IMPORTANT: this is one day. Read spread, trend and "normal/abnormal" off the days table and period_stats, not off it. total_days_logged is how many Garmin days sit in the database IN TOTAL, not the length of this report: it only says whether history exists at all. Never present it as the report's sample size ("N days of history, so the numbers are trustworthy").
- hevy: total_workouts — workouts INSIDE the period; last_workout — date of the latest; mean_gap_days — average interval between sessions; sessions — sessions in the period AND in the equally long stretch before it (in_period=false — before the window starts). Each carries volume_kg (working-set tonnage), working_sets, duration_min, exercises.
  IMPORTANT: training cadence is mean_gap_days and the intervals between dates in sessions, not total_workouts. The counter depends on which day the report was generated: two sessions 5-7 days apart land in one slice or in two. So don't use total_workouts as a measure of the routine at all.
  THE SAME GOES FOR VOLUME. Compare volume_per_session_kg — the tonnage of ONE session. The period sum (training_volume_kg) moves with the session count: one session against two reads as "volume down 51%" when both sessions were identical. Never headline the delta of the sum or call it a drop in volume; when the windows hold different numbers of sessions, the difference in the sum is a difference in session count and doesn't deserve a sentence.
  Silently. Don't explain how the window works, don't write "formally X but actually Y", don't announce that the counter is misleading. He didn't ask for a critique of the method — he asked about his own state. Just say the fact: "you train every 3-4 days, volume is holding" — and move on.
- labs: out_of_range — markers outside their range (marker, value, flag, date); trends — for every marker with history, its last up to 3 values with dates plus the reference range (ref_low/ref_high). Drift INSIDE the normal range is your bread and butter: a marker that went 120 → 95 → 80 is formally normal, shows up green on the dashboard, and can only be seen here. Read direction and proximity to the boundary, not just the flag.
- period_stats: {current, previous} — the same set of averages for the period and for the equally long stretch BEFORE it (sleep score, sleep hours, resting HR, HRV, body battery, stress, steps, weight, workouts, tonnage, calories/protein). THIS IS THE KEY BLOCK. It exists so that you talk about CHANGE rather than current values. Compute the deltas yourself and name them.
  DENOMINATORS, before concluding anything about missed days: days is the window length (every day in it is closed), garmin_days / nutrition_days_logged is how many of them actually carry numbers. A difference built on two days against seven is a difference in coverage, not in the body, and must be called that. Only mention coverage when it actually limits a conclusion — "data is present for every day" is not an observation, it's a status report about yourself.
- nutrition: avg calories/protein per day, days_with_logs, targets
- hrt: hormone protocol — active_compounds (what's running now), doses in the period (date, compound, dose, injection site), side_effects (type, severity 1-5). The strongest intervention here: relate it to sleep/HRV, labs, skin and mood.
- timeline: manual annotations overlapping the period (illness, travel, protocol change, life event). These are the ready-made explanation for a dip or spike in the other domains — check them before blaming training or nutrition.
- signals: what the user said about how he felt, in chronological order. kind=state (always present, value_num 1-5), symptom (happened, value_num 1-5), exposure (did/took it, at_time = time of day). note is his original wording. The only block that explains WHY the numbers look like they do: look for "exposure in the evening → Garmin metric next morning" and "symptom running N days straight → what else those days had". These are his words, not measurements — don't treat them as exact figures and don't build a conclusion on a single row.
- day_context: what each day was made of — where (office/remote/off), gym (did he train), load (light/normal/heavy, answered in the evening about the day just finished). source=manual is his own answer; source=template is the weekly template's guess he didn't bother correcting (weaker — don't build a conclusion on that alone). This is the backdrop for every other number: three heavy days in a row explain a recovery dip better than training does.
- milestones: active goals with progress and deadlines

INVARIANTS (breaking = bug):
1. period_days < 7 → don't call it a "week", say "these N days". Don't extrapolate.
2. labs.date > 14 days from report_date → block doesn't exist, don't mention.
3. garmin.total_days_logged ≤ 3 → don't evaluate sleep/recovery, just say not enough data yet.
4. Use ONLY data from the JSON. Don't invent anything.
5. If period overlaps noise_markers — must flag that weight trend may be distorted (reason from marker).
   - direction="up"      → scale INFLATED by noise (creatine loading, sodium spike, water retention). Real fat-loss pace is BETTER than the trend shows; after the marker ends expect the moving average to bounce up and visible loss to slow — that is normal and does NOT mean progress has stalled.
   - direction="down"    → scale DEFLATED (dehydration, illness). Real situation is WORSE than numbers.
   - direction=null/"neutral" → direction unknown, just note data is noisy.

NUTRITION: user often skips tracking. Low days_with_logs or unrealistically low calories = missed log, not starvation. Don't panic, just note data is sparse.

WHAT TO WRITE:
The test that matters: the report is useless if the user could have got the same thing by opening the dashboard. He already sees every current value there — bigger and fresher. You exist for what the screen physically cannot show, in this order of priority:

1. CHANGE. What moved against the previous period and by how much (period_stats.current vs previous). Restating a current value without a delta is a wasted paragraph.
2. CROSS-DOMAIN LINKS. This is what he is waiting for and not getting. Work off the days table: take a day where one column moved noticeably and look at what the other columns held that day and the days around it. Training ↔ next night's sleep and HRV; an evening exposure ↔ the next morning's metric; a heavy or office day ↔ recovery; a calorie dip ↔ steps, stress, weight a couple of days later; HRT/supplements ↔ labs, skin, mood.
   A link without dates doesn't count. "Sleep is related to load" is an empty sentence anyone could write without opening the data. "The session on the 28th ran 11 t, HRV that night was 41 against a usual 53, and it repeated on the 1st" is an observation. If no such coincidence exists in the data — say so in one line rather than substituting generalities about how domains relate.
   At least one date-checked link per report, whenever the data allows one to be found.
3. DRIFT AND TRAJECTORY. Where this ends up if nothing changes: labs.trends inside the normal range, the weight slope against a goal deadline, tonnage period over period.
4. CONTRADICTIONS. Where the data argues with itself or with his own words — a signal says one thing and the metric another; the trend accelerated while nutrition didn't move. Naming a contradiction beats inventing an explanation for it.
5. WHAT'S MISSING. Which number you needed and didn't have to answer an important question, and what to log so the answer exists next time.

Don't open the report by restating current values. The first thought should already be a conclusion that isn't on the screen.
Honesty over completeness: if a domain's delta is within noise or its data is thin — say so in one line and move on. No conclusion is a valid conclusion; an invented connection is not.

HOW TO WRITE:
- Language: English.
- Tone: direct, confident, friendly. Like a knowledgeable friend sending a voice note with their take. No corporate speak, no "let's dive in", no "it's important to note".
- Length: write with depth and reasoning. Dig into the why, don't just skim. But if a specific domain has thin data or nothing to say — note it briefly and move on.
- Free structure. Group by insight, not by domain. If a domain has nothing to say — skip it. Headers (##) — short, to the point, one fitting emoji at the start is fine.
- Use **bold** for key numbers and conclusions, > for important warnings, lists for enumerations. Tabular data — GFM pipe tables (| ... | with |---|---| separator).
- Emoji: use sparingly and meaningfully. One emoji per section header — fine. In body text — only when it genuinely adds meaning (⚠️ for warnings, ✅ for ok status). Don't spam emoji, but don't avoid them either.
"""



# ── Context assembly ──────────────────────────────────────────────────────────
async def assemble_context(
    session: AsyncSession,
    *,
    on_date: Optional[date_type] = None,
    period_days: int = 7,
) -> dict:
    """Pull a structured cross-domain snapshot. Each domain is read through its own
    service (lazy import to avoid cycles); empty domains come back as nulls/empties
    rather than raising, so a digest works even before every module has data."""
    today = on_date or today_local()

    from datetime import timedelta

    # A period report covers days that are *over*. Ending the window on the current
    # date meant the last slot was a day still being lived — at 00:50 that day had
    # no meals, no sleep and no training in it, and every "6 of 7" the narrative
    # produced was counting the night ahead as a day its owner had skipped. So the
    # window ends yesterday and holds N complete days. The daily brief is exempt:
    # period_days=1 is *about* the morning it runs in.
    period_end = (
        today - timedelta(days=1)
        if period_days > 1 and today == today_local()
        else today
    )
    period_start = period_end - timedelta(days=period_days - 1)
    prev_end = period_start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=period_days - 1)

    from vitals.config import load_config
    cfg = load_config()

    ctx: dict[str, Any] = {
        "date": today.isoformat(),  # Keep for backward compatibility
        "report_meta": {
            "report_date": today.isoformat(),
            "period_days": period_days,
            # What the window actually covers, so the narrative dates the period
            # rather than the moment it was generated in.
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
        },
        "user_profile": {
            "age": cfg.user_age,
            "sex": cfg.sex,
            "height_cm": cfg.height_cm,
            "program": cfg.user_program,
            "goals": cfg.user_goals,
        },
    }

    from vitals.services import weight_service

    series = await weight_service.chart_series(session)
    weights = await weight_service.list_active_weights(session)

    markers = await weight_service.list_noise_markers(session)
    matching_markers = []
    for m in markers:
        if m.start_date <= today and (m.end_date is None or m.end_date >= period_start):
            matching_markers.append({
                "start": m.start_date.isoformat(),
                "end": m.end_date.isoformat() if m.end_date else None,
                "reason": m.reason,
                # direction: which way the scale is biased vs real fat trend.
                # up   = scale inflated (creatine/sodium) → real loss is better
                # down = scale deflated (dehydration)     → real situation worse
                # null = unknown / treat as neutral
                "direction": m.direction,
            })

    last_ma = series["trend_ma"][-1] if series["trend_ma"] else None
    ctx["weight"] = {
        "latest_kg": weights[-1].weight_kg if weights else None,
        # When that measurement was taken. ``latest_kg`` is the newest weight
        # *ever* logged, with no window on it — printed bare it reads as today's,
        # and the empty-day check had no way to tell "he weighed in this morning"
        # from "he last stepped on the scale in March".
        "latest_date": weights[-1].date.isoformat() if weights else None,
        "ma7_kg": last_ma["weight_kg"] if last_ma else None,
        # Date the MA7 was last calculated. During a noise period ALL measurements
        # inside it are excluded from the MA, so ma7_date will be the last clean
        # day BEFORE the noise started — potentially weeks ago. Do NOT compare
        # latest_kg directly to ma7_kg as if they describe the same moment.
        "ma7_date": last_ma["date"] if last_ma else None,
        "trend_kg_per_week": series["trend"]["slope_per_week"] if series.get("trend") else None,
        "noise_markers": matching_markers,
    }

    from vitals.services import glp1_service

    phase = await glp1_service.active_dose_phase(session, on_date=today)
    ctx["glp1"] = {
        "drug": phase.drug if phase else None,
        "dose_mg": phase.dose_mg if phase else None,
        "plateau": await glp1_service.evaluate_plateau(session, on_date=today),
    }

    from vitals.services import body_scan_service
    from vitals.services.analytics.body_metrics import (
        HEADLINE_KEYS,
        METRIC_REGISTRY,
        lbm_from_scan,
    )

    # Body composition (BIA/InBody). Latest scan only — the headline metrics that
    # matter for recomp. Separate source from the Navy tape estimate in `weight`;
    # coexist, never summed. Null when the owner has taken no scan.
    scan = await body_scan_service.latest_scan(session)
    if scan is not None:
        by_key = {m.metric_key: m for m in scan.metrics}
        comp_metrics: dict[str, Any] = {}
        for k in HEADLINE_KEYS:
            m = by_key.get(k)
            if m is not None:
                spec = METRIC_REGISTRY.get(k)
                comp_metrics[k] = {
                    "value": m.value,
                    "unit": m.unit or (spec.unit if spec else None),
                }
        lbm = lbm_from_scan(scan.metrics)
        if lbm is not None:
            comp_metrics["lean_body_mass"] = {"value": lbm, "unit": "кг"}
        ctx["body_comp"] = {
            "date": scan.date.isoformat(),
            "device": scan.device,
            "metrics": comp_metrics,
        }
    else:
        ctx["body_comp"] = None

    from vitals.services import garmin_service

    g = await garmin_service.latest_daily(session, before_or_on=today)
    ctx["garmin"] = (
        {
            "date": g.date.isoformat(),
            "sleep_score": g.sleep_score,
            # The night's own boundaries. Carried so a reader — the brief's
            # unscored-night check, or the model — can tell "no sleep recorded"
            # from "sleep recorded and it was bad", which a bare score cannot.
            "sleep_seconds": g.sleep_seconds,
            "sleep_end": g.sleep_end.isoformat() if g.sleep_end else None,
            "resting_hr": g.resting_hr,
            "hrv_avg": g.hrv_avg,
            "body_battery_high": g.body_battery_high,
            "training_readiness": g.training_readiness,
            "training_status": g.training_status,
            "spo2_lowest": g.spo2_lowest,
            "body_battery_change": g.body_battery_change,
            "breathing_disruption": g.breathing_disruption,
            "advice": garmin_service.recovery_advice(g),
            "total_days_logged": await garmin_service.daily_count(session),
        }
        if g
        else None
    )

    # Recovery over the period and the one before it. A "weekly" report used to be
    # handed exactly one night of it. The rows are joined into the day table at the
    # end of assembly; skipped for the daily brief, which is about one morning by
    # design and carries its own baseline.
    garmin_rows = (
        await garmin_service.list_daily_between(session, prev_start, period_end)
        if period_days > 1
        else []
    )

    from vitals.services import hevy_service

    since = period_start

    # Two periods of sessions, not one number. A count over a rolling window is
    # decided by where the edge happens to land: Saturday plus the Tuesday before
    # it is "две тренировки в неделю" to the person who did them and "1 за 7 дней"
    # to a window opened on Tuesday — and the narrative built a confident verdict
    # on that coin flip. The dates go in so cadence is read off the actual gaps;
    # sessions before the window are marked so they inform the rhythm without
    # inflating the count. Volume and exercises come along because "тренировка"
    # as a bare tally cannot tell a full session from a fifteen-minute one.
    lookback = prev_start
    sessions = [
        {**hevy_service.workout_summary(w), "in_period": w.date >= since}
        for w in reversed(
            await hevy_service.list_workouts(session, limit=max(period_days * 4, 10))
        )
        if lookback <= w.date <= period_end
    ]
    last_workout = await hevy_service.latest_workout_date(session)
    # The gap between sessions is the one training number a window edge cannot
    # move. Handed only a count, the narrative had to explain the boundary to say
    # anything true — and nobody wants a paragraph about window boundaries.
    gaps = [
        (date_type.fromisoformat(b["date"]) - date_type.fromisoformat(a["date"])).days
        for a, b in zip(sessions, sessions[1:])
    ]
    ctx["hevy"] = {
        "total_workouts": sum(1 for s in sessions if s["in_period"]),
        "last_workout": last_workout.isoformat() if last_workout else None,
        "mean_gap_days": round(sum(gaps) / len(gaps), 1) if gaps else None,
        "sessions": sessions or None,
    }

    from vitals.services import labs_service

    latest_labs = await labs_service.latest_per_marker(session)
    ctx["labs"] = {
        "out_of_range": [
            {
                "marker": r.marker,
                "value": r.value,
                "unit": r.unit,
                "flag": r.flag,
                "date": r.date.isoformat(),
            }
            for r in latest_labs
            if labs_service.is_out_of_range(r.flag) and (today - r.date).days <= 14
        ]
    }

    # Labs the way only a lake can show them. A marker that went 120 → 95 → 80 and
    # is still inside its reference range is invisible to `out_of_range`, invisible
    # on a table of current values, and is exactly the kind of thing this report
    # exists to say out loud. One query, grouped in memory — the per-marker history
    # read is a query each and there are dozens of markers.
    marker_rows: dict[str, list] = {}
    # Same anchoring as the doctor report: a trend for this period is drawn from
    # results that existed by its end, not from whatever is newest in the table.
    for r in await labs_service.list_results(session, end=period_end, limit=1000):
        marker_rows.setdefault(r.marker, []).append(r)
    ctx["labs"]["trends"] = [
        {
            "marker": marker,
            "unit": rows[0].unit,
            "ref_low": rows[0].ref_low,
            "ref_high": rows[0].ref_high,
            "points": [
                {"date": r.date.isoformat(), "value": r.value, "flag": r.flag}
                for r in reversed(rows[:3])
            ],
        }
        for marker, rows in marker_rows.items()
        if len(rows) >= 2
    ] or None

    from vitals.services import nutrition_service

    # Two periods of meals: the block below is about this one, the comparison at
    # the end needs the one before it, and one read covers both.
    all_meals = await nutrition_service.list_meals(session, start=prev_start, end=period_end)
    nutrition_meals = [m for m in all_meals if m.date >= since]
    if nutrition_meals:
        per_day_meals: dict = {}
        for m in nutrition_meals:
            per_day_meals.setdefault(m.date, []).append(m)
        days_with_logs = len(per_day_meals)
        total_cal = sum(m.calories or 0 for m in nutrition_meals)
        total_prot = sum(m.protein_g or 0 for m in nutrition_meals)
        goals = nutrition_service.get_goals(cfg)
        ctx["nutrition"] = {
            "avg_calories_per_day": round(total_cal / days_with_logs, 1),
            "avg_protein_per_day_g": round(total_prot / days_with_logs, 1),
            "days_with_logs": days_with_logs,
            "total_meals": len(nutrition_meals),
            "goals": goals,
        }
    else:
        ctx["nutrition"] = None

    # Supplements / skincare / genetics / active alerts — enabled domains the
    # digest used to ignore, so cross-domain reasoning it promises (e.g. "started
    # ashwagandha → sleep/HRV shifted", "introduced a retinoid → skin reacted")
    # had no data to work with. Each domain is read through its own service (lazy
    # import); empty → null.
    from vitals.services import supplements_service

    active_supps = await supplements_service.list_supplements(session, active_only=True)
    ctx["supplements"] = (
        [
            {"name": s.name, "dose": s.dose, "timing": s.timing, "evidence": s.evidence}
            for s in active_supps
        ]
        if active_supps
        else None
    )

    from vitals.services import skincare_service

    skin_obs = [
        o for o in await skincare_service.list_observations(session) if o.date >= since
    ]
    active_products = await skincare_service.list_products(session, active_only=True)
    if skin_obs or active_products:
        ctx["skincare"] = {
            "recent_observations": [
                {
                    "date": o.date.isoformat(),
                    "inflammation": o.inflammation,
                    "pih": o.pih,
                    "zone": o.zone,
                    "note": o.note,
                }
                for o in skin_obs
            ],
            "active_products": len(active_products),
        }
    else:
        ctx["skincare"] = None

    from vitals.services import genetics_service

    variants = await genetics_service.resolve_variants(session)
    ctx["genetics"] = variants or None

    from vitals.services import alerts_service

    active_alerts = await alerts_service.list_active(session)
    ctx["alerts"] = (
        [
            {"severity": a.severity, "domain": a.domain, "message": a.message}
            for a in active_alerts
        ]
        if active_alerts
        else None
    )

    # HRT — the strongest intervention in the lake and, until now, invisible to the
    # digest: a compound change and the sleep/labs/skin shift that follows it could
    # never be connected. Active protocol + doses inside the period + side effects.
    from vitals.services import hrt_cycle_service, hrt_service

    # active_cycle takes on_date (resolve_active is pinned to "now" — wrong for a
    # digest generated for a past date).
    cycle = await hrt_cycle_service.active_cycle(session, on_date=today)
    hrt_doses = await hrt_service.list_doses(session, start=since, end=period_end)
    hrt_effects = [
        e for e in await hrt_service.list_side_effects(session) if e.date >= since
    ]
    ctx["hrt"] = (
        {
            "cycle": (
                {
                    "kind": cycle.kind,
                    "name": cycle.name,
                    "start_date": cycle.start_date.isoformat(),
                    "end_date": cycle.end_date.isoformat() if cycle.end_date else None,
                    "compounds": [i.compound_key for i in cycle.items],
                }
                if cycle is not None
                else None
            ),
            "doses": [
                {
                    "date": d.date.isoformat(),
                    "compound_key": d.compound_key,
                    "dose": d.dose,
                    "unit": d.unit,
                    "site": d.site,
                }
                for d in hrt_doses
            ],
            "side_effects": [
                {
                    "date": e.date.isoformat(),
                    "effect_type": e.effect_type,
                    "severity": e.severity,
                    "note": e.note,
                }
                for e in hrt_effects
            ],
        }
        if (cycle is not None or hrt_doses or hrt_effects)
        else None
    )

    # Timeline — manual annotations (illness, travel, protocol change) overlapping
    # the period. These are exactly the "why" behind a wobble in every other domain,
    # so the narrative has to see them.
    from vitals.services import timeline_service

    annotations = await timeline_service.list_annotations(session, start=since, end=period_end)
    ctx["timeline"] = [
        {
            "date": a.date.isoformat(),
            "end_date": a.end_date.isoformat() if a.end_date else None,
            "kind": a.kind,
            "domain": a.domain,
            "title": a.title,
            "note": a.note,
        }
        for a in annotations
    ] or None

    # Signals — the day in his own words. Every other block is a measurement; this
    # is the only one that can say *why* a measurement moved: "кофе в 22" the night
    # before the HRV dip, a headache running through a week of bad sleep. Read
    # chronologically, because a narrative reads a period forward. Rows tapped
    # "не то" are excluded by the service — a misparse is not evidence, even
    # though it stays on the table as material for the key revision (прогон 7).
    from vitals.services import signals_service

    signals = await signals_service.list_signals(
        session, start=since, end=period_end, limit=period_days * _SIGNALS_PER_DAY
    )
    ctx["signals"] = [signal_row(s) for s in reversed(signals)] or None

    from vitals.services import milestones_service

    ctx["milestones"] = await milestones_service.dashboard_cards(session)

    # Day context — what each day was *made of*: office or remote, gym or not, how
    # heavy it turned out. Every other block says what the body did; this says what
    # the week asked of it, which is the whole difference between "HRV fell" and
    # "HRV fell across three heavy office days in a row". Read straight off the
    # model: the domain's service resolves one day at a time (the bot only ever
    # needs today), and a period read has no other caller.
    from vitals.models.signals import DayContext

    day_rows = (
        await session.execute(
            select(DayContext)
            .where(DayContext.date >= since, DayContext.date <= period_end)
            .order_by(DayContext.date)
        )
    ).scalars().all()
    ctx["day_context"] = [
        {
            "date": row.date.isoformat(),
            "answers": row.answers or {},
            # Whose answer it is. A template guess he never corrected is weaker
            # evidence than a day he spoke for, and the model has no other way to
            # tell them apart.
            "source": row.source,
        }
        for row in day_rows
    ] or None

    # ── The join ──────────────────────────────────────────────────────────────
    # One row per day with every domain on it. The report kept reading as a stack
    # of separate domains because that is exactly what it was handed: recovery in
    # one shape, meals as an average, training as dated sessions, the day itself
    # somewhere else. Finding "the night after a heavy session" in that meant
    # joining five differently-shaped blocks by date in its head, and it simply
    # didn't. The join is arithmetic, so it belongs here, not in the prompt — what
    # arrives is the table a person would draw before looking for a pattern.
    if period_days > 1:
        by_date_workout = {s["date"]: s for s in sessions if s["in_period"]}
        by_date_day = {r.date: r for r in day_rows}
        by_date_garmin = {r.date: r for r in garmin_rows}  # one row per date
        by_date_weight = {x.date: x for x in weights}
        meals_by_date: dict = {}
        for meal in all_meals:
            meals_by_date.setdefault(meal.date, []).append(meal)
        signals_by_date: dict = {}
        for s in signals:
            signals_by_date.setdefault(s.date, []).append(s)

        ctx["days"] = []
        for i in range(period_days):
            d = period_start + timedelta(days=i)
            g_row = by_date_garmin.get(d)
            meals = meals_by_date.get(d) or []
            day_row = by_date_day.get(d)
            workout = by_date_workout.get(d.isoformat())
            ctx["days"].append({
                "date": d.isoformat(),
                "weekday": d.strftime("%a"),
                "sleep_score": g_row.sleep_score if g_row else None,
                "sleep_hours": (
                    round(g_row.sleep_seconds / 3600, 1)
                    if g_row and g_row.sleep_seconds
                    else None
                ),
                "resting_hr": g_row.resting_hr if g_row else None,
                "hrv_avg": g_row.hrv_avg if g_row else None,
                "body_battery_high": g_row.body_battery_high if g_row else None,
                "avg_stress": g_row.avg_stress if g_row else None,
                "steps": g_row.steps if g_row else None,
                "training_readiness": g_row.training_readiness if g_row else None,
                "weight_kg": (
                    by_date_weight[d].weight_kg if d in by_date_weight else None
                ),
                "calories": sum(m.calories or 0 for m in meals) or None,
                "protein_g": round(sum(m.protein_g or 0 for m in meals), 1) or None,
                "workout": (
                    {
                        "title": workout["title"],
                        "volume_kg": workout["volume_kg"],
                        "working_sets": workout["working_sets"],
                        "duration_min": workout["duration_min"],
                    }
                    if workout
                    else None
                ),
                "day": (day_row.answers or None) if day_row else None,
                # Labels only — the full wording, with his own phrasing, stays in
                # the signals block. Here they exist so a signal is visible on the
                # same line as the metric it might explain.
                "signals": [
                    (f"{s.key}@{s.at_time.strftime('%H:%M')}" if s.at_time else s.key)
                    for s in signals_by_date.get(d, [])
                ] or None,
            })

    # ── The comparison ────────────────────────────────────────────────────────
    # The reason the report was worth reading and wasn't: handed only current
    # values, a narrative can do nothing but read them back, and the dashboard
    # already did that better. Change is the part that isn't on any screen —
    # so the period and the period before it are reduced to the same shape and
    # handed over together. Weight was the one domain that already carried its
    # own history (MA + slope), and the one domain the digest ever said anything
    # about; this gives every other domain the same footing.
    if period_days > 1:
        ctx["period_stats"] = {
            "current": _window_stats(
                period_start, period_end, garmin_rows, weights, all_meals, sessions
            ),
            "previous": _window_stats(
                prev_start, prev_end, garmin_rows, weights, all_meals, sessions
            ),
        }
    return ctx


def _mean(values) -> Optional[float]:
    present = [v for v in values if v is not None]
    return round(sum(present) / len(present), 1) if present else None


_GARMIN_STAT_COLS = (
    "sleep_score", "resting_hr", "hrv_avg", "body_battery_high", "avg_stress", "steps",
)


def _window_stats(start, end, garmin_rows, weights, meals, sessions) -> dict:
    """One window reduced to the numbers worth comparing against another window.

    Symmetric on purpose: the model gets two identical shapes to subtract, rather
    than this period's rows plus an invitation to recall the last one — which is
    where a narrative starts supplying the half it doesn't have.

    Every count carries the denominator it should be read against — how many days
    actually carry numbers, not how many dates the window spans.
    """
    g = [r for r in garmin_rows if start <= r.date <= end]
    w = [x for x in weights if start <= x.date <= end]
    m = [x for x in meals if start <= x.date <= end]
    s = [x for x in sessions if start.isoformat() <= x["date"] <= end.isoformat()]
    logged_days = len({x.date for x in m})
    days = (end - start).days + 1
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "days": days,
        "sleep_score": _mean(r.sleep_score for r in g),
        "sleep_hours": _mean(
            round(r.sleep_seconds / 3600, 1) for r in g if r.sleep_seconds
        ),
        "resting_hr": _mean(r.resting_hr for r in g),
        "hrv_avg": _mean(r.hrv_avg for r in g),
        "body_battery_high": _mean(r.body_battery_high for r in g),
        "avg_stress": _mean(r.avg_stress for r in g),
        "steps": _mean(r.steps for r in g),
        # Days with numbers on them, not rows. A row is written for a date before
        # the watch has scored anything for it, so counting rows reported seven
        # Garmin days behind means that were computed from six.
        "garmin_days": sum(
            1 for r in g if any(getattr(r, c) is not None for c in _GARMIN_STAT_COLS)
        ),
        "weight_kg": _mean(x.weight_kg for x in w),
        "workouts": len(s),
        # Tonnage per session, not per window. Summed over a window it inherits the
        # window's arbitrariness exactly as the count does: one session against two
        # reads as "volume down 51%" when both sessions were the same size. Per
        # session the number is a fact about training; summed it is a fact about
        # where the window edge fell. The sum is kept, one rung below.
        "volume_per_session_kg": _mean(x["volume_kg"] for x in s),
        "training_volume_kg": sum(x["volume_kg"] or 0 for x in s) or None,
        "calories_per_day": (
            round(sum(x.calories or 0 for x in m) / logged_days, 1) if logged_days else None
        ),
        "protein_per_day_g": (
            round(sum(x.protein_g or 0 for x in m) / logged_days, 1) if logged_days else None
        ),
        "nutrition_days_logged": logged_days,
    }


def signal_row(signal) -> dict:
    """One signal as the model sees it.

    Shared with the daily brief, which reads the same rows over a wider window —
    two shapes for the same block would mean two prompt descriptions to keep in
    step, and the one that drifted would drift silently.
    """
    return {
        "date": signal.date.isoformat(),
        "kind": signal.kind,
        "key": signal.key,
        "value_num": signal.value_num,
        "unit": signal.unit,
        # The hour is what makes an exposure correlatable at all: "кофе в 22" and
        # "кофе в 9" are opposite facts wearing the same key.
        "at_time": signal.at_time.strftime("%H:%M") if signal.at_time else None,
        "note": signal.note,
    }


def build_prompt(context: dict, lang: str = "ru") -> str:
    """Render the structured context into the user prompt for the narrative."""
    import json

    if lang == "en":
        prefix = "Structured data snapshot for the period (JSON):\n\n"
        suffix = "\n\nWrite an analytical digest based on this data."
    else:
        prefix = "Структурный срез данных за период (JSON):\n\n"
        suffix = "\n\nНапиши аналитический разбор по этим данным."

    return (
        prefix
        + json.dumps(context, ensure_ascii=False, indent=2)
        + suffix
    )


# ── Generation ────────────────────────────────────────────────────────────────
async def generate_digest(
    session: AsyncSession,
    llm: Any,
    *,
    on_date: Optional[date_type] = None,
    period_days: int = 7,
    source: str = Source.MANUAL.value,
) -> WeeklyDigest:
    """Assemble context, get the narrative from the LLM, and persist the digest.
    Raises whatever the LLM client raises (e.g. ``LLMNotConfigured``), or
    ``LLMEmptyResponse`` if the model comes back blank twice in a row. No commit."""
    from vitals.i18n import current_lang
    lang = current_lang.get()

    context = await assemble_context(session, on_date=on_date, period_days=period_days)
    prompt = build_prompt(context, lang=lang)
    system = DIGEST_SYSTEM_EN if lang == "en" else DIGEST_SYSTEM

    content = await llm.complete_text(prompt, system=system, max_tokens=_DIGEST_MAX_TOKENS)
    if not content:
        # Seen in prod: the upstream occasionally returns a blank message with no
        # error at all. One retry clears it in practice; if it's still empty,
        # fail loudly instead of silently persisting nothing.
        content = await llm.complete_text(prompt, system=system, max_tokens=_DIGEST_MAX_TOKENS)
    if not content:
        raise LLMEmptyResponse("LLM returned an empty digest narrative twice in a row")

    row = WeeklyDigest(
        date=on_date or today_local(),
        domain=DOMAIN,
        source=source,
        kind=DigestKind.WEEKLY.value,
        content=content,
        context_json=context,
        model=getattr(llm, "digest_model", None),
    )
    session.add(row)
    await session.flush()
    return row


async def latest_digest(
    session: AsyncSession, *, kind: str = DigestKind.WEEKLY.value
) -> Optional[WeeklyDigest]:
    """The most recent narrative of one kind. Defaults to the weekly digest, so a
    daily brief can never show up where a weekly one is expected."""
    result = await session.execute(
        select(WeeklyDigest)
        .where(WeeklyDigest.kind == kind)
        .order_by(WeeklyDigest.date.desc(), WeeklyDigest.id.desc())
        .limit(1)
    )
    return result.scalars().first()


async def list_digests(
    session: AsyncSession, *, limit: int = 20, kind: str = DigestKind.WEEKLY.value
) -> Sequence[WeeklyDigest]:
    result = await session.execute(
        select(WeeklyDigest)
        .where(WeeklyDigest.kind == kind)
        .order_by(WeeklyDigest.date.desc(), WeeklyDigest.id.desc())
        .limit(limit)
    )
    return result.scalars().all()


# ── Scheduler job ─────────────────────────────────────────────────────────────
async def digest_job(session_factory, redis=None) -> None:
    """Weekly digest generation. No-ops when no OpenRouter key is configured so the
    scheduler never logs spurious failures."""
    from vitals.config import load_config
    from vitals.integrations.llm_client import LLMClient

    if not load_config().openrouter_api_key:
        return
    async with session_factory() as session:
        from vitals.services.language_service import get_language
        from vitals.i18n import current_lang
        lang = await get_language(session, redis)
        current_lang.set(lang)

        await generate_digest(session, LLMClient(), source=Source.SCHEDULER.value)
        await session.commit()
