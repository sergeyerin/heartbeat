#!/usr/bin/env python3
"""Форматирование: карточка эпизода, сводка за день, отчёт за период, CSV.

Каждая функция принимает язык — тексты берутся из ``i18n``. Всё отдаётся простым
текстом без parse_mode: в заметках пользователя легко встречаются `_`, `*`, `[`,
на Markdown они ломают отправку сообщения.
"""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta, timezone

import config
import vocab
from db import Episode, Med
from i18n import csv_header, months, t, trim_utf16, utf16_len, weekdays

# Границы «времени суток» для гистограммы отчёта
HOUR_BUCKETS = ((0, 6), (6, 12), (12, 18), (18, 24))

# Excel и LibreOffice исполняют ячейку, начинающуюся с этих символов. Файл
# задуман для передачи врачу, то есть открывается на чужой машине, поэтому
# любое пользовательское поле обезвреживается апострофом.
CSV_RISKY_PREFIX = ("=", "+", "-", "@", "\t", "\r")

# Длина заметки в ленте дня, в UTF-16-единицах (как считает Telegram): в сводке
# заметок может быть много, а лимит сообщения один на всех. Полный текст виден
# в карточке эпизода.
FEED_NOTE_LIMIT = 200


def csv_safe(value):
    """Обезвреживает ячейку CSV, не меняя её содержимого для человека."""
    if isinstance(value, str) and value[:1] in CSV_RISKY_PREFIX:
        return "'" + value
    return value


def local(dt: datetime) -> datetime:
    return dt.astimezone(config.local_tz())


def today_local() -> date:
    return local(datetime.now(timezone.utc)).date()


def day_bounds(day: date) -> tuple[datetime, datetime]:
    """Границы локальных суток в UTC — для выборок из БД."""
    tz = config.local_tz()
    start = datetime.combine(day, datetime.min.time(), tzinfo=tz)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


def hhmm(dt: datetime) -> str:
    return local(dt).strftime("%H:%M")


def date_day_month(day: date, lang: str) -> str:
    return t(lang, "date_day_month", day=day.day, month=months(lang)[day.month - 1])


def day_title(day: date, lang: str) -> str:
    return f"{date_day_month(day, lang)}, {weekdays(lang)[day.weekday()]}"


def human_duration(td: timedelta, lang: str) -> str:
    secs = max(0, int(td.total_seconds()))
    if secs < 60:
        return t(lang, "dur_sec", n=secs)
    minutes = secs // 60
    if minutes < 60:
        return t(lang, "dur_min", n=minutes)
    hours, mins = divmod(minutes, 60)
    if hours < 24:
        return t(lang, "dur_h", h=hours) if mins == 0 else t(lang, "dur_h_m", h=hours, m=mins)
    # Приступ может длиться сутки и дольше — «336 ч» глазами не читается.
    days, hours = divmod(hours, 24)
    return t(lang, "dur_d", d=days) if hours == 0 else t(lang, "dur_d_h", d=days, h=hours)


def duration_label(ep: Episode, lang: str) -> str:
    """Длительность эпизода с «~», если она указана приблизительно."""
    return ("~" if ep.end_approx else "") + human_duration(ep.duration(), lang)


def _day_prefix(dt: datetime, lang: str, today: date | None = None) -> str:
    """«сегодня» / «вчера» / «3 октября» — чтобы не читать даты глазами."""
    day = local(dt).date()
    today = today or today_local()
    if day == today:
        return t(lang, "day_today")
    if day == today - timedelta(days=1):
        return t(lang, "day_yesterday")
    return date_day_month(day, lang)


def place_link(ep: Episode) -> str | None:
    """Ссылка на точку. OpenStreetMap, а не Google: без аккаунта и трекинга."""
    if ep.lat is None or ep.lon is None:
        return None
    return f"https://www.openstreetmap.org/?mlat={ep.lat:.5f}&mlon={ep.lon:.5f}#map=16/{ep.lat:.5f}/{ep.lon:.5f}"


# --- карточка эпизода ------------------------------------------------------

def episode_card(ep: Episode, lang: str) -> str:
    now = datetime.now(timezone.utc)
    lines = []
    if ep.needs_end(config.STALE_AFTER_MIN):
        lines.append(t(lang, "card_stale", id=ep.id))
        lines.append(t(lang, "card_start", day=_day_prefix(ep.started_at, lang),
                       time=hhmm(ep.started_at)))
        lines.append(t(lang, "card_unknown_dur", dur=human_duration(ep.duration(now), lang)))
        # Подсказка «отметьте кнопкой ниже» убрана: кнопка «⏱ Сколько длилось?»
        # теперь живая и находится прямо под этой строкой — текст сообщал бы
        # человеку нажать то, на что он и так смотрит, занимая строку на самой
        # перегруженной карточке.
    elif ep.is_open:
        # Секунды на открытой карточке не нужны: «идёт, уже 6 сек» — это шум,
        # а карточка всё равно обновляется раз в минуту.
        elapsed = ep.duration(now)
        if elapsed < timedelta(minutes=1):
            lines.append(t(lang, "card_open_fresh", id=ep.id))
        else:
            lines.append(t(lang, "card_open", id=ep.id,
                           dur=human_duration(elapsed, lang)))
        lines.append(t(lang, "card_start", day=_day_prefix(ep.started_at, lang),
                       time=hhmm(ep.started_at)))
    else:
        lines.append(t(lang, "card_closed", id=ep.id, dur=duration_label(ep, lang)))
        lines.append(
            t(lang, "card_range", day=_day_prefix(ep.started_at, lang),
              start=hhmm(ep.started_at), end=hhmm(ep.ended_at))
            + (t(lang, "card_approx_suffix") if ep.end_approx else "")
        )
    # Значение по умолчанию обязательно: иначе неожиданное число в колонке
    # выводилось пользователю как литерал «None».
    severity = (
        vocab.severity(lang).get(ep.severity, str(ep.severity))
        if ep.severity else t(lang, "dash")
    )
    lines.append(t(lang, "card_severity", value=severity))
    lines.append(t(lang, "card_pulse", value=ep.pulse or t(lang, "dash")))
    if ep.symptoms:
        lines.append(t(lang, "card_symptoms",
                       list=", ".join(vocab.labels(ep.symptoms, vocab.symptoms(lang)))))
    if ep.triggers:
        lines.append(t(lang, "card_triggers",
                       list=", ".join(vocab.labels(ep.triggers, vocab.triggers(lang)))))
    link = place_link(ep)
    if link:
        lines.append(t(lang, "card_place", link=link))
    if ep.note:
        lines.append(t(lang, "card_note", text=ep.note))
    return "\n".join(lines)


# --- сводка за день --------------------------------------------------------

def _episode_line(ep: Episode, now: datetime, lang: str) -> list[str]:
    if ep.needs_end(config.STALE_AFTER_MIN):
        head = t(lang, "line_stale", time=hhmm(ep.started_at))
    elif ep.is_open:
        head = t(lang, "line_open", time=hhmm(ep.started_at),
                 dur=human_duration(ep.duration(now), lang))
    else:
        head = t(lang, "line_closed", start=hhmm(ep.started_at), end=hhmm(ep.ended_at),
                 dur=duration_label(ep, lang))
    tail = []
    if ep.severity:
        # .get, а не [] — неожиданное значение в колонке (старая запись, ручная
        # правка базы) должно ухудшить одну строку, а не сломать всю сводку дня.
        tail.append(vocab.severity(lang).get(ep.severity, str(ep.severity)))
    if ep.pulse:
        tail.append(t(lang, "card_pulse", value=ep.pulse).lower())
    out = [head + (" · " + " · ".join(tail) if tail else "") + f"  [#{ep.id}]"]
    # Симптомы и причины — отдельными строками: в одной строке «слабость, кофе»
    # читается как один список и путает.
    if ep.symptoms:
        out.append(t(lang, "line_symptoms",
                     list=", ".join(vocab.labels(ep.symptoms, vocab.symptoms(lang)))))
    if ep.triggers:
        out.append(t(lang, "line_triggers",
                     list=", ".join(vocab.labels(ep.triggers, vocab.triggers(lang)))))
    if ep.note:
        # Заметка может быть многострочной — отбиваем каждую строку. В ленте
        # показываем начало: полный текст есть в карточке эпизода.
        for part in ep.note.splitlines():
            part = part.strip()
            if not part:
                continue
            if utf16_len(part) > FEED_NOTE_LIMIT:
                part = trim_utf16(part, FEED_NOTE_LIMIT) + "…"
            out.append("    « " + part + " »")
    return out


def day_summary(day: date, episodes: list[Episode], meds: list[Med], lang: str) -> str:
    now = datetime.now(timezone.utc)
    title = t(lang, "day_title_today") if day == today_local() else day_title(day, lang)
    lines = [t(lang, "day_header", title=title, date=day.strftime("%d.%m.%Y"))]
    if not episodes and not meds:
        lines.append(t(lang, "day_empty"))
        return "\n".join(lines)

    closed = [ep for ep in episodes if not ep.is_open]
    total = sum((ep.duration() for ep in closed), timedelta())
    summary = t(lang, "day_count", n=len(episodes))
    if closed:
        summary += t(lang, "day_total", dur=human_duration(total, lang))
    lines.append(summary)
    lines.append("")

    # Эпизоды и лекарства — одной лентой по времени.
    feed: list[tuple[datetime, list[str]]] = [
        (ep.started_at, _episode_line(ep, now, lang)) for ep in episodes
    ]
    feed += [
        (m.taken_at, [t(lang, "line_med", time=hhmm(m.taken_at),
                        name=m.name or t(lang, "med_default"))])
        for m in meds
    ]
    for _, block in sorted(feed, key=lambda item: item[0]):
        lines += block
    return "\n".join(lines)


# --- отчёт за период ------------------------------------------------------

def _bar(value: int, peak: int, width: int = 12) -> str:
    if peak <= 0:
        return ""
    filled = max(1, round(width * value / peak)) if value else 0
    return "█" * filled


def period_report(days: int, episodes: list[Episode], meds: list[Med], lang: str) -> str:
    end = today_local()
    start = end - timedelta(days=days - 1)
    head = t(lang, "rep_header", days=days,
             start=date_day_month(start, lang), end=date_day_month(end, lang))
    if not episodes:
        extra = "\n" + t(lang, "rep_meds", n=len(meds)) if meds else ""
        return f"{head}\n\n{t(lang, 'rep_empty')}{extra}"

    lines = [head, ""]
    lines.append(t(lang, "rep_count", n=len(episodes), per_day=f"{len(episodes) / days:.1f}"))

    closed = [ep for ep in episodes if not ep.is_open]
    if closed:
        durations = [ep.duration() for ep in closed]
        total = sum(durations, timedelta())
        longest = max(closed, key=lambda ep: ep.duration())
        lines.append(t(lang, "rep_duration",
                       total=human_duration(total, lang),
                       avg=human_duration(total / len(closed), lang),
                       max=duration_label(longest, lang),
                       day=_day_prefix(longest.started_at, lang)))
        approx = sum(1 for ep in closed if ep.end_approx)
        if approx:
            lines.append(t(lang, "rep_approx", n=approx))
    stale = [ep for ep in episodes if ep.needs_end(config.STALE_AFTER_MIN)]
    if stale:
        lines.append(t(lang, "rep_stale", n=len(stale)))
    if len(episodes) - len(closed) - len(stale):
        lines.append(t(lang, "rep_ongoing"))

    pulses = [ep.pulse for ep in episodes if ep.pulse]
    if pulses:
        lo, hi = min(pulses), max(pulses)
        lines.append(t(lang, "rep_pulse", range=f"{lo}–{hi}" if lo != hi else str(lo),
                       avg=round(sum(pulses) / len(pulses)), n=len(pulses)))

    sev_labels = vocab.severity(lang)
    sev_parts = [
        f"{sev_labels[level]} {sum(1 for ep in episodes if ep.severity == level)}"
        for level in (3, 2, 1)
        if any(ep.severity == level for ep in episodes)
    ]
    if sev_parts:
        lines.append(t(lang, "rep_severity", parts=" · ".join(sev_parts)))

    for title_key, vocabulary, attr in (
        ("rep_symptoms", vocab.symptoms(lang), "symptoms"),
        ("rep_triggers", vocab.triggers(lang), "triggers"),
    ):
        counts: dict[str, int] = {}
        for ep in episodes:
            for code in getattr(ep, attr):
                counts[code] = counts.get(code, 0) + 1
        if counts:
            top = sorted(counts.items(), key=lambda kv: -kv[1])[:5]
            lines.append("")
            lines.append(t(lang, title_key))
            lines += [f"  {vocabulary.get(c, c)} — {n}" for c, n in top]

    buckets = [0] * len(HOUR_BUCKETS)
    for ep in episodes:
        hour = local(ep.started_at).hour
        for i, (lo, hi) in enumerate(HOUR_BUCKETS):
            if lo <= hour < hi:
                buckets[i] += 1
    peak = max(buckets)
    lines.append("")
    lines.append(t(lang, "rep_hours"))
    for (lo, hi), count in zip(HOUR_BUCKETS, buckets):
        lines.append(f"  {lo:02d}–{hi:02d} {_bar(count, peak)} {count}")

    by_wd = [0] * 7
    for ep in episodes:
        by_wd[local(ep.started_at).weekday()] += 1
    names = weekdays(lang)
    lines.append(t(lang, "rep_weekdays", parts=" · ".join(
        f"{names[i]} {by_wd[i]}" for i in range(7) if by_wd[i]
    )))

    if meds:
        lines.append(t(lang, "rep_meds", n=len(meds)))
    return "\n".join(lines)


# --- карточка приёма лекарства ---------------------------------------------

def med_card(med: Med, lang: str) -> str:
    """Живая карточка приёма: сколько времени прошло — чтобы знать, когда
    принимать следующий раз. Бот интервал не знает и не подсказывает его —
    только честно считает «принято N назад»."""
    now = datetime.now(timezone.utc)
    name = med.name or t(lang, "med_unnamed")
    elapsed = med.since(now)
    if elapsed < timedelta(minutes=1):
        head = t(lang, "med_card_fresh", name=name)
    else:
        head = t(lang, "med_card_since", name=name,
                 dur=human_duration(elapsed, lang))
    when = t(lang, "med_card_when", day=_day_prefix(med.taken_at, lang),
             time=hhmm(med.taken_at))
    if med.approx:
        when += t(lang, "med_card_approx")
    return head + "\n" + when


# --- экспорт ---------------------------------------------------------------

def episodes_csv(episodes: list[Episode], meds: list[Med], lang: str) -> bytes:
    """CSV для врача/Excel. BOM — чтобы Excel не ломал кириллицу и диакритику."""
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow(csv_header(lang))
    sev = vocab.severity_plain(lang)
    rows: list[tuple[datetime, list]] = []
    for ep in episodes:
        start = local(ep.started_at)
        rows.append((ep.started_at, [
            t(lang, "csv_type_episode"),
            ep.id,
            start.strftime("%Y-%m-%d"),
            start.strftime("%H:%M"),
            local(ep.ended_at).strftime("%H:%M") if ep.ended_at else "",
            round(ep.duration().total_seconds() / 60) if ep.ended_at else "",
            t(lang, "csv_yes") if ep.end_approx else "",
            sev.get(ep.severity, ""),
            ep.pulse or "",
            csv_safe(", ".join(vocab.labels(ep.symptoms, vocab.symptoms(lang)))),
            csv_safe(", ".join(vocab.labels(ep.triggers, vocab.triggers(lang)))),
            csv_safe((ep.note or "").replace("\n", " / ").replace("\r", " ")),
            f"{ep.lat:.5f}" if ep.lat is not None else "",
            f"{ep.lon:.5f}" if ep.lon is not None else "",
        ]))
    for med in meds:
        taken = local(med.taken_at)
        rows.append((med.taken_at, [
            t(lang, "csv_type_med"), med.id, taken.strftime("%Y-%m-%d"),
            taken.strftime("%H:%M"), "", "",
            t(lang, "csv_yes") if med.approx else "",
            "", "", "", "", csv_safe(med.name or ""),
        ]))
    for _, row in sorted(rows, key=lambda item: item[0]):
        writer.writerow(row)
    return b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8")
