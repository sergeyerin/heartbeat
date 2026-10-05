#!/usr/bin/env python3
"""SQLite-хранилище эпизодов аритмии и приёмов лекарств.

Время в БД — всегда UTC ISO-8601 с секундной точностью. Перевод в локальную
зону делается только на отображении (``report.py``): смена TZ на сервере не
должна переписывать историю.

Доступ синхронный (stdlib ``sqlite3``): база локальная, однопользовательская,
запросы занимают микросекунды — отдельный async-слой тут только мешал бы.
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from i18n import tail_lines_utf16, trim_utf16, utf16_len

_CREATE_EPISODES = """
CREATE TABLE IF NOT EXISTS episodes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    started_at TEXT    NOT NULL,          -- UTC ISO
    ended_at   TEXT,                      -- UTC ISO; NULL = эпизод ещё идёт
    end_approx INTEGER NOT NULL DEFAULT 0, -- 1 = длительность указана примерно
    end_unknown INTEGER NOT NULL DEFAULT 0, -- 1 = человек сказал «не знаю»
    lat        REAL,                       -- где это было, если отметили
    lon        REAL,
    card_msg   INTEGER,                    -- id сообщения с живой карточкой
    remind_stage INTEGER NOT NULL DEFAULT 0, -- 0 = не спрашивали, 1 = уже спросили
    stale_shown INTEGER NOT NULL DEFAULT 0, -- 1 = переход в «забыт» уже показан
    confirmed_at TEXT,                    -- когда последний раз сказали «ещё идёт»
    severity   INTEGER,                   -- 1 терпимо / 2 средне / 3 тяжело
    pulse      INTEGER,
    symptoms   TEXT    NOT NULL DEFAULT '',  -- коды через запятую
    triggers   TEXT    NOT NULL DEFAULT '',
    note       TEXT,
    created_at TEXT    NOT NULL
)
"""

# Лекарства — отдельной таблицей: у приёма нет длительности, а в сводке по дню
# он нужен рядом с эпизодами (видно, что было до, что после).
_CREATE_MEDS = """
CREATE TABLE IF NOT EXISTS meds (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id  INTEGER NOT NULL,
    taken_at TEXT    NOT NULL,            -- UTC ISO
    name     TEXT
)
"""

# Выбор языка командой /lang. Нужен именно в БД, а не в user_data: напоминания
# шлёт job без апдейта, профиля Telegram там нет, а рестарт обнуляет память.
_CREATE_PREFS = """
CREATE TABLE IF NOT EXISTS user_prefs (
    user_id INTEGER PRIMARY KEY,
    lang    TEXT
)
"""

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_episodes_user_time ON episodes(user_id, started_at)",
    "CREATE INDEX IF NOT EXISTS idx_meds_user_time ON meds(user_id, taken_at)",
)

# Лимиты заметки — в UTF-16-единицах, как их считает Telegram (см.
# i18n.utf16_len): по символам эмодзи дают вдвое больший запас, чем есть.
# Сообщение Telegram — 4096 единиц, а карточка эпизода и сводка дня включают
# заметку, поэтому без лимита одна длинная заметка делает их неотправляемыми
# навсегда. Остаток до 4096 — запас на остальные строки карточки.
MAX_NOTE_CHUNK = 1000
MAX_NOTE_TOTAL = 3000
# Что произошло с заметкой при дописывании
NOTE_OK = "ok"
NOTE_CHUNK_TRIMMED = "chunk_trimmed"   # одно сообщение было длиннее лимита
NOTE_FULL = "full"                     # места не осталось, ничего не записано

_conn: sqlite3.Connection | None = None


@dataclass
class Episode:
    id: int
    user_id: int
    started_at: datetime
    ended_at: datetime | None = None
    severity: int | None = None
    pulse: int | None = None
    symptoms: list[str] = field(default_factory=list)
    triggers: list[str] = field(default_factory=list)
    note: str | None = None
    end_approx: bool = False
    end_unknown: bool = False
    lat: float | None = None
    lon: float | None = None
    card_msg: int | None = None
    remind_stage: int = 0
    stale_shown: bool = False
    confirmed_at: datetime | None = None

    @property
    def is_open(self) -> bool:
        return self.ended_at is None

    def is_stale(self, stale_after_min: int, now: datetime | None = None) -> bool:
        """Открытый эпизод, про который давно ничего не подтверждали."""
        if not self.is_open or stale_after_min <= 0:
            return False
        anchor = self.confirmed_at or self.started_at
        return (now or utcnow()) - anchor >= timedelta(minutes=stale_after_min)

    def needs_end(self, stale_after_min: int, now: datetime | None = None) -> bool:
        """Открыт, но «идёт» про него сказать нельзя: окончание объявлено
        неизвестным («🤷 Не знаю», ретроспектива) или эпизод забыт.

        Везде, где карточка, сводка или кнопки ветвятся по «забыт ли», ветвиться
        надо по этому методу: свежая ретроспективная запись с неизвестным концом
        рисовалась как «идёт, уже 29 мин», перехватывала свободный ввод и
        блокировала «⚡️ Аритмия» — а при живом приступе воровала у него статус
        текущего, и «✅ Отпустило» закрывало её точным выдуманным временем.
        """
        return self.is_open and (self.end_unknown or self.is_stale(stale_after_min, now))

    def duration(self, now: datetime | None = None) -> timedelta:
        end = self.ended_at or (now or utcnow())
        return end - self.started_at


@dataclass
class Med:
    id: int
    user_id: int
    taken_at: datetime
    name: str | None = None


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _codes(value: str | None) -> list[str]:
    return [c for c in (value or "").split(",") if c]


def init(path: str) -> None:
    """Открывает БД и создаёт схему. Вызывать один раз при старте."""
    global _conn
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    _conn = sqlite3.connect(path, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    # WAL: запись не блокирует чтение, переживает жёсткий рестарт контейнера.
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute(_CREATE_EPISODES)
    _conn.execute(_CREATE_MEDS)
    _conn.execute(_CREATE_PREFS)
    _migrate(_conn)
    for stmt in _INDEXES:
        _conn.execute(stmt)
    _conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """Догоняет схему на базах, созданных более старой версией бота."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(episodes)")}
    if "end_approx" not in columns:
        conn.execute("ALTER TABLE episodes ADD COLUMN end_approx INTEGER NOT NULL DEFAULT 0")
    if "confirmed_at" not in columns:
        conn.execute("ALTER TABLE episodes ADD COLUMN confirmed_at TEXT")
    if "end_unknown" not in columns:
        conn.execute(
            "ALTER TABLE episodes ADD COLUMN end_unknown INTEGER NOT NULL DEFAULT 0"
        )
    if "lat" not in columns:
        conn.execute("ALTER TABLE episodes ADD COLUMN lat REAL")
        conn.execute("ALTER TABLE episodes ADD COLUMN lon REAL")
    if "card_msg" not in columns:
        conn.execute("ALTER TABLE episodes ADD COLUMN card_msg INTEGER")
    if "remind_stage" not in columns:
        conn.execute(
            "ALTER TABLE episodes ADD COLUMN remind_stage INTEGER NOT NULL DEFAULT 0"
        )
    if "stale_shown" not in columns:
        conn.execute(
            "ALTER TABLE episodes ADD COLUMN stale_shown INTEGER NOT NULL DEFAULT 0"
        )


def _db() -> sqlite3.Connection:
    if _conn is None:
        raise RuntimeError("db.init() не вызван")
    return _conn


def _row_to_episode(row: sqlite3.Row) -> Episode:
    return Episode(
        id=row["id"],
        user_id=row["user_id"],
        started_at=_parse(row["started_at"]),
        ended_at=_parse(row["ended_at"]),
        severity=row["severity"],
        pulse=row["pulse"],
        symptoms=_codes(row["symptoms"]),
        triggers=_codes(row["triggers"]),
        note=row["note"],
        end_approx=bool(row["end_approx"]),
        end_unknown=bool(row["end_unknown"]),
        lat=row["lat"],
        lon=row["lon"],
        card_msg=row["card_msg"],
        remind_stage=row["remind_stage"],
        stale_shown=bool(row["stale_shown"]),
        confirmed_at=_parse(row["confirmed_at"]),
    )


# --- эпизоды ---------------------------------------------------------------

def start_episode(user_id: int, started_at: datetime | None = None) -> Episode:
    now = utcnow()
    started = started_at or now
    cur = _db().execute(
        "INSERT INTO episodes (user_id, started_at, created_at) VALUES (?, ?, ?)",
        (user_id, _iso(started), _iso(now)),
    )
    _db().commit()
    return Episode(id=cur.lastrowid, user_id=user_id, started_at=started)


def get_episode(user_id: int, episode_id: int) -> Episode | None:
    row = _db().execute(
        "SELECT * FROM episodes WHERE id = ? AND user_id = ?", (episode_id, user_id)
    ).fetchone()
    return _row_to_episode(row) if row else None


def open_episode(user_id: int) -> Episode | None:
    """Любой незакрытый эпизод (если их несколько — самый свежий).

    Включая «забытые»: для статистики и восстановления напоминаний.
    Для логики бота нужен ``active_episode`` — он отсекает забытые.
    """
    row = _db().execute(
        "SELECT * FROM episodes WHERE user_id = ? AND ended_at IS NULL "
        "ORDER BY started_at DESC, id DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    return _row_to_episode(row) if row else None


def active_episode(user_id: int, stale_after_min: int) -> Episode | None:
    """Эпизод, который реально идёт прямо сейчас.

    Открытый и подтверждённый не позже ``stale_after_min`` минут назад. Забытый
    (никто не отметил окончание, на вопросы бота не ответили) сюда не попадает:
    иначе он навсегда заблокировал бы кнопку «⚡️ Аритмия» и перетягивал на себя
    весь свободный ввод.
    """
    cutoff = _iso(utcnow() - timedelta(minutes=stale_after_min))
    row = _db().execute(
        # end_unknown = 0 обязателен: человек сам сказал «не знаю, когда
        # кончилось» — такой эпизод по его же заявлению НЕ идёт сейчас.
        "SELECT * FROM episodes WHERE user_id = ? AND ended_at IS NULL "
        "AND end_unknown = 0 AND COALESCE(confirmed_at, started_at) > ? "
        "ORDER BY started_at DESC, id DESC LIMIT 1",
        (user_id, cutoff),
    ).fetchone()
    return _row_to_episode(row) if row else None


def stale_episodes(user_id: int, stale_after_min: int) -> list[Episode]:
    """Открытые эпизоды без отметки окончания — от свежего к старым.

    Эпизоды, по которым человек уже сказал «не знаю», исключены: он ответил
    на вопрос, и напоминать о них снова — это навязчивость, а принцип проекта
    в том, чтобы спросить один раз.
    """
    cutoff = _iso(utcnow() - timedelta(minutes=stale_after_min))
    return [
        _row_to_episode(r)
        for r in _db().execute(
            "SELECT * FROM episodes WHERE user_id = ? AND ended_at IS NULL "
            "AND end_unknown = 0 AND COALESCE(confirmed_at, started_at) <= ? "
            "ORDER BY started_at DESC, id DESC",
            (user_id, cutoff),
        ).fetchall()
    ]


def mark_end_unknown(user_id: int, episode_id: int) -> Episode | None:
    """«Не знаю, когда прошло»: факт приступа записан, окончание неизвестно."""
    cur = _db().execute(
        "UPDATE episodes SET end_unknown = 1 "
        "WHERE id = ? AND user_id = ? AND ended_at IS NULL",
        (episode_id, user_id),
    )
    _db().commit()
    if cur.rowcount == 0:
        return None
    return get_episode(user_id, episode_id)


def all_open_episodes(stale_after_min: int, limit: int = 500) -> list[Episode]:
    """Открытые и ещё не забытые эпизоды всех пользователей.

    Для восстановления напоминаний после рестарта (job queue живёт в памяти).
    Фильтр и лимит — в SQL: раньше выбирались ВСЕ открытые эпизоды всех
    пользователей, и один человек с тысячами брошенных эпизодов тормозил старт
    всем остальным, а при 256 МБ в контейнере мог и не дать боту запуститься.
    """
    cutoff = _iso(utcnow() - timedelta(minutes=stale_after_min))
    return [
        _row_to_episode(r)
        for r in _db().execute(
            "SELECT * FROM episodes WHERE ended_at IS NULL AND end_unknown = 0 "
            "AND COALESCE(confirmed_at, started_at) > ? "
            "ORDER BY started_at ASC LIMIT ?",
            (cutoff, limit),
        ).fetchall()
    ]


def stale_cards_to_refresh(stale_after_min: int, limit: int = 500) -> list[Episode]:
    """Забытые открытые эпизоды с живой карточкой — чтобы догнать их после
    рестарта: тик для них не планируется, и их карточка осталась бы висеть на
    «идёт». Границы и LIMIT в SQL, как у all_open_episodes."""
    cutoff = _iso(utcnow() - timedelta(minutes=stale_after_min))
    return [
        _row_to_episode(r)
        for r in _db().execute(
            "SELECT * FROM episodes WHERE ended_at IS NULL AND end_unknown = 0 "
            "AND card_msg IS NOT NULL AND stale_shown = 0 "
            "AND COALESCE(confirmed_at, started_at) <= ? "
            "ORDER BY started_at DESC LIMIT ?",
            (cutoff, limit),
        ).fetchall()
    ]


def confirm_still_on(user_id: int, episode_id: int) -> Episode | None:
    """«Ещё идёт»: продлевает окно активности, не трогая время начала.

    Только для открытого эпизода: условие в UPDATE — второй рубеж после
    проверки в боте, как у close_episode.
    """
    cur = _db().execute(
        # «Ещё идёт» снимает и «не знаю»: раз идёт — конец не неизвестен,
        # его просто ещё нет.
        "UPDATE episodes SET confirmed_at = ?, end_unknown = 0, stale_shown = 0 "
        "WHERE id = ? AND user_id = ? AND ended_at IS NULL",
        (_iso(utcnow()), episode_id, user_id),
    )
    _db().commit()
    if cur.rowcount == 0:
        return None
    return get_episode(user_id, episode_id)


def last_episode(user_id: int) -> Episode | None:
    row = _db().execute(
        "SELECT * FROM episodes WHERE user_id = ? ORDER BY started_at DESC, id DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    return _row_to_episode(row) if row else None


def close_episode(user_id: int, episode_id: int, ended_at: datetime | None = None,
                  approx: bool = False) -> Episode | None:
    """Закрывает ОТКРЫТЫЙ эпизод. Возвращает None, если он уже закрыт.

    Условие ``ended_at IS NULL`` в UPDATE — второй рубеж после проверки в боте:
    инлайн-кнопки живут в истории чата вечно, и нажатие на старую карточку не
    должно перезаписывать уже записанное время окончания.
    """
    end = ended_at or utcnow()
    ep = get_episode(user_id, episode_id)
    if ep is None or not ep.is_open:
        return None
    # Отрицательной длительности быть не должно: сдвиг конца не раньше начала.
    if end < ep.started_at:
        end = ep.started_at
    cur = _db().execute(
        # end_unknown снимается: окончание записано, неизвестным оно больше
        # не является — иначе закрытая строка навсегда несла противоречие.
        "UPDATE episodes SET ended_at = ?, end_approx = ?, end_unknown = 0 "
        "WHERE id = ? AND user_id = ? AND ended_at IS NULL",
        (_iso(end), 1 if approx else 0, episode_id, user_id),
    )
    _db().commit()
    if cur.rowcount == 0:
        return None
    return get_episode(user_id, episode_id)


def reopen_episode(user_id: int, episode_id: int) -> Episode | None:
    """Возвращает ЗАКРЫТЫЙ эпизод в работу. None, если он и так открыт."""
    cur = _db().execute(
        "UPDATE episodes SET ended_at = NULL, end_approx = 0, end_unknown = 0, "
        "remind_stage = 0, stale_shown = 0, confirmed_at = ? "
        "WHERE id = ? AND user_id = ? AND ended_at IS NOT NULL",
        (_iso(utcnow()), episode_id, user_id),
    )
    _db().commit()
    if cur.rowcount == 0:
        return None
    return get_episode(user_id, episode_id)


def set_severity(user_id: int, episode_id: int, severity: int | None) -> Episode | None:
    _db().execute(
        "UPDATE episodes SET severity = ? WHERE id = ? AND user_id = ?",
        (severity, episode_id, user_id),
    )
    _db().commit()
    return get_episode(user_id, episode_id)


def set_pulse(user_id: int, episode_id: int, pulse: int | None) -> Episode | None:
    _db().execute(
        "UPDATE episodes SET pulse = ? WHERE id = ? AND user_id = ?",
        (pulse, episode_id, user_id),
    )
    _db().commit()
    return get_episode(user_id, episode_id)


def toggle_code(user_id: int, episode_id: int, column: str, code: str) -> Episode | None:
    """Переключает код в symptoms/triggers. ``column`` — только из белого списка."""
    if column not in ("symptoms", "triggers"):
        raise ValueError(column)
    ep = get_episode(user_id, episode_id)
    if ep is None:
        return None
    codes = list(ep.symptoms if column == "symptoms" else ep.triggers)
    if code in codes:
        codes.remove(code)
    else:
        codes.append(code)
    _db().execute(
        f"UPDATE episodes SET {column} = ? WHERE id = ? AND user_id = ?",
        (",".join(codes), episode_id, user_id),
    )
    _db().commit()
    return get_episode(user_id, episode_id)


def append_note(user_id: int, episode_id: int, text: str) -> tuple[Episode | None, str]:
    """Дописывает заметку. Возвращает (эпизод, что произошло).

    Если места не осталось, НИЧЕГО не записывается и возвращается ``NOTE_FULL``:
    молча проглотить текст и ответить «заметка добавлена» нельзя — человек мог
    написать «пульс 210, потерял сознание», и эта запись обязана либо попасть в
    дневник, либо получить честный отказ с кнопкой очистки.
    """
    ep = get_episode(user_id, episode_id)
    if ep is None:
        return None, NOTE_FULL
    chunk = trim_utf16(text, MAX_NOTE_CHUNK)
    status = NOTE_CHUNK_TRIMMED if chunk != text else NOTE_OK
    existing = ep.note or ""
    separator = "\n" if existing else ""
    room = MAX_NOTE_TOTAL - utf16_len(existing) - utf16_len(separator)
    if room < utf16_len(chunk):
        return ep, NOTE_FULL
    note = existing + separator + chunk
    _db().execute(
        "UPDATE episodes SET note = ? WHERE id = ? AND user_id = ?",
        (note, episode_id, user_id),
    )
    _db().commit()
    return get_episode(user_id, episode_id), status


NOTE_DROPPED_MARK = "[…]"


def append_note_dropping_oldest(
    user_id: int, episode_id: int, text: str
) -> tuple[Episode | None, bool]:
    """Дописывает заметку, вытесняя её самое старое начало.

    Нужно, когда места нет, а записать надо: в дневнике приступов свежая
    строка («пульс 210, потерял сознание») важнее самой старой. Альтернатива —
    заставлять человека удалить всё записанное ранее, чтобы записать новое, —
    для медицинского дневника неверный обмен.

    Возвращает (эпизод, пришлось ли что-то выбросить).
    """
    ep = get_episode(user_id, episode_id)
    if ep is None:
        return None, False
    chunk = trim_utf16(text, MAX_NOTE_CHUNK)
    existing = ep.note or ""
    if not existing:
        return append_note(user_id, episode_id, chunk)[0], False
    room = MAX_NOTE_TOTAL - utf16_len(chunk) - 1 - utf16_len(NOTE_DROPPED_MARK) - 1
    kept, dropped = tail_lines_utf16(existing, max(room, 0))
    head = f"{NOTE_DROPPED_MARK}\n{kept}" if dropped and kept else kept
    note = f"{head}\n{chunk}" if head else chunk
    _db().execute(
        "UPDATE episodes SET note = ? WHERE id = ? AND user_id = ?",
        (note, episode_id, user_id),
    )
    _db().commit()
    return get_episode(user_id, episode_id), dropped


def mark_stale_shown(user_id: int, episode_id: int) -> None:
    """Отметить, что переход эпизода в «забыт» уже показан пользователю —
    чтобы не слать плашку-сообщение на каждом рестарте."""
    _db().execute(
        "UPDATE episodes SET stale_shown = 1 WHERE id = ? AND user_id = ?",
        (episode_id, user_id),
    )
    _db().commit()


def set_remind_stage(user_id: int, episode_id: int, stage: int) -> None:
    """0 — вопрос «отпустило?» ещё не задан, 1 — задан и ждём ответа."""
    _db().execute(
        "UPDATE episodes SET remind_stage = ? WHERE id = ? AND user_id = ?",
        (stage, episode_id, user_id),
    )
    _db().commit()


def set_card_msg(user_id: int, episode_id: int, message_id: int | None) -> None:
    """Запоминает сообщение с карточкой, чтобы править его, а не слать новое."""
    _db().execute(
        "UPDATE episodes SET card_msg = ? WHERE id = ? AND user_id = ?",
        (message_id, episode_id, user_id),
    )
    _db().commit()


def set_place(user_id: int, episode_id: int,
              lat: float | None, lon: float | None) -> Episode | None:
    """Координаты эпизода. None/None стирает их — это право пользователя."""
    cur = _db().execute(
        "UPDATE episodes SET lat = ?, lon = ? WHERE id = ? AND user_id = ?",
        (lat, lon, episode_id, user_id),
    )
    _db().commit()
    if cur.rowcount == 0:
        return None
    return get_episode(user_id, episode_id)


def clear_note(user_id: int, episode_id: int) -> Episode | None:
    _db().execute(
        "UPDATE episodes SET note = NULL WHERE id = ? AND user_id = ?",
        (episode_id, user_id),
    )
    _db().commit()
    return get_episode(user_id, episode_id)


def shift_start(user_id: int, episode_id: int, minutes: int) -> Episode | None:
    """Сдвигает начало открытого эпизода (обычно назад: записал не сразу).

    Начало не уезжает в будущее и не перескакивает время окончания — иначе
    длительность стала бы отрицательной.
    """
    ep = get_episode(user_id, episode_id)
    if ep is None or not ep.is_open:
        return None
    started = min(ep.started_at + timedelta(minutes=minutes), utcnow())
    # confirmed_at обязательно обновить: «забытость» считается от
    # COALESCE(confirmed_at, started_at), поэтому сдвиг начала назад СТАРИЛ
    # эпизод. Два нажатия «⏪ −30 мин» переваливали порог, и идущий приступ
    # объявлялся незавершённым: пульс, набранный следующим сообщением, уходил
    # в никуда, плашка возвращалась к «Аритмия», а бот предлагал записать
    # второй эпизод про то же самое. Человек всего лишь уточнил время.
    cur = _db().execute(
        "UPDATE episodes SET started_at = ?, confirmed_at = ? "
        "WHERE id = ? AND user_id = ? AND ended_at IS NULL",
        (_iso(started), _iso(utcnow()), episode_id, user_id),
    )
    _db().commit()
    if cur.rowcount == 0:
        return None
    return get_episode(user_id, episode_id)


def shift_end(user_id: int, episode_id: int, minutes: int) -> Episode | None:
    """Сдвигает время ОКОНЧАНИЯ закрытого эпизода назад.

    Задержка у конца — систематическая: человек нажимает «отпустило» не когда
    полегчало, а когда вспомнил, и всегда позже, никогда раньше. За месяц это
    не усредняется, а раздувает и сумму, и среднее, и максимум.

    Отказывает, а не подрезает: подрезание до начала дало бы эпизод нулевой
    длительности — выдуманные данные. И всегда помечает длительность
    приблизительной: время, названное по памяти, это оценка.
    """
    ep = get_episode(user_id, episode_id)
    if ep is None or ep.is_open or ep.ended_at is None:
        return None
    ended = ep.ended_at + timedelta(minutes=minutes)
    if ended <= ep.started_at:
        return None
    cur = _db().execute(
        "UPDATE episodes SET ended_at = ?, end_approx = 1 "
        "WHERE id = ? AND user_id = ? AND ended_at IS NOT NULL",
        (_iso(ended), episode_id, user_id),
    )
    _db().commit()
    if cur.rowcount == 0:
        return None
    return get_episode(user_id, episode_id)


def delete_episode(user_id: int, episode_id: int) -> bool:
    cur = _db().execute(
        "DELETE FROM episodes WHERE id = ? AND user_id = ?", (episode_id, user_id)
    )
    _db().commit()
    return cur.rowcount > 0


def list_episodes(user_id: int, since: datetime, until: datetime | None = None) -> list[Episode]:
    """Эпизоды, НАЧАВШИЕСЯ в интервале [since, until). Порядок — по времени."""
    params: list = [user_id, _iso(since)]
    sql = "SELECT * FROM episodes WHERE user_id = ? AND started_at >= ?"
    if until is not None:
        sql += " AND started_at < ?"
        params.append(_iso(until))
    sql += " ORDER BY started_at ASC, id ASC"
    return [_row_to_episode(r) for r in _db().execute(sql, params).fetchall()]


def all_episodes(user_id: int) -> list[Episode]:
    return [
        _row_to_episode(r)
        for r in _db().execute(
            "SELECT * FROM episodes WHERE user_id = ? ORDER BY started_at ASC, id ASC",
            (user_id,),
        ).fetchall()
    ]


def count_open_episodes(user_id: int) -> int:
    return _db().execute(
        "SELECT COUNT(*) FROM episodes WHERE user_id = ? AND ended_at IS NULL",
        (user_id,),
    ).fetchone()[0]


def purge_user(user_id: int) -> dict[str, int]:
    """Удаляет ВСЁ про пользователя. Возвращает, сколько чего удалено.

    Право на удаление своих данных — не фича, а обязанность; для данных о
    здоровье тем более. Чистится всё, включая координаты (они уходят вместе со
    строками эпизодов) и выбранный язык.
    """
    counts = {
        "episodes": count_episodes(user_id),
        "meds": len(all_meds(user_id)),
    }
    for table in ("episodes", "meds", "user_prefs"):
        _db().execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
    _db().commit()
    return counts


def episode_at(user_id: int, started_at: datetime) -> Episode | None:
    """Эпизод, начатый ровно в этот момент. Защита от повторной кнопки:
    два настоящих приступа в одну и ту же минуту не записать и вручную."""
    row = _db().execute(
        "SELECT * FROM episodes WHERE user_id = ? AND started_at = ? LIMIT 1",
        (user_id, _iso(started_at)),
    ).fetchone()
    return _row_to_episode(row) if row else None


def count_episodes(user_id: int) -> int:
    return _db().execute(
        "SELECT COUNT(*) FROM episodes WHERE user_id = ?", (user_id,)
    ).fetchone()[0]


# --- настройки пользователя ---

def get_lang(user_id: int) -> str | None:
    row = _db().execute(
        "SELECT lang FROM user_prefs WHERE user_id = ?", (user_id,)
    ).fetchone()
    return row["lang"] if row else None


def set_lang(user_id: int, lang: str) -> None:
    _db().execute(
        "INSERT INTO user_prefs (user_id, lang) VALUES (?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET lang = excluded.lang",
        (user_id, lang),
    )
    _db().commit()


# --- лекарства -------------------------------------------------------------

def add_med(user_id: int, name: str | None = None, taken_at: datetime | None = None) -> Med:
    taken = taken_at or utcnow()
    cur = _db().execute(
        "INSERT INTO meds (user_id, taken_at, name) VALUES (?, ?, ?)",
        (user_id, _iso(taken), name),
    )
    _db().commit()
    return Med(id=cur.lastrowid, user_id=user_id, taken_at=taken, name=name)


def get_med(user_id: int, med_id: int) -> Med | None:
    """Приём лекарства по id, только свой. Нужен, чтобы подделанный payload не
    заставлял бота ждать ввода для чужой или несуществующей записи."""
    row = _db().execute(
        "SELECT * FROM meds WHERE id = ? AND user_id = ?", (med_id, user_id)
    ).fetchone()
    if row is None:
        return None
    return Med(id=row["id"], user_id=row["user_id"], taken_at=_parse(row["taken_at"]),
               name=row["name"])


def set_med_name(user_id: int, med_id: int, name: str) -> None:
    _db().execute(
        "UPDATE meds SET name = ? WHERE id = ? AND user_id = ?", (name, med_id, user_id)
    )
    _db().commit()


def delete_med(user_id: int, med_id: int) -> bool:
    cur = _db().execute("DELETE FROM meds WHERE id = ? AND user_id = ?", (med_id, user_id))
    _db().commit()
    return cur.rowcount > 0


def list_meds(user_id: int, since: datetime, until: datetime | None = None) -> list[Med]:
    params: list = [user_id, _iso(since)]
    sql = "SELECT * FROM meds WHERE user_id = ? AND taken_at >= ?"
    if until is not None:
        sql += " AND taken_at < ?"
        params.append(_iso(until))
    sql += " ORDER BY taken_at ASC, id ASC"
    return [
        Med(id=r["id"], user_id=r["user_id"], taken_at=_parse(r["taken_at"]), name=r["name"])
        for r in _db().execute(sql, params).fetchall()
    ]


def all_meds(user_id: int) -> list[Med]:
    return [
        Med(id=r["id"], user_id=r["user_id"], taken_at=_parse(r["taken_at"]), name=r["name"])
        for r in _db().execute(
            "SELECT * FROM meds WHERE user_id = ? ORDER BY taken_at ASC, id ASC", (user_id,)
        ).fetchall()
    ]


def recent_med_names(user_id: int, limit: int = 4) -> list[str]:
    """Названия лекарств, которые пользователь вводил раньше — для кнопок."""
    rows = _db().execute(
        "SELECT name, MAX(taken_at) AS last FROM meds "
        "WHERE user_id = ? AND name IS NOT NULL AND name <> '' "
        "GROUP BY name ORDER BY last DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    return [r["name"] for r in rows]
