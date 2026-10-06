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
# Прогон жмёт кнопки за миллисекунды — предохранитель «не чаще раза в N секунд»
# иначе отклонял бы половину сценария. Сам предохранитель проверяется отдельно.
os.environ.setdefault("MIN_ACTION_INTERVAL_SEC", "0")
os.environ.setdefault("EXPORT_COOLDOWN_SEC", "0")
os.environ.setdefault("TZ", "Europe/Moscow")

from telegram import (  # noqa: E402
    CallbackQuery,
    Chat,
    Document,
    Message,
    Location,
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
        self.deleted: list[int] = []
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

    async def delete_message(self, chat_id=None, message_id=None, **kwargs):
        self.deleted.append(message_id)
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
        self.store: dict[str, dict] = {}
        self.user_data: dict = {}

    def run_once(self, callback, when, chat_id=None, user_id=None, data=None,
                 name=None, job_kwargs=None):
        self.store[name] = {"callback": callback, "when": when, "chat_id": chat_id,
                           "user_id": user_id, "data": data, "name": name,
                           "job_kwargs": job_kwargs or {}}

    def run_repeating(self, callback, interval, first=None, chat_id=None,
                      user_id=None, data=None, name=None, job_kwargs=None):
        self.store[name] = {"callback": callback, "interval": interval, "first": first,
                           "chat_id": chat_id, "user_id": user_id, "data": data,
                           "name": name, "job_kwargs": job_kwargs or {},
                           "repeating": True}

    def jobs(self):
        """Как в PTB: список всех задач."""
        return [
            SimpleNamespace(name=n,
                            schedule_removal=lambda n=n: self.store.pop(n, None))
            for n in list(self.store)
        ]

    def get_jobs_by_name(self, name):
        job = self.store.get(name)
        return [SimpleNamespace(schedule_removal=lambda n=name: self.store.pop(n, None))] if job else []

    async def fire(self, name, fake):
        """Выполняет напоминание так, как это сделал бы планировщик."""
        spec = self.store.pop(name)
        job = SimpleNamespace(data=spec["data"], user_id=spec["user_id"], chat_id=spec["chat_id"])
        await bot._remind(SimpleNamespace(bot=fake, job=job, job_queue=self,
                                          user_data=self.user_data))

    async def tick(self, name, fake):
        """Выполняет обновление живой карточки; задача остаётся запланированной."""
        spec = self.store[name]
        removed = []
        job = SimpleNamespace(data=spec["data"], user_id=spec["user_id"],
                              chat_id=spec["chat_id"],
                              schedule_removal=lambda: removed.append(True))
        # user_data — как у настоящей задачи, созданной с user_id
        await bot._tick(SimpleNamespace(bot=fake, job=job, job_queue=self,
                                        user_data=self.user_data))
        if removed:
            self.store.pop(name, None)
        return not removed


JQ = FakeJobQueue()


def make_context(fake: FakeBot, user_data: dict) -> SimpleNamespace:
    return SimpleNamespace(bot=fake, user_data=user_data, chat_data={}, bot_data={},
                           error=None, job_queue=JQ)


async def make_bot_message(fake, chat_id, text):
    """Сообщение «от бота» с message_id — для подмены живой карточки в тестах."""
    return await fake.send_message(chat_id, text)


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

    # 7. Лекарство: приём получает живую карточку. Истории ещё нет —
    # карточка предлагает ввести название.
    await tap(t(LANG, "btn_med"))
    med_id = db.all_meds(uid)[-1].id
    first_med = db.get_med(uid, med_id)
    check(first_med.name is None, "без истории название не подставляется")
    check(first_med.card_msg is not None, "у приёма есть живая карточка")
    await press(f"mc:type:{med_id}")
    await tap("конкор")
    check(db.get_med(uid, med_id).name == "конкор", "название лекарства записано")

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
    check(job in JQ.store, "на новый эпизод поставлено напоминание «отпустило?»")
    check(JQ.store[job]["when"] == timedelta(minutes=cfg.EPISODE_WINDOW_MIN),
          f"напоминание через {cfg.EPISODE_WINDOW_MIN} мин")
    check(JQ.store[job]["data"].get("final") is False, "это обычный вопрос, не контрольный")
    # APScheduler по умолчанию выбрасывает задачу, опоздавшую больше секунды
    check(JQ.store[job]["job_kwargs"].get("misfire_grace_time", "missing") is None,
          "опоздавшее напоминание не выбрасывается планировщиком")

    # Сработало напоминание, эпизод ещё идёт.
    card_before_q = db.get_episode(uid, ep.id).card_msg
    fake.deleted.clear()
    await JQ.fire(job, fake)
    check(any("Отпустило?" in t for t in fake.sent), "бот сам спросил, отпустило ли")
    # Жалоба владельца: было два сообщения с «✅ Отпустило» — карточка и вопрос.
    check(card_before_q in fake.deleted,
          "вопрос ЗАМЕНИЛ карточку: старая удалена, поверхность одна")
    card_after_q = db.get_episode(uid, ep.id).card_msg
    check(card_after_q is not None and card_after_q != card_before_q,
          "и вопрос сам стал карточкой эпизода")
    check(any("идёт" in m and "Отпустило?" in m for m in fake.sent),
          "текст карточки и вопрос — в одном сообщении")
    check(job in JQ.store and JQ.store[job]["data"]["final"] is True,
          "после вопроса запланирован один контрольный заход, а не повтор")
    check(JQ.store[job]["when"] == timedelta(minutes=cfg.EPISODE_WINDOW_MIN),
          "контрольный заход — через то же окно")

    # «Ещё идёт» — эпизод продлевается на окно, начало не трогается.
    started = db.get_episode(uid, ep.id).started_at
    await press(f"go:{ep.id}")
    check(db.get_episode(uid, ep.id).started_at == started, "«ещё идёт» не сдвинуло начало")
    check(db.active_episode(uid, cfg.STALE_AFTER_MIN) is not None, "эпизод всё ещё активен")
    check(JQ.store[job]["when"] == timedelta(minutes=cfg.EPISODE_WINDOW_MIN),
          f"«ещё идёт» продлил эпизод на {cfg.EPISODE_WINDOW_MIN} мин")
    check(JQ.store[job]["data"].get("final") is False,
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
    # Карточку запомним: по забытому эпизоду напоминание правит её НА МЕСТЕ
    # (одна поверхность), а не шлёт второе сообщение.
    stale_card = db.get_episode(uid, ep.id).card_msg
    fake.sent.clear(); fake.edited.clear(); fake.deleted.clear(); fake.markups.clear()
    await JQ.fire(job, fake)
    check(any("окончание не отмечено" in m for m in fake.edited),
          "по забытому эпизоду карточка стала стальной НА МЕСТЕ")
    check(stale_card not in fake.deleted,
          "старую карточку не удаляли — одна поверхность, без мерцания")
    check(db.get_episode(uid, ep.id).card_msg == stale_card,
          "та же карточка, не новое сообщение")
    plate_after = [m for m in fake.markups if m and hasattr(m, "keyboard")]
    check(plate_after and plate_after[-1].keyboard[0][0].text == t(LANG, "btn_start"),
          "и нижнее меню вернулось к «Аритмия»")
    check(job not in JQ.store, "забытый эпизод больше не дёргает напоминаниями")
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
    # Напоминание о забытом эпизоде НЕ должно приходить в момент нового приступа
    check(not any("остался без окончания" in m for m in fake.sent),
          "в момент приступа бот не разбирает старый бэклог")
    check(len(fake.sent) <= 2,
          f"на одно нажатие — не больше двух сообщений ({len(fake.sent)})")
    # Зато приходит, когда человек сам пришёл смотреть записи
    fake.sent.clear()
    await tap(t(LANG, "btn_today"))
    check(any("остался без окончания" in m for m in fake.sent),
          "в «Сегодня» напоминание о забытом эпизоде приходит")

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
    check(f"remind:{uid}:{unknown.id}" not in JQ.store, "напоминания по нему выключены")
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
    JQ.store.clear()
    live = db.start_episode(uid)
    forgotten_long = db.start_episode(uid, started_at=db.utcnow() - timedelta(hours=20))
    await bot.post_init(SimpleNamespace(bot=fake, job_queue=JQ))
    check(f"remind:{uid}:{live.id}" in JQ.store, "после рестарта напоминание восстановлено")
    check(f"remind:{uid}:{forgotten_long.id}" not in JQ.store,
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
    med_id = db.all_meds(uid)[-1].id
    await bot.on_callback(callback_update(fake, f"mc:name:{med_id}"), ctx2)
    shown = ud2.get(f"medopts:{med_id}") or []
    check(shown == db.recent_med_names(uid),
          "снимок названий равен показанному списку")
    # Пока выбор открыт, порядок в БД меняется
    db.add_med(uid, shown[-1], db.utcnow())
    check(db.recent_med_names(uid) != shown, "порядок в БД успел измениться")
    await bot.on_callback(callback_update(fake, f"mc:pick:{med_id}:0"), ctx2)
    saved = db.get_med(uid, med_id)
    check(saved.name == shown[0],
          f"название — с нажатой кнопки, не из свежего списка ({saved.name!r} == {shown[0]!r})")

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

    # 19b3. Этап вопроса живёт в БД: после рестарта бот не спрашивает заново.
    staged = db.start_episode(uid)
    check(db.get_episode(uid, staged.id).remind_stage == 0, "вопрос ещё не задан")
    sjob = f"remind:{uid}:{staged.id}"
    JQ.store[sjob] = {"callback": bot._remind, "when": timedelta(minutes=30),
                     "chat_id": uid, "user_id": uid,
                     "data": {"episode_id": staged.id, "final": False},
                     "name": sjob, "job_kwargs": {}}
    fake.sent.clear()
    await JQ.fire(sjob, fake)
    check(db.get_episode(uid, staged.id).remind_stage == 1, "после вопроса этап записан")
    # Имитируем рестарт: задача потеряна, post_init ставит её заново
    JQ.store.clear()
    await bot.post_init(SimpleNamespace(bot=fake, job_queue=JQ))
    check(JQ.store[sjob]["data"]["final"] is True,
          "после рестарта запланирован контрольный заход, а не повторный вопрос")
    fake.sent.clear()
    await JQ.fire(sjob, fake)
    check(not any("Отпустило?" in m for m in fake.sent),
          f"и вопрос не задаётся повторно ({fake.sent})")
    # «Ещё идёт» начинает цикл заново
    db.set_remind_stage(uid, staged.id, 1)
    await press(f"go:{staged.id}")
    check(db.get_episode(uid, staged.id).remind_stage == 0,
          "«ещё идёт» сбрасывает этап: цикл вопросов начинается заново")
    db.delete_episode(uid, staged.id)

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

    # 19d. Присланный файл получает ответ, а не тишину. Вложения намеренно не
    # храним, но молчание человек читает как «бот сломан».
    def file_update(photo=False, user=USER):
        chat = Chat(id=user.id, type="private")
        if photo:
            extra = {"photo": (PhotoSize(file_id="PH", file_unique_id="u1",
                                         width=90, height=90),)}
        else:
            extra = {"document": Document(file_id="DOC", file_unique_id="u2",
                                          file_name="holter.pdf")}
        msg = Message(message_id=30, date=datetime.now(timezone.utc), chat=chat,
                      from_user=user, **extra)
        msg.set_bot(fake)
        return Update(update_id=30, message=msg)

    handlers = build_handlers()
    for photo in (False, True):
        upd = file_update(photo=photo)
        takers = [h.callback.__name__ for h in handlers
                  if h.check_update(upd) not in (False, None)]
        check(takers == ["on_file"],
              f"{'фото' if photo else 'документ'} попадает в хендлер ({takers})")
    fake.sent.clear()
    before_files = db.count_episodes(uid)
    await bot.on_file(file_update(), make_context(fake, {}))
    check(any(t(LANG, "file_declined") in m for m in fake.sent),
          "на файл приходит объяснение, а не тишина")
    check(db.count_episodes(uid) == before_files,
          "и файл не создаёт эпизодов на пустом месте")

    # 19e. Геопозиция: где был приступ.
    place_kb = bot.main_keyboard(True, LANG).keyboard
    place_btn = [b for row in place_kb for b in row
                 if b.text == t(LANG, "btn_place")]
    check(place_btn and place_btn[0].request_location,
          "кнопка места запрашивает геопозицию (это умеет только нижняя плашка)")

    def loc_update(lat=55.751244, lon=37.618423, user=USER):
        chat = Chat(id=user.id, type="private")
        msg = Message(message_id=40, date=datetime.now(timezone.utc), chat=chat,
                      from_user=user, location=Location(latitude=lat, longitude=lon))
        msg.set_bot(fake)
        return Update(update_id=40, message=msg)

    takers = [h.callback.__name__ for h in build_handlers()
              if h.check_update(loc_update()) not in (False, None)]
    check(takers == ["on_location"], f"геопозиция попадает в свой хендлер ({takers})")

    geo_ep = db.start_episode(uid)
    fake.sent.clear()
    await bot.on_location(loc_update(), make_context(fake, {}))
    stored = db.get_episode(uid, geo_ep.id)
    check(stored.lat is not None and stored.lon is not None, "координаты записаны")
    check(any(t(LANG, "place_saved", id=geo_ep.id) in m for m in fake.sent),
          "бот подтвердил место")
    card_geo = report.episode_card(stored, LANG)
    check("openstreetmap.org" in card_geo, "в карточке ссылка на точку")
    csv_geo = report.episodes_csv([stored], [], LANG).decode("utf-8-sig")
    check("55.75124" in csv_geo, "координаты попали в выгрузку")
    # Убрать место можно — координаты это данные, их должно быть можно забрать
    geo_board = [b for row in bot.refine_keyboard(stored, LANG).inline_keyboard for b in row]
    check(any(b.callback_data == f"pc:{geo_ep.id}" for b in geo_board),
          "кнопка «убрать место» есть в панели уточнений")
    card_board = [b.callback_data for row in
                  bot.card_keyboard(stored, LANG).inline_keyboard for b in row]
    check(f"rf:{geo_ep.id}" in card_board, "а на карточку ведёт одна кнопка «Уточнить»")
    await press(f"pc:{geo_ep.id}")
    check(db.get_episode(uid, geo_ep.id).lat is None, "место убрано по кнопке")
    # Без активного эпизода бот объясняет, а не молчит
    while bot._current_episode(uid) is not None:
        cur = bot._current_episode(uid)
        db.close_episode(uid, cur.id)
        age_out(uid, cur.id, days=3)
    fake.sent.clear()
    await bot.on_location(loc_update(), make_context(fake, {}))
    check(any(t(LANG, "place_needs_episode") in m for m in fake.sent),
          "геопозиция без эпизода: объяснение, а не тишина")
    db.delete_episode(uid, geo_ep.id)

    # 19f. Живая карточка: обновляется сама, старая перестаёт быть кнопочной.
    while bot._current_episode(uid) is not None:
        cur = bot._current_episode(uid)
        db.close_episode(uid, cur.id)
        age_out(uid, cur.id, days=3)
    JQ.store.clear(); fake.sent.clear(); fake.edited.clear()
    await tap(t(LANG, "btn_start"))
    live = db.active_episode(uid, cfg.STALE_AFTER_MIN)
    tick_job = f"tick:{uid}:{live.id}"
    check(tick_job in JQ.store, "на идущий эпизод поставлено обновление карточки")
    check(JQ.store[tick_job]["interval"] == timedelta(seconds=cfg.CARD_TICK_SEC),
          f"интервал обновления {cfg.CARD_TICK_SEC} с, а не каждую секунду")
    check(db.get_episode(uid, live.id).card_msg is not None,
          "бот запомнил, какое сообщение править")
    check(any(t(LANG, "card_open_fresh", id=live.id) in m for m in fake.sent),
          "свежая карточка без «0 сек»")

    # Прошла минута — карточка показывает длительность
    db.shift_start(uid, live.id, -3)
    fake.edited.clear()
    alive = await JQ.tick(tick_job, fake)
    check(alive, "задача обновления продолжает жить")
    check(any("3 мин" in m for m in fake.edited),
          f"карточка обновилась на месте ({fake.edited[-1:]})")

    # Новая карточка гасит кнопки прежней
    old_msg = db.get_episode(uid, live.id).card_msg
    fake.markups.clear(); fake.deleted.clear()
    await press(f"s:{live.id}:2")       # любое действие, перерисовывающее карточку
    await bot._send_card(text_update(fake, "x"), make_context(fake, ud),
                         db.get_episode(uid, live.id), LANG, header="тест")
    check(db.get_episode(uid, live.id).card_msg != old_msg,
          "карточка переехала в новое сообщение")
    check(old_msg in fake.deleted, "прежняя карточка убрана из чата")

    # Эпизод закрыт — обновление прекращается
    await press(f"e:{live.id}")
    check(tick_job not in JQ.store, "после закрытия карточка больше не обновляется")
    # А если задача всё же сработает — она сама себя снимет
    JQ.store[tick_job] = {"callback": bot._tick, "interval": timedelta(seconds=60),
                         "first": None, "chat_id": uid, "user_id": uid,
                         "data": {"episode_id": live.id}, "name": tick_job,
                         "job_kwargs": {}, "repeating": True}
    check(not await JQ.tick(tick_job, fake),
          "задача обновления снимает себя сама на закрытом эпизоде")
    db.delete_episode(uid, live.id)

    # 19g. Предохранители: один человек не должен мешать остальным.
    saved_interval = cfg.MIN_ACTION_INTERVAL_SEC
    cfg.MIN_ACTION_INTERVAL_SEC = 60
    ud_rl: dict = {}
    fake.sent.clear()
    await bot.action_start_episode(text_update(fake, "/log"), make_context(fake, ud_rl))
    before_rl = db.count_episodes(uid)
    await bot.action_start_episode(text_update(fake, "/log"), make_context(fake, ud_rl))
    check(db.count_episodes(uid) == before_rl, "второе нажатие подряд отклонено")
    check(any(t(LANG, "too_fast") in m for m in fake.sent), "и объяснено, а не молча")
    cfg.MIN_ACTION_INTERVAL_SEC = saved_interval

    saved_open = cfg.MAX_OPEN_EPISODES
    cfg.MAX_OPEN_EPISODES = 2
    while bot._current_episode(uid) is not None:
        cur = bot._current_episode(uid)
        db.close_episode(uid, cur.id)
        age_out(uid, cur.id, days=3)
    # Именно забытые открытые: активный эпизод короткое замыкание даёт раньше
    # (бот отвечает «уже идёт»), а копятся как раз брошенные.
    openers = [db.start_episode(uid) for _ in range(2)]
    for o in openers:
        age_out(uid, o.id, days=2)
        db._db().execute("UPDATE episodes SET ended_at = NULL WHERE id = ?", (o.id,))
    db._db().commit()
    fake.sent.clear()
    await bot.action_start_episode(text_update(fake, "/log"), make_context(fake, {}))
    check(any(t(LANG, "too_many_open", n=2) in m for m in fake.sent),
          "лимит незакрытых эпизодов объяснён")
    # и «вернуть в работу» тоже не обходит лимит
    closed_one = db.close_episode(uid, openers[0].id)
    fake.answers.clear()
    await press(f"ro:{openers[1].id}")   # уже открыт — отсечётся раньше
    db.close_episode(uid, openers[1].id)
    for o in openers:
        db.delete_episode(uid, o.id)
    cfg.MAX_OPEN_EPISODES = saved_open

    # 19h. /forget — полное удаление своих данных.
    db.start_episode(uid)
    db.add_med(uid, "конкор")
    db.set_lang(uid, LANG)
    fake.sent.clear(); fake.markups.clear()
    await bot.cmd_forget(text_update(fake, "/forget"), make_context(fake, ud))
    check(any("Удалить все мои записи" in m for m in fake.sent),
          "/forget спрашивает подтверждение, а не удаляет сразу")
    confirm_btns = [b for m in fake.markups if m
                    for row in getattr(m, "inline_keyboard", []) for b in row]
    check(any((b.callback_data or "").startswith("fy:") for b in confirm_btns),
          "есть кнопка подтверждения с одноразовым токеном")
    token = ud.get("forget_token")
    check(token and f"fy:{token}" in [b.callback_data for b in confirm_btns],
          "токен кнопки совпадает с выданным")
    other = 200
    db.start_episode(other)   # чужие данные не должны пострадать
    # Старое или поддельное подтверждение не должно ничего удалять
    before_fake_tap = db.count_episodes(uid)
    await press("fy:подделка")
    check(db.count_episodes(uid) == before_fake_tap,
          "чужой токен подтверждения ничего не удаляет")
    await press("fy")  # без токена вообще
    check(db.count_episodes(uid) == before_fake_tap,
          "подтверждение без токена тоже отклонено")
    ud["forget_token"] = token
    await press(f"fy:{token}")
    check(db.count_episodes(uid) == 0, "все эпизоды удалены")
    check(db.all_meds(uid) == [], "лекарства удалены")
    check(db.get_lang(uid) is None, "выбранный язык тоже удалён")
    check(db.count_episodes(other) == 1, "данные другого пользователя не тронуты")
    check(not [n for n in JQ.store if f":{uid}:" in n],
          f"задачи пользователя сняты ({[n for n in JQ.store if f':{uid}:' in n]})")
    fake.sent.clear()
    await bot.cmd_forget(text_update(fake, "/forget"), make_context(fake, {}))
    check(any(t(LANG, "forget_empty") in m for m in fake.sent),
          "на пустом дневнике /forget говорит, что удалять нечего")

    # 19i. Одна карточка на эпизод, а не растущая куча.
    while bot._current_episode(uid) is not None:
        cur = bot._current_episode(uid)
        db.close_episode(uid, cur.id)
        age_out(uid, cur.id, days=3)
    await tap(t(LANG, "btn_start"))
    one = db.active_episode(uid, cfg.STALE_AFTER_MIN)
    first_msg = db.get_episode(uid, one.id).card_msg
    # /last при живой карточке правит её на месте, не присылая новой
    fake.sent.clear(); fake.edited.clear()
    await bot.cmd_last(text_update(fake, "/last"), make_context(fake, ud))
    check(not fake.sent, f"/last не присылает новую карточку ({fake.sent})")
    check(fake.edited, "/last обновляет существующую")
    check(db.get_episode(uid, one.id).card_msg == first_msg,
          "и карточка осталась тем же сообщением")
    # А когда есть что сообщить — прежняя карточка удаляется
    fake.deleted.clear()
    await bot._send_card(text_update(fake, "x"), make_context(fake, ud),
                         db.get_episode(uid, one.id), LANG, header="📝 тест")
    check(first_msg in fake.deleted,
          f"прежняя карточка удалена, а не оставлена ({fake.deleted})")
    check(db.get_episode(uid, one.id).card_msg != first_msg,
          "карточка переехала в новое сообщение")
    db.close_episode(uid, one.id)
    db.delete_episode(uid, one.id)

    # 19j. Кнопки на закрытом эпизоде: уточнения нужны, «ещё идёт» — нет.
    fresh_closed = db.start_episode(uid)
    db.close_episode(uid, fresh_closed.id)
    fresh_closed = db.get_episode(uid, fresh_closed.id)
    fresh_btns = [b.callback_data for row in
                  bot.card_keyboard(fresh_closed, LANG).inline_keyboard for b in row]
    for needed, why in ((f"s:{fresh_closed.id}:3", "тяжесть"),
                        (f"p:{fresh_closed.id}", "пульс"),
                        (f"n:{fresh_closed.id}", "заметка"),
                        (f"rf:{fresh_closed.id}", "панель уточнений"),
                        (f"d:{fresh_closed.id}", "удаление")):
        check(needed in fresh_btns, f"на закрытом эпизоде остаётся {why}")
    panel_btns = [b.callback_data for row in
                  bot.refine_keyboard(fresh_closed, LANG).inline_keyboard for b in row]
    for needed, why in ((f"m:{fresh_closed.id}:sym", "симптомы"),
                        (f"m:{fresh_closed.id}:trg", "причины")):
        check(needed in panel_btns, f"а {why} — в панели, в одном нажатии")
    check(f"sc:{fresh_closed.id}" in fresh_btns,
          "у только что закрытого — кнопка-вопрос «когда отпустило»")
    when_btns = [b.callback_data for row in
                 bot.end_when_panel(fresh_closed, LANG).inline_keyboard for b in row]
    check(f"ro:{fresh_closed.id}" in when_btns,
          "«ещё не отпустило» — внутри неё: это ответ на тот же вопрос")
    check(f"se:{fresh_closed.id}:-30" in when_btns, "и сдвиги конца там же")
    # Регресс жалобы владельца: на карточке нет «кучи зелёных галок» — ✅
    # остаётся только у выбранной тяжести.
    texts_fresh = [b.text for row in
                   bot.card_keyboard(fresh_closed, LANG).inline_keyboard for b in row]
    check(not any(x.startswith("✅ −") for x in texts_fresh),
          "на закрытой карточке нет кнопок «✅ −N мин»")

    old_closed = db.start_episode(uid)
    db.close_episode(uid, old_closed.id)
    age_out(uid, old_closed.id, days=3)
    old_closed = db.get_episode(uid, old_closed.id)
    old_btns = [b.callback_data for row in
                bot.card_keyboard(old_closed, LANG).inline_keyboard for b in row]
    check(f"ro:{old_closed.id}" not in old_btns,
          "на давно закрытом эпизоде «ещё идёт» не предлагается")
    check(f"s:{old_closed.id}:3" in old_btns,
          "а уточнения остаются — детали заполняются когда угодно позже")
    fake.answers.clear()
    await press(f"ro:{old_closed.id}")
    check(not db.get_episode(uid, old_closed.id).is_open,
          "и подделанный payload его не открывает")
    check(t(LANG, "reopen_too_old") in fake.answers, "с объяснением почему")
    db.delete_episode(uid, fresh_closed.id); db.delete_episode(uid, old_closed.id)

    # 19k. Панель уточнений: открывается подменой клавиатуры, текст карточки
    # остаётся на месте, и ежеминутное обновление её не затирает.
    while bot._current_episode(uid) is not None:
        cur = bot._current_episode(uid)
        db.close_episode(uid, cur.id)
        age_out(uid, cur.id, days=3)
    JQ.store.clear()
    await tap(t(LANG, "btn_start"))
    panel_ep = db.active_episode(uid, cfg.STALE_AFTER_MIN)
    card_rows = bot.card_keyboard(panel_ep, LANG).inline_keyboard
    check(len(card_rows) == 4, f"карточка в четыре ряда ({len(card_rows)})")
    fake.markups.clear(); fake.sent.clear()
    await press(f"rf:{panel_ep.id}")
    check(ud.get(f"panel:{panel_ep.id}") == "refine", "панель помечена открытой")
    check(not fake.sent, "панель не присылает новых сообщений")
    opened = [b.callback_data for m in fake.markups if m
              for row in getattr(m, "inline_keyboard", []) for b in row]
    check(f"m:{panel_ep.id}:sym" in opened, "в панели симптомы")
    check(f"sh:{panel_ep.id}:-30" in opened, "и сдвиг начала")
    check(opened and opened[0] == f"c:{panel_ep.id}", "выход — первой строкой")

    # Тик не затирает открытую панель
    JQ.user_data = ud
    fake.markups.clear()
    tick_name = f"tick:{uid}:{panel_ep.id}"
    db.shift_start(uid, panel_ep.id, -3)
    await JQ.tick(tick_name, fake)
    after_tick = [b.callback_data for m in fake.markups if m
                  for row in getattr(m, "inline_keyboard", []) for b in row]
    check(f"m:{panel_ep.id}:sym" in after_tick,
          f"после обновления панель осталась открытой ({after_tick[:3]})")

    # Выход возвращает карточку и снимает отметку
    await press(f"c:{panel_ep.id}")
    check(f"panel:{panel_ep.id}" not in ud, "отметка снята")
    # «Готово» из меню симптомов ведёт обратно в панель, а не на карточку
    sym_done = bot._toggle_keyboard(panel_ep, "sym", LANG).inline_keyboard[0][0]
    check(sym_done.callback_data == f"rf:{panel_ep.id}",
          "«Готово» из симптомов возвращает в панель")
    db.close_episode(uid, panel_ep.id); db.delete_episode(uid, panel_ep.id)

    # 19l. Правка окончания — на карточке, а не в панели: про неё надо
    # вспомнить за минуты, и спрятанную кнопку не найдёт никто.
    while bot._current_episode(uid) is not None:
        cur = bot._current_episode(uid)
        db.close_episode(uid, cur.id)
        age_out(uid, cur.id, days=3)
    late_ep = db.start_episode(uid)
    db._db().execute("UPDATE episodes SET started_at = ? WHERE id = ?",
                     (db._iso(db.utcnow() - timedelta(minutes=50)), late_ep.id))
    db._db().commit()
    db.close_episode(uid, late_ep.id)
    late_ep = db.get_episode(uid, late_ep.id)
    rows = bot.card_keyboard(late_ep, LANG).inline_keyboard
    flat_late = [b.callback_data for r in rows for b in r]
    check(f"sc:{late_ep.id}" in flat_late,
          "правка окончания доступна с карточки кнопкой-вопросом")
    check(len(rows) <= 4, f"и карточка снова в четыре ряда ({len(rows)})")
    fake.markups.clear()
    await press(f"sc:{late_ep.id}")
    sc_btns = [b.callback_data for m in fake.markups if m
               for row in getattr(m, "inline_keyboard", []) for b in row]
    check(f"se:{late_ep.id}:-30" in sc_btns, "в панели — сдвиги конца")
    panel_data = [b.callback_data for row in
                  bot.refine_keyboard(late_ep, LANG).inline_keyboard for b in row]
    check(not any((c or "").startswith("se:") for c in panel_data),
          "и её нет в панели — там её не нашли бы")
    await press(f"se:{late_ep.id}:-30")
    check(db.get_episode(uid, late_ep.id).duration() <= timedelta(minutes=21),
          "нажатие сдвинуло окончание")
    check(db.get_episode(uid, late_ep.id).end_approx,
          "и пометило длительность приблизительной")
    fake.answers.clear()
    await press(f"se:{late_ep.id}:-60")
    check(any(t(LANG, "end_before_start", time=report.hhmm(late_ep.started_at)) in a
              for a in fake.answers),
          f"сдвиг раньше начала объяснён, а не выполнен ({fake.answers})")
    # Подделанный шаг не проходит
    before_bad = db.get_episode(uid, late_ep.id).ended_at
    await press(f"se:{late_ep.id}:-7")
    check(db.get_episode(uid, late_ep.id).ended_at == before_bad,
          "шаг не из набора отклонён")
    # У давно закрытого эпизода правки уже нет
    age_out(uid, late_ep.id, days=3)
    old_rows = [b.callback_data for row in
                bot.card_keyboard(db.get_episode(uid, late_ep.id), LANG).inline_keyboard
                for b in row]
    check(not any((c or "").startswith("se:") for c in old_rows),
          "у давно закрытого эпизода правка окончания не предлагается")
    fake.answers.clear()
    await press(f"se:{late_ep.id}:-15")
    check(t(LANG, "reopen_too_old") in fake.answers,
          "и подделанный payload её не выполняет")
    db.delete_episode(uid, late_ep.id)

    # 19m. Взаимное исключение в паре: ответы противоположны, а не дополняют
    # друг друга. «Началось резко» и «нарастало» вместе — противоречие.
    pair_ep = db.start_episode(uid)
    await press(f"ts:{pair_ep.id}:abrupt_on")
    check(db.get_episode(uid, pair_ep.id).symptoms == ["abrupt_on"], "первый ответ отмечен")
    await press(f"ts:{pair_ep.id}:gradual_on")
    marks = db.get_episode(uid, pair_ep.id).symptoms
    check(marks == ["gradual_on"], f"противоположный заменил его, а не добавился ({marks})")
    # Повторное нажатие снимает ответ целиком — поле не становится обязательным
    await press(f"ts:{pair_ep.id}:gradual_on")
    check(db.get_episode(uid, pair_ep.id).symptoms == [], "повторное нажатие снимает ответ")
    # Непарные симптомы по-прежнему независимы
    for code in ("short", "weak", "dizzy"):
        await press(f"ts:{pair_ep.id}:{code}")
    check(set(db.get_episode(uid, pair_ep.id).symptoms) == {"short", "weak", "dizzy"},
          "непарные симптомы остаются многовыборными")
    # Исключение не распространяется на причины
    for code in ("sex", "heat"):
        await press(f"tt:{pair_ep.id}:{code}")
    check(set(db.get_episode(uid, pair_ep.id).triggers) == {"sex", "heat"},
          "причины независимы между собой")
    db.delete_episode(uid, pair_ep.id)

    # 19n. Ретроспективная запись: приступ, который уже прошёл.
    while bot._current_episode(uid) is not None:
        cur = bot._current_episode(uid)
        db.close_episode(uid, cur.id)
        age_out(uid, cur.id, days=3)

    today_d = report.today_local()
    yd = today_d - timedelta(days=1)
    now_local = report.local(db.utcnow())

    # Полный путь кнопками: вчера → 23 часа → 23:40 → 30 минут.
    before_bf = db.count_episodes(uid)
    fake.edited.clear(); fake.markups.clear()
    await press(f"bf:d:{yd}")
    hour_btns = [b for m in fake.markups if m
                 for row in getattr(m, "inline_keyboard", []) for b in row]
    check(any(b.callback_data == f"bf:h:{yd}:23" for b in hour_btns),
          "для вчера предлагаются все 24 часа")
    await press(f"bf:h:{yd}:23")
    minute_btns = [b for m in fake.markups if m
                   for row in getattr(m, "inline_keyboard", []) for b in row
                   if (b.callback_data or "").startswith("bf:m:")]
    check(any(b.text == "23:40" for b in minute_btns),
          "минуты подписаны полным временем — выбор самопроверяемый")
    await press(f"bf:m:{yd}:23:40")
    await press(f"bf:f:{yd}:23:40:30")
    check(db.count_episodes(uid) == before_bf + 1, "эпизод создан")
    made = max(db.all_episodes(uid), key=lambda e: e.id)
    local_start = report.local(made.started_at)
    check((local_start.hour, local_start.minute) == (23, 40),
          f"начало — вчерашние 23:40 ({local_start:%H:%M})")
    check(local_start.date() == yd, "и именно вчера")
    check(not made.is_open and made.end_approx,
          "эпизод закрыт, длительность честно помечена примерной")
    check(made.duration() == timedelta(minutes=30), "длительность 30 минут")

    # Повтор той же кнопки: близнец не создаётся, показывается существующий.
    fake.edited.clear()
    await press(f"bf:f:{yd}:23:40:30")
    check(db.count_episodes(uid) == before_bf + 1,
          "повторная кнопка не создала эпизод-близнец")
    check(any(t(LANG, "back_duplicate") in m for m in fake.edited),
          "и бот объяснил, что приступ уже записан")
    db.delete_episode(uid, made.id)

    # «Не знаю» с НЕДАВНИМ сегодняшним началом: эпизод обязан быть инертным.
    # Верификация нашла: раньше он становился текущим — рисовался «идёт, уже
    # 29 мин», блокировал «⚡️» и воровал ввод у настоящего приступа.
    recent_hour = now_local.hour
    recent_min = (now_local.minute // 10) * 10
    await press(f"bf:f:{today_d}:{recent_hour}:{recent_min}:x")
    unk = max(db.all_episodes(uid), key=lambda e: e.id)
    check(unk.is_open and unk.end_unknown, "эпизод открыт и помечен «не знаю»")
    check(unk.needs_end(cfg.STALE_AFTER_MIN),
          "needs_end: сказать «идёт» про него нельзя")
    check(db.active_episode(uid, cfg.STALE_AFTER_MIN) is None,
          "свежий «не знаю» НЕ считается текущим")
    check(f"tick:{uid}:{unk.id}" not in JQ.store,
          "и его карточка не тикает «идёт уже N минут»")
    card_unk = report.episode_card(db.get_episode(uid, unk.id), LANG)
    check("идёт" not in card_unk and "окончание не отмечено" in card_unk,
          f"карточка честная: окончание не отмечено, а не «идёт»")
    kb_unk = [b.callback_data for row in
              bot.card_keyboard(db.get_episode(uid, unk.id), LANG).inline_keyboard
              for b in row]
    check(f"e:{unk.id}" not in kb_unk and f"dp:{unk.id}" in kb_unk,
          "вместо «Отпустило» — вопрос о длительности")
    # Подделанное «Отпустило» не фабрикует точное время окончания
    await press(f"e:{unk.id}")
    check(db.get_episode(uid, unk.id).is_open,
          "форсированное «Отпустило» не закрыло его точным временем")
    # «⚡️ Аритмия» не заблокирована
    await tap(t(LANG, "btn_start"))
    live2 = db.active_episode(uid, cfg.STALE_AFTER_MIN)
    check(live2 is not None and live2.id != unk.id,
          "«⚡️» записывает настоящий приступ, несмотря на «не знаю» рядом")
    # Свободный ввод идёт в живой эпизод, а не в «не знаю»
    await tap("142")
    check(db.get_episode(uid, live2.id).pulse == 142, "пульс — в живой эпизод")
    check(db.get_episode(uid, unk.id).pulse is None, "а не в инертный")
    # Закрытие по кнопке длительности снимает отметку «не знаю»
    await press(f"ap:{unk.id}:15")
    unk_after = db.get_episode(uid, unk.id)
    check(not unk_after.is_open and not unk_after.end_unknown,
          "закрытие длительностью снимает отметку «не знаю»")
    await press(f"e:{live2.id}")
    db.delete_episode(uid, live2.id); db.delete_episode(uid, unk.id)

    # Живой эпизод неприкосновенен и для обычной (закрытой) ретроспективы.
    await tap(t(LANG, "btn_start"))
    live_ep = db.active_episode(uid, cfg.STALE_AFTER_MIN)
    live_card = db.get_episode(uid, live_ep.id).card_msg
    live_tick = f"tick:{uid}:{live_ep.id}"
    check(live_tick in JQ.store, "живая карточка обновляется")
    await press(f"bf:f:{yd}:22:10:60")
    check(db.active_episode(uid, cfg.STALE_AFTER_MIN).id == live_ep.id,
          "текущий эпизод остался текущим")
    check(db.get_episode(uid, live_ep.id).card_msg == live_card,
          "живая карточка не тронута")
    check(live_tick in JQ.store, "и продолжает обновляться")
    backfilled = max(db.all_episodes(uid), key=lambda e: e.id)
    db.delete_episode(uid, backfilled.id)
    await press(f"e:{live_ep.id}")
    db.delete_episode(uid, live_ep.id)

    # Подделанные payload: мимо окна, мимо наборов, нестрогие числа, арность.
    before_hostile = db.count_episodes(uid)
    hostile_bf = [
        f"bf:d:{today_d - timedelta(days=9)}",   # за окном владельца
        f"bf:d:{today_d + timedelta(days=1)}",   # завтра
        "bf:d:5", "bf:d:не-дата", f"bf:h:{yd}:99", f"bf:h:{yd}:٣",
        f"bf:m:{yd}:23:7", f"bf:f:{yd}:23:40:7",
        f"bf:f:{yd}:23:40:+30", f"bf:f:{yd}:23:40:1_5",
        f"bf:f:{yd}:23:40:30:хвост",             # лишний сегмент
        f"bf:f:{yd}:23:40", "bf:f", "bf:zzz", "bf",
    ]
    if now_local.hour < 23:
        hostile_bf.append(f"bf:f:{today_d}:{now_local.hour + 1}:0:15")
        hostile_bf.append(f"bf:f:{today_d}:{now_local.hour + 1}:0:x")
    for data in hostile_bf:
        await press(data)
    check(db.count_episodes(uid) == before_hostile,
          "ни один подделанный bf-payload не создал эпизод")

    # Будущее отклоняется и на уровне функции (не зависит от часа суток,
    # кроме ровно 23:59) — это убийца мутации «выкинули проверку будущего».
    if now_local.minute < 59:
        check(bot._backfill_start(today_d, now_local.hour, 59) is None,
              "функция старта отклоняет будущую минуту")

    # Сетка минут для текущего часа не предлагает будущего — убийца мутации
    # «выкинули фильтр из клавиатуры минут».
    kb_min = bot._backfill_minute_keyboard(today_d, now_local.hour, LANG)
    offered = [b.callback_data for row in kb_min.inline_keyboard for b in row
               if (b.callback_data or "").startswith("bf:m:")]
    check(all(int(c.rsplit(":", 1)[-1]) <= now_local.minute for c in offered),
          f"минутная сетка не предлагает будущего ({offered})")
    check(offered, "и хотя бы одна прошедшая минута предложена")

    # Будущий час форсированным payload — отказ, а не экран-тупик из «Назад».
    fake.edited.clear()
    if now_local.hour < 23:
        await press(f"bf:h:{today_d}:{now_local.hour + 1}")
        check(not fake.edited, "будущий час: отказ без экрана-тупика")

    # Сетка часов для «сегодня» не предлагает будущего.
    _, today_kb = bot._backfill_hour_screen(today_d, LANG)
    hours_offered = [int(b.text) for row in today_kb.inline_keyboard
                     for b in row if b.text.isdigit()]
    check(max(hours_offered) == now_local.hour,
          f"сегодня последний час — текущий ({max(hours_offered)})")

    # Кнопка в сводке дня: есть у свежих дней, нет у старых, дата абсолютная.
    kb_today = bot._day_keyboard(today_d, LANG)
    flat_today = [b.callback_data for row in kb_today.inline_keyboard for b in row]
    check(f"bf:n:{today_d}" in flat_today,
          "в сегодняшней сводке вход в ретроспективу с абсолютной датой")
    kb_old = bot._day_keyboard(today_d - timedelta(days=10), LANG)
    flat_old = [(b.callback_data or "") for row in kb_old.inline_keyboard for b in row]
    check(not any(c.startswith("bf:") for c in flat_old),
          "у старых дней входа нет — владелец ограничил тремя днями")
    fake.sent.clear()
    await press(f"bf:n:{yd}")
    check(fake.sent, "вход из сводки присылает новое сообщение, сводка цела")

    # Отклонённый мусор не сжигает слот лимита частоты у честного нажатия.
    saved_interval = cfg.MIN_ACTION_INTERVAL_SEC
    cfg.MIN_ACTION_INTERVAL_SEC = 60
    ud.pop("rl:episode", None)
    await press(f"bf:f:{yd}:21:0:7")      # мусор: длительность не из набора
    await press(f"bf:f:{yd}:21:0:15")     # честное нажатие сразу после
    fresh = max(db.all_episodes(uid), key=lambda e: e.id)
    check(report.local(fresh.started_at).hour == 21,
          "мусор не съел слот частоты: честное нажатие записано")
    fake.answers.clear()
    await press(f"bf:f:{yd}:20:0:15")     # а вот теперь слот занят
    check(t(LANG, "too_fast") in fake.answers, "второе честное — уже «слишком часто»")
    cfg.MIN_ACTION_INTERVAL_SEC = saved_interval
    ud.pop("rl:episode", None)
    db.delete_episode(uid, fresh.id)

    # Лимит объяснён и здесь.
    saved_cap = cfg.MAX_EPISODES_PER_USER
    cfg.MAX_EPISODES_PER_USER = db.count_episodes(uid)
    fake.sent.clear()
    await press(f"bf:d:{yd}")
    check(any(t(LANG, "too_many_episodes", n=cfg.MAX_EPISODES_PER_USER) in m
              for m in fake.sent),
          "переполненный дневник объяснён на входе в ретроспективу")
    cfg.MAX_EPISODES_PER_USER = saved_cap

    # 19o. Застревание на «идёт»: тик по забытому эпизоду должен ПЕРЕРИСОВАТЬ
    # карточку в стальную и остановиться, а не замереть на «идёт, уже N мин».
    # (Жалоба: эпизод шёл 2 часа и «сам не закрывался».)
    while bot._current_episode(uid) is not None:
        cur = bot._current_episode(uid)
        db.close_episode(uid, cur.id); age_out(uid, cur.id, days=3)
    stuck = db.start_episode(uid, started_at=db.utcnow() - timedelta(minutes=125))
    s_msg = (await make_bot_message(fake, uid, "идёт")).message_id
    db.set_card_msg(uid, stuck.id, s_msg)
    stuck_tick = f"tick:{uid}:{stuck.id}"
    JQ.store[stuck_tick] = {"callback": bot._tick, "interval": timedelta(seconds=60),
        "first": None, "chat_id": uid, "user_id": uid,
        "data": {"episode_id": stuck.id}, "name": stuck_tick, "job_kwargs": {},
        "repeating": True}
    JQ.user_data = ud
    fake.edited.clear()
    alive = await JQ.tick(stuck_tick, fake)
    check(any("окончание не отмечено" in m for m in fake.edited),
          "тик перерисовал забытую карточку в стальную, а не заморозил на «идёт»")
    check(not alive, "и после этого остановился — обновлять больше нечего")
    db.delete_episode(uid, stuck.id)

    # 19p. Рестарт: забытый эпизод в restore не попадает (all_open_episodes его
    # отсекает), поэтому post_init должен ОТДЕЛЬНО догнать его карточку —
    # иначе после редеплоя она висит замороженной навсегда.
    frozen = db.start_episode(uid, started_at=db.utcnow() - timedelta(minutes=125))
    f_msg = (await make_bot_message(fake, uid, "идёт, уже 2 ч")).message_id
    db.set_card_msg(uid, frozen.id, f_msg)
    check(not any(e.id == frozen.id
                  for e in db.all_open_episodes(cfg.STALE_AFTER_MIN)),
          "забытый эпизод НЕ в списке восстановления напоминаний")
    check(any(e.id == frozen.id
              for e in db.stale_cards_to_refresh(cfg.STALE_AFTER_MIN)),
          "но он в списке карточек для догона")
    fake.edited.clear(); fake.markups.clear()
    await bot.post_init(SimpleNamespace(bot=fake, job_queue=JQ))
    check(any("окончание не отмечено" in m for m in fake.edited),
          "post_init расклеил замороженную карточку")
    check(db.get_episode(uid, frozen.id).card_msg == f_msg,
          "та же карточка, не новое сообщение")
    # И вернул нижнее меню: карточка — инлайн, а плашка меняется только новым
    # сообщением. (Жалоба: «исправилось, но не появилось меню снизу».)
    plate_sent = [m for m in fake.markups if m and hasattr(m, "keyboard")]
    check(plate_sent, "post_init прислал сообщение с нижним меню")
    check(plate_sent[-1].keyboard[0][0].text == t(LANG, "btn_start"),
          "и меню вернулось к «Аритмия» — эпизод больше не активен")
    check(db.get_episode(uid, frozen.id).stale_shown,
          "переход в «забыт» отмечен показанным")
    # Второй рестарт НЕ должен снова слать плашку (жалоба: сообщение на каждом
    # редеплое).
    fake.markups.clear()
    await bot.post_init(SimpleNamespace(bot=fake, job_queue=JQ))
    check(not [m for m in fake.markups if m and hasattr(m, "keyboard")],
          "на втором рестарте плашка повторно НЕ приходит")
    db.delete_episode(uid, frozen.id)

    # 19p2. Плашка НЕ приходит, если сейчас есть активный эпизод: там плашка и
    # так верная, а текст «начнётся снова» ей противоречит. (Жалоба: сообщение
    # при активном эпизоде.)
    live_now = db.start_episode(uid)  # активный
    ln = (await make_bot_message(fake, uid, "идёт")).message_id
    db.set_card_msg(uid, live_now.id, ln)
    stale_too = db.start_episode(uid, started_at=db.utcnow() - timedelta(minutes=200))
    stm = (await make_bot_message(fake, uid, "идёт")).message_id
    db.set_card_msg(uid, stale_too.id, stm)
    check(db.active_episode(uid, cfg.STALE_AFTER_MIN) is not None, "активный эпизод есть")
    fake.markups.clear()
    await bot.post_init(SimpleNamespace(bot=fake, job_queue=JQ))
    check(not [m for m in fake.markups if m and hasattr(m, "keyboard")],
          "при активном эпизоде плашка-сообщение не приходит")
    check(db.get_episode(uid, stale_too.id).stale_shown,
          "но карточка забытого всё равно расклеена (флаг стоит)")
    await press(f"e:{live_now.id}")
    db.delete_episode(uid, live_now.id); db.delete_episode(uid, stale_too.id)

    # 19q. /cancel не оставляет «Отменил ввод.» висеть последним: пока идёт
    # эпизод, последней в чате должна быть его карточка (превью в списке чатов).
    while bot._current_episode(uid) is not None:
        cur = bot._current_episode(uid)
        db.close_episode(uid, cur.id); age_out(uid, cur.id, days=3)
    await tap(t(LANG, "btn_start"))
    cancel_ep = db.active_episode(uid, cfg.STALE_AFTER_MIN)
    # имитируем ожидание ввода (нажали «Пульс»)
    ud["await"] = {"what": "pulse", "id": cancel_ep.id}
    old_card = db.get_episode(uid, cancel_ep.id).card_msg
    fake.sent.clear(); fake.deleted.clear()
    await bot.cmd_cancel(text_update(fake, "/cancel"), make_context(fake, ud))
    check("await" not in ud, "ожидание ввода снято")
    check(not any(t(LANG, "cancelled") in m for m in fake.sent),
          "нет сообщения «Отменил ввод.»")
    check(any("идёт" in m for m in fake.sent),
          "вместо него карточка эпизода прислана вниз")
    check(old_card in fake.deleted, "прежняя карточка убрана — одна карточка")
    new_card = db.get_episode(uid, cancel_ep.id).card_msg
    check(new_card is not None and new_card != old_card,
          "карточка стала последним сообщением в чате")
    await press(f"e:{cancel_ep.id}"); db.delete_episode(uid, cancel_ep.id)

    # /cancel без эпизода — короткая реплика, карточку слать неоткуда
    fake.sent.clear()
    await bot.cmd_cancel(text_update(fake, "/cancel"), make_context(fake, {}))
    check(any(t(LANG, "nothing_to_cancel") in m for m in fake.sent),
          "без эпизода /cancel отвечает коротко")

    # /cancel нет в меню команд — редкая и путает
    check("cancel" not in [c.command for c in bot._commands(LANG)],
          "/cancel убрана из списка команд")
    menu_cmds = [c.command for c in bot._commands(LANG)]
    for dup in ("log", "stop", "med", "today"):
        check(dup not in menu_cmds, f"/{dup} убрана из меню — дублирует плашку")
    for keep in ("earlier", "export", "forget"):
        check(keep in menu_cmds, f"/{keep} остаётся в меню")

    # 19s. Карточка приёма лекарства: живой счётчик «принято N назад»,
    # сетка «когда» с шагом 15 минут, дефолт названия, удаление с токеном.
    db.add_med(uid, "конкор", db.utcnow())  # история не пуста → будет дефолт
    db.add_med(uid, "конкор", db.utcnow())
    expected_default = db.default_med_name(uid)
    fake.sent.clear(); fake.markups.clear()
    await tap(t(LANG, "btn_med"))
    mcard = max(db.all_meds(uid), key=lambda m: m.id)
    check(expected_default is not None and mcard.name == expected_default,
          "название подставлено по умолчанию из истории")
    check(mcard.card_msg is not None, "карточка приёма создана")
    mtick = f"mtick:{uid}:{mcard.id}"
    check(mtick in JQ.store, "счётчик карточки запущен")
    check(any("принято только что" in m for m in fake.sent),
          "свежий приём: «принято только что»")

    # сетка «когда» — «только что» = точно, сдвиги НАКОПИТЕЛЬНЫЕ
    fake.markups.clear()
    await press(f"mc:when:{mcard.id}")
    when_btns = [b.callback_data for m in fake.markups if m
                 for row in getattr(m, "inline_keyboard", []) for b in row
                 if (b.callback_data or "").startswith("mc:set:")]
    check(f"mc:set:{mcard.id}:30" in when_btns and f"mc:set:{mcard.id}:0" in when_btns,
          "в сетке есть «только что» и сдвиг −30")
    await press(f"mc:set:{mcard.id}:30")
    await press(f"mc:set:{mcard.id}:30")  # накопительно → −60, не идемпотентно
    after = db.get_med(uid, mcard.id)
    check(after.approx and int(after.since().total_seconds() // 60) == 60,
          "два «−30» дают −60 (кнопки накопительные, а не идемпотентные)")
    await press(f"mc:set:{mcard.id}:0")  # «только что» — сброс на сейчас
    reset = db.get_med(uid, mcard.id)
    check(not reset.approx and reset.since().total_seconds() < 90,
          "«только что» сбрасывает время на сейчас и снимает «примерно»")

    # живой счётчик обновляет текст (снова сдвинем на −60 двумя −30)
    await press(f"mc:set:{mcard.id}:30")
    await press(f"mc:set:{mcard.id}:30")
    fake.edited.clear()
    tick_job = SimpleNamespace(data={"med_id": mcard.id}, user_id=uid, chat_id=uid,
                               schedule_removal=lambda: None)
    await bot._med_tick(SimpleNamespace(bot=fake, job=tick_job, job_queue=JQ, user_data=ud))
    check(any("1 ч" in m and "назад" in m for m in fake.edited),
          "счётчик показывает «принято 1 ч ... назад»")

    # сменить название: ожидание ввода — ЯВНЫМ сообщением с плашкой; после ввода
    # карточка правится на месте, без лишнего подтверждения.
    await press(f"mc:name:{mcard.id}")
    fake.sent.clear(); fake.markups.clear()
    await press(f"mc:type:{mcard.id}")
    check(any("Напишите название" in m for m in fake.sent),
          "ожидание ввода написано ЯВНО отдельным сообщением")
    check(any(isinstance(m, type(bot.main_keyboard(True, LANG))) for m in fake.markups),
          "запрос ввода несёт нижнюю плашку")
    fake.edited.clear(); fake.sent.clear()
    await tap("аспирин")
    check(db.get_med(uid, mcard.id).name == "аспирин", "название сменилось вводом")
    check(any("аспирин" in e for e in fake.edited),
          "карточка после ввода правится НА МЕСТЕ (в edited)")
    check(not any("Записал" in s for s in fake.sent),
          "после ввода нет лишнего подтверждения «Записал»")

    # удаление — только с токеном
    await press(f"mc:del:{mcard.id}")
    mtok = ud.get(f"mdel:{mcard.id}")
    check(mtok, "удаление приёма выдало одноразовый токен")
    await press(f"mc:dy:{mcard.id}:подделка")
    check(db.get_med(uid, mcard.id) is not None, "чужой токен приём не удалил")
    ud[f"mdel:{mcard.id}"] = mtok
    await press(f"mc:dy:{mcard.id}:{mtok}")
    check(db.get_med(uid, mcard.id) is None, "по токену приём удалён")
    check(mtick not in JQ.store, "и счётчик карточки остановлен")

    # восстановление тика после рестарта
    rec = db.add_med(uid, "конкор", db.utcnow())
    db.set_med_card_msg(uid, rec.id, 9000)
    JQ.store.clear()
    await bot.post_init(SimpleNamespace(bot=fake, job_queue=JQ))
    check(f"mtick:{uid}:{rec.id}" in JQ.store, "тик карточки приёма восстановлен после рестарта")
    db.delete_med(uid, rec.id)

    # 19s2. Чистота чата: нажатие кнопки «💊 Лекарство» не плодит подтверждение
    # (меню и так видно — его только что нажали), а команда /med прячет меню
    # набором текста и потому возвращает его отдельной репликой с плашкой.
    plate_type = type(bot.main_keyboard(True, LANG))

    def _drop_newest_med():
        m = max(db.live_med_cards(uid, cfg.MED_TICK_MAX_H * 60), key=lambda x: x.id)
        bot._cancel_med_tick(JQ, uid, m.id)
        db.set_med_card_msg(uid, m.id, None)
        db.delete_med(uid, m.id)

    fake.markups.clear(); fake.sent.clear()
    await tap(t(LANG, "btn_med"))
    check(not any("Записал приём" in m for m in fake.sent),
          "кнопка «💊 Лекарство» не шлёт лишнее подтверждение «Записал приём»")
    _drop_newest_med()

    fake.markups.clear(); fake.sent.clear()
    await bot.action_med(text_update(fake, "/med"), ctx)
    check(any(isinstance(m, plate_type) for m in fake.markups)
          and any("Записал приём" in m for m in fake.sent),
          "/med (команда) возвращает плашку подтверждением")
    _drop_newest_med()

    # 19t. Предохранители и починки приёма лекарств (ревью med-фичи).
    # Чистим живые карточки от предыдущих блоков, чтобы счёт был предсказуем.
    for m in db.live_med_cards(uid, cfg.MED_TICK_MAX_H * 60):
        db.set_med_card_msg(uid, m.id, None)
        bot._cancel_med_tick(JQ, uid, m.id)

    # (a) BLOCKER: предел числа строк — у предела новый приём не создаётся,
    # отказ объяснён словами (публичный бот, risk #5).
    old_max = cfg.MAX_MEDS_PER_USER
    cfg.MAX_MEDS_PER_USER = db.count_meds(uid)  # уже на пределе
    try:
        before = db.count_meds(uid)
        fake.sent.clear()
        await tap(t(LANG, "btn_med"))
        check(db.count_meds(uid) == before, "у предела лекарств новый приём не создаётся")
        check(any("предел" in m for m in fake.sent), "отказ по лимиту объяснён словами")
    finally:
        cfg.MAX_MEDS_PER_USER = old_max

    # (b) BLOCKER: предел числа ЖИВЫХ карточек — гаснет счётчик у самой давней,
    # но сама запись приёма остаётся (один тап = факт, запись не блокируется).
    old_cap = cfg.MAX_LIVE_MED_CARDS
    cfg.MAX_LIVE_MED_CARDS = 2
    try:
        base = db.utcnow()
        made = []
        for i in range(3):  # от старой к свежей
            m = db.add_med(uid, "конкор", base - timedelta(minutes=10 * (3 - i)))
            db.set_med_card_msg(uid, m.id, 7000 + i)
            bot._schedule_med_tick(JQ, uid, m.id, uid)
            made.append(m)
        bot._enforce_live_med_cap(ctx, uid)
        live_ids = {m.id for m in db.live_med_cards(uid, cfg.MED_TICK_MAX_H * 60)}
        check(made[0].id not in live_ids, "самая давняя карточка погашена при переполнении")
        check(made[1].id in live_ids and made[2].id in live_ids, "две свежие остаются живыми")
        check(f"mtick:{uid}:{made[0].id}" not in JQ.store, "и тик давней карточки снят")
        check(f"mtick:{uid}:{made[2].id}" in JQ.store, "тик свежей карточки жив")
        check(db.get_med(uid, made[0].id) is not None, "но сама запись приёма сохранена")
        for m in made:
            db.set_med_card_msg(uid, m.id, None)
            bot._cancel_med_tick(JQ, uid, m.id)
            db.delete_med(uid, m.id)
    finally:
        cfg.MAX_LIVE_MED_CARDS = old_cap

    # (c) Ревью #2: тик снимается по возрасту ДАЖЕ при открытой панели —
    # иначе брошенная панель оставляла бы бессмертную ежеминутную задачу.
    stuck = db.add_med(uid, "конкор", db.utcnow() - timedelta(hours=cfg.MED_TICK_MAX_H + 1))
    db.set_med_card_msg(uid, stuck.id, 7777)
    ud[f"medpanel:{stuck.id}"] = "when"
    killed = {"v": False}
    job = SimpleNamespace(data={"med_id": stuck.id}, user_id=uid, chat_id=uid,
                          schedule_removal=lambda: killed.__setitem__("v", True))
    await bot._med_tick(SimpleNamespace(bot=fake, job=job, job_queue=JQ, user_data=ud))
    check(killed["v"], "старый тик снимается по возрасту даже при открытой панели")
    ud.pop(f"medpanel:{stuck.id}", None)
    db.delete_med(uid, stuck.id)

    # (d) Ревью #3: экран подтверждения удаления переживает тик — mc:del ставит
    # паузу, и ближайший тик ничего не перерисовывает поверх кнопки «Удалить».
    delc = db.add_med(uid, "конкор", db.utcnow())
    db.set_med_card_msg(uid, delc.id, 8888)
    await press(f"mc:del:{delc.id}")
    check(ud.get(f"medpanel:{delc.id}") == "del", "подтверждение удаления ставит паузу тика")
    fake.edited.clear()
    job = SimpleNamespace(data={"med_id": delc.id}, user_id=uid, chat_id=uid,
                          schedule_removal=lambda: None)
    await bot._med_tick(SimpleNamespace(bot=fake, job=job, job_queue=JQ, user_data=ud))
    check(not fake.edited, "тик при открытом подтверждении удаления ничего не перерисовывает")
    ud.pop(f"medpanel:{delc.id}", None)
    db.delete_med(uid, delc.id)

    # (e) Продукт S1: примерное время приёма попадает в CSV отдельным флагом,
    # а не как точное (честность данных, как у end_approx эпизода).
    csv_med = db.add_med(uid, "конкор", db.utcnow() - timedelta(minutes=120), approx=True)
    csv_bytes = report.episodes_csv([], [db.get_med(uid, csv_med.id)], LANG).decode("utf-8")
    med_line = [ln for ln in csv_bytes.splitlines() if "конкор" in ln][-1]
    check(med_line.split(";")[6] == t(LANG, "csv_yes"),
          "примерное время приёма помечено в колонке «примерная»")
    exact_med = db.add_med(uid, "конкор", db.utcnow(), approx=False)
    csv_bytes = report.episodes_csv([], [db.get_med(uid, exact_med.id)], LANG).decode("utf-8")
    exact_line = [ln for ln in csv_bytes.splitlines() if "конкор" in ln][-1]
    check(exact_line.split(";")[6] == "", "точное время приёма колонку не метит")
    db.delete_med(uid, csv_med.id); db.delete_med(uid, exact_med.id)

    # (f) Ревью #5: пустое имя не затирает заданное и не рисует «названную»
    # раскладку. UX #1/#2: кнопки панели — нейтральные и без обрезки.
    nam= db.add_med(uid, None, db.utcnow())
    ud["await"] = {"what": "med", "id": nam.id}
    await tap("   ")  # одни пробелы
    check(db.get_med(uid, nam.id).name is None, "пустое имя не сохраняется")
    when_kb = bot._med_when_keyboard(db.get_med(uid, nam.id), LANG)
    labels = [b.text for row in when_kb.inline_keyboard for b in row]
    check(all(i18n.utf16_len(x) <= 16 for x in labels),
          "подписи сетки «когда» влезают в ряд по три (≤16)")
    check(t(LANG, "btn_med_back") in labels and t(LANG, "btn_card") not in labels,
          "в панели приёма нейтральная «Назад», а не «К эпизоду»")
    db.delete_med(uid, nam.id)

    # 19u. «Что приняли?» ловит набранный ответ (не уводит его в «новый эпизод»);
    # сетка «когда» помечает выбранное «● только что» и подписывает минусами.
    while bot._current_episode(uid) is not None:  # чистое поле для проверки эпизодов
        cur = bot._current_episode(uid)
        db.close_episode(uid, cur.id)
        age_out(uid, cur.id, days=3)
    typed = db.add_med(uid, None, db.utcnow())
    eps_before = db.count_episodes(uid)
    fake.sent.clear()
    await press(f"mc:name:{typed.id}")
    check(ud.get("await") == {"what": "med", "id": typed.id},
          "панель «Что приняли?» переводит бота в ожидание названия")
    await tap("Валерьянка")  # печатаем, НЕ нажимая «✏️ Другое»
    check(db.get_med(uid, typed.id).name == "Валерьянка",
          "набранный у «Что приняли?» текст становится названием")
    check(db.count_episodes(uid) == eps_before, "и не создаёт новый эпизод")
    check(not any(t(LANG, "ask_text_as_episode") in m for m in fake.sent),
          "и не предлагает «записать как новый эпизод»")

    # выбор из списка тоже снимает ожидание — иначе следующий текст стал бы
    # названием вместо заметки
    await press(f"mc:name:{typed.id}")
    snap = ud.get(f"medopts:{typed.id}") or []
    if snap:
        await press(f"mc:pick:{typed.id}:0")
        check("await" not in ud, "выбор названия из списка снимает ожидание ввода")

    # отметка выбранного и минусы в сетке «когда»
    fresh = db.add_med(uid, "конкор", db.utcnow())  # approx=False → «только что»
    kb = bot._med_when_keyboard(db.get_med(uid, fresh.id), LANG)
    flat = [b.text for row in kb.inline_keyboard for b in row]
    now_label = next(x for x in flat if "только что" in x)
    check(now_label.startswith("● "), "у свежего приёма «только что» помечено точкой")
    minute_labels = [x for x in flat if "мин" in x or (" ч" in x and "только" not in x)]
    check(minute_labels and all(x.startswith("−") for x in minute_labels),
          "кнопки минут подписаны минусом — «−15 мин» это «15 минут назад»")
    db.set_med_time(uid, fresh.id, db.utcnow() - timedelta(minutes=30), approx=True)
    kb2 = bot._med_when_keyboard(db.get_med(uid, fresh.id), LANG)
    flat2 = [b.text for row in kb2.inline_keyboard for b in row]
    now2 = next(x for x in flat2 if "только что" in x)
    check(not now2.startswith("● "), "у приёма с примерным временем «только что» не помечено")
    db.delete_med(uid, typed.id); db.delete_med(uid, fresh.id)

    # 19v. Плоская карточка: сдвиги времени и названия (до 3) стоят ПРЯМО на
    # ней — без захода в подменю; снимок названий строится вместе с кнопками.
    flatm = db.add_med(uid, None, db.utcnow())
    ud.pop(f"medopts:{flatm.id}", None)
    kb = bot._med_card_markup(ctx, db.get_med(uid, flatm.id), LANG)
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    check(f"mc:set:{flatm.id}:0" in cbs and f"mc:set:{flatm.id}:30" in cbs,
          "сдвиги времени стоят прямо на карточке")
    check(f"mc:when:{flatm.id}" not in cbs and f"mc:name:{flatm.id}" not in cbs,
          "на новой карточке нет кнопок-подменю «Когда»/«Название»")
    picks = [c for c in cbs if c.startswith(f"mc:pick:{flatm.id}:")]
    check(1 <= len(picks) <= 3, f"названий на карточке не больше 3 (их {len(picks)})")
    check(len(ud.get(f"medopts:{flatm.id}") or []) == len(picks),
          "снимок названий совпадает с показанными кнопками")
    check(f"mc:type:{flatm.id}" in cbs, "есть кнопка ручного ввода названия")
    snap0 = (ud.get(f"medopts:{flatm.id}") or [None])[0]
    await bot.on_callback(callback_update(fake, f"mc:pick:{flatm.id}:0"), ctx)
    check(db.get_med(uid, flatm.id).name == snap0, "тап по названию на карточке ставит его")
    bot._cancel_med_tick(JQ, uid, flatm.id)
    db.delete_med(uid, flatm.id)
    other = 770077
    oh = db.add_med(other, None, db.utcnow())
    kb2 = bot._med_card_markup(make_context(fake, {}), db.get_med(other, oh.id), LANG)
    cbs2 = [b.callback_data for row in kb2.inline_keyboard for b in row]
    check(not any(c.startswith("mc:pick:") for c in cbs2), "без истории кнопок названий нет")
    check(f"mc:type:{oh.id}" in cbs2, "без истории — только кнопка ввести название")
    db.delete_med(other, oh.id)

    # 19w. Повторный приём того же лекарства гасит счётчик прежней карточки;
    # совпадение терпимо к регистру и опечатке, но не склеивает разные.
    check(bot._med_names_match("Конкор", "конкор"), "регистр не мешает совпадению")
    check(bot._med_names_match("конкор", "конкро"), "перестановка букв — то же лекарство")
    check(bot._med_names_match("конкор", "конкол"), "одна опечатка — то же лекарство")
    check(not bot._med_names_match("конкор", "аспирин"), "разные лекарства не совпадают")
    check(not bot._med_names_match("мг", "мл"), "слишком короткие по опечатке не склеиваем")
    check(not bot._med_names_match("", "конкор"), "пустое имя ни с чем не совпадает")
    oldc = db.add_med(uid, "Конкор", db.utcnow() - timedelta(minutes=20))
    db.set_med_card_msg(uid, oldc.id, 4242)
    bot._schedule_med_tick(JQ, uid, oldc.id, uid)
    newc = db.add_med(uid, "конкро", db.utcnow())  # опечатка того же лекарства
    db.set_med_card_msg(uid, newc.id, 4243)
    bot._schedule_med_tick(JQ, uid, newc.id, uid)
    bot._retire_same_name(ctx, uid, db.get_med(uid, newc.id))
    check(db.get_med(uid, oldc.id).card_msg is None, "счётчик прежней карточки погашен")
    check(f"mtick:{uid}:{oldc.id}" not in JQ.store, "тик прежней карточки снят")
    check(db.get_med(uid, newc.id).card_msg is not None, "новая карточка продолжает считать")
    check(db.get_med(uid, oldc.id) is not None, "запись о прежнем приёме сохранена")
    bot._cancel_med_tick(JQ, uid, newc.id)
    db.delete_med(uid, oldc.id); db.delete_med(uid, newc.id)

    # 19x. Справочник из истории: «📋 Все лекарства» открывает полный список,
    # выбор — кнопкой (печать только для нового препарата). Тик на паузе, пока
    # открыт справочник.
    for nm in ("апре", "бпре", "впре", "гпре"):
        db.add_med(uid, nm, db.utcnow())
    allm = db.add_med(uid, None, db.utcnow())
    ud.pop(f"medopts:{allm.id}", None); ud.pop(f"medpanel:{allm.id}", None)
    kb = bot._med_card_markup(ctx, db.get_med(uid, allm.id), LANG)
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    check(f"mc:all:{allm.id}" in cbs, "при >3 названиях на карточке есть «Все лекарства»")
    picks_on_card = [c for c in cbs if c.startswith(f"mc:pick:{allm.id}:")]
    check(len(picks_on_card) == 3, "на самой карточке по-прежнему только 3 названия")
    await bot.on_callback(callback_update(fake, f"mc:all:{allm.id}"), ctx)
    check(ud.get(f"medpanel:{allm.id}") == "all", "«Все лекарства» ставит паузу тика")
    full = ud.get(f"medopts:{allm.id}") or []
    check(len(full) > 3, f"в справочнике больше 3 названий (их {len(full)})")
    db.set_med_card_msg(uid, allm.id, 5151)
    fake.edited.clear()
    job = SimpleNamespace(data={"med_id": allm.id}, user_id=uid, chat_id=uid,
                          schedule_removal=lambda: None)
    await bot._med_tick(SimpleNamespace(bot=fake, job=job, job_queue=JQ, user_data=ud))
    check(not fake.edited, "пока открыт справочник, тик карточку не трогает")
    target = full[-1]  # есть только в полном списке, не в тройке на карточке
    await bot.on_callback(callback_update(fake, f"mc:pick:{allm.id}:{full.index(target)}"), ctx)
    check(db.get_med(uid, allm.id).name == target, "выбор из справочника ставит название")
    check("await" not in ud and f"medpanel:{allm.id}" not in ud,
          "после выбора пауза и ожидание ввода сняты")
    bot._cancel_med_tick(JQ, uid, allm.id)
    db.delete_med(uid, allm.id)
    u2 = 770088
    db.add_med(u2, "альфа", db.utcnow()); db.add_med(u2, "бета", db.utcnow())
    m2 = db.add_med(u2, None, db.utcnow())
    kb2 = bot._med_card_markup(make_context(fake, {}), db.get_med(u2, m2.id), LANG)
    cbs2 = [b.callback_data for row in kb2.inline_keyboard for b in row]
    check(f"mc:all:{m2.id}" not in cbs2, "при ≤3 названиях «Все лекарства» не показываем")
    db.delete_med(u2, m2.id)

    # 19y. Карточка приёма замерзает через окно: правки убираются (остаётся
    # удаление), а старые кнопки из истории отклоняются вторым рубежом.
    frz = db.add_med(uid, "конкор", db.utcnow())
    check(bot._med_editable(db.get_med(uid, frz.id)), "свежая карточка — редактируемая")
    cbs_live = [b.callback_data for row in
                bot._med_card_markup(ctx, db.get_med(uid, frz.id), LANG).inline_keyboard
                for b in row]
    check(any(c.startswith(f"mc:set:{frz.id}:") for c in cbs_live),
          "на свежей карточке есть кнопки правок")

    past = db.utcnow() - timedelta(minutes=cfg.MED_EDIT_WINDOW_MIN + 1)
    db._db().execute("UPDATE meds SET modified_at = ? WHERE id = ?", (db._iso(past), frz.id))
    db._db().commit()
    check(not bot._med_editable(db.get_med(uid, frz.id)), "за окном карточка не редактируется")
    cbs_frozen = [b.callback_data for row in
                  bot._med_card_markup(ctx, db.get_med(uid, frz.id), LANG).inline_keyboard
                  for b in row]
    check(cbs_frozen == [f"mc:del:{frz.id}"], "замороженная карточка — только «Удалить»")

    before_time = db.get_med(uid, frz.id).taken_at
    fake.answers.clear()
    await press(f"mc:set:{frz.id}:30")
    check(db.get_med(uid, frz.id).taken_at == before_time,
          "сдвиг времени на замороженной карточке отклонён (второй рубеж)")
    check(any("не изменить" in a for a in fake.answers), "отказ объяснён всплывашкой")
    ud.pop("await", None)
    await press(f"mc:type:{frz.id}")
    check((ud.get("await") or {}).get("id") != frz.id,
          "ручной ввод названия на замороженной не запускается")

    await press(f"mc:del:{frz.id}")
    check(ud.get(f"mdel:{frz.id}"), "удаление на замороженной карточке остаётся доступным")
    await press(f"mc:dy:{frz.id}:{ud[f'mdel:{frz.id}']}")
    check(db.get_med(uid, frz.id) is None, "замороженную карточку можно удалить")

    # 20. Удаление с подтверждением.
    # После /forget дневник пуст — создаём, что удалять
    if db.last_episode(uid) is None:
        db.close_episode(uid, db.start_episode(uid).id)
    last_id = db.last_episode(uid).id
    await press(f"d:{last_id}")
    check(any("Удалить эпизод" in t for t in fake.edited), "спросил подтверждение удаления")
    confirm_rows = [row for m in fake.markups if m
                    for row in getattr(m, "inline_keyboard", []) if len(row) == 2]
    check(confirm_rows and (confirm_rows[-1][0].callback_data or "").startswith("c:"),
          "безопасный выбор первым, деструктивный вторым")
    del_token = ud.get(f"del_token:{last_id}")
    check(del_token, "подтверждение удаления выдало одноразовый токен")
    await press(f"dy:{last_id}:подделка")
    check(db.get_episode(uid, last_id) is not None,
          "старое подтверждение из истории чата не удаляет эпизод")
    ud[f"del_token:{last_id}"] = del_token
    await press(f"dy:{last_id}:{del_token}")
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
