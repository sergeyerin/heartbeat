#!/usr/bin/env python3
"""Конфигурация бота: всё через .env."""
from __future__ import annotations

import os
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

# Файл БД. В Docker каталог data/ смонтирован как volume — иначе история
# исчезнет при пересборке образа.
DB_PATH = os.getenv("DB_PATH", "data/heartbeat.db")

# Зона для отображения времени. В БД всё лежит в UTC.
TZ_NAME = os.getenv("TZ", "Europe/Moscow")

# Язык по умолчанию: берётся, когда профиль Telegram не на ru/en/pt и
# пользователь не выбрал язык сам командой /lang.
DEFAULT_LANG = os.getenv("DEFAULT_LANG", "en")

# Насколько давно закончившийся эпизод ещё считается «текущим»: столько минут
# после конца простой текст/число всё ещё дописывается в него, а не создаёт новый.
RECENT_EPISODE_MIN = int(os.getenv("RECENT_EPISODE_MIN", "180"))

# Как часто перерисовывать карточку идущего эпизода, секунд. 0 = не обновлять.
# Чаще минуты незачем: секунды в дневнике не нужны, а каждая правка — вызов API.
CARD_TICK_SEC = int(os.getenv("CARD_TICK_SEC", "60"))

# Окно эпизода. Через столько минут после начала бот спрашивает «отпустило?»,
# и на столько же «⏳ Ещё идёт» продлевает эпизод. 0 = не спрашивать.
EPISODE_WINDOW_MIN = int(os.getenv("EPISODE_WINDOW_MIN", "30"))

# Сколько минут молчания означают «окончание не отмечено»: вопрос задан и
# остался без ответа ещё одно окно. Такой эпизод не блокирует новую запись и не
# ловит свободный ввод, но и не выбрасывается — остаётся в дневнике с неизвестной
# длительностью. Отсчёт идёт от последнего «ещё идёт», а не от начала: приступ
# действительно может длиться часами, пока человек его подтверждает.
# Если вопросы выключены (окно = 0), держим прежние 6 часов.
_DEFAULT_STALE_MIN = EPISODE_WINDOW_MIN * 2 if EPISODE_WINDOW_MIN > 0 else 360
STALE_AFTER_MIN = int(os.getenv("STALE_AFTER_MIN", str(_DEFAULT_STALE_MIN)))


def local_tz() -> ZoneInfo:
    try:
        return ZoneInfo(TZ_NAME)
    except Exception:
        return ZoneInfo("UTC")


def check() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit(
            "Не задан TELEGRAM_BOT_TOKEN. Создайте .env по образцу .env.example."
        )
