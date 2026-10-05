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

from telegram import CallbackQuery, Chat, Message, Update, User  # noqa: E402

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


def age_out(user_id: int, episode_id: int) -> None:
    """Уводит эпизод на два дня назад — чтобы он перестал быть «текущим»."""
    db.shift_start(user_id, episode_id, -60 * 48)
    db.close_episode(user_id, episode_id, ended_at=db.utcnow() - timedelta(days=2))


class FakeJobQueue:
    """Ловит запланированные напоминания, не запуская планировщик."""

    def __init__(self) -> None:
        self.jobs: dict[str, dict] = {}

    def run_once(self, callback, when, chat_id=None, user_id=None, data=None, name=None):
        self.jobs[name] = {"callback": callback, "when": when, "chat_id": chat_id,
                           "user_id": user_id, "data": data, "name": name}

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
    await press(f"ap:{ep.id}:45")
    stale_closed = db.get_episode(uid, ep.id)
    check(not stale_closed.is_open and stale_closed.end_approx,
          "длительность забытого эпизода записана как примерная")
    check(stale_closed.duration() == timedelta(minutes=45), "записано ровно 45 минут")

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
    for code, profile in (("en", "en-GB"), ("pt", "pt-BR")):
        peer = User(id=100 + len(code) + (0 if code == "en" else 1), first_name="X",
                    is_bot=False, language_code=profile)
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
    await bot.on_callback(callback_update(fake, "lang:pt", user=german),
                          make_context(fake, {}))
    check(db.get_lang(german.id) == "pt", "выбор языка сохранён в БД")
    check(any(t("pt", "lang_set") in m for m in fake.edited), "подтверждение на новом языке")
    fake.sent.clear()
    await bot.cmd_help(text_update(fake, "/help", user=german), make_context(fake, {}))
    check(any("Comandos" in m for m in fake.sent), "дальше бот говорит по-португальски")

    # 16. Удаление с подтверждением.
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
