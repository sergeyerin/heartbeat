#!/usr/bin/env python3
"""Офлайн-смоук-тест: БД, форматирование, клавиатуры. Без сети и токена.

Запускается при сборке образа — сломанный образ падает на `docker build`,
а не в продакшене.
"""
from __future__ import annotations

import os
import tempfile
from datetime import timedelta

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "smoke")
os.environ.setdefault("TZ", "Europe/Moscow")

import bot  # noqa: E402
import db  # noqa: E402
import i18n  # noqa: E402
import report  # noqa: E402
import vocab  # noqa: E402

LANG = "ru"  # проверки текстов идут на русском; переводы проверяются отдельно

FAILURES: list[str] = []


def check(cond: bool, what: str) -> None:
    print(("  ok  " if cond else " FAIL ") + what)
    if not cond:
        FAILURES.append(what)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        db.init(os.path.join(tmp, "test.db"))
        uid = 42

        # --- эпизод: создание, уточнения, закрытие ---
        ep = db.start_episode(uid)
        check(ep.is_open, "новый эпизод открыт")
        check(db.open_episode(uid).id == ep.id, "open_episode находит текущий")

        ep = db.start_episode(uid)
        check(db.open_episode(uid).id == ep.id, "open_episode берёт самый свежий")

        ep = db.set_severity(uid, ep.id, 3)
        ep = db.set_pulse(uid, ep.id, 142)
        ep = db.toggle_code(uid, ep.id, "symptoms", "short")
        ep = db.toggle_code(uid, ep.id, "symptoms", "dizzy")
        ep = db.toggle_code(uid, ep.id, "symptoms", "short")  # снятие
        ep = db.toggle_code(uid, ep.id, "triggers", "coffee")
        ep = db.append_note(uid, ep.id, "началось после кофе")
        ep = db.append_note(uid, ep.id, "вторая строка")
        check(ep.severity == 3 and ep.pulse == 142, "тяжесть и пульс сохранились")
        check(ep.symptoms == ["dizzy"], f"повторное нажатие снимает симптом: {ep.symptoms}")
        check(ep.triggers == ["coffee"], "триггер сохранился")
        check(ep.note.count("\n") == 1, "заметки дописываются, а не перетирают друг друга")

        before = ep.started_at
        ep = db.shift_start(uid, ep.id, -30)
        check(ep.started_at == before - timedelta(minutes=30), "сдвиг начала назад")

        ep = db.close_episode(uid, ep.id)
        check(not ep.is_open and ep.duration() >= timedelta(minutes=30), "эпизод закрыт с длительностью")
        ep = db.reopen_episode(uid, ep.id)
        check(ep.is_open, "эпизод можно снова открыть")
        ep = db.close_episode(uid, ep.id)

        # Конец раньше начала не должен давать отрицательную длительность.
        weird = db.start_episode(uid)
        weird = db.close_episode(uid, weird.id, ended_at=weird.started_at - timedelta(hours=1))
        check(weird.duration() == timedelta(0), "конец не уезжает раньше начала")

        # --- «нажал Аритмию и забыл Отпустило» ---
        # Отдельный пользователь: у uid выше остались открытые эпизоды, они бы
        # мешали проверке «что считается активным».
        STALE_MIN = 60
        fuid = uid + 10
        forgotten = db.start_episode(fuid)
        db.shift_start(fuid, forgotten.id, -60 * 20)  # началось 20 часов назад
        forgotten = db.get_episode(fuid, forgotten.id)
        check(forgotten.is_open, "забытый эпизод остаётся открытым")
        check(forgotten.is_stale(STALE_MIN), "через 20 часов молчания эпизод считается забытым")
        check(db.active_episode(fuid, STALE_MIN) is None,
              "забытый эпизод НЕ блокирует новую запись")
        check([e.id for e in db.stale_episodes(fuid, STALE_MIN)] == [forgotten.id],
              "забытый эпизод виден отдельным списком")

        # «ещё идёт» возвращает эпизод в активные, не трогая начало
        started_before = forgotten.started_at
        forgotten = db.confirm_still_on(fuid, forgotten.id)
        check(not forgotten.is_stale(STALE_MIN), "«ещё идёт» снова делает эпизод активным")
        check(forgotten.started_at == started_before, "«ещё идёт» не сдвигает начало")
        check(db.active_episode(fuid, STALE_MIN).id == forgotten.id,
              "после подтверждения эпизод снова активный")
        check(forgotten.duration() >= timedelta(hours=20),
              "длительность считается от начала, а не от подтверждения")

        # примерная длительность по памяти
        db.shift_start(fuid, forgotten.id, 0)
        approx = db.close_episode(
            fuid, forgotten.id, ended_at=forgotten.started_at + timedelta(minutes=45), approx=True
        )
        check(approx.end_approx and approx.duration() == timedelta(minutes=45),
              "примерная длительность записана и помечена")
        check(db.all_open_episodes() == [] or all(e.id != approx.id for e in db.all_open_episodes()),
              "закрытый эпизод уходит из списка открытых")
        check("~" in report.duration_label(approx, LANG), "примерная длительность показана с «~»")
        check("примерной длительностью" in report.period_report(30, [approx], [], LANG),
              "отчёт отмечает примерные длительности")

        hung = db.start_episode(fuid)
        db.shift_start(fuid, hung.id, -60 * 30)
        hung = db.get_episode(fuid, hung.id)
        card_stale = report.episode_card(hung, LANG)
        check("окончание не отмечено" in card_stale, "карточка забытого эпизода честно это пишет")
        check("Длительность неизвестна" in card_stale, "длительность не выдумывается")
        rep_stale = report.period_report(30, [hung], [], LANG)
        check("Без отметки окончания: 1" in rep_stale, "отчёт выносит такие эпизоды отдельно")
        check("в средние значения не входят" in rep_stale, "и не портит ими статистику")
        kb_stale = bot.card_keyboard(hung, LANG)
        texts = [btn.text for row in kb_stale.inline_keyboard for btn in row]
        check(i18n.t(LANG, "btn_end") not in texts, "забытому эпизоду не предлагают «Отпустило сейчас»")
        check("⏳ Ещё идёт" in texts and "🤷 Не знаю" in texts, "предлагают длительность и «ещё идёт»")
        db.delete_episode(fuid, hung.id)

        check(report.human_duration(timedelta(hours=30), LANG) == "1 дн 6 ч", "длительность: сутки")
        check(report.human_duration(timedelta(hours=48), LANG) == "2 дн", "длительность: ровные сутки")

        # --- изоляция пользователей ---
        check(db.get_episode(uid + 1, ep.id) is None, "чужой эпизод не читается")
        check(db.delete_episode(uid + 1, ep.id) is False, "чужой эпизод не удаляется")

        # --- лекарства ---
        med = db.add_med(uid, "конкор")
        db.add_med(uid)
        check("конкор" in db.recent_med_names(uid), "история названий лекарств")

        # --- форматирование ---
        card = report.episode_card(ep, LANG)
        check("#" in card and "Пульс: 142" in card, "карточка эпизода собирается")
        check("идёт" in report.episode_card(db.start_episode(uid), LANG), "карточка открытого эпизода")

        day = report.today_local()
        start, end = report.day_bounds(day)
        summary = report.day_summary(day, db.list_episodes(uid, start, end), db.list_meds(uid, start, end), LANG)
        check("Сегодня" in summary and "💊" in summary, "сводка за день с лекарствами")
        check("Записей нет" in report.day_summary(day, [], [], LANG), "пустой день")

        month_start, _ = report.day_bounds(day - timedelta(days=29))
        rep = report.period_report(30, db.list_episodes(uid, month_start), db.list_meds(uid, month_start), LANG)
        check("Отчёт за 30" in rep and "По времени суток" in rep, "отчёт за период")
        check("не зарегистрировано" in report.period_report(7, [], [], LANG), "пустой отчёт")

        csv_bytes = report.episodes_csv(db.all_episodes(uid), db.all_meds(uid), LANG)
        check(csv_bytes.startswith(b"\xef\xbb\xbf"), "CSV с BOM для Excel")
        check(csv_bytes.decode("utf-8-sig").count("\n") >= 3, "CSV содержит строки")

        check(report.human_duration(timedelta(seconds=30), LANG) == "30 сек", "длительность: секунды")
        check(report.human_duration(timedelta(minutes=17), LANG) == "17 мин", "длительность: минуты")
        check(report.human_duration(timedelta(minutes=72), LANG) == "1 ч 12 мин", "длительность: часы")
        check(report.human_duration(timedelta(hours=2), LANG) == "2 ч", "длительность: ровные часы")

        # --- клавиатуры и лимит callback_data (64 байта) ---
        kb = bot.main_keyboard(True, LANG)
        check(kb.keyboard[0][0].text == i18n.t(LANG, "btn_end"), "плашка показывает «Отпустило» при открытом эпизоде")
        check(bot.main_keyboard(False, LANG).keyboard[0][0].text == i18n.t(LANG, "btn_start"), "и «Аритмия» при закрытом")

        payloads = []
        for markup in (
            bot.card_keyboard(ep, LANG),
            bot.card_keyboard(db.open_episode(uid), LANG),
            bot._toggle_keyboard(ep, "sym", LANG),
            bot._toggle_keyboard(ep, "trg", LANG),
            bot._report_keyboard(7, LANG),
            bot._day_keyboard(day - timedelta(days=1)),
        ):
            for row in markup.inline_keyboard:
                payloads += [b.callback_data for b in row if b.callback_data]
        check(all(len(p.encode()) <= 64 for p in payloads), "callback_data укладывается в 64 байта")
        check(len(bot._toggle_keyboard(ep, "sym", LANG).inline_keyboard) == len(vocab.SYMPTOM_CODES) + 1,
              "в меню симптомов все пункты + «Готово»")
        check(len(bot._toggle_keyboard(ep, "trg", LANG).inline_keyboard) == len(vocab.TRIGGER_CODES) + 1,
              "в меню причин все пункты + «Готово»")

        # --- целостность переводов ---
        # Пропущенный ключ или разъехавшийся плейсхолдер обнаружится здесь,
        # а не в проде на незнакомом языке.
        import string as _string

        def placeholders(text: str) -> set[str]:
            return {
                name for _, name, _, _ in _string.Formatter().parse(text) if name
            }

        missing, mismatched = [], []
        for key, bundle in i18n.STRINGS.items():
            ref = placeholders(bundle[i18n.FALLBACK])
            for code in i18n.SUPPORTED:
                if code not in bundle:
                    missing.append(f"{key}/{code}")
                elif placeholders(bundle[code]) != ref:
                    mismatched.append(f"{key}/{code}")
        check(not missing, f"все ключи есть во всех языках (нет: {missing[:5]})")
        check(not mismatched, f"плейсхолдеры совпадают между языками ({mismatched[:5]})")

        for code in i18n.SUPPORTED:
            check(len(i18n.months(code)) == 12, f"12 месяцев для {code}")
            check(len(i18n.weekdays(code)) == 7, f"7 дней недели для {code}")
            check(len(i18n.csv_header(code)) == 12, f"12 колонок CSV для {code}")
            check(set(vocab.symptoms(code)) == set(vocab.SYMPTOM_CODES),
                  f"переведены все симптомы для {code}")
            check(set(vocab.triggers(code)) == set(vocab.TRIGGER_CODES),
                  f"переведены все причины для {code}")
            check(set(vocab.severity(code)) == {1, 2, 3}, f"три уровня тяжести для {code}")
            check(set(vocab.severity_plain(code)) == {1, 2, 3},
                  f"три уровня тяжести без эмодзи для {code}")

        # Текст кнопки приходит обратно от Telegram, поэтому подписи не должны
        # совпадать между языками — иначе нажатие не разобрать.
        menu_texts = [i18n.t(c, k) for k in bot.MENU_ACTIONS for c in i18n.SUPPORTED]
        check(len(menu_texts) == len(set(menu_texts)),
              "подписи кнопок уникальны по всем языкам")
        check(len(bot.ACTION_BY_TEXT) == len(bot.MENU_ACTIONS) * len(i18n.SUPPORTED),
              "разбор кнопок покрывает все языки")

        check(i18n.resolve(None, "en-US", "ru") == "en", "en-US → en")
        check(i18n.resolve(None, "pt-BR", "ru") == "pt", "pt-BR → pt")
        check(i18n.resolve(None, "de", "ru") == "ru", "незнакомый язык → дефолт")
        check(i18n.resolve("pt", "en", "ru") == "pt", "выбор пользователя важнее профиля")
        check(i18n.resolve(None, None, "zz") == i18n.FALLBACK, "битый дефолт → fallback")

        # Отчёты и карточки собираются на всех языках без KeyError.
        for code in i18n.SUPPORTED:
            card_any = report.episode_card(approx, code)
            check(str(approx.id) in card_any, f"карточка собирается на {code}")
            rep_any = report.period_report(30, [approx], [], code)
            check(bool(rep_any.strip()), f"отчёт собирается на {code}")
            day_any = report.day_summary(report.today_local(), [], [], code)
            check(bool(day_any.strip()), f"сводка собирается на {code}")
            csv_any = report.episodes_csv([approx], [], code)
            check(csv_any.startswith(b"\xef\xbb\xbf"), f"CSV собирается на {code}")
            check(bool(bot.main_keyboard(True, code).keyboard), f"плашка собирается на {code}")
            check(bool(bot.card_keyboard(approx, code).inline_keyboard),
                  f"карточка-клавиатура собирается на {code}")

        # --- M2: состояние эпизода защищено от повторных действий ---
        # Инлайн-кнопки живут в истории чата вечно. Старая карточка не должна
        # перезаписывать уже записанное время окончания.
        guard = db.start_episode(fuid)
        db.shift_start(fuid, guard.id, -20)
        first = db.close_episode(fuid, guard.id)
        check(first is not None and not first.is_open, "эпизод закрылся")
        recorded = first.duration()
        again = db.close_episode(fuid, guard.id)
        check(again is None, "повторное закрытие отклонено, а не перезаписано")
        check(db.get_episode(fuid, guard.id).duration() == recorded,
              "длительность осталась прежней после повторного нажатия")
        check(db.close_episode(fuid, guard.id, ended_at=db.utcnow(), approx=True) is None,
              "примерная длительность не затирает точную")
        check(db.shift_start(fuid, guard.id, -30) is None,
              "сдвиг начала у закрытого эпизода отклонён")
        reopened = db.reopen_episode(fuid, guard.id)
        check(reopened is not None and reopened.is_open, "закрытый эпизод можно открыть")
        check(db.reopen_episode(fuid, guard.id) is None,
              "повторное открытие отклонено")
        # И начало не уезжает в будущее
        future = db.shift_start(fuid, guard.id, 10 ** 6)
        check(future.started_at <= db.utcnow(), "сдвиг не уводит начало в будущее")
        db.delete_episode(fuid, guard.id)

        # --- M5: длина заметки ограничена, карточка остаётся отправляемой ---
        wordy = db.start_episode(fuid)
        for _ in range(10):
            db.append_note(fuid, wordy.id, "я" * 5000)
        wordy = db.get_episode(fuid, wordy.id)
        check(len(wordy.note) <= db.MAX_NOTE_TOTAL, f"заметка обрезана до {len(wordy.note)}")
        check(len(report.episode_card(wordy, LANG)) < 4096,
              "карточка с огромной заметкой влезает в сообщение")
        many = [wordy] * 30
        day_long = report.day_summary(report.today_local(), many, [], LANG)
        check(all(len(part) <= bot.TG_TEXT_LIMIT for part in bot._split(day_long)),
              "длинная сводка режется на отправляемые части")
        check("".join(bot._split(day_long)).replace("\n", "") ==
              day_long.replace("\n", ""), "при нарезке текст не теряется")
        check(db.clear_note(fuid, wordy.id).note is None, "заметку можно очистить")
        db.delete_episode(fuid, wordy.id)

        # Неожиданное значение в колонке тяжести не должно ломать сводку дня:
        # валидация не пускает такое впредь, но в базе оно могло оказаться раньше.
        odd = db.start_episode(fuid)
        db._db().execute("UPDATE episodes SET severity = 7 WHERE id = ?", (odd.id,))
        db._db().commit()
        odd = db.get_episode(fuid, odd.id)
        try:
            rendered = report.day_summary(report.today_local(), [odd], [], LANG)
            check(bool(rendered), "сводка дня переживает чужое значение тяжести")
        except Exception as exc:  # noqa: BLE001
            check(False, f"сводка дня упала на severity=7: {exc!r}")
        check(bool(report.episode_card(odd, LANG)), "карточка тоже переживает")
        db.delete_episode(fuid, odd.id)

        # --- M6: формулы в CSV обезврежены во всех пользовательских колонках ---
        nasty = db.start_episode(fuid)
        db.append_note(fuid, nasty.id, '=cmd|\'/c calc\'!A1')
        db.toggle_code(fuid, nasty.id, "symptoms", "=HYPERLINK(\"http://x\")")
        db.close_episode(fuid, nasty.id)
        db.add_med(fuid, "@SUM(1+1)")
        import csv as _csv
        import io as _io
        text = report.episodes_csv(
            db.all_episodes(fuid), db.all_meds(fuid), LANG
        ).decode("utf-8-sig")
        rows = list(_csv.reader(_io.StringIO(text), delimiter=";"))
        dangerous = [
            field for row in rows[1:] for field in row
            if field[:1] in report.CSV_RISKY_PREFIX
        ]
        check(not dangerous, f"ни одна ячейка CSV не исполняется в Excel ({dangerous[:2]})")
        check(any("'=cmd" in field for row in rows for field in row),
              "содержимое заметки при этом сохранено, только обезврежено")
        check(len(rows[0]) == len(rows[1]), "колонки не разъехались")
        db.delete_episode(fuid, nasty.id)

        # --- разбор пульса из текста ---
        check(bool(bot.PULSE_RE.match(" 120 ")), "число с пробелами = пульс")
        check(bot.PULSE_RE.match("12.5") is None, "дробное не пульс")
        check(bot.PULSE_RE.match("плохо") is None, "текст не пульс")

    print()
    if FAILURES:
        print(f"ПРОВАЛЕНО {len(FAILURES)}: " + "; ".join(FAILURES))
        return 1
    print("Смоук-тест пройден.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
