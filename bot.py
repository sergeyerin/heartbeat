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
from datetime import date, datetime, timedelta, timezone

from telegram import (
    BotCommand,
    Chat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
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
NEEDS_CLOSED = frozenset({"ro"})

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
    """Нижняя плашка. Главная кнопка меняется по состоянию: начать / закрыть."""
    return ReplyKeyboardMarkup(
        [
            [t(lang, "btn_end" if has_open else "btn_start")],
            [t(lang, "btn_med"), t(lang, "btn_today")],
            [t(lang, "btn_report"), t(lang, "btn_export")],
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
    rows = [
        [
            InlineKeyboardButton(
                ("✅ " if ep.severity == level else "") + severity[level],
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
                t(lang, "btn_note") + (" ✅" if ep.note else ""),
                callback_data=f"n:{ep.id}",
            ),
        ],
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
    if ep.is_open and ep.is_stale(config.STALE_AFTER_MIN):
        # «Отпустило сейчас» тут было бы ложью: эпизод забыт часы назад.
        # Спрашиваем длительность, а не время окончания.
        rows.append([InlineKeyboardButton(t(lang, "btn_how_long"), callback_data="noop")])
        rows += duration_rows(ep, lang)
    elif ep.is_open:
        rows.append([InlineKeyboardButton(t(lang, "btn_end"), callback_data=f"e:{ep.id}")])
        # Записал не сразу — сдвигаем начало назад.
        rows.append([
            InlineKeyboardButton(t(lang, "btn_shift", minutes=m), callback_data=f"sh:{ep.id}:-{m}")
            for m in SHIFT_CHOICES
        ])
    else:
        rows.append([InlineKeyboardButton(t(lang, "btn_reopen"), callback_data=f"ro:{ep.id}")])
    rows.append([InlineKeyboardButton(t(lang, "btn_delete"), callback_data=f"d:{ep.id}")])
    return InlineKeyboardMarkup(rows)


def _toggle_keyboard(ep: db.Episode, kind: str, lang: str) -> InlineKeyboardMarkup:
    if kind == "sym":
        codes, vocabulary, prefix = vocab.SYMPTOM_CODES, vocab.symptoms(lang), "ts"
        chosen = ep.symptoms
    else:
        codes, vocabulary, prefix = vocab.TRIGGER_CODES, vocab.triggers(lang), "tt"
        chosen = ep.triggers
    rows = [
        [
            InlineKeyboardButton(
                ("✅ " if code in chosen else "") + vocabulary[code],
                callback_data=f"{prefix}:{ep.id}:{code}",
            )
        ]
        for code in codes
    ]
    rows.append([InlineKeyboardButton(t(lang, "btn_done"), callback_data=f"c:{ep.id}")])
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


def _day_keyboard(day: date) -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton("⬅️ " + (day - timedelta(days=1)).strftime("%d.%m"),
                             callback_data=f"dn:{day - timedelta(days=1)}"),
    ]]
    if day < report.today_local():
        rows[0].append(
            InlineKeyboardButton((day + timedelta(days=1)).strftime("%d.%m") + " ➡️",
                                 callback_data=f"dn:{day + timedelta(days=1)}")
        )
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
                     ep: db.Episode | None, lang: str, header: str = "") -> None:
    """Отправляет карточку. ``ep`` может быть None: эпизод успели удалить между
    записью и отправкой — тогда отправлять нечего."""
    if ep is None:
        return
    text = (header + "\n\n" if header else "") + report.episode_card(ep, lang)
    await _send_text(context, update.effective_chat.id, text, card_keyboard(ep, lang))


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


def _job_name(user_id: int, episode_id: int) -> str:
    return f"remind:{user_id}:{episode_id}"


def _cancel_reminder(job_queue, user_id: int, episode_id: int) -> None:
    if job_queue is None:
        return
    for job in job_queue.get_jobs_by_name(_job_name(user_id, episode_id)):
        job.schedule_removal()


def _schedule_reminder(job_queue, user_id: int, episode_id: int, minutes: int,
                       final: bool = False) -> None:
    """Запланировать вопрос «отпустило?» через N минут.

    ``final=True`` — это уже контрольный заход: если и к нему ответа не будет,
    эпизод останется с неотмеченным окончанием. Повторных вопросов нет: бот
    спрашивает один раз, а продлевает эпизод только сам человек кнопкой
    «⏳ Ещё идёт». Имя job одно на эпизод, поэтому новый вызов заменяет прежний.
    """
    if job_queue is None or minutes <= 0:
        return
    _cancel_reminder(job_queue, user_id, episode_id)
    job_queue.run_once(
        _remind,
        when=timedelta(minutes=minutes),
        # Бот личный, диалог приватный: chat_id совпадает с user_id.
        chat_id=user_id,
        user_id=user_id,
        data={"episode_id": episode_id, "final": final},
        name=_job_name(user_id, episode_id),
    )


async def _remind(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Эпизод всё ещё открыт — спрашиваем, что с ним."""
    job = context.job
    user_id, chat_id = job.user_id, job.chat_id
    ep = db.get_episode(user_id, job.data["episode_id"])
    if ep is None or not ep.is_open:
        return
    lang = _job_lang(user_id)
    final = bool(job.data.get("final"))
    stale = ep.is_stale(config.STALE_AFTER_MIN)
    if final and not stale:
        # STALE_AFTER_MIN задан вручную больше окна: ещё рано называть эпизод
        # незавершённым — дождёмся фактического порога, чтобы слова бота
        # совпадали с тем, что показывают карточка и отчёт.
        anchor = ep.confirmed_at or ep.started_at
        due = anchor + timedelta(minutes=config.STALE_AFTER_MIN)
        left = (due - db.utcnow()).total_seconds() / 60
        _schedule_reminder(context.job_queue, user_id, ep.id, max(1, int(left) + 1), final=True)
        return
    if final or stale:
        # Вопрос был задан и остался без ответа: эпизод закрывать нечем, время
        # окончания выдумывать нельзя. Фиксируем как есть и предлагаем указать
        # длительность по памяти. Больше не напоминаем.
        await context.bot.send_message(
            chat_id,
            t(lang, "remind_stale", id=ep.id, time=report.hhmm(ep.started_at),
              day=report._day_prefix(ep.started_at, lang)),
            reply_markup=InlineKeyboardMarkup(duration_rows(ep, lang)),
        )
        return
    await context.bot.send_message(
        chat_id,
        t(lang, "remind_ask", id=ep.id, dur=report.human_duration(ep.duration(), lang),
          minutes=config.EPISODE_WINDOW_MIN),
        reply_markup=remind_keyboard(ep, lang),
    )
    _schedule_reminder(
        context.job_queue, user_id, ep.id, config.EPISODE_WINDOW_MIN, final=True
    )


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


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = _lang(update)
    pending = context.user_data.pop("await", None)
    await update.message.reply_text(
        t(lang, "cancelled" if pending else "nothing_to_cancel"),
        reply_markup=main_keyboard(_active(_uid(update)) is not None, lang),
    )


async def action_start_episode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = _uid(update)
    lang = _lang(update)
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
    ep = db.start_episode(user_id, started_at=update.message.date)
    await update.message.reply_text(
        t(lang, "ep_logged", id=ep.id, time=report.hhmm(ep.started_at)),
        reply_markup=main_keyboard(True, lang),
    )
    await _send_card(update, context, ep, lang)
    _schedule_reminder(context.job_queue, user_id, ep.id, config.EPISODE_WINDOW_MIN)
    await _mention_forgotten(update.effective_chat.id, user_id, context, lang, skip_id=ep.id)


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
    _cancel_reminder(context.job_queue, user_id, episode_id)
    await update.message.reply_text(
        t(lang, "ep_closed", id=ep.id, dur=report.human_duration(ep.duration(), lang)),
        reply_markup=main_keyboard(False, lang),
    )
    await _send_card(update, context, ep, lang)


async def action_med(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = _uid(update)
    lang = _lang(update)
    med = db.add_med(user_id, taken_at=update.message.date)
    recent = db.recent_med_names(user_id)
    rows = [
        [InlineKeyboardButton(name, callback_data=f"mn:{med.id}:{i}")]
        for i, name in enumerate(recent)
    ]
    # Снимок именно этого набора кнопок: индекс в callback_data должен
    # разрешаться тем списком, который человек видел, а не свежим запросом.
    context.user_data[f"med_opts:{med.id}"] = list(recent)
    rows.append([
        InlineKeyboardButton(t(lang, "btn_med_other"), callback_data=f"mt:{med.id}"),
        InlineKeyboardButton(t(lang, "btn_med_cancel"), callback_data=f"md:{med.id}"),
    ])
    await update.message.reply_text(
        t(lang, "med_logged", time=report.hhmm(med.taken_at)),
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def action_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_day(update.effective_chat.id, _uid(update), context, report.today_local(),
                    _lang(update))


async def cmd_yesterday(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_day(update.effective_chat.id, _uid(update), context,
                    report.today_local() - timedelta(days=1), _lang(update))


async def _send_day(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE,
                    day: date, lang: str) -> None:
    start, end = report.day_bounds(day)
    episodes = db.list_episodes(user_id, start, end)
    meds = db.list_meds(user_id, start, end)
    await _send_text(
        context, chat_id, report.day_summary(day, episodes, meds, lang), _day_keyboard(day)
    )


async def action_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = _lang(update)
    await _send_text(
        context, update.effective_chat.id, _report_text(_uid(update), 7, lang),
        _report_keyboard(7, lang),
    )


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
    await _send_csv(update.effective_chat.id, _uid(update), context, _lang(update))


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
            name = i18n.trim_utf16(text, MED_NAME_LIMIT)
            db.set_med_name(user_id, target_id, name)
            await update.message.reply_text(
                t(lang, "med_saved", name=name),
                reply_markup=main_keyboard(_active(user_id) is not None, lang),
            )
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
            _day_keyboard(day),
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
        ep = db.start_episode(user_id)
        status = db.NOTE_OK
        if note:
            ep, status = db.append_note(user_id, ep.id, note)
        await query.edit_message_text(
            _note_header(lang, status, t(lang, "ep_recorded", id=ep.id))
        )
        await _send_card(update, context, ep, lang)
        _schedule_reminder(context.job_queue, user_id, ep.id, config.EPISODE_WINDOW_MIN)
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
        ep = db.start_episode(user_id)
        ep = db.set_pulse(user_id, ep.id, pulse)
        await query.edit_message_text(t(lang, "ep_recorded_pulse", id=ep.id, pulse=ep.pulse))
        await _send_card(update, context, ep, lang)
        _schedule_reminder(context.job_queue, user_id, ep.id, config.EPISODE_WINDOW_MIN)
        return
    if action in ("mn", "mt", "md"):  # лекарство: имя из истории / ввести / отменить
        med_id = _row_id(parts)
        med = db.get_med(user_id, med_id) if med_id is not None else None
        if med is None:
            # Чужой или несуществующий id: ничего не делаем и, главное, не
            # переводим бота в режим ожидания ввода для этой записи.
            await ack()
            return
        if action == "mn":
            # M4: берём снимок списка, сделанный в момент отправки кнопок.
            # Перечитывать из БД нельзя: порядок там «по последнему приёму» и
            # меняется, поэтому индекс начинал указывать на другое лекарство.
            names = context.user_data.get(f"med_opts:{med_id}") or []
            idx = _arg(parts, 2, range(len(names))) if names else None
            name = names[idx] if idx is not None else None
            if name:
                db.set_med_name(user_id, med_id, name)
                context.user_data.pop(f"med_opts:{med_id}", None)
                await ack(t(lang, "ack_saved"))
                await query.edit_message_text(t(lang, "med_saved", name=name))
            else:
                await ack(t(lang, "med_list_stale"))
                if med.name is None:
                    # Снимок потерян рестартом — просим название, но только для
                    # ещё не названной своей записи.
                    await query.message.reply_text(t(lang, "med_name_prompt"))
                    context.user_data["await"] = {"what": "med", "id": med_id}
            return
        if action == "mt":
            context.user_data.pop(f"med_opts:{med_id}", None)
            context.user_data["await"] = {"what": "med", "id": med_id}
            await ack()
            await query.edit_message_text(t(lang, "med_name_prompt"))
            return
        context.user_data.pop(f"med_opts:{med_id}", None)
        db.delete_med(user_id, med_id)
        await ack(t(lang, "ack_deleted"))
        await query.edit_message_text(t(lang, "med_deleted"))
        return

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

    # M2: кнопки живут в истории чата вечно, и старая карточка нарисована в том
    # состоянии, которое было на момент отправки. Применять её действие к
    # эпизоду, который с тех пор изменился, нельзя: так закрытый эпизод получал
    # новое время окончания, а длительность 20 минут превращалась в 2 часа.
    if action in NEEDS_OPEN and not ep.is_open:
        await ack(t(lang, "ep_state_changed"))
        await _refresh(query, ep, lang, context)
        return
    if action in NEEDS_ACTIVE and (
        not ep.is_open or ep.is_stale(config.STALE_AFTER_MIN)
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
        if ep.note:
            # Заметка — единственное поле, которое раньше нельзя было
            # поправить: пульс перезаписывается, переключатели снимаются,
            # а дописанное в заметку убиралось только удалением эпизода.
            rows.insert(0, [InlineKeyboardButton(
                t(lang, "btn_clear_note"), callback_data=f"nc:{episode_id}"
            )])
        await query.message.reply_text(
            t(lang, "note_prompt", id=episode_id),
            reply_markup=InlineKeyboardMarkup(rows),
        )
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
        await ack()
        try:
            await query.edit_message_reply_markup(
                reply_markup=_toggle_keyboard(ep, "sym" if action == "ts" else "trg", lang)
            )
        except Exception as exc:
            if "not modified" not in str(exc):
                log.warning("Переключатель не обновился: %s", exc)
        return
    if action == "c":
        await ack()
        await _refresh(query, ep, lang)
        return
    if action == "e":
        closed = db.close_episode(user_id, episode_id)
        if closed is None:  # кто-то успел закрыть его раньше
            await ack(t(lang, "ep_state_changed"))
            await _refresh(query, db.get_episode(user_id, episode_id), lang)
            return
        ep = closed
        _cancel_reminder(context.job_queue, user_id, episode_id)
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
        reopened = db.reopen_episode(user_id, episode_id)
        if reopened is None:
            await ack(t(lang, "ep_state_changed"))
            await _refresh(query, db.get_episode(user_id, episode_id), lang)
            return
        ep = reopened
        _schedule_reminder(context.job_queue, user_id, episode_id, config.EPISODE_WINDOW_MIN)
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
        _schedule_reminder(context.job_queue, user_id, episode_id, config.EPISODE_WINDOW_MIN)
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
        _cancel_reminder(context.job_queue, user_id, episode_id)
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
        _cancel_reminder(context.job_queue, user_id, episode_id)
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
            await query.edit_message_text(
                t(lang, "delete_confirm", id=ep.id, time=report.hhmm(ep.started_at)),
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(t(lang, "btn_delete"), callback_data=f"dy:{ep.id}"),
                    InlineKeyboardButton(t(lang, "btn_back"), callback_data=f"c:{ep.id}"),
                ]]),
            )
        except Exception as exc:
            log.warning("Подтверждение удаления не показалось: %s", exc)
        return
    if action == "dy":
        db.delete_episode(user_id, episode_id)
        _cancel_reminder(context.job_queue, user_id, episode_id)
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
        BotCommand("log", t(lang, "cmd_log")),
        BotCommand("stop", t(lang, "cmd_stop")),
        BotCommand("last", t(lang, "cmd_last")),
        BotCommand("med", t(lang, "cmd_med")),
        BotCommand("today", t(lang, "cmd_today")),
        BotCommand("yesterday", t(lang, "cmd_yesterday")),
        BotCommand("week", t(lang, "cmd_week")),
        BotCommand("month", t(lang, "cmd_month")),
        BotCommand("export", t(lang, "cmd_export")),
        BotCommand("lang", t(lang, "cmd_lang")),
        BotCommand("cancel", t(lang, "cmd_cancel")),
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
        for ep in db.all_open_episodes():
            if ep.is_stale(config.STALE_AFTER_MIN):
                continue  # забытым эпизодам напоминания уже не помогут
            anchor = ep.confirmed_at or ep.started_at
            due = anchor + timedelta(minutes=config.EPISODE_WINDOW_MIN)
            left = (due - db.utcnow()).total_seconds() / 60
            _schedule_reminder(app.job_queue, ep.user_id, ep.id, max(1, int(left)))
            restored += 1
        if restored:
            log.info("Восстановлено напоминаний: %s", restored)

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
        ("cancel", cmd_cancel), ("log", action_start_episode),
        ("stop", action_end_episode), ("med", action_med), ("last", cmd_last),
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
