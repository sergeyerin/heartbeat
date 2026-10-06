#!/usr/bin/env python3
"""Heartbeat — дневник аритмии в Telegram (ru / en / pt).

Главный принцип: регистрация эпизода стоит одно нажатие. Кнопка «⚡️ Аритмия»
всегда висит внизу диалога (persistent reply keyboard) и сразу пишет время
начала. Всё остальное — тяжесть, пульс, симптомы, причина, заметка, время
окончания — опциональные уточнения на карточке эпизода: можно заполнить
потом, когда отпустит, можно не заполнять вовсе.

Язык берётся из профиля Telegram и переопределяется командой /lang.
"""
from __future__ import annotations

import io
import logging
import re
import secrets
from datetime import date, datetime, time, timedelta, timezone

from telegram import (
    BotCommand,
    Chat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
import db
import i18n
import report
import vocab
from i18n import t

logging.basicConfig(
    format="%(asctime)s %(name)s %(levelname)s %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("heartbeat")

PRIVATE = filters.ChatType.PRIVATE

PULSE_RE = re.compile(r"^\s*(\d{2,3})\s*$")
MIN_PULSE, MAX_PULSE = 20, 300
REPORT_PERIODS = (7, 30, 90)
# Нижняя граница листания сводок: раньше дневников не бывает, а date.min
# ломает арифметику в клавиатуре.
DAY_NAV_MIN = date(2020, 1, 1)
SHIFT_CHOICES = (5, 15, 30)
# Для окончания шаги крупнее: пятиминутная ошибка здесь ниже собственного шума,
# а предлагать её — приглашать ковыряться. Реальная задержка это «задремал»
# (15–30) или «уснул, увлёкся» (45–90), и 45 набирается двумя нажатиями.
END_SHIFT_CHOICES = (15, 30, 60)

# Сетка «когда приняли лекарство»: шаг 15 минут вблизи (где важно для «когда
# следующий раз»), крупнее дальше. «только что» = точное время, остальное —
# примерно.
MED_WHEN_OFFSETS = (0, 15, 30, 45, 60, 90, 120, 180, 240, 360)
MED_NAME_BUTTONS = 3  # сколько недавних названий показываем прямо на карточке
MED_ALL_LIMIT = 50    # потолок полного справочника названий (кнопки Telegram)

# Ретроспективная запись. Состояние потока живёт в payload кнопки, а не в
# user_data: `bf:f:2:23:40:30` — 15 байт из 64 доступных, переживает рестарт,
# не перехватывает свободный текст (который должен оставаться заметкой к
# живому эпизоду) и валидируется шаг за шагом как любой недоверенный payload.
BACKFILL_OFFSETS = (0, 1, 2)          # сегодня / вчера / позавчера — решение владельца
BACKFILL_MINUTES = (0, 10, 20, 30, 40, 50)
SEVERITY_LEVELS = (1, 2, 3)
# Длина названия лекарства — в UTF-16-единицах, как и остальные лимиты
MED_NAME_LIMIT = 100
# Варианты для «не помню, когда отпустило»: ставим примерную длительность.
DURATION_CHOICES = (15, 30, 60, 120, 240, 480)

# Какие действия над эпизодом осмысленны только в определённом состоянии.
# NEEDS_ACTIVE строже NEEDS_OPEN: «✅ Отпустило» и сдвиг начала применимы лишь
# к эпизоду, который идёт прямо сейчас. На забытом эпизоде карточка намеренно
# не предлагает «отпустило сейчас» (это была бы ложь), но старая карточка из
# истории чата такую кнопку ещё содержит — иначе нажатие на неё записало бы
# ровную десятичасовую длительность как точную.
NEEDS_ACTIVE = frozenset({"e", "sh"})
NEEDS_OPEN = frozenset({"ap", "apm", "go", "unk"})
NEEDS_CLOSED = frozenset({"ro", "se", "sc"})

MENU_ACTIONS = ("btn_start", "btn_end", "btn_med", "btn_today", "btn_report", "btn_export")
# Текст кнопки приходит обратно от Telegram, поэтому разбираем его по всем
# языкам сразу: пользователь мог сменить язык, а старая плашка осталась висеть.
ACTION_BY_TEXT = {
    t(lang, key): key for key in MENU_ACTIONS for lang in i18n.SUPPORTED
}


# --- разбор callback_data --------------------------------------------------
# Payload кнопки управляется клиентом: модифицированный клиент может прислать
# что угодно. Поэтому каждое значение сверяется с тем набором, из которого его
# вообще могла бы предложить кнопка, а не просто приводится к int.

# Предел для идентификаторов: sqlite3 падает с OverflowError на числах,
# не влезающих в signed 64-bit, а это необработанное исключение на каждый
# подделанный payload.
SQLITE_MAX_INT = 2 ** 63 - 1


def _arg(parts: list[str], index: int, allowed=None) -> int | None:
    try:
        value = int(parts[index])
    except (IndexError, ValueError, TypeError):
        return None
    if abs(value) > SQLITE_MAX_INT:
        return None
    if allowed is not None and value not in allowed:
        return None
    return value


def _row_id(parts: list[str], index: int = 1) -> int | None:
    """Идентификатор строки из payload: только положительный и влезающий в БД."""
    value = _arg(parts, index)
    if value is None or value <= 0:
        return None
    return value


def _episode_id(parts: list[str]) -> int | None:
    if len(parts) < 2 or not parts[1].isdecimal():
        return None
    return _row_id(parts)


# --- язык ------------------------------------------------------------------

def _lang(update: Update) -> str:
    """Язык пользователя: явный выбор → профиль Telegram → DEFAULT_LANG.

    Разрешённый язык запоминаем при первом обращении, чтобы напоминания (у них
    нет апдейта с профилем) говорили на том же языке.
    """
    user = update.effective_user
    if user is None:
        return config.DEFAULT_LANG
    stored = db.get_lang(user.id)
    lang = i18n.resolve(stored, user.language_code, config.DEFAULT_LANG)
    if stored is None:
        db.set_lang(user.id, lang)
    return lang


def _job_lang(user_id: int) -> str:
    return i18n.resolve(db.get_lang(user_id), None, config.DEFAULT_LANG)


# --- клавиатуры ------------------------------------------------------------

def main_keyboard(has_open: bool, lang: str) -> ReplyKeyboardMarkup:
    """Нижняя плашка. Главная кнопка меняется по состоянию: начать / закрыть.

    «📍 Место» — единственная кнопка, которая обязана жить здесь: запросить
    геопозицию умеет только reply-клавиатура, в инлайновой такого нет.
    Выгрузка с плашки убрана: она есть командой /export и кнопкой внутри
    отчёта, а три пути к одному и тому же — лишний ряд на экране.
    """
    return ReplyKeyboardMarkup(
        [
            [t(lang, "btn_end" if has_open else "btn_start")],
            [t(lang, "btn_med"), KeyboardButton(t(lang, "btn_place"),
                                                request_location=True)],
            [t(lang, "btn_today"), t(lang, "btn_report")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def duration_rows(ep: db.Episode, lang: str) -> list[list[InlineKeyboardButton]]:
    buttons = [
        InlineKeyboardButton(
            report.human_duration(timedelta(minutes=m), lang), callback_data=f"ap:{ep.id}:{m}"
        )
        for m in DURATION_CHOICES
    ]
    return [
        buttons[:3],
        buttons[3:],
        [
            InlineKeyboardButton(t(lang, "btn_still_on"), callback_data=f"go:{ep.id}"),
            InlineKeyboardButton(t(lang, "btn_dunno"), callback_data=f"unk:{ep.id}"),
        ],
    ]


def remind_keyboard(ep: db.Episode, lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(t(lang, "btn_end"), callback_data=f"e:{ep.id}"),
            InlineKeyboardButton(t(lang, "btn_still_on"), callback_data=f"go:{ep.id}"),
        ],
        [InlineKeyboardButton(t(lang, "btn_dont_remember"), callback_data=f"apm:{ep.id}")],
    ])


def card_keyboard(ep: db.Episode, lang: str) -> InlineKeyboardMarkup:
    severity = vocab.severity(lang)
    plain = vocab.severity_plain(lang)
    rows = [
        [
            # У выбранной оценки лицо заменяется ТОЧКОЙ, а не галочкой: галка
            # на «средне» стояла рядом с «✅ Отпустило», и это две галки с
            # разными смыслами на одной карточке — владелец увидел тот же
            # конфликт, что и с рядом «✅ −N». Теперь грамматика значков
            # одна на весь продукт: ✅ = «отпустило/закрыто/сохранено»,
            # ● = «выбрано». Замена лица (а не префикс) сохранена — она
            # короче и заметнее, лицо исчезает.
            InlineKeyboardButton(
                f"● {plain[level]}" if ep.severity == level else severity[level],
                callback_data=f"s:{ep.id}:{level}",
            )
            for level in SEVERITY_LEVELS
        ],
        [
            InlineKeyboardButton(
                t(lang, "btn_pulse") + (f": {ep.pulse}" if ep.pulse else ""),
                callback_data=f"p:{ep.id}",
            ),
            InlineKeyboardButton(
                # Без бейджа «заполнено»: текст карточки прямо над кнопкой и
                # так показывает заметку, а галка здесь была третьим смыслом
                # ✅ на одном экране.
                t(lang, "btn_note"),
                callback_data=f"n:{ep.id}",
            ),
        ],
    ]

    # Ряд состояния — единственный, который меняется. Остальные стоят на месте:
    # карточка живёт одна на эпизод и обновляется сама, человек к ней
    # ВОЗВРАЩАЕТСЯ, а значит её текст и кнопки должны помещаться на экране
    # вместе. Семь рядов под восемью строками текста на телефон не влезали.
    if ep.needs_end(config.STALE_AFTER_MIN):
        # Подпись уже была правильной для той кнопки, которой притворялась;
        # раньше она висела на пустом обработчике и не делала ничего — худшее,
        # что может быть на карточке у пожилого человека.
        rows.append([InlineKeyboardButton(t(lang, "btn_how_long"),
                                          callback_data=f"dp:{ep.id}")])
    elif ep.is_open:
        rows.append([InlineKeyboardButton(t(lang, "btn_end"), callback_data=f"e:{ep.id}")])
    elif _can_shift_end(ep):
        # Один самоназывающийся вопрос вместо ряда «✅ −15/−30/−60» и
        # «↩️ Ещё не отпустило» по отдельности: владелец увидел на карточке
        # «кучу зелёных галок» — ✅ сдвигов сталкивался с ✅ выбранной тяжести,
        # а «не отпустило» посередине спорило с ними. Это ОДИН вопрос («когда
        # на самом деле отпустило?») с разными ответами, и живёт он за одной
        # кнопкой, которая называет себя сама — обнаружимость не страдает.
        rows.append([InlineKeyboardButton(t(lang, "btn_end_when"),
                                          callback_data=f"sc:{ep.id}")])

    rows.append([
        InlineKeyboardButton(t(lang, "btn_refine"), callback_data=f"rf:{ep.id}"),
        InlineKeyboardButton(t(lang, "btn_delete"), callback_data=f"d:{ep.id}"),
    ])
    return InlineKeyboardMarkup(rows)


def refine_keyboard(ep: db.Episode, lang: str) -> InlineKeyboardMarkup:
    """Панель уточнений: то, что не нужно в момент приступа.

    Открывается подменой ТОЛЬКО клавиатуры: текст карточки остаётся на месте,
    человек не теряет из виду, какой эпизод правит и что уже заполнено.
    Выход — наверху, как в меню симптомов, иначе он уезжает за край экрана.
    """
    rows = [
        [InlineKeyboardButton(t(lang, "btn_card"), callback_data=f"c:{ep.id}")],
        [
            InlineKeyboardButton(
                t(lang, "btn_symptoms") + (f" {len(ep.symptoms)}" if ep.symptoms else ""),
                callback_data=f"m:{ep.id}:sym",
            ),
            InlineKeyboardButton(
                t(lang, "btn_triggers") + (f" {len(ep.triggers)}" if ep.triggers else ""),
                callback_data=f"m:{ep.id}:trg",
            ),
        ],
    ]
    if ep.is_open:
        # Внутри панели клавиатура стоит на месте, а текст карточки над ней
        # меняет «Начало: …» — накопить два-три нажатия становится понятно.
        rows.append([
            InlineKeyboardButton(t(lang, "btn_shift", minutes=m),
                                 callback_data=f"sh:{ep.id}:-{m}")
            for m in SHIFT_CHOICES
        ])
    if ep.lat is not None:
        rows.append([InlineKeyboardButton(t(lang, "btn_place_clear"),
                                          callback_data=f"pc:{ep.id}")])
    return InlineKeyboardMarkup(rows)


def end_when_panel(ep: db.Episode, lang: str) -> InlineKeyboardMarkup:
    """«Когда на самом деле отпустило?» — все ответы в одном месте.

    «Раньше на N» и «ещё не отпустило» — ответы на один и тот же вопрос,
    поэтому они соседи, а не разбросаны по карточке. Без значка ✅: он
    зарезервирован за «отпустило сейчас» и за отметкой выбранного.
    """
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t(lang, "btn_card"), callback_data=f"c:{ep.id}")],
        [
            InlineKeyboardButton(
                t(lang, "btn_minus",
                  dur=report.human_duration(timedelta(minutes=m), lang)),
                callback_data=f"se:{ep.id}:-{m}")
            for m in END_SHIFT_CHOICES
        ],
        [InlineKeyboardButton(t(lang, "btn_reopen"), callback_data=f"ro:{ep.id}")],
    ])


def duration_panel(ep: db.Episode, lang: str) -> InlineKeyboardMarkup:
    """Длительности плюс выход: без него подмена клавиатуры была бы тупиком."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(t(lang, "btn_card"), callback_data=f"c:{ep.id}")]]
        + duration_rows(ep, lang)
    )


def _toggle_keyboard(ep: db.Episode, kind: str, lang: str) -> InlineKeyboardMarkup:
    if kind == "sym":
        codes, vocabulary, prefix = vocab.SYMPTOM_CODES, vocab.symptoms(lang), "ts"
        chosen = ep.symptoms
    else:
        codes, vocabulary, prefix = vocab.TRIGGER_CODES, vocab.triggers(lang), "tt"
        chosen = ep.triggers
    # «Готово» сверху: список длинный (характер ритма + симптомы), и внизу
    # кнопка выхода уходит за пределы экрана — меню заменяет карточку на месте,
    # поэтому другого выхода, кроме этой кнопки, нет.
    # «Готово» возвращает в панель, а не на карточку: симптомы и причины —
    # самая частая пара, и ходить за второй через карточку незачем.
    rows = [[InlineKeyboardButton(t(lang, "btn_done"), callback_data=f"rf:{ep.id}")]]
    def button(code: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            ("● " if code in chosen else "") + vocabulary[code],
            callback_data=f"{prefix}:{ep.id}:{code}",
        )

    # Парные коды идут по два в ряд: пара — это один вопрос с двумя ответами,
    # и две колонки её именно так и изображают. Остальное по одному: список
    # симптомов не поиск, а подсказка памяти, и одна колонка даёт один путь
    # взгляда, где каждый пункт прочитан. Две приглашали бы пропускать.
    # Принадлежность к паре берём из vocab.PAIRED_CODES, а не из позиции:
    # перестановка кодов не должна молча ломать раскладку.
    placed: set[str] = set()
    for code in codes:
        if code in placed:
            continue
        pair = vocab.PAIRED_CODES.get(code) if kind == "sym" else None
        if pair and all(c in codes for c in pair):
            rows.append([button(c) for c in pair])
            placed.update(pair)
        else:
            rows.append([button(code)])
            placed.add(code)
    return InlineKeyboardMarkup(rows)


def _report_keyboard(active: int, lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                ("• " if days == active else "") + t(lang, "btn_period", days=days),
                callback_data=f"r:{days}",
            )
            for days in REPORT_PERIODS
        ],
        [InlineKeyboardButton(t(lang, "btn_csv"), callback_data="csv")],
    ])


def _day_keyboard(day: date, lang: str) -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton("⬅️ " + (day - timedelta(days=1)).strftime("%d.%m"),
                             callback_data=f"dn:{day - timedelta(days=1)}"),
    ]]
    if day < report.today_local():
        rows[0].append(
            InlineKeyboardButton((day + timedelta(days=1)).strftime("%d.%m") + " ➡️",
                                 callback_data=f"dn:{day + timedelta(days=1)}")
        )
    offset = (report.today_local() - day).days
    if offset in BACKFILL_OFFSETS:
        # Сводка — правильное место входа: человек утром видит, что ночного
        # приступа в списке нет, и кнопка рядом; день она уже знает, поэтому
        # шаг выбора дня пропускается. Окно то же, что у /earlier: владелец
        # решил, что глубже позавчера запись не нужна.
        rows.append([InlineKeyboardButton(t(lang, "btn_backfill"),
                                          callback_data=f"bf:n:{day}")])
    return InlineKeyboardMarkup(rows)


def _lang_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(i18n.LANG_NAMES[code], callback_data=f"lang:{code}")]
        for code in i18n.SUPPORTED
    ])


def _note_header(lang: str, status: str, base: str | None = None) -> str:
    header = base or t(lang, "note_saved")
    if status == db.NOTE_CHUNK_TRIMMED:
        header += "\n" + t(lang, "note_trimmed", n=db.MAX_NOTE_CHUNK)
    return header


async def _note_full(update: Update, context: ContextTypes.DEFAULT_TYPE,
                     ep: db.Episode | None, lang: str, text: str) -> None:
    """Места в заметке не осталось.

    Текст остаётся у бота, и первая кнопка его СОХРАНЯЕТ, вытесняя самое старое
    начало заметки. Предлагать «очистите всё и пришлите заново» единственным
    действием нельзя: человек, делающий ровно то, что написано, уничтожал
    записанные симптомы, а присланный заново текст уезжал в новый, выдуманный
    эпизод, потому что ожидание ввода к тому моменту уже снималось.
    """
    if ep is None:
        return
    context.user_data[f"note_overflow:{ep.id}"] = text
    await context.bot.send_message(
        update.effective_chat.id,
        t(lang, "note_full"),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(t(lang, "btn_note_push"), callback_data=f"nt:{ep.id}")],
            [InlineKeyboardButton(t(lang, "btn_clear_note"), callback_data=f"nc:{ep.id}")],
            [InlineKeyboardButton(t(lang, "btn_cancel"), callback_data="nx")],
        ]),
    )


# --- отправка длинных текстов ----------------------------------------------
# Лимит сообщения Telegram — 4096, причём считаются UTF-16-единицы, а не
# символы, поэтому берём запас. Сводка за день с десятком эпизодов или отчёт
# могут его перерасти, и тогда они не отправляются ВООБЩЕ — то есть ломаются
# насовсем. Длина заметки ограничена в db, это второй рубеж на выводе.
TG_TEXT_LIMIT = 3800


def _split(text: str, limit: int = TG_TEXT_LIMIT) -> list[str]:
    """Режет текст на части, влезающие в сообщение. Длина — в UTF-16-единицах.

    Слишком длинная строка не обрезается, а делится: терять хвост молча нельзя,
    это данные человека.
    """
    if i18n.utf16_len(text) <= limit:
        return [text]
    pieces: list[str] = []
    current: str | None = None
    for line in text.split("\n"):
        while i18n.utf16_len(line) > limit:
            head = i18n.trim_utf16(line, limit)
            if not head:  # лимит меньше одного символа — дальше не разрезать
                break
            pieces.append(head)
            line = line[len(head):]
        pieces.append(line)
    parts: list[str] = []
    for piece in pieces:
        if current is None:
            current = piece
        elif i18n.utf16_len(current) + i18n.utf16_len(piece) + 1 > limit:
            parts.append(current)
            current = piece
        else:
            current = f"{current}\n{piece}"
    if current is not None:
        parts.append(current)
    return parts


async def _send_text(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str,
                     reply_markup=None) -> None:
    """Отправляет текст, при необходимости частями. Клавиатура — к последней."""
    if not text:
        return
    chunks = _split(text)
    for index, chunk in enumerate(chunks):
        await context.bot.send_message(
            chat_id, chunk,
            reply_markup=reply_markup if index == len(chunks) - 1 else None,
        )


async def _edit_text(query, context, text: str, reply_markup=None) -> None:
    """Правит сообщение на месте; слишком длинный текст досылает новым."""
    if i18n.utf16_len(text) > TG_TEXT_LIMIT:
        # Не влезает в правку — досылаем новым, а у старого снимаем клавиатуру,
        # чтобы она не осталась активной и не вводила в заблуждение.
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await _send_text(context, query.message.chat_id, text, reply_markup)
        return
    try:
        await query.edit_message_text(text, reply_markup=reply_markup)
    except Exception as exc:
        if "not modified" not in str(exc):
            log.warning("Сообщение не обновилось: %s", exc)


# --- вспомогательное -------------------------------------------------------

def _uid(update: Update) -> int:
    return update.effective_user.id


def _is_private(update: Update) -> bool:
    """Приватный ли диалог. Апдейты без пользователя (посты в канале) не наши."""
    chat = update.effective_chat
    return (
        update.effective_user is not None
        and chat is not None
        and chat.type == Chat.PRIVATE
    )


def _throttled(context: ContextTypes.DEFAULT_TYPE, key: str, seconds: float) -> bool:
    """Простой предохранитель на пользователя: не чаще раза в N секунд.

    Бот публичный и однопроцессный: сценарий «тысяча нажатий в минуту» измерен
    и упирается не в этого человека, а в отзывчивость для всех остальных.
    """
    now = db.utcnow().timestamp()
    last = context.user_data.get(f"rl:{key}")
    # 0 <= : после шага часов назад метка оказывается в будущем, и без нижней
    # границы каждое действие считалось бы «слишком частым», пока время не
    # догонит метку.
    if last is not None and 0 <= now - last < seconds:
        return True
    context.user_data[f"rl:{key}"] = now
    return False


async def _can_add_episode(update: Update, context: ContextTypes.DEFAULT_TYPE,
                           user_id: int, lang: str) -> bool:
    """Можно ли создать ещё один эпизод. Объясняет отказ, а не молчит."""
    if db.count_episodes(user_id) >= config.MAX_EPISODES_PER_USER:
        await context.bot.send_message(
            update.effective_chat.id,
            t(lang, "too_many_episodes", n=config.MAX_EPISODES_PER_USER),
        )
        return False
    if db.count_open_episodes(user_id) >= config.MAX_OPEN_EPISODES:
        await context.bot.send_message(
            update.effective_chat.id,
            t(lang, "too_many_open", n=config.MAX_OPEN_EPISODES),
        )
        return False
    return True


def _backfill_day(raw: str) -> date | None:
    """День из payload: только ISO-дата в окне владельца (0..2 дня назад).

    Дата абсолютная, а не смещением: кнопка живёт в истории чата вечно, и
    «позавчера», нажатое через год, записывало бы не тот день. Абсолютная дата
    либо означает ровно тот день, который кнопка показывала, либо выпала из
    окна — и тогда отказ.
    """
    if len(raw) != 10 or not raw.isascii():
        return None
    try:
        day = date.fromisoformat(raw)
    except ValueError:
        return None
    if not 0 <= (report.today_local() - day).days <= max(BACKFILL_OFFSETS):
        return None
    return day


def _bf_int(parts: list[str], index: int, allowed) -> int | None:
    """Строгое число из bf-payload: только ASCII-цифры, без знаков и пробелов.

    int() принимает «+30», « 30 », «1_5» и арабские цифры — клавиатура такого
    не выпускает, значит это подделка, и ей положен отказ, а не терпимость.
    """
    raw = parts[index] if len(parts) > index else ""
    if not raw.isascii() or not raw.isdigit():
        return None
    value = int(raw)
    return value if value in allowed else None


def _backfill_start(day: date, hour: int, minute: int) -> datetime | None:
    """Момент начала из выбора кнопками. None, если он ещё не наступил.

    Будущее отклоняется, а не подрезается: клавиатуры будущих вариантов не
    предлагают, значит такой payload подделан или устарел за время раздумий.
    """
    start = datetime.combine(day, time(hour, minute), tzinfo=config.local_tz())
    start = start.astimezone(timezone.utc)
    if start > db.utcnow():
        return None
    return start


def _backfill_day_keyboard(lang: str) -> InlineKeyboardMarkup:
    today = report.today_local()
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(t(lang, "btn_day_today"),
                                 callback_data=f"bf:d:{today}"),
            InlineKeyboardButton(t(lang, "btn_day_yesterday"),
                                 callback_data=f"bf:d:{today - timedelta(days=1)}"),
        ],
        [InlineKeyboardButton(t(lang, "btn_day_before"),
                              callback_data=f"bf:d:{today - timedelta(days=2)}")],
        [InlineKeyboardButton(t(lang, "btn_cancel"), callback_data="bf:x")],
    ])


def _backfill_hour_screen(day: date, lang: str) -> tuple[str, InlineKeyboardMarkup]:
    text = t(lang, "back_ask_hour", date=report.date_day_month(day, lang))
    max_hour = 23
    if day == report.today_local():
        # Будущих часов сегодня не существует — и об этом сказано словами,
        # иначе человек, искавший вчерашние 23:00, решит, что сломано.
        max_hour = report.local(db.utcnow()).hour
        text += "\n" + t(lang, "back_today_hours_note")
    buttons = [
        InlineKeyboardButton(f"{h:02d}", callback_data=f"bf:h:{day}:{h}")
        for h in range(max_hour + 1)
    ]
    rows = [buttons[i:i + 4] for i in range(0, len(buttons), 4)]
    rows.append([InlineKeyboardButton(t(lang, "btn_back_step"), callback_data="bf:s")])
    return text, InlineKeyboardMarkup(rows)


def _backfill_minute_keyboard(day: date, hour: int, lang: str) -> InlineKeyboardMarkup:
    # На кнопке ПОЛНОЕ время («23:40», а не «:40»): выбор самопроверяемый,
    # человек читает готовый ответ — и подтверждающий экран не нужен.
    buttons = [
        InlineKeyboardButton(f"{hour:02d}:{m:02d}", callback_data=f"bf:m:{day}:{hour}:{m}")
        for m in BACKFILL_MINUTES
        if _backfill_start(day, hour, m) is not None
    ]
    rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    rows.append([InlineKeyboardButton(t(lang, "btn_back_step"),
                                      callback_data=f"bf:d:{day}")])
    return InlineKeyboardMarkup(rows)


def _backfill_duration_keyboard(day: date, hour: int, minute: int,
                                lang: str) -> InlineKeyboardMarkup:
    start = _backfill_start(day, hour, minute)
    now = db.utcnow()
    # Предлагаются только длительности, чей конец уже наступил: невозможное
    # состояние предотвращается, а не сообщается. «Не знаю» доступно всегда.
    buttons = [
        InlineKeyboardButton(report.human_duration(timedelta(minutes=m), lang),
                             callback_data=f"bf:f:{day}:{hour}:{minute}:{m}")
        for m in DURATION_CHOICES
        if start is not None and start + timedelta(minutes=m) <= now
    ]
    rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    rows.append([InlineKeyboardButton(t(lang, "btn_dunno"),
                                      callback_data=f"bf:f:{day}:{hour}:{minute}:x")])
    rows.append([InlineKeyboardButton(t(lang, "btn_back_step"),
                                      callback_data=f"bf:h:{day}:{hour}")])
    return InlineKeyboardMarkup(rows)


def _can_shift_end(ep: db.Episode) -> bool:
    """Окно правки окончания — то же, что у возврата в работу.

    Намеренно вызывает `_can_reopen`, а не повторяет условие: это одна семья
    исправлений с одним обоснованием (дальше человек уже не помнит), и два
    отдельных предиката неизбежно разъехались бы.
    """
    return _can_reopen(ep)


def _can_reopen(ep: db.Episode) -> bool:
    """Можно ли вернуть эпизод в работу: только если он закрыт недавно."""
    if ep.is_open or ep.ended_at is None:
        return False
    return (db.utcnow() - ep.ended_at) <= timedelta(minutes=config.RECENT_EPISODE_MIN)


def _active(user_id: int) -> db.Episode | None:
    """Эпизод, который идёт прямо сейчас (забытые не считаются)."""
    return db.active_episode(user_id, config.STALE_AFTER_MIN)


def _current_episode(user_id: int) -> db.Episode | None:
    """Эпизод, к которому относится свободный ввод: активный или только что закрытый."""
    ep = _active(user_id)
    if ep:
        return ep
    ep = db.last_episode(user_id)
    if ep and ep.ended_at:
        age = datetime.now(timezone.utc) - ep.ended_at
        if age <= timedelta(minutes=config.RECENT_EPISODE_MIN):
            return ep
    return None


async def _send_card(update: Update, context: ContextTypes.DEFAULT_TYPE,
                     ep: db.Episode | None, lang: str, header: str = "",
                     force_new: bool = False) -> None:
    """Отправляет карточку. ``ep`` может быть None: эпизод успели удалить между
    записью и отправкой — тогда отправлять нечего.

    ``force_new`` — переслать карточку ВНИЗ новым сообщением даже без заголовка:
    нужно, когда последним в чате оказалась служебная реплика, а в списке чатов
    должно быть видно, что эпизод идёт (превью = последнее сообщение).
    """
    if ep is None:
        return
    text = (header + "\n\n" if header else "") + report.episode_card(ep, lang)
    chat_id = update.effective_chat.id

    # Одна карточка на эпизод, а не растущая куча.
    if ep.card_msg:
        if not header and not force_new:
            # Нечего сообщать отдельно (например /last) — обновляем живую
            # карточку на месте и не плодим сообщений вообще.
            try:
                await context.bot.edit_message_text(
                    text, chat_id=chat_id, message_id=ep.card_msg,
                    reply_markup=card_keyboard(ep, lang),
                )
                if ep.is_open and not ep.end_unknown:
                    _schedule_tick(context.job_queue, ep.user_id, ep.id, chat_id)
                return
            except Exception as exc:
                if "not modified" in str(exc):
                    return
        # Есть что сообщить — карточка уезжает вниз новым сообщением, а прежнюю
        # убираем совсем: иначе в чате остаётся несколько карточек одного
        # эпизода с замершей длительностью, и непонятно, какая настоящая.
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=ep.card_msg)
        except Exception:
            # Старше 48 часов или уже удалена — тогда хотя бы снимаем кнопки,
            # чтобы по ним нельзя было действовать.
            try:
                await context.bot.edit_message_reply_markup(
                    chat_id=chat_id, message_id=ep.card_msg, reply_markup=None
                )
            except Exception:
                pass
    # Длинная карточка уходит частями; кнопки и id для последующих правок —
    # у последней, рядом с актуальным состоянием.
    chunks = _split(text)
    sent = None
    for index, chunk in enumerate(chunks):
        sent = await context.bot.send_message(
            chat_id, chunk,
            reply_markup=card_keyboard(ep, lang) if index == len(chunks) - 1 else None,
        )
    db.set_card_msg(ep.user_id, ep.id, sent.message_id)
    # Эпизоду с объявленно неизвестным концом «идёт уже N минут» не рисуем —
    # и обновлять там нечего.
    if ep.is_open and not ep.end_unknown:
        _schedule_tick(context.job_queue, ep.user_id, ep.id, chat_id)


async def _refresh(query, ep: db.Episode | None, lang: str,
                   context: ContextTypes.DEFAULT_TYPE | None = None) -> None:
    """Перерисовывает карточку на месте. Повторное нажатие той же кнопки даёт
    тот же текст — Telegram отвечает 'message is not modified', это не ошибка.

    ``ep`` может быть None: эпизод успели удалить между выборкой и перерисовкой.
    """
    if ep is None:
        return
    try:
        await query.edit_message_text(
            report.episode_card(ep, lang), reply_markup=card_keyboard(ep, lang)
        )
    except Exception as exc:
        if "not modified" in str(exc):
            return
        log.warning("Не удалось обновить карточку #%s: %s", ep.id, exc)
        # Сообщение старше 48 часов правке не поддаётся. Молча проглотить
        # нельзя: всплывающая подсказка уже сказала «обновил карточку», и
        # человек остался бы с неработающей кнопкой и без объяснений.
        if context is not None:
            await _send_text(context, query.message.chat_id,
                             report.episode_card(ep, lang), card_keyboard(ep, lang))


def _window() -> timedelta:
    return timedelta(minutes=config.EPISODE_WINDOW_MIN)


def _until(due: datetime) -> timedelta:
    """Сколько ждать до срока, но не меньше пяти секунд.

    Считаем в секундах: прежняя арифметика в минутах отбрасывала дробную часть
    (`int(1.9)` → 1), и после рестарта напоминание срабатывало почти на минуту
    раньше срока — человек получал «идёт уже 29 мин» на тридцатиминутном окне.
    Пол в пять секунд нужен для уже просроченных задач: сработать надо сразу,
    но не внутри самого старта.
    """
    return max(timedelta(seconds=5), due - db.utcnow())


def _job_name(user_id: int, episode_id: int) -> str:
    return f"remind:{user_id}:{episode_id}"


def _cancel_reminder(job_queue, user_id: int, episode_id: int) -> None:
    """Снимает ТОЛЬКО напоминание.

    Живое обновление карточки здесь трогать нельзя: `_schedule_reminder`
    начинается с отмены прежнего напоминания, и если заодно снимать обновление,
    то планирование напоминания убивало бы только что созданную живую карточку.
    Для «эпизод перестал идти» есть `_stop_live`.
    """
    if job_queue is None:
        return
    for job in job_queue.get_jobs_by_name(_job_name(user_id, episode_id)):
        job.schedule_removal()


def _stop_live(job_queue, user_id: int, episode_id: int) -> None:
    """Эпизод перестал идти: не нужны ни напоминание, ни обновление карточки."""
    _cancel_reminder(job_queue, user_id, episode_id)
    _cancel_tick(job_queue, user_id, episode_id)


def _schedule_reminder(job_queue, user_id: int, episode_id: int,
                       delay: timedelta, final: bool = False) -> None:
    """Запланировать вопрос «отпустило?» через N минут.

    ``final=True`` — это уже контрольный заход: если и к нему ответа не будет,
    эпизод останется с неотмеченным окончанием. Повторных вопросов нет: бот
    спрашивает один раз, а продлевает эпизод только сам человек кнопкой
    «⏳ Ещё идёт». Имя job одно на эпизод, поэтому новый вызов заменяет прежний.
    """
    if job_queue is None or delay.total_seconds() <= 0:
        return
    _cancel_reminder(job_queue, user_id, episode_id)
    job_queue.run_once(
        _remind,
        when=delay,
        # misfire_grace_time=None — иначе APScheduler берёт свой дефолт в ОДНУ
        # СЕКУНДУ и просто выбрасывает задачу, опоздавшую больше: цикл событий
        # занят выгрузкой CSV, контейнер приостановлен, шаг NTP — и вопрос
        # «отпустило?» не задаётся вообще, а перепланировать его уже некому.
        # Опоздавшее напоминание не страшно: _remind заново смотрит состояние
        # эпизода и сам решает, спрашивать или фиксировать как незавершённый.
        job_kwargs={"misfire_grace_time": None},
        # Бот личный, диалог приватный: chat_id совпадает с user_id.
        chat_id=user_id,
        user_id=user_id,
        data={"episode_id": episode_id, "final": final},
        name=_job_name(user_id, episode_id),
    )


def _med_offset_buttons(med: db.Med, lang: str) -> list[InlineKeyboardButton]:
    """Кнопки сдвига времени приёма. «только что» = точное время (approx=0),
    поэтому у свежей карточки оно помечено «● » (точка = выбранное, как у тяжести);
    при примерном времени не помечено ничего — какое смещение выбирали, из
    уехавшего «сейчас» уже не восстановить."""
    now_selected = not med.approx
    out = []
    for m in MED_WHEN_OFFSETS:
        if m == 0:
            label = t(lang, "btn_med_now")
            if now_selected:
                label = "● " + label
        else:
            label = t(lang, "btn_med_ago",
                      dur=report.human_duration(timedelta(minutes=m), lang))
        out.append(InlineKeyboardButton(label, callback_data=f"mc:set:{med.id}:{m}"))
    return out


def _med_card_keyboard(med: db.Med, lang: str, names: "list[str] | tuple" = (),
                       has_more: bool = False) -> InlineKeyboardMarkup:
    """Карточка приёма: сдвиги времени и недавние названия стоят ПРЯМО на ней —
    без захода в подменю. `names` — снимок показанных названий (его индексы
    адресует `mc:pick`), сохранить снимок обязан вызывающий (`_med_card_markup`).
    `has_more` → кнопка «📋 Все лекарства» (полный справочник из истории)."""
    offs = _med_offset_buttons(med, lang)
    rows = [offs[i:i + 3] for i in range(0, len(offs), 3)]
    if names:
        rows.append([InlineKeyboardButton(n, callback_data=f"mc:pick:{med.id}:{i}")
                     for i, n in enumerate(names)])
        if has_more:
            # Свой ряд, во всю ширину: подпись длинная, в паре обрезалась бы.
            rows.append([InlineKeyboardButton(t(lang, "btn_med_all"),
                                              callback_data=f"mc:all:{med.id}")])
        rows.append([InlineKeyboardButton(t(lang, "btn_med_type"),
                                          callback_data=f"mc:type:{med.id}")])
    else:
        # Истории ещё нет — одна кнопка «дать название» (ручной ввод).
        rows.append([InlineKeyboardButton(t(lang, "btn_med_name"),
                                          callback_data=f"mc:type:{med.id}")])
    rows.append([InlineKeyboardButton(t(lang, "btn_delete"),
                                      callback_data=f"mc:del:{med.id}")])
    return InlineKeyboardMarkup(rows)


def _med_card_markup(context: ContextTypes.DEFAULT_TYPE, med: db.Med,
                     lang: str) -> InlineKeyboardMarkup:
    """Клавиатура карточки + снимок названий в user_data. Снимок и показанные
    кнопки строятся ВМЕСТЕ на каждой перерисовке (в том числе в тике), поэтому
    индекс из `mc:pick` всегда адресует то название, что видно сейчас, — тот же
    приём от гонки порядка, что был у подменю, но на живой карточке. Берём на
    одно название больше, чем показываем: лишнее означает «есть ещё» → кнопка
    «📋 Все лекарства»."""
    names = db.recent_med_names(med.user_id, MED_NAME_BUTTONS + 1)
    shown = names[:MED_NAME_BUTTONS]
    if context.user_data is not None:
        context.user_data[f"medopts:{med.id}"] = list(shown)
    return _med_card_keyboard(med, lang, shown, has_more=len(names) > MED_NAME_BUTTONS)


def _med_all_keyboard(med: db.Med, lang: str, names: list[str]) -> InlineKeyboardMarkup:
    """Полный справочник названий из истории — по одному в ряд (их читают, а не
    ищут), плюс ручной ввод нового и возврат к карточке."""
    rows = [[InlineKeyboardButton(n, callback_data=f"mc:pick:{med.id}:{i}")]
            for i, n in enumerate(names)]
    rows.append([InlineKeyboardButton(t(lang, "btn_med_type"), callback_data=f"mc:type:{med.id}")])
    rows.append([InlineKeyboardButton(t(lang, "btn_med_back"), callback_data=f"mc:back:{med.id}")])
    return InlineKeyboardMarkup(rows)


def _med_when_keyboard(med: db.Med, lang: str) -> InlineKeyboardMarkup:
    """Старое подменю «когда» — осталось для карточек, уже висящих в истории
    чата с кнопкой «⏱ Когда». Новые карточки показывают сдвиги сразу."""
    offs = _med_offset_buttons(med, lang)
    rows = [offs[i:i + 3] for i in range(0, len(offs), 3)]
    rows.append([InlineKeyboardButton(t(lang, "btn_med_back"), callback_data=f"mc:back:{med.id}")])
    return InlineKeyboardMarkup(rows)


def _med_name_keyboard(med: db.Med, lang: str, names: list[str]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(n, callback_data=f"mc:pick:{med.id}:{i}")]
            for i, n in enumerate(names)]
    rows.append([InlineKeyboardButton(t(lang, "btn_med_type"), callback_data=f"mc:type:{med.id}")])
    rows.append([InlineKeyboardButton(t(lang, "btn_med_back"), callback_data=f"mc:back:{med.id}")])
    return InlineKeyboardMarkup(rows)


async def _send_med_card(update: Update, context: ContextTypes.DEFAULT_TYPE,
                         med: db.Med | None, lang: str, force_new: bool = False) -> None:
    """Отправляет/обновляет живую карточку приёма — одна на запись."""
    if med is None:
        return
    chat_id = update.effective_chat.id
    text = report.med_card(med, lang)
    if med.card_msg and not force_new:
        try:
            await context.bot.edit_message_text(
                text, chat_id=chat_id, message_id=med.card_msg,
                reply_markup=_med_card_markup(context, med, lang),
            )
            _schedule_med_tick(context.job_queue, med.user_id, med.id, chat_id)
            return
        except Exception as exc:
            if "not modified" in str(exc):
                return
    if med.card_msg:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=med.card_msg)
        except Exception:
            try:
                await context.bot.edit_message_reply_markup(
                    chat_id=chat_id, message_id=med.card_msg, reply_markup=None)
            except Exception:
                pass
    sent = await context.bot.send_message(
        chat_id, text, reply_markup=_med_card_markup(context, med, lang))
    db.set_med_card_msg(med.user_id, med.id, sent.message_id)
    _schedule_med_tick(context.job_queue, med.user_id, med.id, chat_id)


def _med_tick_name(user_id: int, med_id: int) -> str:
    return f"mtick:{user_id}:{med_id}"


def _cancel_med_tick(job_queue, user_id: int, med_id: int) -> None:
    if job_queue is None:
        return
    for job in job_queue.get_jobs_by_name(_med_tick_name(user_id, med_id)):
        job.schedule_removal()


def _schedule_med_tick(job_queue, user_id: int, med_id: int, chat_id: int) -> None:
    if job_queue is None or config.CARD_TICK_SEC <= 0:
        return
    _cancel_med_tick(job_queue, user_id, med_id)
    job_queue.run_repeating(
        _med_tick,
        interval=timedelta(seconds=config.CARD_TICK_SEC),
        first=timedelta(seconds=config.CARD_TICK_SEC),
        chat_id=chat_id, user_id=user_id, data={"med_id": med_id},
        name=_med_tick_name(user_id, med_id),
        job_kwargs={"misfire_grace_time": None, "coalesce": True},
    )


async def _med_tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обновляет «принято N назад». Останавливается через MED_TICK_MAX_H."""
    job = context.job
    user_id, chat_id = job.user_id, job.chat_id
    med = db.get_med(user_id, job.data["med_id"])
    if med is None or med.card_msg is None:
        job.schedule_removal()
        return
    # Возрастной предел проверяется ПЕРВЫМ: иначе брошенная открытая панель
    # (ранний возврат ниже) оставляла бы тик бессмертным до рестарта.
    if med.since() > timedelta(hours=config.MED_TICK_MAX_H):
        job.schedule_removal()
        return
    # Открыта панель (когда/название/удаление) — не перерисовываем, чтобы не
    # затереть её; тик продолжает жить до возрастного предела выше.
    if (context.user_data or {}).get(f"medpanel:{med.id}"):
        return
    lang = _job_lang(user_id)
    try:
        await context.bot.edit_message_text(
            report.med_card(med, lang), chat_id=chat_id, message_id=med.card_msg,
            reply_markup=_med_card_markup(context, med, lang),
        )
    except Exception as exc:
        if "not modified" not in str(exc):
            log.warning("Карточка приёма #%s больше не правится: %s", med.id, exc)
            # Сообщение не правится (старше 48 ч) — снимаем card_msg, чтобы
            # post_init не воскрешал заведомо мёртвый тик после рестарта.
            db.set_med_card_msg(user_id, med.id, None)
            job.schedule_removal()
            return


def _tick_name(user_id: int, episode_id: int) -> str:
    return f"tick:{user_id}:{episode_id}"


def _cancel_tick(job_queue, user_id: int, episode_id: int) -> None:
    if job_queue is None:
        return
    for job in job_queue.get_jobs_by_name(_tick_name(user_id, episode_id)):
        job.schedule_removal()


def _schedule_tick(job_queue, user_id: int, episode_id: int, chat_id: int) -> None:
    """Обновлять карточку идущего эпизода раз в минуту.

    Человек видит, сколько уже длится приступ, не трогая телефон. Раз в минуту,
    а не чаще: секунды в такой записи не нужны, а правка сообщения — это вызов
    API, и при десятке пользователей частить незачем.
    """
    if job_queue is None or config.CARD_TICK_SEC <= 0:
        return
    _cancel_tick(job_queue, user_id, episode_id)
    job_queue.run_repeating(
        _tick,
        interval=timedelta(seconds=config.CARD_TICK_SEC),
        first=timedelta(seconds=config.CARD_TICK_SEC),
        chat_id=chat_id,
        user_id=user_id,
        data={"episode_id": episode_id},
        name=_tick_name(user_id, episode_id),
        job_kwargs={"misfire_grace_time": None, "coalesce": True},
    )


async def _tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Перерисовывает живую карточку. Сам себя останавливает, когда пора."""
    job = context.job
    user_id, chat_id = job.user_id, job.chat_id
    ep = db.get_episode(user_id, job.data["episode_id"])
    lang = _job_lang(user_id)
    if ep is None or not ep.is_open or ep.card_msg is None:
        job.schedule_removal()
        return
    # Если открыта панель, перерисовываем ЕЁ: иначе ежеминутное обновление
    # затирало бы её обычной клавиатурой прямо под пальцем.
    # context.user_data доступен в задаче, потому что она создана с user_id
    # (CallbackContext.from_job прокидывает его) — лезть в application не нужно.
    panel = (context.user_data or {}).get(f"panel:{ep.id}")
    if panel == "refine":
        markup = refine_keyboard(ep, lang)
    elif panel == "duration":
        markup = duration_panel(ep, lang)
    else:
        markup = card_keyboard(ep, lang)
    # Перерисовываем В ЛЮБОМ случае, в том числе когда эпизод ТОЛЬКО ЧТО стал
    # забытым: это тот самый последний кадр, который меняет «идёт, уже N мин»
    # на «⚠️ окончание не отмечено» с кнопкой «Сколько длилось». Раньше тик
    # останавливался ДО перерисовки — и карточка застывала на «идёт» навсегда.
    try:
        await context.bot.edit_message_text(
            report.episode_card(ep, lang),
            chat_id=chat_id, message_id=ep.card_msg,
            reply_markup=markup,
        )
    except Exception as exc:
        if "not modified" not in str(exc):
            log.warning("Живая карточка #%s больше не правится: %s", ep.id, exc)
            job.schedule_removal()
            return
    # Эпизод забыт или слишком стар — обновлять больше нечего, кадр был последним.
    if ep.needs_end(config.STALE_AFTER_MIN) or ep.duration() > timedelta(hours=47):
        job.schedule_removal()
        if ep.is_stale(config.STALE_AFTER_MIN):
            # Тик расклеил карточку сам (без напоминания) — значит он же и
            # возвращает плашку, один раз.
            await _notify_stale_once(context.bot, user_id,
                                     db.get_episode(user_id, ep.id), lang)


async def _notify_stale_once(bot_obj, user_id: int, ep: db.Episode,
                            lang: str) -> None:
    """Один раз при переходе эпизода в «забыт» вернуть нижнее меню.

    Карточка правится на месте (инлайн), а reply-клавиатура меняется только с
    новым сообщением — поэтому плашку обновляем отдельной короткой репликой.
    Но ТОЛЬКО если:
    - это ещё не показывали (`stale_shown`): иначе сообщение прилетало на каждом
      рестарте, а пользователь редеплоил много раз;
    - СЕЙЧАС нет активного эпизода: если он есть, плашка и так верная
      («✅ Отпустило»), а текст «начнётся снова — жмите Аритмия» ей противоречит.
    """
    if ep.stale_shown:
        return
    db.mark_stale_shown(user_id, ep.id)
    if _active(user_id) is not None:
        return
    await bot_obj.send_message(
        user_id,
        t(lang, "plate_after_stale", btn_start=t(lang, "btn_start")),
        reply_markup=main_keyboard(False, lang),
    )


async def _remind(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Эпизод всё ещё открыт — спрашиваем, что с ним."""
    job = context.job
    user_id, chat_id = job.user_id, job.chat_id
    ep = db.get_episode(user_id, job.data["episode_id"])
    if ep is None or not ep.is_open:
        return
    lang = _job_lang(user_id)
    # Этап берём из БД, а не из данных задачи: задачи живут в памяти, и после
    # рестарта контейнера бот спрашивал «отпустило?» заново — а при краш-луме
    # примерно раз в минуту.
    final = bool(job.data.get("final")) or ep.remind_stage >= 1
    stale = ep.is_stale(config.STALE_AFTER_MIN)
    if final and not stale:
        # STALE_AFTER_MIN задан вручную больше окна: ещё рано называть эпизод
        # незавершённым — дождёмся фактического порога, чтобы слова бота
        # совпадали с тем, что показывают карточка и отчёт.
        anchor = ep.confirmed_at or ep.started_at
        due = anchor + timedelta(minutes=config.STALE_AFTER_MIN)
        _schedule_reminder(context.job_queue, user_id, ep.id, _until(due), final=True)
        return
    # Тик и напоминание — больше не два владельца одной карточки. Как только
    # напоминание берётся за карточку, тик снимается, иначе через минуту он
    # перерисовал бы её обычной клавиатурой поверх меню вопроса — именно это
    # мерцание («показалось сообщение, но пропало меню») и видел пользователь.
    _cancel_tick(context.job_queue, user_id, ep.id)
    if context.user_data is not None:
        context.user_data.pop(f"panel:{ep.id}", None)

    if final or stale:
        # Эпизод стал забытым. Правим карточку НА МЕСТЕ в ту же стальную форму,
        # что рисуют тик и post_init — одна карточка, один вид, без разрыва и
        # без второго сообщения. Окно уже уведомило 30 мин назад; молчание и
        # есть ответ, и тихий переход ему соответствует.
        edited_in_place = False
        if ep.card_msg:
            try:
                await context.bot.edit_message_text(
                    report.episode_card(ep, lang),
                    chat_id=chat_id, message_id=ep.card_msg,
                    reply_markup=card_keyboard(ep, lang),
                )
                edited_in_place = True
            except Exception as exc:
                if "not modified" in str(exc):
                    edited_in_place = True
        if not edited_in_place:
            # Карточки нет или её уже нельзя править — присылаем стальную заново.
            sent = await context.bot.send_message(
                chat_id,
                report.episode_card(ep, lang),
                reply_markup=card_keyboard(ep, lang),
            )
            db.set_card_msg(user_id, ep.id, sent.message_id)
        # И один раз возвращаем нижнее меню (если активного эпизода нет).
        await _notify_stale_once(context.bot, user_id,
                                 db.get_episode(user_id, ep.id), lang)
        return

    # Окно: это и есть уведомление. Старую карточку удаляем, вопрос приходит
    # новым сообщением (новое сообщение = пуш), и становится карточкой.
    if ep.card_msg:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=ep.card_msg)
        except Exception:
            try:
                await context.bot.edit_message_reply_markup(
                    chat_id=chat_id, message_id=ep.card_msg, reply_markup=None)
            except Exception:
                pass
    sent = await context.bot.send_message(
        chat_id,
        report.episode_card(ep, lang) + "\n\n"
        + t(lang, "remind_ask", minutes=config.EPISODE_WINDOW_MIN),
        reply_markup=remind_keyboard(ep, lang),
    )
    db.set_card_msg(user_id, ep.id, sent.message_id)
    db.set_remind_stage(user_id, ep.id, 1)
    # Тик не возобновляем: человека спросили, и до ответа «уже N мин» не
    # обновляем. «⏳ Ещё идёт» вернёт живое обновление, контрольный заход
    # учтёт молчание.
    _schedule_reminder(context.job_queue, user_id, ep.id, _window(), final=True)


async def _mention_forgotten(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE,
                             lang: str, skip_id: int | None = None) -> None:
    """Напоминает про самый свежий эпизод без отметки окончания."""
    stale = [
        ep for ep in db.stale_episodes(user_id, config.STALE_AFTER_MIN) if ep.id != skip_id
    ]
    if not stale:
        return
    ep = stale[0]
    tail = t(lang, "forgotten_tail", n=len(stale) - 1) if len(stale) > 1 else ""
    await context.bot.send_message(
        chat_id,
        t(lang, "forgotten_mention", id=ep.id, day=report._day_prefix(ep.started_at, lang),
          time=report.hhmm(ep.started_at), tail=tail),
        reply_markup=InlineKeyboardMarkup(duration_rows(ep, lang)),
    )


# --- команды и кнопки -----------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = _lang(update)
    context.user_data.pop("await", None)
    await update.message.reply_text(
        t(lang, "start",
          btn_start=t(lang, "btn_start"), btn_end=t(lang, "btn_end"),
          btn_today=t(lang, "btn_today"), btn_report=t(lang, "btn_report"),
          btn_export=t(lang, "btn_export")),
        reply_markup=main_keyboard(_active(_uid(update)) is not None, lang),
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = _lang(update)
    await update.message.reply_text(
        t(lang, "help", tz=config.TZ_NAME),
        reply_markup=main_keyboard(_active(_uid(update)) is not None, lang),
    )


async def cmd_lang(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        t(_lang(update), "lang_prompt"), reply_markup=_lang_keyboard()
    )


async def cmd_forget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Удаление всех своих данных. В два шага: это необратимо."""
    user_id = _uid(update)
    lang = _lang(update)
    episodes = db.count_episodes(user_id)
    meds = len(db.all_meds(user_id))
    if not episodes and not meds:
        await update.message.reply_text(t(lang, "forget_empty"))
        return
    # Одноразовый токен: инлайн-кнопки живут в истории чата вечно, и без него
    # одно случайное нажатие на старое подтверждение стирало весь дневник
    # безвозвратно. Для эпизодов этот инвариант уже соблюдался, а здесь — нет.
    token = secrets.token_urlsafe(6)
    context.user_data["forget_token"] = token
    await update.message.reply_text(
        t(lang, "forget_confirm", episodes=episodes, meds=meds),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(t(lang, "btn_forget_yes"), callback_data=f"fy:{token}")],
            [InlineKeyboardButton(t(lang, "btn_cancel"), callback_data="nx")],
        ]),
    )


async def cmd_earlier(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Запись приступа, который уже прошёл. Не на плашке и не рядом с «⚡️»:
    перепутать «сейчас» со «вчера ночью» — худшая опечатка в продукте."""
    lang = _lang(update)
    if not await _can_add_episode(update, context, _uid(update), lang):
        return
    await update.message.reply_text(
        t(lang, "back_intro"), reply_markup=_backfill_day_keyboard(lang)
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = _lang(update)
    context.user_data.pop("await", None)
    context.user_data.pop("pending_note", None)
    ep = _current_episode(_uid(update))
    if ep is not None:
        # Не оставляем «Отменил ввод.» последней репликой: пока идёт эпизод,
        # последней в чате должна быть его карточка — тогда в списке чатов
        # видно «идёт, уже N мин», а не служебное сообщение. Карточку шлём
        # заново вниз (force_new), без текста.
        await _send_card(update, context, ep, lang, force_new=True)
        return
    # Эпизода нет — показывать нечего, короткая реплика с правильной плашкой.
    await update.message.reply_text(
        t(lang, "nothing_to_cancel"),
        reply_markup=main_keyboard(False, lang),
    )


async def action_start_episode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = _uid(update)
    lang = _lang(update)
    if _throttled(context, "episode", config.MIN_ACTION_INTERVAL_SEC):
        await update.message.reply_text(t(lang, "too_fast"))
        return
    context.user_data.pop("await", None)
    existing = _active(user_id)
    if existing:
        await update.message.reply_text(
            t(lang, "ep_already", id=existing.id, time=report.hhmm(existing.started_at),
              btn_end=t(lang, "btn_end")),
            reply_markup=main_keyboard(True, lang),
        )
        await _send_card(update, context, existing, lang)
        return
    # Время — из сообщения, а не из момента обработки: нажатие, доставленное с
    # задержкой (или накопившееся за время простоя бота), сохранит своё время.
    if not await _can_add_episode(update, context, user_id, lang):
        return
    ep = db.start_episode(user_id, started_at=update.message.date)
    await update.message.reply_text(
        t(lang, "ep_logged", id=ep.id, time=report.hhmm(ep.started_at)),
        reply_markup=main_keyboard(True, lang),
    )
    await _send_card(update, context, ep, lang)
    _schedule_reminder(context.job_queue, user_id, ep.id, _window())
    # Напоминание о старых незакрытых эпизодах здесь НЕ показываем: человеку
    # сейчас плохо, а разбор бэклога — не то, чем его стоит занимать. Оно
    # появится в «Сегодня» и в отчёте, то есть когда он сам пришёл смотреть.


async def action_end_episode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = _uid(update)
    lang = _lang(update)
    context.user_data.pop("await", None)
    ep = _active(user_id)
    if ep is None:
        await update.message.reply_text(
            t(lang, "no_active", btn_start=t(lang, "btn_start")),
            reply_markup=main_keyboard(False, lang),
        )
        await _mention_forgotten(update.effective_chat.id, user_id, context, lang)
        return
    episode_id = ep.id
    closed = db.close_episode(user_id, episode_id, ended_at=update.message.date)
    if closed is None:
        # Эпизод закрыли другим путём между выборкой и записью (второй инстанс
        # на том же файле — известное состояние, см. CLAUDE.md).
        await update.message.reply_text(
            t(lang, "ep_state_changed"),
            reply_markup=main_keyboard(_active(user_id) is not None, lang),
        )
        return
    ep = closed
    _stop_live(context.job_queue, user_id, episode_id)
    await update.message.reply_text(
        t(lang, "ep_closed", id=ep.id, dur=report.human_duration(ep.duration(), lang)),
        reply_markup=main_keyboard(False, lang),
    )
    await _send_card(update, context, ep, lang)


def _norm_med_name(name: "str | None") -> str:
    """Название к сравнимому виду: регистронезависимо, без краевых и двойных
    пробелов. «Конкор», «конкор», « конкор » — одно и то же."""
    return " ".join((name or "").casefold().split())


def _dl_distance(a: str, b: str) -> int:
    """Расстояние Дамерау–Левенштейна (ограниченное): вставка/удаление/замена и
    перестановка соседних букв — каждая по 1. «конкор»↔«конкро» = 1."""
    la, lb = len(a), len(b)
    if not la:
        return lb
    if not lb:
        return la
    prev2 = None
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        prev2, prev = prev, cur
    return prev[lb]


def _med_names_match(a: "str | None", b: "str | None") -> bool:
    """Одно ли это лекарство. Регистр и пробелы игнорируем всегда; опечатку
    (пара перепутанных букв) прощаем тем щедрее, чем длиннее название, чтобы не
    склеить два разных коротких препарата. Пустое имя не совпадает ни с чем."""
    a, b = _norm_med_name(a), _norm_med_name(b)
    if not a or not b:
        return False
    if a == b:
        return True
    n = min(len(a), len(b))
    if n <= 3:
        return False  # слишком коротко, чтобы прощать опечатку безопасно
    limit = 1 if n <= 7 else 2
    return _dl_distance(a, b) <= limit


def _retire_same_name(context: ContextTypes.DEFAULT_TYPE, user_id: int,
                      keep: db.Med) -> None:
    """Новая доза того же лекарства — у прежних ЖИВЫХ карточек этого препарата
    счётчик гаснет (запись остаётся): «сколько прошло с последнего приёма»
    теперь считает новая карточка. Совпадение имени — терпимое к регистру и
    опечатке (`_med_names_match`)."""
    if not keep.name:
        return
    for m in db.live_med_cards(user_id, config.MED_TICK_MAX_H * 60):
        if m.id == keep.id:
            continue
        if _med_names_match(m.name, keep.name):
            db.set_med_card_msg(user_id, m.id, None)
            _cancel_med_tick(context.job_queue, user_id, m.id)


def _enforce_live_med_cap(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    """Снимает счётчик с самых давних карточек, если живых больше предела.
    Запись приёма при этом НЕ блокируется (один тап = факт) — гаснет лишь
    тик у старейших, данные остаются. Так число ежеминутных задач на
    человека ограничено, и один пользователь не топит очередь для всех."""
    cap = config.MAX_LIVE_MED_CARDS
    if cap <= 0:
        return
    live = db.live_med_cards(user_id, config.MED_TICK_MAX_H * 60)
    for old in live[:-cap]:
        db.set_med_card_msg(user_id, old.id, None)
        _cancel_med_tick(context.job_queue, user_id, old.id)


async def action_med(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = _uid(update)
    lang = _lang(update)
    if _throttled(context, "med", config.MIN_ACTION_INTERVAL_SEC):
        await update.message.reply_text(t(lang, "too_fast"))
        return
    # Название по умолчанию — самое частое из истории: обычно его и приняли,
    # поэтому ноль нажатий. Если истории нет, карточка сразу предложит выбрать.
    if db.count_meds(user_id) >= config.MAX_MEDS_PER_USER:
        await update.message.reply_text(
            t(lang, "too_many_meds", n=config.MAX_MEDS_PER_USER))
        return
    default_name = db.default_med_name(user_id)
    med = db.add_med(user_id, name=default_name, taken_at=update.message.date)
    # Плашку держим отдельным коротким сообщением, как у эпизода: карточка
    # несёт инлайн-клавиатуру, а нижнее меню умеет ехать только на reply-
    # клавиатуре. Без этого во время работы только с карточками приёма плашка
    # уезжала — «пропало меню снизу». Карточка идёт следом и остаётся
    # последним сообщением.
    await update.message.reply_text(
        t(lang, "med_logged", time=report.hhmm(med.taken_at)),
        reply_markup=main_keyboard(_active(user_id) is not None, lang),
    )
    await _send_med_card(update, context, med, lang)
    _retire_same_name(context, user_id, med)
    _enforce_live_med_cap(context, user_id)




async def action_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = _lang(update)
    await _send_day(update.effective_chat.id, _uid(update), context,
                    report.today_local(), lang)
    # Спокойный момент: человек сам пришёл смотреть записи — самое время
    # напомнить про эпизод без отметки окончания.
    await _mention_forgotten(update.effective_chat.id, _uid(update), context, lang)


async def cmd_yesterday(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_day(update.effective_chat.id, _uid(update), context,
                    report.today_local() - timedelta(days=1), _lang(update))


async def _send_day(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE,
                    day: date, lang: str) -> None:
    start, end = report.day_bounds(day)
    episodes = db.list_episodes(user_id, start, end)
    meds = db.list_meds(user_id, start, end)
    await _send_text(
        context, chat_id, report.day_summary(day, episodes, meds, lang), _day_keyboard(day, lang)
    )


async def action_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = _lang(update)
    await _send_text(
        context, update.effective_chat.id, _report_text(_uid(update), 7, lang),
        _report_keyboard(7, lang),
    )
    await _mention_forgotten(update.effective_chat.id, _uid(update), context, lang)


async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await action_report(update, context)


async def cmd_month(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = _lang(update)
    await _send_text(
        context, update.effective_chat.id, _report_text(_uid(update), 30, lang),
        _report_keyboard(30, lang),
    )


def _report_text(user_id: int, days: int, lang: str) -> str:
    start, _ = report.day_bounds(report.today_local() - timedelta(days=days - 1))
    return report.period_report(
        days, db.list_episodes(user_id, start), db.list_meds(user_id, start), lang
    )


async def action_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = _lang(update)
    # Выгрузка собирает весь дневник в памяти и блокирует цикл событий —
    # единственное место, где один человек заметно мешает остальным.
    if _throttled(context, "export", config.EXPORT_COOLDOWN_SEC):
        await update.message.reply_text(t(lang, "export_cooldown"))
        return
    await _send_csv(update.effective_chat.id, _uid(update), context, lang)


async def _send_csv(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE,
                    lang: str) -> None:
    episodes = db.all_episodes(user_id)
    if not episodes:
        await context.bot.send_message(chat_id, t(lang, "nothing_to_export"))
        return
    data = report.episodes_csv(episodes, db.all_meds(user_id), lang)
    name = f"heartbeat_{report.today_local().strftime('%Y-%m-%d')}.csv"
    await context.bot.send_document(
        chat_id,
        document=InputFile(io.BytesIO(data), filename=name),
        caption=t(lang, "csv_caption", n=len(episodes)),
    )


async def cmd_last(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = _lang(update)
    ep = db.last_episode(_uid(update))
    if ep is None:
        await update.message.reply_text(
            t(lang, "no_records", btn_start=t(lang, "btn_start")),
            reply_markup=main_keyboard(False, lang),
        )
        return
    await _send_card(update, context, ep, lang)


async def on_menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Нажатие на нижнюю плашку — в любом из языков."""
    handler = {
        "btn_start": action_start_episode,
        "btn_end": action_end_episode,
        "btn_med": action_med,
        "btn_today": action_today,
        "btn_report": action_report,
        "btn_export": action_export,
    }[ACTION_BY_TEXT[update.message.text]]
    await handler(update, context)


# --- свободный ввод --------------------------------------------------------

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = _uid(update)
    lang = _lang(update)
    text = (update.message.text or "").strip()
    pending = context.user_data.get("await")

    if pending:
        kind, target_id = pending["what"], pending["id"]
        if kind == "pulse":
            match = PULSE_RE.match(text)
            if not match or not MIN_PULSE <= int(match.group(1)) <= MAX_PULSE:
                await update.message.reply_text(
                    t(lang, "pulse_bad", min=MIN_PULSE, max=MAX_PULSE)
                )
                return
            context.user_data.pop("await", None)
            ep = db.set_pulse(user_id, target_id, int(match.group(1)))
            if ep:
                await _send_card(update, context, ep, lang,
                                 t(lang, "pulse_saved", pulse=ep.pulse))
            else:
                await update.message.reply_text(t(lang, "ep_gone", id=target_id))
            return
        if kind == "note":
            context.user_data.pop("await", None)
            ep, status = db.append_note(user_id, target_id, text)
            if ep is None:
                await update.message.reply_text(t(lang, "ep_gone", id=target_id))
            elif status == db.NOTE_FULL:
                await _note_full(update, context, ep, lang, text)
            else:
                await _send_card(update, context, ep, lang, _note_header(lang, status))
            return
        if kind == "med":
            context.user_data.pop("await", None)
            context.user_data.pop(f"medpanel:{target_id}", None)
            med = db.get_med(user_id, target_id)
            if med is None:
                await update.message.reply_text(
                    t(lang, "med_list_stale"),
                    reply_markup=main_keyboard(_active(user_id) is not None, lang))
                return
            name = i18n.trim_utf16(text.strip(), MED_NAME_LIMIT)
            if name:  # пустое имя не затирает уже заданное и не рисует
                db.set_med_name(user_id, target_id, name)  # «названную» раскладку
            med = db.get_med(user_id, target_id)
            await _send_med_card(update, context, med, lang, force_new=True)
            _retire_same_name(context, user_id, med)
            # Ввод текста прячет нижнюю плашку за системной клавиатурой, а
            # инлайн-карточка её не возвращает — поэтому плашку досылаем
            # последним сообщением (как у эпизода после ввода пульса/заметки).
            await update.message.reply_text(
                t(lang, "med_saved", name=med.name or t(lang, "med_unnamed")),
                reply_markup=main_keyboard(_active(user_id) is not None, lang))
            return

    ep = _current_episode(user_id)
    match = PULSE_RE.match(text)
    if match and MIN_PULSE <= int(match.group(1)) <= MAX_PULSE:
        pulse = int(match.group(1))
        if ep is None:
            await update.message.reply_text(
                t(lang, "pulse_needs_episode"),
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        t(lang, "btn_log_with_pulse", pulse=pulse), callback_data=f"np:{pulse}"
                    )
                ]]),
            )
            return
        ep = db.set_pulse(user_id, ep.id, pulse)
        await _send_card(update, context, ep, lang,
                         t(lang, "pulse_to_ep", pulse=ep.pulse, id=ep.id))
        return

    if ep is None:
        # Не обрезаем здесь: обрезку делает append_note и сообщает о ней.
        # Размер уже ограничен лимитом сообщения Telegram.
        context.user_data["pending_note"] = text
        await update.message.reply_text(
            t(lang, "ask_text_as_episode"),
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(t(lang, "btn_yes_episode"), callback_data="nn"),
                InlineKeyboardButton(t(lang, "btn_no"), callback_data="nx"),
            ]]),
        )
        return
    ep, status = db.append_note(user_id, ep.id, text)
    if status == db.NOTE_FULL:
        await _note_full(update, context, ep, lang, text)
        return
    await _send_card(update, context, ep, lang,
                     _note_header(lang, status, t(lang, "note_appended", id=ep.id)))


async def on_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Геопозиция — где был приступ. Привязывается к текущему эпизоду."""
    user_id = _uid(update)
    lang = _lang(update)
    point = update.message.location
    ep = _current_episode(user_id)
    if ep is None:
        await update.message.reply_text(
            t(lang, "place_needs_episode"),
            reply_markup=main_keyboard(False, lang),
        )
        return
    ep = db.set_place(user_id, ep.id, point.latitude, point.longitude)
    await _send_card(update, context, ep, lang, t(lang, "place_saved", id=ep.id))


async def on_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Фото или документ: вежливо отказываем.

    Вложения в боте не хранятся осознанно (см. CLAUDE.md: сценарий опирался на
    прибор, которого у пользователя нет, а копии ЭКГ из поликлиники и отчётов
    холтера и так есть у врача). Но и ронять файл в тишину нельзя: человек,
    попробовавший очевидное, решает, что бот сломан.
    """
    await update.message.reply_text(t(_lang(update), "file_declined"))


# --- inline-кнопки ---------------------------------------------------------

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    # CallbackQueryHandler нельзя отфильтровать по типу чата так же, как
    # сообщения: нажатия приходят и из групп. Отсекаем здесь.
    if not _is_private(update):
        try:
            await query.answer()
        except Exception:
            pass
        return
    user_id = _uid(update)
    lang = _lang(update)
    parts = (query.data or "").split(":")
    action = parts[0]

    async def ack(text: str = "") -> None:
        try:
            await query.answer(text)
        except Exception:
            pass

    # --- действия, не привязанные к эпизоду ---
    if action == "lang":
        chosen = i18n.normalize(parts[1]) if len(parts) > 1 else None
        if chosen is None:
            # Неизвестный код не должен молча переключать язык на дефолтный.
            await ack()
            return
        db.set_lang(user_id, chosen)
        await ack()
        await query.edit_message_text(t(chosen, "lang_set"))
        await context.bot.send_message(
            query.message.chat_id,
            t(chosen, "help", tz=config.TZ_NAME),
            reply_markup=main_keyboard(_active(user_id) is not None, chosen),
        )
        return
    if action == "fy":  # подтверждённое удаление всех своих данных
        token = context.user_data.pop("forget_token", None)
        if not token or len(parts) < 2 or parts[1] != token:
            # Старое подтверждение из истории чата, или уже использованное,
            # или после рестарта. Молча удалять дневник по такому нажатию нельзя.
            await ack(t(lang, "forget_stale"))
            try:
                await query.edit_message_text(t(lang, "forget_stale"))
            except Exception:
                pass
            return
        # Снимаем все задачи этого пользователя: иначе напоминания и обновления
        # карточек продолжат ходить по удалённым эпизодам.
        if context.job_queue is not None:
            # Имена задач — "remind:<user>:<episode>" и "tick:<user>:<episode>",
            # поэтому сверяем именно поле пользователя. Проверка на суффикс
            # ловила позицию ЭПИЗОДА: пользователь №99 снимал задачу чужого
            # эпизода №99.
            for job in list(context.job_queue.jobs()):
                parts_name = (job.name or "").split(":")
                if len(parts_name) == 3 and parts_name[1] == str(user_id):
                    job.schedule_removal()
        counts = db.purge_user(user_id)
        context.user_data.clear()
        await ack(t(lang, "ack_deleted"))
        await query.edit_message_text(t(lang, "forget_done", **counts))
        await context.bot.send_message(
            query.message.chat_id, t(lang, "done"),
            reply_markup=main_keyboard(False, lang),
        )
        return
    if action == "r":
        await ack()
        days = _arg(parts, 1, REPORT_PERIODS)
        if days is None:
            return
        await _edit_text(query, context, _report_text(user_id, days, lang),
                         _report_keyboard(days, lang))
        return
    if action == "csv":
        await ack(t(lang, "csv_preparing"))
        await _send_csv(query.message.chat_id, user_id, context, lang)
        return
    if action == "dn":
        await ack()
        try:
            day = date.fromisoformat(parts[1])
        except (IndexError, ValueError):
            return
        # Листать можно только по разумному диапазону: date.min ломает
        # арифметику в клавиатуре, а будущее в дневнике смысла не имеет.
        if not DAY_NAV_MIN <= day <= report.today_local():
            return
        start, end = report.day_bounds(day)
        await _edit_text(
            query, context,
            report.day_summary(
                day, db.list_episodes(user_id, start, end),
                db.list_meds(user_id, start, end), lang,
            ),
            _day_keyboard(day, lang),
        )
        return
    if action == "nn":  # свободный текст → новый эпизод с этой заметкой
        await ack()
        note = context.user_data.pop("pending_note", None)
        existing = _active(user_id)
        if existing is not None:
            # Эпизод успел начаться другим путём — дописываем в него, а не
            # создаём второй открытый.
            if not note:
                await query.edit_message_text(t(lang, "ep_already", id=existing.id,
                                                time=report.hhmm(existing.started_at),
                                                btn_end=t(lang, "btn_end")))
                await _send_card(update, context, existing, lang)
                return
            ep, status = db.append_note(user_id, existing.id, note)
            if ep is None:
                await query.edit_message_text(t(lang, "ep_gone", id=existing.id))
                return
            if status == db.NOTE_FULL:
                # Заметка переполнена: сказать «дописал» было бы неправдой.
                await query.edit_message_text(t(lang, "note_full"))
                await _note_full(update, context, ep, lang, note)
                return
            await query.edit_message_text(
                _note_header(lang, status, t(lang, "note_appended", id=ep.id))
            )
            await _send_card(update, context, ep, lang)
            return
        if not await _can_add_episode(update, context, user_id, lang):
            return
        ep = db.start_episode(user_id)
        status = db.NOTE_OK
        if note:
            ep, status = db.append_note(user_id, ep.id, note)
        await query.edit_message_text(
            _note_header(lang, status, t(lang, "ep_recorded", id=ep.id))
        )
        await _send_card(update, context, ep, lang)
        _schedule_reminder(context.job_queue, user_id, ep.id, _window())
        return
    if action == "nx":  # «Нет» / «Отмена» — снимает и ожидание ввода
        await ack()
        context.user_data.pop("pending_note", None)
        pending = context.user_data.pop("await", None)
        await query.edit_message_text(
            t(lang, "cancelled" if pending else "not_logged")
        )
        return
    if action == "np":  # число без эпизода → эпизод + пульс
        pulse = _arg(parts, 1, range(MIN_PULSE, MAX_PULSE + 1))
        if pulse is None:
            await ack()
            return
        await ack()
        # Тот же запрет на второй одновременный эпизод, что и у кнопки
        # «⚡️ Аритмия»: кнопка из истории чата не должна его обходить.
        existing = _active(user_id)
        if existing is not None:
            ep = db.set_pulse(user_id, existing.id, pulse)
            await query.edit_message_text(t(lang, "pulse_to_ep", pulse=pulse, id=ep.id))
            await _send_card(update, context, ep, lang)
            return
        if not await _can_add_episode(update, context, user_id, lang):
            return
        ep = db.start_episode(user_id)
        ep = db.set_pulse(user_id, ep.id, pulse)
        await query.edit_message_text(t(lang, "ep_recorded_pulse", id=ep.id, pulse=ep.pulse))
        await _send_card(update, context, ep, lang)
        _schedule_reminder(context.job_queue, user_id, ep.id, _window())
        return
    if action == "mc":  # карточка приёма лекарства
        sub = parts[1] if len(parts) > 1 else ""
        med_id = _row_id(parts, 2)
        med = db.get_med(user_id, med_id) if med_id is not None else None
        if med is None:
            await ack()
            return
        if sub == "back":  # вернуться к карточке
            context.user_data.pop(f"medpanel:{med_id}", None)
            context.user_data.pop("await", None)
            await ack()
            try:
                await query.edit_message_text(
                    report.med_card(med, lang),
                    reply_markup=_med_card_markup(context, med, lang))
            except Exception as exc:
                if "not modified" not in str(exc):
                    log.warning("Карточка приёма не обновилась: %s", exc)
            return
        if sub == "when":  # сетка «когда приняли»
            context.user_data[f"medpanel:{med_id}"] = "when"
            await ack()
            ask = (t(lang, "med_ask_when", name=med.name) if med.name
                   else t(lang, "med_ask_when_generic"))
            title = report.med_card(med, lang) + "\n\n" + ask
            try:
                await query.edit_message_text(
                    title, reply_markup=_med_when_keyboard(med, lang))
            except Exception as exc:
                if "not modified" not in str(exc):
                    log.warning("Сетка «когда» не открылась: %s", exc)
            return
        if sub == "set":  # выбрано время приёма
            minutes = _arg(parts, 3, MED_WHEN_OFFSETS)
            if minutes is None:
                await ack()
                return
            taken = db.utcnow() - timedelta(minutes=minutes)
            db.set_med_time(user_id, med_id, taken, approx=minutes != 0)
            context.user_data.pop(f"medpanel:{med_id}", None)
            await ack()
            await _send_med_card(update, context, db.get_med(user_id, med_id), lang)
            return
        if sub == "name":  # выбор названия из истории
            names = db.recent_med_names(user_id)
            context.user_data[f"medopts:{med_id}"] = list(names)
            context.user_data[f"medpanel:{med_id}"] = "name"
            # «Что приняли?» приглашает печатать — ловим ввод как название, иначе
            # набранный текст уезжал в «Записать как новый эпизод?».
            context.user_data["await"] = {"what": "med", "id": med_id}
            await ack()
            title = report.med_card(med, lang) + "\n\n" + t(lang, "med_pick_name")
            try:
                await query.edit_message_text(
                    title, reply_markup=_med_name_keyboard(med, lang, names))
            except Exception as exc:
                if "not modified" not in str(exc):
                    log.warning("Выбор названия не открылся: %s", exc)
            return
        if sub == "all":  # полный справочник названий из истории
            names = db.recent_med_names(user_id, MED_ALL_LIMIT)
            context.user_data[f"medopts:{med_id}"] = list(names)
            context.user_data[f"medpanel:{med_id}"] = "all"
            # Экран приглашает и печатать новое название — ловим ввод.
            context.user_data["await"] = {"what": "med", "id": med_id}
            await ack()
            title = report.med_card(med, lang) + "\n\n" + t(lang, "med_pick_name")
            try:
                await query.edit_message_text(
                    title, reply_markup=_med_all_keyboard(med, lang, names))
            except Exception as exc:
                if "not modified" not in str(exc):
                    log.warning("Справочник названий не открылся: %s", exc)
            return
        if sub == "pick":  # название из снимка истории
            names = (context.user_data.get(f"medopts:{med_id}")
                     or db.recent_med_names(user_id, MED_NAME_BUTTONS))
            idx = _arg(parts, 3, range(len(names))) if names else None
            if idx is None:
                await ack(t(lang, "med_list_stale"))
                return
            db.set_med_name(user_id, med_id, names[idx])
            context.user_data.pop(f"medopts:{med_id}", None)
            context.user_data.pop(f"medpanel:{med_id}", None)
            context.user_data.pop("await", None)
            await ack(t(lang, "ack_saved"))
            picked = db.get_med(user_id, med_id)
            await _send_med_card(update, context, picked, lang)
            _retire_same_name(context, user_id, picked)
            return
        if sub == "type":  # ввести название вручную
            context.user_data["await"] = {"what": "med", "id": med_id}
            await ack()
            await query.message.reply_text(t(lang, "med_name_prompt"))
            return
        if sub == "del":  # подтверждение удаления (одноразовый токен)
            token = secrets.token_urlsafe(4)
            context.user_data[f"mdel:{med_id}"] = token
            # Пауза тика: иначе через минуту он перерисует карточку поверх
            # экрана подтверждения и кнопка «Удалить» исчезнет под пальцем.
            context.user_data[f"medpanel:{med_id}"] = "del"
            await ack()
            try:
                await query.edit_message_text(
                    t(lang, "med_delete_confirm",
                      name=med.name or t(lang, "med_unnamed"),
                      time=report.hhmm(med.taken_at)),
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton(t(lang, "btn_back"),
                                             callback_data=f"mc:back:{med_id}"),
                        InlineKeyboardButton(t(lang, "btn_delete"),
                                             callback_data=f"mc:dy:{med_id}:{token}"),
                    ]]),
                )
            except Exception as exc:
                log.warning("Подтверждение удаления приёма не показалось: %s", exc)
            return
        if sub == "dy":  # удаление подтверждено
            context.user_data.pop(f"medpanel:{med_id}", None)
            token = context.user_data.pop(f"mdel:{med_id}", None)
            if not token or len(parts) < 4 or parts[3] != token:
                await ack(t(lang, "forget_stale"))
                return
            _cancel_med_tick(context.job_queue, user_id, med_id)
            db.delete_med(user_id, med_id)
            await ack(t(lang, "ack_deleted"))
            try:
                await query.edit_message_text(t(lang, "med_deleted_card"))
            except Exception:
                pass
            return
        await ack()
        return

    

    if action == "bf":  # ретроспективная запись: приступ, который уже прошёл
        sub = parts[1] if len(parts) > 1 else ""
        if sub == "x" and len(parts) == 2:
            await ack()
            try:
                await query.edit_message_text(t(lang, "not_logged"))
            except Exception:
                pass
            return
        if sub == "s" and len(parts) == 2:  # назад к выбору дня
            await ack()
            await _edit_text(query, context, t(lang, "back_intro"),
                             _backfill_day_keyboard(lang))
            return
        if sub in ("d", "n") and len(parts) == 3:  # день выбран → сетка часов
            day = _backfill_day(parts[2])
            if day is None:
                await ack()
                return
            if not await _can_add_episode(update, context, user_id, lang):
                await ack()
                return
            await ack()
            text, keyboard = _backfill_hour_screen(day, lang)
            if sub == "n":
                # Вход из сводки дня: сводку не затираем — человек её читал и
                # по ней заметил пропуск. Поток уезжает в новое сообщение.
                await context.bot.send_message(query.message.chat_id, text,
                                               reply_markup=keyboard)
            else:
                await _edit_text(query, context, text, keyboard)
            return
        if sub == "h" and len(parts) == 4:  # час выбран → минуты полным временем
            day = _backfill_day(parts[2])
            hour = _bf_int(parts, 3, range(24))
            if day is None or hour is None:
                await ack()
                return
            keyboard = _backfill_minute_keyboard(day, hour, lang)
            if len(keyboard.inline_keyboard) == 1:
                # Будущий час: ни одной минуты не существует. Экран-тупик из
                # одной кнопки «Назад» хуже простого отказа.
                await ack()
                return
            await ack()
            await _edit_text(query, context, t(lang, "back_ask_minute"), keyboard)
            return
        if sub == "m" and len(parts) == 5:  # минута выбрана → длительность
            day = _backfill_day(parts[2])
            hour = _bf_int(parts, 3, range(24))
            minute = _bf_int(parts, 4, BACKFILL_MINUTES)
            if day is None or hour is None or minute is None:
                await ack()
                return
            start = _backfill_start(day, hour, minute)
            if start is None:
                await ack()
                return
            await ack()
            await _edit_text(
                query, context,
                t(lang, "back_ask_duration",
                  date=report.date_day_month(day, lang), time=report.hhmm(start)),
                _backfill_duration_keyboard(day, hour, minute, lang),
            )
            return
        if sub == "f" and len(parts) == 6:  # длительность выбрана → создаём
            day = _backfill_day(parts[2])
            hour = _bf_int(parts, 3, range(24))
            minute = _bf_int(parts, 4, BACKFILL_MINUTES)
            if day is None or hour is None or minute is None:
                await ack()
                return
            start = _backfill_start(day, hour, minute)
            if start is None:
                await ack()
                return
            # Сначала ПОЛНАЯ валидация, потом предохранители: отклонённый
            # мусор не должен сжигать слот лимита частоты у честного нажатия.
            duration = None
            if parts[5] != "x":
                duration = _bf_int(parts, 5, DURATION_CHOICES)
                if duration is None:
                    await ack()
                    return
                if start + timedelta(minutes=duration) > db.utcnow():
                    # Клавиатура такого не предлагает: payload подделан или
                    # устарел. Будущее окончание — выдуманные данные.
                    await ack()
                    return
            existing = db.episode_at(user_id, start)
            if existing is not None:
                # Повтор кнопки (Telegram передоставил нажатие, или человек
                # нажал старую из истории): эпизод с этой минуты уже есть —
                # показываем его, а не создаём близнеца.
                await ack()
                try:
                    await query.edit_message_text(t(lang, "back_duplicate"))
                except Exception:
                    pass
                await _send_card(update, context, existing, lang)
                return
            if _throttled(context, "episode", config.MIN_ACTION_INTERVAL_SEC):
                await ack(t(lang, "too_fast"))
                return
            if not await _can_add_episode(update, context, user_id, lang):
                await ack()
                return
            if duration is None:
                # Длительность неизвестна: эпизод сохраняется открытым с
                # честной отметкой. Благодаря ей он НЕ считается текущим, не
                # блокирует «⚡️», не ловит ввод и не тикает.
                ep = db.start_episode(user_id, started_at=start)
                db.mark_end_unknown(user_id, ep.id)
                await ack()
                try:
                    await query.edit_message_text(
                        t(lang, "back_saved_unknown",
                          date=report.date_day_month(day, lang),
                          time=report.hhmm(start)))
                except Exception:
                    pass
                await _send_card(update, context, db.get_episode(user_id, ep.id), lang)
                return
            end = start + timedelta(minutes=duration)
            ep = db.start_episode(user_id, started_at=start)
            closed = db.close_episode(user_id, ep.id, ended_at=end, approx=True)
            await ack()
            try:
                await query.edit_message_text(
                    t(lang, "back_saved",
                      date=report.date_day_month(day, lang),
                      start=report.hhmm(start), end=report.hhmm(end),
                      dur=report.human_duration(timedelta(minutes=duration), lang)))
            except Exception:
                pass
            await _send_card(update, context, closed, lang)
            return
        await ack()
        return

    # --- действия над эпизодом ---

    # --- действия над эпизодом ---
    episode_id = _episode_id(parts)
    if episode_id is None:
        await ack()
        return
    ep = db.get_episode(user_id, episode_id)
    if ep is None:
        await ack(t(lang, "ep_not_found"))
        try:
            await query.edit_message_text(t(lang, "ep_deleted_msg"))
        except Exception:
            pass
        return

    # «Отпустило» и сдвиг начала на забытом эпизоде: отказывать нечестно —
    # человек мог отвечать на вопрос бота через час, и для него ничего не
    # «изменилось». Но и записать «отпустило сейчас» нельзя, это была бы
    # догадка. Поэтому сразу спрашиваем длительность — это и есть следующий
    # полезный шаг, а не тупик.
    if action in NEEDS_ACTIVE and ep.needs_end(config.STALE_AFTER_MIN):
        await ack()
        await _edit_text(
            query, context,
            t(lang, "ask_duration_stale", id=ep.id,
              day=report._day_prefix(ep.started_at, lang),
              time=report.hhmm(ep.started_at),
              dur=report.human_duration(ep.duration(), lang)),
            InlineKeyboardMarkup(duration_rows(ep, lang)),
        )
        return

    # M2: кнопки живут в истории чата вечно, и старая карточка нарисована в том
    # состоянии, которое было на момент отправки. Применять её действие к
    # эпизоду, который с тех пор изменился, нельзя: так закрытый эпизод получал
    # новое время окончания, а длительность 20 минут превращалась в 2 часа.
    if action in NEEDS_OPEN and not ep.is_open:
        await ack(t(lang, "ep_state_changed"))
        await _refresh(query, ep, lang, context)
        return
    if action in NEEDS_ACTIVE and (
        not ep.is_open or ep.needs_end(config.STALE_AFTER_MIN)
    ):
        await ack(t(lang, "ep_state_changed"))
        await _refresh(query, ep, lang, context)
        return
    if action in NEEDS_CLOSED and ep.is_open:
        await ack(t(lang, "ep_state_changed"))
        await _refresh(query, ep, lang)
        return

    if action == "s":
        level = _arg(parts, 2, tuple(SEVERITY_LEVELS))
        if level is None:
            await ack()
            return
        # Повторное нажатие той же оценки снимает её.
        ep = db.set_severity(user_id, episode_id, None if ep.severity == level else level)
        await ack()
        await _refresh(query, ep, lang)
        return
    if action == "p":
        context.user_data["await"] = {"what": "pulse", "id": episode_id}
        await ack()
        await query.message.reply_text(t(lang, "pulse_prompt", id=episode_id))
        return
    if action == "n":
        context.user_data["await"] = {"what": "note", "id": episode_id}
        await ack()
        rows = [[InlineKeyboardButton(t(lang, "btn_cancel"), callback_data="nx")]]
        clear_token = secrets.token_urlsafe(4)
        context.user_data[f"nc_token:{episode_id}"] = clear_token
        if ep.note:
            # Заметка — единственное поле, которое раньше нельзя было
            # поправить: пульс перезаписывается, переключатели снимаются,
            # а дописанное в заметку убиралось только удалением эпизода.
            rows.insert(0, [InlineKeyboardButton(
                t(lang, "btn_clear_note"),
                callback_data=f"nc:{episode_id}:{clear_token}",
            )])
        await query.message.reply_text(
            t(lang, "note_prompt", id=episode_id),
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return
    if action == "pc":  # убрать место — координаты это данные, их должно быть
        ep = db.set_place(user_id, episode_id, None, None)  # можно забрать назад
        await ack(t(lang, "ack_ok"))
        await _refresh(query, ep, lang, context)
        return
    if action == "nt":  # дописать, вытеснив самое старое начало заметки
        text = context.user_data.pop(f"note_overflow:{episode_id}", None)
        if not text:
            await ack(t(lang, "note_lost_text"))
            context.user_data["await"] = {"what": "note", "id": episode_id}
            await query.edit_message_text(t(lang, "note_lost_text"))
            return
        updated, dropped = db.append_note_dropping_oldest(user_id, episode_id, text)
        await ack(t(lang, "ack_saved"))
        try:
            await query.edit_message_text(
                t(lang, "note_pushed" if dropped else "note_saved")
            )
        except Exception:
            pass
        await _send_card(update, context, updated, lang)
        return
    if action == "nc":
        pending_text = context.user_data.pop(f"note_overflow:{episode_id}", None)
        if pending_text is None:
            # Чистая очистка — действие деструктивное, поэтому нужен свежий
            # токен. Без него старая кнопка из истории чата стирала заметку.
            token = context.user_data.pop(f"nc_token:{episode_id}", None)
            if not token or len(parts) < 3 or parts[2] != token:
                await ack(t(lang, "forget_stale"))
                await _refresh(query, ep, lang, context)
                return
        cleared = db.clear_note(user_id, episode_id)
        if pending_text:
            # Очистили, чтобы освободить место под уже присланный текст — сразу
            # его и записываем, а не просим прислать заново (и не отправляем
            # человека в диалог «записать как новый эпизод?»).
            cleared, _ = db.append_note(user_id, episode_id, pending_text)
        else:
            context.user_data.pop("await", None)
        await ack(t(lang, "ack_ok"))
        try:
            await query.edit_message_text(
                t(lang, "note_saved" if pending_text else "note_cleared")
            )
        except Exception:
            pass
        await _send_card(update, context, cleared, lang)
        return
    if action == "m":
        kind = parts[2] if len(parts) > 2 else ""
        if kind not in ("sym", "trg"):
            await ack()
            return
        await ack()
        try:
            await query.edit_message_text(
                t(lang, "menu_symptoms" if kind == "sym" else "menu_triggers", id=ep.id),
                reply_markup=_toggle_keyboard(ep, kind, lang),
            )
        except Exception as exc:
            log.warning("Меню не открылось: %s", exc)
        return
    if action in ("ts", "tt"):
        column = "symptoms" if action == "ts" else "triggers"
        codes = vocab.SYMPTOM_CODES if action == "ts" else vocab.TRIGGER_CODES
        code = parts[2] if len(parts) > 2 else ""
        if code not in codes:
            # Иначе в колонку симптомов попадала произвольная строка, которая
            # потом рендерилась в карточку, в ленту дня и в CSV.
            await ack()
            return
        ep = db.toggle_code(user_id, episode_id, column, code)
        # В паре ответы взаимоисключающие: включили один — гасим противоположный.
        # «Началось резко» и «нарастало» одновременно — не более полный ответ, а
        # противоречие, и две колонки это уже обещают видом радиокнопок.
        opposite = vocab.sibling(code) if action == "ts" else None
        if opposite and code in ep.symptoms and opposite in ep.symptoms:
            ep = db.toggle_code(user_id, episode_id, column, opposite)
        await ack()
        try:
            await query.edit_message_reply_markup(
                reply_markup=_toggle_keyboard(ep, "sym" if action == "ts" else "trg", lang)
            )
        except Exception as exc:
            if "not modified" not in str(exc):
                log.warning("Переключатель не обновился: %s", exc)
        return
    if action == "rf":  # панель уточнений
        context.user_data[f"panel:{episode_id}"] = "refine"
        await ack()
        try:
            await query.edit_message_reply_markup(reply_markup=refine_keyboard(ep, lang))
        except Exception as exc:
            if "not modified" not in str(exc):
                log.warning("Панель уточнений не открылась: %s", exc)
        return
    if action == "sc":  # закрытый эпизод: «когда на самом деле отпустило?»
        await ack()
        try:
            await query.edit_message_reply_markup(
                reply_markup=end_when_panel(ep, lang))
        except Exception as exc:
            if "not modified" not in str(exc):
                log.warning("Панель окончания не открылась: %s", exc)
        return
    if action == "dp":  # панель длительности у забытого эпизода
        context.user_data[f"panel:{episode_id}"] = "duration"
        await ack()
        try:
            await query.edit_message_reply_markup(reply_markup=duration_panel(ep, lang))
        except Exception as exc:
            if "not modified" not in str(exc):
                log.warning("Панель длительности не открылась: %s", exc)
        return
    if action == "c":  # возврат к карточке
        context.user_data.pop(f"panel:{episode_id}", None)
        await ack()
        await _refresh(query, ep, lang, context)
        return
    if action == "e":
        closed = db.close_episode(user_id, episode_id)
        if closed is None:  # кто-то успел закрыть его раньше
            await ack(t(lang, "ep_state_changed"))
            await _refresh(query, db.get_episode(user_id, episode_id), lang)
            return
        ep = closed
        _stop_live(context.job_queue, user_id, episode_id)
        await ack(t(lang, "ack_lasted", dur=report.human_duration(ep.duration(), lang)))
        await _refresh(query, ep, lang)
        # Нижняя плашка меняется только с новым сообщением.
        await context.bot.send_message(
            query.message.chat_id,
            t(lang, "ep_closed", id=ep.id, dur=report.human_duration(ep.duration(), lang)),
            reply_markup=main_keyboard(_active(user_id) is not None, lang),
        )
        return
    if action == "ro":
        if not _can_reopen(ep):
            await ack(t(lang, "reopen_too_old"))
            await _refresh(query, ep, lang, context)
            return
        if db.count_open_episodes(user_id) >= config.MAX_OPEN_EPISODES:
            await ack(t(lang, "too_many_open", n=config.MAX_OPEN_EPISODES))
            return
        reopened = db.reopen_episode(user_id, episode_id)
        if reopened is None:
            await ack(t(lang, "ep_state_changed"))
            await _refresh(query, db.get_episode(user_id, episode_id), lang)
            return
        ep = reopened
        _schedule_reminder(context.job_queue, user_id, episode_id, _window())
        await ack(t(lang, "ack_reopened"))
        await _refresh(query, ep, lang)
        await context.bot.send_message(
            query.message.chat_id,
            t(lang, "still_on", id=ep.id, dur=report.human_duration(ep.duration(), lang)),
            reply_markup=main_keyboard(True, lang),
        )
        return
    if action == "go":  # «ещё идёт» — продлеваем окно активности
        was_stale = ep.is_stale(config.STALE_AFTER_MIN)
        confirmed = db.confirm_still_on(user_id, episode_id)
        if confirmed is None:
            # Эпизод закрыли между выборкой и записью. Ни продлевать, ни
            # обещать «отмечу окончание» нельзя: отмечать уже нечего.
            await ack(t(lang, "ep_state_changed"))
            await _refresh(query, db.get_episode(user_id, episode_id), lang)
            return
        ep = confirmed
        # Человек ответил — цикл вопросов начинается заново.
        db.set_remind_stage(user_id, episode_id, 0)
        _schedule_reminder(context.job_queue, user_id, episode_id, _window())
        # Живое обновление карточки напоминание снимало — «ещё идёт» его
        # возвращает: человек снова видит, сколько идёт, не трогая телефон.
        _schedule_tick(context.job_queue, user_id, episode_id, query.message.chat_id)
        await ack(t(lang, "ack_extended", minutes=config.EPISODE_WINDOW_MIN))
        try:
            await query.edit_message_text(
                t(lang, "still_on", id=ep.id, dur=report.human_duration(ep.duration(), lang))
                + (t(lang, "still_on_next", minutes=config.EPISODE_WINDOW_MIN)
                   if config.EPISODE_WINDOW_MIN > 0 else ""),
                reply_markup=card_keyboard(ep, lang),
            )
        except Exception as exc:
            if "not modified" not in str(exc):
                log.warning("Карточка не обновилась: %s", exc)
        if was_stale:
            # Эпизод снова активен — нижнюю плашку надо вернуть к «Отпустило»,
            # а она меняется только вместе с новым сообщением.
            await context.bot.send_message(
                query.message.chat_id,
                t(lang, "will_mark_end", btn_end=t(lang, "btn_end")),
                reply_markup=main_keyboard(True, lang),
            )
        return
    if action == "apm":  # «не помню, когда прошло» — предлагаем длительности
        await ack()
        try:
            await query.edit_message_text(
                t(lang, "btn_how_long") + f" (#{ep.id}, {report.hhmm(ep.started_at)})",
                reply_markup=InlineKeyboardMarkup(duration_rows(ep, lang)),
            )
        except Exception as exc:
            log.warning("Меню длительности не открылось: %s", exc)
        return
    if action == "ap":  # примерная длительность от начала эпизода
        minutes = _arg(parts, 2, DURATION_CHOICES)
        if minutes is None:
            await ack()
            return
        closed = db.close_episode(
            user_id, episode_id,
            ended_at=ep.started_at + timedelta(minutes=minutes), approx=True,
        )
        if closed is None:
            await ack(t(lang, "ep_state_changed"))
            await _refresh(query, db.get_episode(user_id, episode_id), lang)
            return
        ep = closed
        _stop_live(context.job_queue, user_id, episode_id)
        dur = report.human_duration(ep.duration(), lang)
        await ack(t(lang, "ack_approx", dur=dur))
        await _refresh(query, ep, lang)
        await context.bot.send_message(
            query.message.chat_id,
            t(lang, "approx_saved", id=ep.id, dur=dur),
            reply_markup=main_keyboard(_active(user_id) is not None, lang),
        )
        return
    if action == "unk":  # окончание так и осталось неизвестным
        _stop_live(context.job_queue, user_id, episode_id)
        # Запоминаем ответ, иначе эпизод остаётся в «забытых» и бот напоминает
        # о нём при каждом новом приступе — вечно.
        db.mark_end_unknown(user_id, episode_id)
        await ack(t(lang, "ack_ok"))
        try:
            await query.edit_message_text(
                t(lang, "left_unknown", id=ep.id,
                  day=report._day_prefix(ep.started_at, lang),
                  time=report.hhmm(ep.started_at)),
            )
        except Exception as exc:
            log.warning("Сообщение не обновилось: %s", exc)
        await context.bot.send_message(
            query.message.chat_id,
            t(lang, "done"),
            reply_markup=main_keyboard(_active(user_id) is not None, lang),
        )
        return
    if action == "se":  # правка времени окончания
        minutes = _arg(parts, 2, tuple(-m for m in END_SHIFT_CHOICES))
        if minutes is None:
            await ack()
            return
        # Окно проверяем и здесь: payload недоверенный, а кнопка остаётся в
        # истории чата навсегда — та же дисциплина, что у `ro`.
        if not _can_shift_end(ep):
            await ack(t(lang, "reopen_too_old"))
            await _refresh(query, ep, lang, context)
            return
        moved = db.shift_end(user_id, episode_id, minutes)
        if moved is None:
            # Отказ, а не подрезание: эпизод нулевой длительности — выдумка.
            await ack(t(lang, "end_before_start", time=report.hhmm(ep.started_at)))
            return
        await ack(t(lang, "ack_end_set", time=report.hhmm(moved.ended_at)))
        await _refresh(query, moved, lang, context)
        return
    if action == "sh":
        minutes = _arg(parts, 2, tuple(-m for m in SHIFT_CHOICES))
        if minutes is None:
            await ack()
            return
        ep = db.shift_start(user_id, episode_id, minutes)
        if ep is None:
            await ack(t(lang, "ep_state_changed"))
            return
        await ack(t(lang, "ack_start_set", time=report.hhmm(ep.started_at)))
        await _refresh(query, ep, lang)
        return
    if action == "d":
        await ack()
        try:
            # Одноразовый токен, как у /forget: кнопка подтверждения остаётся
            # в истории чата навсегда, и нажатие на старую удаляло бы эпизод
            # без повторного вопроса.
            token = secrets.token_urlsafe(4)
            context.user_data[f"del_token:{ep.id}"] = token
            await query.edit_message_text(
                t(lang, "delete_confirm", id=ep.id, time=report.hhmm(ep.started_at)),
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(t(lang, "btn_back"), callback_data=f"c:{ep.id}"),
                    InlineKeyboardButton(t(lang, "btn_delete"),
                                         callback_data=f"dy:{ep.id}:{token}"),
                ]]),
            )
        except Exception as exc:
            log.warning("Подтверждение удаления не показалось: %s", exc)
        return
    if action == "dy":
        token = context.user_data.pop(f"del_token:{episode_id}", None)
        if not token or len(parts) < 3 or parts[2] != token:
            await ack(t(lang, "forget_stale"))
            await _refresh(query, ep, lang, context)
            return
        db.delete_episode(user_id, episode_id)
        _stop_live(context.job_queue, user_id, episode_id)
        await ack(t(lang, "ack_deleted"))
        await query.edit_message_text(t(lang, "deleted", id=episode_id))
        await context.bot.send_message(
            query.message.chat_id,
            t(lang, "done"),
            reply_markup=main_keyboard(_active(user_id) is not None, lang),
        )
        return
    await ack()


# --- служебное -------------------------------------------------------------

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Ошибка обработчика", exc_info=context.error)
    chat = getattr(update, "effective_chat", None)
    chat_id = getattr(chat, "id", None)
    if chat_id and getattr(chat, "type", None) == Chat.PRIVATE:
        lang = _job_lang(chat_id)
        try:
            await context.bot.send_message(
                chat_id, t(lang, "error_generic", btn_today=t(lang, "btn_today"))
            )
        except Exception:
            pass


def _commands(lang: str) -> list[BotCommand]:
    return [
        # Команды, дублирующие нижнюю плашку (⚡️ log/stop, 💊 med,
        # «Сегодня» today), из меню убраны — они засоряют и список
        # команд, и чат. Хендлеры остаются рабочими (мышечная память,
        # подсказки), просто не рекламируются, как и /cancel.
        BotCommand("earlier", t(lang, "cmd_earlier")),
        BotCommand("last", t(lang, "cmd_last")),
        BotCommand("yesterday", t(lang, "cmd_yesterday")),
        BotCommand("week", t(lang, "cmd_week")),
        BotCommand("month", t(lang, "cmd_month")),
        BotCommand("export", t(lang, "cmd_export")),
        BotCommand("lang", t(lang, "cmd_lang")),
        BotCommand("forget", t(lang, "cmd_forget")),
        # /cancel намеренно НЕ в меню: команда редкая и путает. Остаётся
        # рабочей (на неё ссылаются подсказки «/cancel — отменить»), но из
        # списка команд убрана.
        BotCommand("help", t(lang, "cmd_help")),
    ]


async def post_init(app) -> None:
    # Job queue живёт в памяти процесса: после рестарта напоминания по открытым
    # эпизодам нужно поставить заново, иначе они потеряются.
    if app.job_queue is None:
        log.warning("Job queue недоступна — напоминания «отпустило?» выключены. "
                    "Поставьте python-telegram-bot[job-queue].")
    else:
        restored = 0
        for ep in db.all_open_episodes(config.STALE_AFTER_MIN):
            # Срок считается от того порога, на котором эпизод сейчас стоит:
            # если вопрос уже задан, ждать надо до «забытости», а не до окна.
            asked = ep.remind_stage >= 1
            threshold = config.STALE_AFTER_MIN if asked else config.EPISODE_WINDOW_MIN
            anchor = ep.confirmed_at or ep.started_at
            due = anchor + timedelta(minutes=threshold)
            _schedule_reminder(app.job_queue, ep.user_id, ep.id, _until(due),
                               final=asked)
            if ep.card_msg:
                # chat_id == user_id: диалог приватный (см. register_handlers)
                _schedule_tick(app.job_queue, ep.user_id, ep.id, ep.user_id)
            restored += 1
        if restored:
            log.info("Восстановлено напоминаний: %s", restored)

        # Забытые открытые эпизоды в restore НЕ попадают (all_open_episodes их
        # отсекает), поэтому их живая карточка после рестарта оставалась бы
        # замороженной на «идёт, уже N мин». Один раз догоняем: правим в
        # стальную форму. Это и расклеивает карточку, зависшую у пользователя.
        caught = 0
        for ep in db.stale_cards_to_refresh(config.STALE_AFTER_MIN):
            ep_lang = _job_lang(ep.user_id)
            try:
                await app.bot.edit_message_text(
                    report.episode_card(ep, ep_lang),
                    chat_id=ep.user_id, message_id=ep.card_msg,
                    reply_markup=card_keyboard(ep, ep_lang),
                )
                caught += 1
            except Exception:
                pass  # сообщение удалено, слишком старое или уже стальное
            # Плашку — один раз на эпизод (флаг в БД) и только если активного
            # нет. Раньше это слалось на КАЖДОМ рестарте и даже при активном
            # эпизоде, где текст «начнётся снова» противоречил плашке.
            try:
                await _notify_stale_once(app.bot, ep.user_id,
                                         db.get_episode(ep.user_id, ep.id), ep_lang)
            except Exception:
                pass
        if caught:
            log.info("Расклеено забытых карточек: %s", caught)

        # Тики карточек приёмов лекарств: как и у эпизодов, живут в памяти.
        for med in db.meds_with_live_card(config.MED_TICK_MAX_H * 60):
            _schedule_med_tick(app.job_queue, med.user_id, med.id, med.user_id)

    # Описания команд — на каждом языке плюс дефолтный набор для остальных.
    for lang in i18n.SUPPORTED:
        await app.bot.set_my_commands(_commands(lang), language_code=lang)
    await app.bot.set_my_commands(_commands(config.DEFAULT_LANG))


def register_handlers(app) -> None:
    """Регистрирует все хендлеры. Вынесено из build_app, чтобы тесты проверяли
    ровно тот набор, который работает в боте, а не его копию рядом.

    Только приватные диалоги: в группе бот отвечал бы тому чату, где нажали
    кнопку, а данные брал по нажавшему — сосед по группе мог бы спровоцировать
    публикацию чужого дневника. Дублирует настройку /setjoingroups в BotFather,
    потому что настройка в одном переключателе от того, чтобы стать неверной.

    UpdateType.MESSAGE отсекает edited_message и business_message: у них
    update.message пустой, а правка опечатки в уже отправленной команде
    переотправляла бы весь дневник заново.
    """
    for name, handler in (
        ("start", cmd_start), ("help", cmd_help), ("lang", cmd_lang),
        ("cancel", cmd_cancel), ("forget", cmd_forget),
        ("log", action_start_episode),
        ("stop", action_end_episode), ("earlier", cmd_earlier),
        ("med", action_med), ("last", cmd_last),
        ("today", action_today), ("yesterday", cmd_yesterday),
        ("week", cmd_week), ("month", cmd_month), ("export", action_export),
    ):
        app.add_handler(CommandHandler(
            name, handler, filters=PRIVATE & filters.UpdateType.MESSAGE
        ))

    app.add_handler(MessageHandler(
        filters.Text(list(ACTION_BY_TEXT)) & PRIVATE & filters.UpdateType.MESSAGE,
        on_menu_button,
    ))
    app.add_handler(CallbackQueryHandler(on_callback))
    # Файлы не храним, но отвечаем: без этого хендлера присланное фото не
    # попадало ни в один обработчик, и человек получал молчание.
    app.add_handler(MessageHandler(
        filters.LOCATION & PRIVATE & filters.UpdateType.MESSAGE, on_location
    ))
    app.add_handler(MessageHandler(
        (filters.PHOTO | filters.Document.ALL | filters.VIDEO | filters.AUDIO
         | filters.VOICE) & PRIVATE & filters.UpdateType.MESSAGE,
        on_file,
    ))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & PRIVATE & filters.UpdateType.MESSAGE, on_text
    ))


def build_app():
    config.check()
    db.init(config.DB_PATH)
    app = ApplicationBuilder().token(config.TELEGRAM_BOT_TOKEN).post_init(post_init).build()
    register_handlers(app)
    app.add_error_handler(on_error)
    return app


def main() -> None:
    app = build_app()
    log.info("Bot started (tz=%s, db=%s, langs=%s)",
             config.TZ_NAME, config.DB_PATH, ",".join(i18n.SUPPORTED))
    # drop_pending_updates=False: нажатия, сделанные пока бот лежал, обработаются
    # после старта — и попадут в дневник временем нажатия (см. action_start_episode).
    app.run_polling(
        # Только то, что бот действительно обрабатывает: ALL_TYPES втягивал
        # правки сообщений и business_message, у которых update.message пустой.
        allowed_updates=[Update.MESSAGE, Update.CALLBACK_QUERY],
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
