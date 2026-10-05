#!/usr/bin/env python3
"""Офлайн-прогон сценариев: хендлеры вызываются по-настоящему, но Telegram
подменён заглушкой. Проверяет связку «нажатие → запись в БД → ответ».

Запускается при сборке образа вместе со smoke_test.py.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "flow")
os.environ.setdefault("TZ", "Europe/Moscow")

from telegram import (  # noqa: E402
    CallbackQuery,
    Chat,
    Document,
    Message,
    MessageEntity,
    PhotoSize,
    Update,
    User,
)

import bot  # noqa: E402
import report  # noqa: E402
import config as cfg  # noqa: E402
import db  # noqa: E402
import i18n  # noqa: E402
from i18n import t  # noqa: E402

LANG = "ru"

USER = User(id=7, first_name="Т", is_bot=False, language_code="ru")
CHAT = Chat(id=7, type="private")
FAILURES: list[str] = []


def check(cond: bool, what: str) -> None:
    print(("  ok  " if cond else " FAIL ") + what)
    if not cond:
        FAILURES.append(what)


class FakeBot:
    """Ловит исходящие вызовы вместо отправки в Telegram."""

    def __init__(self) -> None:
        # PTB спрашивает defaults у бота при reply_text — без атрибута сработает
        # __getattr__ и вернёт функцию вместо None.
        self.defaults = None
        # CommandHandler сверяет адресацию «/cmd@bot» с именем бота
        self.username = "heartbeat_test_bot"
        self.sent: list[str] = []
        self.edited: list[str] = []
        self.documents: list[str] = []
        self.answers: list[str] = []
        self.markups: list[object] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append(text)
        self.markups.append(kwargs.get("reply_markup"))
        msg = Message(message_id=len(self.sent) + 100, date=datetime.now(timezone.utc),
                      chat=CHAT, from_user=USER, text=text)
        msg.set_bot(self)
        return msg

    async def edit_message_text(self, text=None, **kwargs):
        self.edited.append(text)
        # Клавиатуру при правке тоже запоминаем: без этого заглушка скрывала бы
        # ошибки в кнопках, которые бот ставит через edit, а не через send.
        self.markups.append(kwargs.get("reply_markup"))
        return True

    async def edit_message_reply_markup(self, **kwargs):
        self.markups.append(kwargs.get("reply_markup"))
        return True

    async def send_document(self, chat_id, document, **kwargs):
        self.documents.append(getattr(document, "filename", "?"))
        return True

    async def answer_callback_query(self, callback_query_id, text=None, **kwargs):
        self.answers.append(text or "")
        return True

    async def send_chat_action(self, *a, **k):
        return True

    def __getattr__(self, name):  # прочие вызовы API в сценариях не нужны
        async def noop(*a, **k):
            return True
        return noop


def build_handlers():
    """Тот же набор хендлеров, что и в боте: вызываем настоящую регистрацию.

    Раньше тест повторял список из build_app своей копией — и не замечал бы,
    например, пропавшего фильтра у команд.
    """
    collected = []

    class Collector:
        def add_handler(self, handler, group=0):
            collected.append(handler)

    bot.register_handlers(Collector())
    return collected


def age_out(user_id: int, episode_id: int, days: int = 2) -> None:
    """Переводит часы эпизода на `days` назад, сохраняя его длительность.

    Пишет в БД напрямую: это фикстура времени, а не действие пользователя.
    Через db.shift_start/close_episode так уже нельзя — они (правильно)
    отказываются менять закрытый эпизод.
    """
    ep = db.get_episode(user_id, episode_id)
    delta = (db.utcnow() - timedelta(days=days)) - ep.started_at
    ended = db._iso(ep.ended_at + delta) if ep.ended_at else None
    db._db().execute(
        "UPDATE episodes SET started_at = ?, ended_at = ?, confirmed_at = NULL "
        "WHERE id = ? AND user_id = ?",
        (db._iso(ep.started_at + delta), ended, episode_id, user_id),
    )
    db._db().commit()


class FakeJobQueue:
    """Ловит запланированные напоминания, не запуская планировщик."""

    def __init__(self) -> None:
        self.jobs: dict[str, dict] = {}

    def run_once(self, callback, when, chat_id=None, user_id=None, data=None,
                 name=None, job_kwargs=None):
        self.jobs[name] = {"callback": callback, "when": when, "chat_id": chat_id,
                           "user_id": user_id, "data": data, "name": name,
                           "job_kwargs": job_kwargs or {}}

    def get_jobs_by_name(self, name):
        job = self.jobs.get(name)
        return [SimpleNamespace(schedule_removal=lambda n=name: self.jobs.pop(n, None))] if job else []

    async def fire(self, name, fake):
        """Выполняет напоминание так, как это сделал бы планировщик."""
        spec = self.jobs.pop(name)
        job = SimpleNamespace(data=spec["data"], user_id=spec["user_id"], chat_id=spec["chat_id"])
        await bot._remind(SimpleNamespace(bot=fake, job=job, job_queue=self))


JQ = FakeJobQueue()


def make_context(fake: FakeBot, user_data: dict) -> SimpleNamespace:
    return SimpleNamespace(bot=fake, user_data=user_data, chat_data={}, bot_data={},
                           error=None, job_queue=JQ)


def text_update(fake: FakeBot, text: str, user: User = USER) -> Update:
    chat = Chat(id=user.id, type="private")
    msg = Message(message_id=1, date=datetime.now(timezone.utc), chat=chat,
                  from_user=user, text=text, entities=[])
    msg.set_bot(fake)
    return Update(update_id=1, message=msg)


def callback_update(fake: FakeBot, data: str, user: User = USER) -> Update:
    chat = Chat(id=user.id, type="private")
    msg = Message(message_id=2, date=datetime.now(timezone.utc), chat=chat,
                  from_user=user, text="card")
    msg.set_bot(fake)
    query = CallbackQuery(id="q", from_user=user, chat_instance="ci", data=data, message=msg)
    query.set_bot(fake)
    return Update(update_id=2, callback_query=query)


async def run() -> None:
    fake = FakeBot()
    ud: dict = {}
    ctx = make_context(fake, ud)
    uid = USER.id

    async def tap(text: str) -> None:
        upd = text_update(fake, text)
        handler = bot.on_menu_button if text in bot.ACTION_BY_TEXT else bot.on_text
        await handler(upd, make_context(fake, ud))

    async def press(data: str) -> None:
        await bot.on_callback(callback_update(fake, data), make_context(fake, ud))

    # 1. Одно нажатие — эпизод зарегистрирован.
    await tap(t(LANG, "btn_start"))
    ep = db.open_episode(uid)
    check(ep is not None, "нажатие «Аритмия» создаёт открытый эпизод")
    check(any("Записал" in t for t in fake.sent), "бот подтвердил запись")
    check(isinstance(fake.markups[0], type(bot.main_keyboard(True, LANG))), "плашка пришла с ответом")
    check(fake.markups[0].keyboard[0][0].text == t(LANG, "btn_end"),
          "после начала эпизода плашка предлагает «Отпустило»")

    # 2. Повторное нажатие не плодит второй эпизод.
    await tap(t(LANG, "btn_start"))
    check(db.count_episodes(uid) == 1, "второй эпизод не создаётся, пока первый открыт")

    # 3. Уточнения кнопками карточки.
    await press(f"s:{ep.id}:3")
    await press(f"m:{ep.id}:sym")
    await press(f"ts:{ep.id}:short")
    await press(f"ts:{ep.id}:dizzy")
    await press(f"c:{ep.id}")
    await press(f"m:{ep.id}:trg")
    await press(f"tt:{ep.id}:coffee")
    await press(f"c:{ep.id}")
    ep = db.get_episode(uid, ep.id)
    check(ep.severity == 3, "тяжесть выставлена кнопкой")
    check(ep.symptoms == ["short", "dizzy"], f"симптомы отмечены: {ep.symptoms}")
    check(ep.triggers == ["coffee"], "причина отмечена")

    # 4. Пульс: кнопка просит число, следующее сообщение его записывает.
    await press(f"p:{ep.id}")
    check(ud.get("await", {}).get("what") == "pulse", "бот ждёт пульс")
    await tap("136")
    check(db.get_episode(uid, ep.id).pulse == 136, "пульс записан из ответа")
    check("await" not in ud, "ожидание ввода снято")

    # 5. Сдвиг начала назад — «записал не сразу».
    before = db.get_episode(uid, ep.id).started_at
    await press(f"sh:{ep.id}:-15")
    check((before - db.get_episode(uid, ep.id).started_at).total_seconds() == 900,
          "начало сдвинулось на 15 минут назад")

    # 6. Свободный текст дописывается заметкой в текущий эпизод.
    await tap("тяжело дышать, сижу")
    check("тяжело дышать" in (db.get_episode(uid, ep.id).note or ""), "текст стал заметкой")

    # 7. Лекарство.
    await tap(t(LANG, "btn_med"))
    med_id = db.list_meds(uid, db.utcnow().replace(hour=0, minute=0, second=0))[-1].id
    await press(f"mt:{med_id}")
    await tap("конкор")
    check(db.all_meds(uid)[-1].name == "конкор", "название лекарства записано")

    # 8. Закрытие эпизода кнопкой на карточке.
    await press(f"e:{ep.id}")
    ep = db.get_episode(uid, ep.id)
    check(not ep.is_open, "эпизод закрыт inline-кнопкой")
    check(db.open_episode(uid) is None, "открытых эпизодов больше нет")
    check(fake.markups[-1].keyboard[0][0].text == t(LANG, "btn_start"),
          "плашка вернулась к «Аритмия» после закрытия")

    # 9. Нажатие «Отпустило», когда нечего закрывать.
    await tap(t(LANG, "btn_end"))
    check(any("нет активного эпизода" in t for t in fake.sent), "подсказка вместо ошибки")

    # 10. Число без эпизода предлагает создать его одним нажатием.
    # Сначала уводим эпизод в прошлое, потом закрываем: close_episode не даёт
    # концу уехать раньше начала, поэтому порядок важен.
    fake.sent.clear()
    age_out(uid, ep.id)
    await tap("150")
    check(any("Сначала отметьте эпизод" in t for t in fake.sent), "число без эпизода: спросил")
    await press("np:150")
    check(db.count_episodes(uid) == 2 and db.last_episode(uid).pulse == 150,
          "эпизод создан вместе с пульсом")

    # 11. Свободный текст без эпизода: подтверждение, затем запись.
    age_out(uid, db.last_episode(uid).id)
    await tap("колотится сердце")
    check(ud.get("pending_note") == "колотится сердце", "текст отложен до подтверждения")
    await press("nn")
    check("колотится" in (db.last_episode(uid).note or ""), "подтверждённый текст стал эпизодом")

    # 12. «Нажал Аритмию и забыл Отпустило».
    # Эпизод из предыдущего шага ещё открыт — закрываем, чтобы начать с чистого.
    leftover = db.active_episode(uid, cfg.STALE_AFTER_MIN)
    if leftover:
        await press(f"e:{leftover.id}")
    fake.sent.clear(); fake.edited.clear()
    await tap(t(LANG, "btn_start"))
    ep = db.active_episode(uid, cfg.STALE_AFTER_MIN)
    job = f"remind:{uid}:{ep.id}"
    check(job in JQ.jobs, "на новый эпизод поставлено напоминание «отпустило?»")
    check(JQ.jobs[job]["when"] == timedelta(minutes=cfg.EPISODE_WINDOW_MIN),
          f"напоминание через {cfg.EPISODE_WINDOW_MIN} мин")
    check(JQ.jobs[job]["data"].get("final") is False, "это обычный вопрос, не контрольный")
    # APScheduler по умолчанию выбрасывает задачу, опоздавшую больше секунды
    check(JQ.jobs[job]["job_kwargs"].get("misfire_grace_time", "missing") is None,
          "опоздавшее напоминание не выбрасывается планировщиком")

    # Сработало напоминание, эпизод ещё идёт.
    await JQ.fire(job, fake)
    check(any("Отпустило?" in t for t in fake.sent), "бот сам спросил, отпустило ли")
    check(job in JQ.jobs and JQ.jobs[job]["data"]["final"] is True,
          "после вопроса запланирован один контрольный заход, а не повтор")
    check(JQ.jobs[job]["when"] == timedelta(minutes=cfg.EPISODE_WINDOW_MIN),
          "контрольный заход — через то же окно")

    # «Ещё идёт» — эпизод продлевается на окно, начало не трогается.
    started = db.get_episode(uid, ep.id).started_at
    await press(f"go:{ep.id}")
    check(db.get_episode(uid, ep.id).started_at == started, "«ещё идёт» не сдвинуло начало")
    check(db.active_episode(uid, cfg.STALE_AFTER_MIN) is not None, "эпизод всё ещё активен")
    check(JQ.jobs[job]["when"] == timedelta(minutes=cfg.EPISODE_WINDOW_MIN),
          f"«ещё идёт» продлил эпизод на {cfg.EPISODE_WINDOW_MIN} мин")
    check(JQ.jobs[job]["data"].get("final") is False,
          "после продления снова обычный вопрос, а не контрольный")
    check(any("продолжается" in t for t in fake.edited), "бот подтвердил продление")

    # Второй вопрос — и на него не ответили: контрольный заход фиксирует эпизод
    # без времени окончания. Время подкручиваем через БД: ждать 30 минут в тесте
    # нечестно, а поведение зависит именно от «молчания длиной в два окна».
    await JQ.fire(job, fake)
    check(any("Отпустило?" in t for t in fake.sent), "вопрос задан снова после продления")
    db.shift_start(uid, ep.id, -60 * 20)
    db._db().execute("UPDATE episodes SET confirmed_at = NULL WHERE id = ?", (ep.id,))
    db._db().commit()
    fake.sent.clear()
    await JQ.fire(job, fake)
    check(any("без отметки окончания" in t for t in fake.sent),
          "по забытому эпизоду бот прислал вопрос о длительности")
    check(job not in JQ.jobs, "забытый эпизод больше не дёргает напоминаниями")
    check(db.active_episode(uid, cfg.STALE_AFTER_MIN) is None,
          "забытый эпизод перестал считаться текущим")

    # И главное: кнопка «Аритмия» снова работает.
    before = db.count_episodes(uid)
    fake.sent.clear()
    await tap(t(LANG, "btn_start"))
    check(db.count_episodes(uid) == before + 1,
          "новый эпизод записывается, несмотря на забытый старый")
    new_ep = db.active_episode(uid, cfg.STALE_AFTER_MIN)
    check(new_ep.id != ep.id, "записался именно новый эпизод")
    check(any("остался без окончания" in t for t in fake.sent),
          "бот напомнил про забытый эпизод, не мешая записать новый")

    # Свободный ввод уходит в новый эпизод, а не в забытый.
    await tap("130")
    check(db.get_episode(uid, new_ep.id).pulse == 130, "пульс ушёл в новый эпизод")
    check(db.get_episode(uid, ep.id).pulse is None, "забытый эпизод свободный ввод не ловит")

    # Длительность забытого — по памяти, помечается примерной.
    await press(f"ap:{ep.id}:30")
    stale_closed = db.get_episode(uid, ep.id)
    check(not stale_closed.is_open and stale_closed.end_approx,
          "длительность забытого эпизода записана как примерная")
    check(stale_closed.duration() == timedelta(minutes=30), "записано ровно 30 минут")

    # Вариант «не знаю»: эпизод остаётся без окончания, но не теряется.
    unknown = db.start_episode(uid, started_at=db.utcnow() - timedelta(hours=20))
    await press(f"unk:{unknown.id}")
    check(db.get_episode(uid, unknown.id) is not None, "эпизод «без окончания» сохранён")
    check(db.get_episode(uid, unknown.id).is_open, "и остался без времени окончания")
    check(f"remind:{uid}:{unknown.id}" not in JQ.jobs, "напоминания по нему выключены")
    db.delete_episode(uid, unknown.id)
    await press(f"e:{new_ep.id}")

    # 13. Сводка, отчёт, выгрузка.
    fake.sent.clear()
    await tap(t(LANG, "btn_today"))
    check(any("Сегодня" in t for t in fake.sent), "сводка за день отправлена")
    await tap(t(LANG, "btn_report"))
    check(any("Отчёт за 7" in t for t in fake.sent), "отчёт отправлен")
    await press("r:30")
    check(any("Отчёт за 30" in t for t in fake.edited), "период отчёта переключается")
    await tap(t(LANG, "btn_export"))
    check(fake.documents and fake.documents[-1].endswith(".csv"), "CSV выгружен")
    await press("dn:" + str(date(2026, 1, 1)))
    check(any("01.01.2026" in t for t in fake.edited), "листание по дням работает")

    # 14. Рестарт контейнера: напоминания по открытым эпизодам ставятся заново.
    JQ.jobs.clear()
    live = db.start_episode(uid)
    forgotten_long = db.start_episode(uid, started_at=db.utcnow() - timedelta(hours=20))
    await bot.post_init(SimpleNamespace(bot=fake, job_queue=JQ))
    check(f"remind:{uid}:{live.id}" in JQ.jobs, "после рестарта напоминание восстановлено")
    check(f"remind:{uid}:{forgotten_long.id}" not in JQ.jobs,
          "забытому эпизоду напоминание не ставится")
    db.delete_episode(uid, live.id); db.delete_episode(uid, forgotten_long.id)

    # 15. Языки: английский и португальский профили Telegram.
    for code, profile in (("en", "en-GB"),):
        peer = User(id=100, first_name="X", is_bot=False, language_code=profile)
        fake.sent.clear(); fake.markups.clear()
        upd = text_update(fake, t(code, "btn_start"), user=peer)
        await bot.on_menu_button(upd, make_context(fake, {}))
        peer_ep = db.active_episode(peer.id, cfg.STALE_AFTER_MIN)
        check(peer_ep is not None, f"{profile}: эпизод записан")
        expected = t(code, "ep_logged", id=peer_ep.id, time=report.hhmm(peer_ep.started_at))
        check(expected in fake.sent, f"{profile}: подтверждение на языке профиля")
        check(fake.markups[0].keyboard[0][0].text == t(code, "btn_end"),
              f"{profile}: плашка на языке профиля")
        check(t(code, "card_open", id=peer_ep.id, dur=report.human_duration(
                   peer_ep.duration(), code)).split("—")[0].strip() in " ".join(fake.sent),
              f"{profile}: карточка на языке профиля")
        check(db.get_lang(peer.id) == code, f"{profile}: язык запомнен в БД")
        # Напоминание уходит на том же языке, хотя профиля у job нет.
        await JQ.fire(f"remind:{peer.id}:{peer_ep.id}", fake)
        check(any(t(code, "remind_ask", id=peer_ep.id,
                    dur=report.human_duration(peer_ep.duration(), code),
                    minutes=cfg.EPISODE_WINDOW_MIN).split("\n")[0] in msg
                  for msg in fake.sent),
              f"{profile}: напоминание на языке профиля")

    # Незнакомый язык профиля → DEFAULT_LANG.
    german = User(id=150, first_name="D", is_bot=False, language_code="de-DE")
    fake.sent.clear()
    await bot.cmd_start(text_update(fake, "/start", user=german), make_context(fake, {}))
    check(t(cfg.DEFAULT_LANG, "start").split("\n")[0] in " ".join(fake.sent),
          "незнакомый язык профиля → язык по умолчанию")

    # /lang переключает язык и переживает «рестарт» (хранится в БД).
    fake.sent.clear(); fake.edited.clear()
    await bot.cmd_lang(text_update(fake, "/lang", user=german), make_context(fake, {}))
    check(any(t(cfg.DEFAULT_LANG, "lang_prompt") in m for m in fake.sent), "/lang спросил язык")
    await bot.on_callback(callback_update(fake, "lang:ru", user=german),
                          make_context(fake, {}))
    check(db.get_lang(german.id) == "ru", "выбор языка сохранён в БД")
    check(any(t("ru", "lang_set") in m for m in fake.edited), "подтверждение на новом языке")
    fake.sent.clear()
    await bot.cmd_help(text_update(fake, "/help", user=german), make_context(fake, {}))
    check(any("Команды" in m for m in fake.sent), "дальше бот говорит по-русски")
    # Неподдерживаемый код языка ничего не меняет
    await bot.on_callback(callback_update(fake, "lang:pt", user=german),
                          make_context(fake, {}))
    check(db.get_lang(german.id) == "ru", "неподдерживаемый lang:pt язык не сменил")

    # 16. M1: бот не работает в группах.
    group = Chat(id=-1001234567890, type="supergroup")
    handlers = build_handlers()

    def in_group(text: str, command: bool = False):
        ents = []
        if command:
            ents = [MessageEntity(type=MessageEntity.BOT_COMMAND, offset=0,
                                  length=len(text.split()[0]))]
        msg = Message(message_id=9, date=datetime.now(timezone.utc), chat=group,
                      from_user=USER, text=text, entities=ents)
        msg.set_bot(fake)
        upd = Update(update_id=9, message=msg)
        return upd, [h for h in handlers if h.check_update(upd) not in (False, None)]

    gupd, taken = in_group(t(LANG, "btn_start"))
    check(not bot._is_private(gupd), "сообщение из группы не считается приватным")
    check(not taken, f"кнопку из группы не берёт никто ({taken})")
    # Команды в группе — главный вектор: /export в чужом чате выгрузил бы дневник.
    # Entity обязателен, иначе CommandHandler не сработал бы и без фильтра,
    # и проверка была бы бессмысленной.
    for cmd in ("/export", "/today", "/week", "/log"):
        _, taken_cmd = in_group(cmd, command=True)
        check(not taken_cmd, f"команду {cmd} из группы не берёт никто ({taken_cmd})")
    # А в приватном чате те же команды обрабатываются
    priv = Message(message_id=10, date=datetime.now(timezone.utc), chat=CHAT,
                   from_user=USER, text="/export",
                   entities=[MessageEntity(type=MessageEntity.BOT_COMMAND,
                                           offset=0, length=7)])
    priv.set_bot(fake)
    check(any(h.check_update(Update(update_id=10, message=priv)) not in (False, None)
              for h in handlers), "в приватном чате команда обрабатывается")
    # Правка уже отправленной команды не должна переотправлять дневник
    edited = Message(message_id=11, date=datetime.now(timezone.utc), chat=CHAT,
                     from_user=USER, text="/export",
                     entities=[MessageEntity(type=MessageEntity.BOT_COMMAND,
                                             offset=0, length=7)])
    edited.set_bot(fake)
    check(not any(h.check_update(Update(update_id=11, edited_message=edited))
                  not in (False, None) for h in handlers),
          "правка команды не обрабатывается заново")

    # Нажатие инлайн-кнопки из группы: данные берутся по нажавшему, а ответ
    # ушёл бы в группу — именно так чужой дневник и утекал.
    gmsg = Message(message_id=12, date=datetime.now(timezone.utc), chat=group,
                   from_user=USER, text="card")
    gmsg.set_bot(fake)
    gq = CallbackQuery(id="g", from_user=USER, chat_instance="ci", data="csv", message=gmsg)
    gq.set_bot(fake)
    fake.documents.clear(); fake.sent.clear()
    await bot.on_callback(Update(update_id=10, callback_query=gq), make_context(fake, {}))
    check(not fake.documents and not fake.sent,
          "нажатие из группы не отправляет ни файла, ни сообщения")

    # 17. M3: подделанный callback_data ничего не ломает и ничего не пишет.
    victim = db.start_episode(uid)

    def fields(ep):
        return (ep.started_at, ep.ended_at, ep.confirmed_at, ep.severity, ep.pulse,
                tuple(ep.symptoms), tuple(ep.triggers), ep.note, ep.end_approx)

    before = fields(victim)
    hostile = [
        f"s:{victim.id}:7", f"s:{victim.id}:-1", f"s:{victim.id}:abc",
        f"ap:{victim.id}:999999999", f"ap:{victim.id}:7",
        f"sh:{victim.id}:-999999", f"sh:{victim.id}:99999",
        f"ts:{victim.id}:=HYPERLINK(1)", f"ts:{victim.id}:a,b", f"tt:{victim.id}:nope",
        "r:0", "r:-5", "r:99999999999", "dn:not-a-date", "dn:0001-01-01",
        "np:999999", "np:0", "mn:1:-1", "mn:99:0", "mt:abc", "lang:xx", "s:abc:1",
        "e:", "s", "", "dy:999999",
        # Формы, которые пропустила первая версия проверки
        f"m:{victim.id}", f"m:{victim.id}:junk", f"nc:{victim.id}:x",
        "go:0", f"go:{victim.id + 10 ** 6}", f"apm:{victim.id}", f"c:{victim.id}:x",
        f"mn:{10 ** 9}:999", "mn:0:0", "mt:0", "md:-1",
        # Числа, не влезающие в БД: раньше это был необработанный OverflowError
        f"md:{10 ** 30}", f"e:{2 ** 63}", f"dy:{2 ** 64}", f"s:{10 ** 40}:1",
        f"ap:{2 ** 63}:30", "r:" + "9" * 400,
    ]
    for data in hostile:
        try:
            await press(data)
        except Exception as exc:  # noqa: BLE001
            check(False, f"подделанный callback уронил хендлер: {data!r} → {exc!r}")
    fake.edited.clear()
    await press(f"m:{victim.id}:junk")
    check(not any(t(LANG, "menu_triggers", id=victim.id) in m for m in fake.edited),
          "m: с чужим аргументом не открывает меню причин")
    check(db.get_lang(uid) == LANG, "подделанный lang:xx не переключил язык")
    check("await" not in ud,
          f"подделанный callback не захватил ожидание ввода ({ud.get('await')})")
    after = db.get_episode(uid, victim.id)
    check(after is not None, "эпизод не удалён подделанным callback")
    check(fields(after) == before,
          "подделанный callback ничего не изменил в эпизоде")
    check(after.symptoms == [] and after.triggers == [],
          f"мусорные коды не попали в симптомы ({after.symptoms}, {after.triggers})")
    check(after.severity is None, "недопустимый уровень тяжести не записан")
    # Сводка дня после всего этого всё ещё собирается
    s_, e_ = report.day_bounds(report.today_local())
    check(bool(report.day_summary(report.today_local(),
                                  db.list_episodes(uid, s_, e_),
                                  db.list_meds(uid, s_, e_), LANG)),
          "сводка дня не сломана мусорными данными")
    db.delete_episode(uid, victim.id)

    # 18. M4: название лекарства берётся из снимка, а не из переставшего
    # совпадать индекса. Раньше нажатие на «Б» сохраняло «А».
    ud2: dict = {}
    ctx2 = make_context(fake, ud2)
    db.add_med(uid, "препарат-А", db.utcnow() - timedelta(days=4))
    db.add_med(uid, "препарат-Б", db.utcnow() - timedelta(days=3))
    await bot.action_med(text_update(fake, t(LANG, "btn_med")), ctx2)
    shown = [n for n in db.recent_med_names(uid) if n.startswith("препарат")]
    med_id = db.all_meds(uid)[-1].id
    snapshot = ud2.get(f"med_opts:{med_id}") or []
    check(snapshot == db.recent_med_names(uid),
          "снимок равен тому списку, который человек увидел")
    shown = snapshot
    # Пока карточка висит, порядок в БД меняется
    db.add_med(uid, shown[-1], db.utcnow())
    check(db.recent_med_names(uid) != shown, "порядок в БД успел измениться")
    await bot.on_callback(callback_update(fake, f"mn:{med_id}:0"), ctx2)
    saved = [m for m in db.all_meds(uid) if m.id == med_id][0]
    check(saved.name == shown[0],
          f"сохранено лекарство с нажатой кнопки ({saved.name!r} == {shown[0]!r})")

    # 19. Проверки состояния: сквозное поведение. Срабатывают два рубежа —
    # в боте и в SQL, и снаружи они неразличимы (это и есть смысл второго
    # рубежа). Отличить умеет только проверка «отпустило на забытом»: там
    # слой БД пропустил бы операцию, а бот — нет.
    probe = db.start_episode(uid)
    await press(f"e:{probe.id}")
    closed_dur = db.get_episode(uid, probe.id).duration()
    check(not db.get_episode(uid, probe.id).is_open, "эпизод закрыт кнопкой")
    fake.sent.clear(); fake.answers.clear()
    await press(f"e:{probe.id}")  # то же нажатие на «старой» карточке
    check(db.get_episode(uid, probe.id).duration() == closed_dur,
          "повторное нажатие на карточке не изменило длительность")
    check(t(LANG, "ep_state_changed") in fake.answers,
          f"бот сказал, что состояние изменилось ({fake.answers})")
    await press(f"sh:{probe.id}:-15")
    check(db.get_episode(uid, probe.id).duration() == closed_dur,
          "сдвиг начала на закрытом эпизоде отклонён")
    await press(f"ap:{probe.id}:60")
    check(not db.get_episode(uid, probe.id).end_approx,
          "примерная длительность не затёрла точную")
    # Возврат в работу применим только к закрытому
    await press(f"ro:{probe.id}")
    check(db.get_episode(uid, probe.id).is_open, "закрытый эпизод вернулся в работу")
    fake.answers.clear()
    await press(f"ro:{probe.id}")
    check(t(LANG, "ep_state_changed") in fake.answers,
          "повторный возврат в работу отклонён")

    # «Отпустило» на забытом эпизоде: длительность 20 часов не должна
    # записаться как точная — на свежей карточке такой кнопки уже нет, но в
    # истории чата она осталась.
    age_out(uid, probe.id, days=1)
    db._db().execute("UPDATE episodes SET ended_at = NULL WHERE id = ?", (probe.id,))
    db._db().commit()
    stale_probe = db.get_episode(uid, probe.id)
    check(stale_probe.is_stale(cfg.STALE_AFTER_MIN), "эпизод забыт")
    fake.answers.clear(); fake.edited.clear()
    await press(f"e:{probe.id}")
    check(db.get_episode(uid, probe.id).is_open,
          "«отпустило» на забытом эпизоде не закрыло его ровной длительностью")
    check(any("#" + str(probe.id) in m and "?" in m for m in fake.edited),
          f"вместо отказа бот спросил длительность ({fake.edited[-1:]})")
    asked = [b for m in fake.markups if m
             for row in getattr(m, "inline_keyboard", []) for b in row]
    check(any(b.callback_data == f"ap:{probe.id}:30" for b in asked),
          "и сразу предложил варианты длительности")
    # Зато указать длительность по памяти можно
    await press(f"ap:{probe.id}:60")
    done = db.get_episode(uid, probe.id)
    check(not done.is_open and done.end_approx,
          "примерная длительность у забытого эпизода записывается")
    db.delete_episode(uid, probe.id)

    # 19b. Переполненная заметка на пути «записать это как эпизод»: раньше бот
    # отвечал «дописал», не записав ничего.
    # Заполняем именно тем текстом, который потом проверяем: отказ зависит от
    # того, влезает ли конкретное сообщение, а не от «однажды переполнилось».
    critical = "пульс 210, потерял сознание"
    full_ep = db.start_episode(uid)
    while db.append_note(uid, full_ep.id, critical)[1] != db.NOTE_FULL:
        pass
    saved_note = db.get_episode(uid, full_ep.id).note
    ud["pending_note"] = critical
    fake.edited.clear(); fake.sent.clear()
    await press("nn")
    check(db.get_episode(uid, full_ep.id).note == saved_note,
          "в переполненную заметку ничего не дописалось")
    check(not any(t(LANG, "note_appended", id=full_ep.id) in m for m in fake.edited),
          "и бот не соврал, что дописал")
    check(any(t(LANG, "note_full") in m for m in list(fake.edited) + list(fake.sent)),
          "бот честно сказал, что заметка заполнена")
    offered = [b for m in fake.markups if m
               for row in getattr(m, "inline_keyboard", []) for b in row]
    check(any(b.callback_data == f"nt:{full_ep.id}" for b in offered),
          "первой кнопкой предложено СОХРАНИТЬ текст, вытеснив старое")
    check(any(b.callback_data == f"nc:{full_ep.id}" for b in offered),
          "очистка тоже доступна, но отдельным выбором")
    # Главное: кнопка СОХРАНЯЕТ текст, а не уничтожает заметку. Раньше человек,
    # делавший ровно то, что написано в сообщении, терял записанные симптомы,
    # а присланный заново текст уезжал в новый, выдуманный эпизод.
    episodes_before = db.count_episodes(uid)
    tail_before = saved_note.splitlines()[-1]
    await press(f"nt:{full_ep.id}")
    after_push = db.get_episode(uid, full_ep.id)
    check(after_push.note.endswith(critical), "критичная строка записана в тот же эпизод")
    check(tail_before in after_push.note, "недавние записи заметки сохранены")
    check(db.NOTE_DROPPED_MARK in after_push.note, "и видно, что начало пришлось убрать")
    check(db.count_episodes(uid) == episodes_before,
          "выдуманный эпизод не создан")
    check(i18n.utf16_len(after_push.note) <= db.MAX_NOTE_TOTAL, "лимит соблюдён")

    # Очистка, когда текст уже у бота, тоже записывает его, а не теряет
    ud["pending_note"] = critical
    fake.edited.clear()
    await press("nn")
    check(any(t(LANG, "note_full") in m for m in fake.edited), "снова переполнено")
    await press(f"nc:{full_ep.id}")
    cleared = db.get_episode(uid, full_ep.id)
    check(cleared.note == critical,
          f"после очистки присланный текст записан, а не потерян ({cleared.note!r})")
    check(db.count_episodes(uid) == episodes_before, "и снова без выдуманных эпизодов")
    db.delete_episode(uid, full_ep.id)

    # Тай-брейкер: два эпизода в одну секунду — «текущий» должен быть
    # определённым, иначе заметка уходит в произвольный из них
    same = db.utcnow()
    first = db.start_episode(uid, started_at=same)
    second = db.start_episode(uid, started_at=same)
    check(db.active_episode(uid, cfg.STALE_AFTER_MIN).id == second.id,
          "при равном времени текущим считается последний созданный")
    check(db.open_episode(uid).id == second.id, "то же для open_episode")
    db.delete_episode(uid, first.id); db.delete_episode(uid, second.id)

    # 19b2. «Не знаю» запоминается: иначе бот напоминал бы об этом эпизоде
    # при каждом новом приступе бесконечно.
    forgotten_ok = db.start_episode(uid, started_at=db.utcnow() - timedelta(hours=20))
    check(any(e.id == forgotten_ok.id
              for e in db.stale_episodes(uid, cfg.STALE_AFTER_MIN)),
          "забытый эпизод сначала в списке для напоминаний")
    await press(f"unk:{forgotten_ok.id}")
    check(db.get_episode(uid, forgotten_ok.id).end_unknown,
          "ответ «не знаю» записан в базу")
    check(not any(e.id == forgotten_ok.id
                  for e in db.stale_episodes(uid, cfg.STALE_AFTER_MIN)),
          "и бот больше о нём не напоминает")
    check(db.get_episode(uid, forgotten_ok.id).is_open,
          "но сам эпизод сохранён, окончание честно неизвестно")
    # Возврат в работу снимает отметку
    db.close_episode(uid, forgotten_ok.id)
    reopened_unk = db.reopen_episode(uid, forgotten_ok.id)
    check(not reopened_unk.end_unknown, "возврат в работу снимает отметку «не знаю»")
    db.delete_episode(uid, forgotten_ok.id)

    # 19c. Второй рубеж на уровне БД: «ещё идёт» и сдвиг начала отклоняются,
    # если эпизод закрыли в обход функции (второй инстанс на том же файле).
    raced = db.start_episode(uid)
    db._db().execute("UPDATE episodes SET ended_at = ? WHERE id = ?",
                     (db._iso(db.utcnow()), raced.id))
    db._db().commit()
    check(db.confirm_still_on(uid, raced.id) is None,
          "confirm_still_on отклоняет закрытый эпизод")
    check(db.shift_start(uid, raced.id, -15) is None,
          "shift_start отклоняет закрытый эпизод")
    check(db.close_episode(uid, raced.id) is None,
          "close_episode отклоняет уже закрытый эпизод")
    db.delete_episode(uid, raced.id)

    # 19d. Вложения: присланный файл больше не пропадает в тишину.
    def file_update(photo=False, name="holter.pdf", user=USER):
        chat = Chat(id=user.id, type="private")
        kwargs = {}
        if photo:
            kwargs["photo"] = (PhotoSize(file_id="PH", file_unique_id="u1",
                                         width=90, height=90),)
        else:
            kwargs["document"] = Document(file_id="DOC", file_unique_id="u2",
                                          file_name=name)
        msg = Message(message_id=30, date=datetime.now(timezone.utc), chat=chat,
                      from_user=user, **kwargs)
        msg.set_bot(fake)
        return Update(update_id=30, message=msg)

    handlers = build_handlers()
    doc_upd = file_update()
    takers = [h.callback.__name__ for h in handlers
              if h.check_update(doc_upd) not in (False, None)]
    check(takers == ["on_file"], f"документ попадает в хендлер файлов ({takers})")
    photo_takers = [h.callback.__name__ for h in handlers
                    if h.check_update(file_update(photo=True)) not in (False, None)]
    check(photo_takers == ["on_file"], f"фото тоже ({photo_takers})")

    att_ep = db.start_episode(uid)
    fake.sent.clear()
    await bot.on_file(doc_upd, make_context(fake, ud))
    check(db.count_attachments(uid, att_ep.id) == 1, "файл привязан к текущему эпизоду")
    check(db.list_attachments(uid, att_ep.id)[0].file_name == "holter.pdf",
          "имя файла сохранено")
    check(any("#" + str(att_ep.id) in m for m in fake.sent), "бот подтвердил вложение")
    card_text = report.episode_card(db.get_episode(uid, att_ep.id), LANG, attachments=1)
    check(t(LANG, "card_files", n=1) in card_text, "в карточке видно вложение")

    # Лимит на эпизод, чтобы карточка не превратилась в простыню
    for _ in range(db.MAX_ATTACHMENTS_PER_EPISODE + 2):
        db.add_attachment(uid, att_ep.id, "X", "photo")
    check(db.count_attachments(uid, att_ep.id) == db.MAX_ATTACHMENTS_PER_EPISODE,
          f"не больше {db.MAX_ATTACHMENTS_PER_EPISODE} вложений на эпизод")

    # Файл без эпизода: бот предлагает создать и приложить, а не молчит.
    # Сначала уводим в прошлое ВСЁ, что бот счёл бы текущим: за предыдущие
    # блоки их накопилось несколько.
    while bot._current_episode(uid) is not None:
        current = bot._current_episode(uid)
        db.close_episode(uid, current.id)
        age_out(uid, current.id, days=3)
    ud2b: dict = {}
    fake.sent.clear(); fake.markups.clear()
    await bot.on_file(file_update(photo=True), make_context(fake, ud2b))
    check(any(t(LANG, "file_needs_episode") in m for m in fake.sent),
          "без эпизода бот объясняет, а не молчит")
    check(ud2b.get("pending_file") is not None, "файл придержан до подтверждения")
    before_files = db.count_episodes(uid)
    await bot.on_callback(callback_update(fake, "nf"), make_context(fake, ud2b))
    new_ep = db.last_episode(uid)
    check(db.count_episodes(uid) == before_files + 1, "эпизод создан по подтверждению")
    check(db.count_attachments(uid, new_ep.id) == 1, "и файл приложен к нему")
    # Вложения уходят вместе с эпизодом
    db.delete_episode(uid, new_ep.id)
    check(db.count_attachments(uid, new_ep.id) == 0,
          "удаление эпизода уносит вложения")
    db.delete_episode(uid, att_ep.id)

    # 20. Удаление с подтверждением.
    last_id = db.last_episode(uid).id
    await press(f"d:{last_id}")
    check(any("Удалить эпизод" in t for t in fake.edited), "спросил подтверждение удаления")
    await press(f"dy:{last_id}")
    check(db.get_episode(uid, last_id) is None, "эпизод удалён после подтверждения")
    await press(f"s:{last_id}:2")
    check(any("удалён" in t for t in fake.edited), "кнопка удалённого эпизода не падает")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        db.init(os.path.join(tmp, "flow.db"))
        asyncio.run(run())
    print()
    if FAILURES:
        print(f"ПРОВАЛЕНО {len(FAILURES)}: " + "; ".join(FAILURES))
        return 1
    print("Прогон сценариев пройден.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
