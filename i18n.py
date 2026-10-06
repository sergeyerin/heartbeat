#!/usr/bin/env python3
"""Строки интерфейса на русском и английском.

Язык определяется по профилю Telegram (``language_code``) и может быть
переопределён командой /lang — выбор хранится в БД, поэтому переживает рестарт
и действует в напоминаниях, где апдейта с профилем нет.

Правила:
- ключ существует во всех языках (проверяется тестом ``smoke_test.py``);
- плейсхолдеры именованные и одинаковые во всех переводах (тоже под тестом);
- медицинские термины переводятся осторожно: «перебои» — это именно ощущение
  пропущенного удара, а не «аритмия» в целом.
"""
from __future__ import annotations

SUPPORTED = ("ru", "en")
FALLBACK = "en"

MONTHS = {
    "ru": ("января", "февраля", "марта", "апреля", "мая", "июня",
           "июля", "августа", "сентября", "октября", "ноября", "декабря"),
    "en": ("January", "February", "March", "April", "May", "June",
           "July", "August", "September", "October", "November", "December"),
}

WEEKDAYS = {
    "ru": ("пн", "вт", "ср", "чт", "пт", "сб", "вс"),
    "en": ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"),
}

# Заголовки CSV — отдельно от обычных строк, это список колонок.
CSV_HEADER = {
    "ru": ["тип", "id", "дата", "начало", "конец", "длительность_мин",
           "длительность_примерная", "тяжесть", "пульс", "симптомы",
           "перед_эпизодом", "заметка", "широта", "долгота"],
    "en": ["type", "id", "date", "start", "end", "duration_min",
           "duration_approx", "severity", "pulse", "symptoms",
           "before_episode", "note", "latitude", "longitude"],
}

STRINGS: dict[str, dict[str, str]] = {
    # --- кнопки нижней плашки (текст приходит обратно от Telegram, поэтому
    # должен быть уникальным среди всех языков) ---
    "btn_start": {"ru": "⚡️ Аритмия", "en": "⚡️ Arrhythmia"},
    "btn_end": {"ru": "✅ Отпустило", "en": "✅ It stopped"},
    "btn_med": {"ru": "💊 Лекарство", "en": "💊 Medication"},
    "btn_place": {"ru": "📍 Место", "en": "📍 Place"},
    "btn_today": {"ru": "📋 Сегодня", "en": "📋 Today"},
    "btn_report": {"ru": "📈 Отчёт", "en": "📈 Report"},
    "btn_export": {"ru": "📤 Выгрузить", "en": "📤 Export"},

    # --- кнопки карточки ---
    "btn_pulse": {"ru": "💓 Пульс", "en": "💓 Pulse"},
    "btn_note": {"ru": "📝 Заметка", "en": "📝 Note"},
    "btn_symptoms": {"ru": "🫁 Симптомы", "en": "🫁 Symptoms"},
    "btn_triggers": {"ru": "☕️ Перед этим", "en": "☕️ Before it"},
    # Три кнопки в ряд: Telegram делит ряд на равные доли и обрезает лишнее.
    # С подписью «⏪ начало −15 мин» (16 символов) на телефоне оставалось
    # «⏪ начало…», и −5 было не отличить от −30. Смысл «сдвинуть начало назад»
    # несёт сама иконка: другого времени у идущего эпизода ещё нет.
    "btn_shift": {"ru": "⏪ −{minutes} мин", "en": "⏪ −{minutes} min",
},
    # Не «Ещё идёт»: это отмена ошибочного нажатия, а не продолжение эпизода,
    # и прежняя подпись дословно совпадала с btn_still_on, у которой другое
    # действие. Новая читается как исправление и спутать её нельзя.
    # ✅ значит «отпустило» во всём продукте, ⏪ — про начало. Два ряда никогда
    # не встречаются на одной карточке, поэтому значок сам говорит, какое
    # время двигается, и подпись остаётся девятисимвольной.
    # Владелец увидел на закрытой карточке «кучу зелёных галок»: ✅ у ряда
    # правки конца сталкивался с ✅ выбранной тяжести. Теперь правка конца и
    # «ещё не отпустило» живут за одним самоназывающимся вопросом.
    "btn_end_when": {"ru": "⏱ Уточнить, когда отпустило",
                     "en": "⏱ Adjust when it stopped"},
    "btn_minus": {"ru": "−{dur}", "en": "−{dur}"},
    "ack_end_set": {"ru": "Отпустило: {time}", "en": "Stopped at: {time}"},
    "end_before_start": {"ru": "Раньше начала не бывает — начало {time}.",
                         "en": "That's before the start ({time})."},
    "btn_reopen": {"ru": "↩️ Ещё не отпустило", "en": "↩️ It hasn't stopped yet"},
    "btn_delete": {"ru": "🗑 Удалить", "en": "🗑 Delete"},
    "btn_done": {"ru": "← Готово", "en": "← Done"},
    # «Отмена» в подтверждении удаления двусмысленна (отменить удаление или
    # отменить эпизод?) — безопасный выбор должен называть себя сам.
    "btn_back": {"ru": "← Не удалять", "en": "← Keep it"},
    "btn_no": {"ru": "Нет", "en": "No"},
    "btn_how_long": {"ru": "⏱ Сколько длилось?", "en": "⏱ How long did it last?",
},
    "btn_still_on": {"ru": "⏳ Ещё идёт", "en": "⏳ Still ongoing"},
    "btn_dunno": {"ru": "🤷 Не знаю", "en": "🤷 Don't know"},
    "btn_dont_remember": {"ru": "🤷 Не помню, когда прошло",
                          "en": "🤷 Don't remember when it stopped",
},
    "btn_refine": {"ru": "➕ Уточнить", "en": "➕ Add details"},
    "btn_card": {"ru": "← К эпизоду", "en": "← Back to episode"},
    "btn_clear_note": {"ru": "🗑 Очистить заметку", "en": "🗑 Clear the note",
},
    "btn_cancel": {"ru": "← Отмена", "en": "← Cancel"},
    "btn_csv": {"ru": "📤 Выгрузить в CSV", "en": "📤 Export to CSV",
},
    "btn_period": {"ru": "{days} дн.", "en": "{days} d"},
    "btn_yes_episode": {"ru": "⚡️ Да, эпизод сейчас", "en": "⚡️ Yes, log it now",
},
    "btn_log_with_pulse": {"ru": "⚡️ Записать эпизод и пульс {pulse}",
                           "en": "⚡️ Log an episode with pulse {pulse}",
},

    # --- приветствие и справка ---
    "start": {
        "ru": "Привет! Я дневник аритмии.\n\n"
              "Начался приступ — жмите «{btn_start}» внизу: я сразу запишу время. "
              "Когда отпустит — «{btn_end}».\n"
              "Остальное (тяжесть, пульс, симптомы, причина, заметка) — кнопками на "
              "карточке эпизода, по желанию и когда удобно.\n\n"
              "Мелкие удобства:\n"
              "• просто число в чат — это пульс;\n"
              "• просто текст — заметка к текущему эпизоду;\n"
              "• «{btn_today}» — что было за день, «{btn_report}» — статистика, "
              "«{btn_export}» — CSV для врача.\n\n"
              "Записали не сразу? На карточке есть «⏪ −5/−15/−30 мин» для начала "
              "а у закрытого — «⏱ Уточнить, когда отпустило».\n\n"
              "⚕️ Я дневник, а не медицинская рекомендация: я записываю то, что вы "
              "мне говорите, и ничего не оцениваю и не советую. Решения обсуждайте "
              "с врачом. Записи хранятся на сервере бота, у каждого свои.\n"
              "Язык: /lang",
        "en": "Hi! I'm your arrhythmia diary.\n\n"
              "When an episode starts, tap «{btn_start}» below — I log the time "
              "instantly. When it stops, tap «{btn_end}».\n"
              "Everything else (severity, pulse, symptoms, trigger, note) sits on the "
              "episode card as optional buttons — fill it in later, or not at all.\n\n"
              "Shortcuts:\n"
              "• send a bare number — that's your pulse;\n"
              "• send plain text — it becomes a note on the current episode;\n"
              "• «{btn_today}» shows the day, «{btn_report}» the statistics, "
              "«{btn_export}» a CSV for your doctor.\n\n"
              "Logged it late? The card has «⏪ −5/−15/−30 min» for the start "
              "and a closed one has «⏱ Adjust when it stopped».\n\n"
              "⚕️ I am a diary, not medical advice: I record what you tell me and "
              "never assess or recommend anything. Discuss decisions with your "
              "doctor. Records are kept on the bot's server, separately per "
              "person.\n"
              "Language: /lang",
    },
    "help": {
        "ru": "Команды:\n"
              "/log — записать начало эпизода\n"
              "/stop — отметить, что отпустило\n"
              "/earlier — записать прошедший приступ\n"
              "/last — карточка последнего эпизода\n"
              "/med — отметить приём лекарства\n"
              "/today, /yesterday — сводка за день\n"
              "/week, /month — отчёт за 7 и 30 дней\n"
              "/export — CSV со всеми записями\n"
              "/lang — язык бота\n"
              "/forget — удалить все мои записи\n"
              "/cancel — отменить ожидание ввода\n\n"
              "Мелкие удобства: число в чат — это пульс, текст — заметка к "
              "текущему эпизоду, «📍 Место» — где это было. Время можно "
              "поправить: «⏪» двигает начало, «⏱ Уточнить, когда отпустило» — окончание.\n\n"
              "Что я храню: времена эпизодов, тяжесть, пульс, симптомы, причины, "
              "заметки, приёмы лекарств и места, если вы их отмечали. Всё это "
              "лежит на сервере бота и удаляется целиком командой /forget.\n\n"
              "Время хранится в UTC, показывается в зоне {tz}.\n\n⚕️ Это дневник, а не медицинская рекомендация. Решения — с врачом.",
        "en": "Commands:\n"
              "/log — log the start of an episode\n"
              "/stop — mark that it stopped\n"
              "/earlier — log a past episode\n"
              "/last — card of the latest episode\n"
              "/med — log medication\n"
              "/today, /yesterday — daily summary\n"
              "/week, /month — 7- and 30-day report\n"
              "/export — CSV with every record\n"
              "/lang — bot language\n"
              "/forget — delete all my records\n"
              "/cancel — cancel a pending input\n\n"
              "Shortcuts: a bare number is your pulse, plain text becomes a note on "
              "the current episode, «📍 Place» records where it happened. Times are "
              "correctable: «⏪» moves the start, «⏱ Adjust when it stopped» moves the end.\n\n"
              "What I store: episode times, severity, pulse, symptoms, triggers, "
              "notes, medication entries, and places if you marked them. It all "
              "lives on the bot's server and /forget deletes the lot.\n\n"
              "Times are stored in UTC and shown in the {tz} zone.\n\n⚕️ This is a diary, not medical advice. Decisions belong with your doctor.",
    },

    # --- выбор языка ---
    "lang_prompt": {"ru": "Язык бота:", "en": "Bot language:"},
    "lang_set": {"ru": "Готово, говорю по-русски.", "en": "Done, I'll speak English.",
},

    # --- эпизоды ---
    "ep_already": {
        "ru": "Эпизод #{id} уже идёт с {time} — не стал создавать второй. "
              "Если отпустило, нажмите «{btn_end}».",
        "en": "Episode #{id} has been running since {time} — I didn't start a second "
              "one. If it has stopped, tap «{btn_end}».",
    },
    "ep_logged": {"ru": "⚡️ Записал: эпизод #{id}, начало {time}.",
                  "en": "⚡️ Logged: episode #{id}, started at {time}.",
},
    "ep_closed": {"ru": "✅ Эпизод #{id} закрыт: {dur}.",
                  "en": "✅ Episode #{id} closed: {dur}.",
},
    "ep_recorded": {"ru": "⚡️ Эпизод #{id} записан.", "en": "⚡️ Episode #{id} logged.",
},
    "ep_recorded_pulse": {"ru": "⚡️ Эпизод #{id}, пульс {pulse}.",
                          "en": "⚡️ Episode #{id}, pulse {pulse}.",
},
    "ep_gone": {"ru": "Эпизод #{id} уже удалён.", "en": "Episode #{id} is already deleted.",
},
    "ep_not_found": {"ru": "Эпизод не найден", "en": "Episode not found",
},
    # Нажали «Отпустило» на забытом эпизоде: точное время окончания записать
    # нельзя (приступ мог кончиться часы назад), но и отказывать бессмысленно —
    # сразу спрашиваем длительность.
    "ask_duration_stale": {
        "ru": "Эпизод #{id} начался {day} в {time}, с тех пор прошло {dur}. Точное "
              "время окончания я записать не могу — сколько примерно он длился?",
        "en": "Episode #{id} started {day} at {time}, {dur} ago. I cannot record an "
              "exact end time for it — roughly how long did it last?",
    },
    "reopen_too_old": {
        "ru": "Этот эпизод закрыт давно — вернуть его в работу уже нельзя. "
              "Если приступ снова начался, это новый эпизод.",
        "en": "This episode was closed a while ago, so it cannot be reopened. "
              "If the attack started again, that is a new episode.",
    },
    "ep_state_changed": {"ru": "Эпизод уже в другом состоянии — обновил карточку",
                         "en": "This episode already changed — card refreshed",
},
    "ep_deleted_msg": {"ru": "Эпизод удалён.", "en": "Episode deleted.",
},
    "no_active": {"ru": "Сейчас нет активного эпизода. Начался приступ — «{btn_start}».",
                  "en": "No episode is running right now. When one starts — «{btn_start}».",
},
    "no_records": {"ru": "Записей ещё нет. Начался приступ — «{btn_start}».",
                   "en": "Nothing logged yet. When an episode starts — «{btn_start}».",
},

    # --- уточнения ---
    "pulse_prompt": {"ru": "Пришлите пульс числом (эпизод #{id}). /cancel — отменить.",
                     "en": "Send your pulse as a number (episode #{id}). /cancel to abort.",
},
    "pulse_bad": {"ru": "Нужно число от {min} до {max}. Или /cancel, чтобы не вводить.",
                  "en": "I need a number between {min} and {max}. Or /cancel to skip.",
},
    "pulse_saved": {"ru": "💓 Пульс {pulse} записан.", "en": "💓 Pulse {pulse} saved.",
},
    "pulse_to_ep": {"ru": "💓 Пульс {pulse} → эпизод #{id}.",
                    "en": "💓 Pulse {pulse} → episode #{id}.",
},
    "pulse_needs_episode": {
        "ru": "Это пульс? Сначала отметьте эпизод — тогда число привяжется к нему.",
        "en": "Is that a pulse? Log an episode first and the number will attach to it.",
    },
    "note_prompt": {"ru": "Напишите заметку к эпизоду #{id} одним сообщением. "
                          "/cancel — отменить.",
                    "en": "Send a note for episode #{id} in one message. /cancel to abort.",
                          },
    "note_saved": {"ru": "📝 Заметка добавлена.", "en": "📝 Note added.",
},
    "note_cleared": {"ru": "📝 Заметка очищена.", "en": "📝 Note cleared.",
},
    "note_full": {"ru": "Заметка эпизода заполнена, поэтому я пока ничего не "
                        "дописал. Ваш текст у меня — выберите, что с ним сделать.",
                  "en": "This episode's note is full, so I have appended nothing "
                        "yet. I still have your text — choose what to do with it.",
                        },
    "btn_note_push": {"ru": "📌 Дописать, убрав начало",
                      "en": "📌 Append, drop the oldest",
},
    "note_pushed": {"ru": "📝 Дописал. Самое старое начало заметки пришлось убрать.",
                    "en": "📝 Appended. The oldest part of the note had to go.",
},
    "note_lost_text": {"ru": "Текст уже не у меня — пришлите его снова.",
                       "en": "I no longer have that text — please send it again.",
},
    # Без указания числа: {n} — это UTF-16-единицы, и для эмодзи «1000
    # символов» было бы неправдой (их влезает 500).
    "note_trimmed": {"ru": "Сообщение было длинным — сохранил его начало.",
                     "en": "That message was long — I saved the beginning of it.",
},
    "note_appended": {"ru": "📝 Дописал в эпизод #{id}.",
                      "en": "📝 Appended to episode #{id}.",
},
    "ask_text_as_episode": {"ru": "Записать это как новый эпизод?",
                            "en": "Log this as a new episode?",
},
    "not_logged": {"ru": "Ок, не записал.", "en": "Fine, nothing logged.",
},
    "menu_symptoms": {"ru": "Эпизод #{id}. Что чувствуете? Жмите всё подходящее.",
                      "en": "Episode #{id}. What do you feel? Tap everything that applies.",
},
    "menu_triggers": {"ru": "Эпизод #{id}. Что было перед эпизодом? Жмите всё подходящее.",
                      "en": "Episode #{id}. What came before it? Tap everything that applies.",
},

    # --- лекарства ---
    # --- карточка приёма лекарства ---
    "med_unnamed": {"ru": "Лекарство", "en": "Medication"},
    "med_logged": {"ru": "💊 Записал приём в {time}.",
                   "en": "💊 Medication logged at {time}."},
    "med_locked": {"ru": "Эту запись уже не изменить — прошло время. Можно удалить.",
                   "en": "This entry is locked now — too late to change. You can delete it."},
    "med_card_head": {"ru": "💊 {when} — {name}", "en": "💊 {when} — {name}"},
    "med_card_fresh": {"ru": "⏳ только что", "en": "⏳ just now"},
    "med_card_ago": {"ru": "⏳ прошло {dur}", "en": "⏳ {dur} ago"},
    "med_card_when": {"ru": "{day} в {time}", "en": "{day} at {time}"},
    "med_tag": {"ru": "#лекарство", "en": "#medication"},
    "med_saved_ack": {"ru": "✅ Сохранил", "en": "✅ Saved"},
    "med_pick_name": {"ru": "Что приняли?", "en": "What did you take?"},
    "med_ask_when": {"ru": "Когда приняли {name}?", "en": "When did you take {name}?"},
    "btn_med_when": {"ru": "⏱ Когда", "en": "⏱ When"},
    "btn_med_name": {"ru": "🏷 Название", "en": "🏷 Name"},
    "btn_med_now": {"ru": "только что", "en": "just now"},
    "btn_med_ago": {"ru": "−{dur}", "en": "−{dur}"},
    "btn_med_type": {"ru": "✏️ Другое", "en": "✏️ Other"},
    "btn_med_back": {"ru": "← Назад", "en": "← Back"},
    "btn_med_all": {"ru": "📋 Все лекарства", "en": "📋 All medications"},
    "med_ask_when_generic": {"ru": "Когда приняли лекарство?",
                             "en": "When did you take it?"},
    "too_many_meds": {
        "ru": "Записей о лекарствах уже {n} — это предел. Выгрузите и удалите "
              "старые (/export, затем /forget), и можно писать дальше.",
        "en": "The diary already holds {n} medication entries, which is the "
              "limit. Export and delete the old ones (/export, then /forget).",
    },
    "med_deleted_card": {"ru": "🗑 Приём удалён.", "en": "🗑 Entry deleted."},
    "med_delete_confirm": {"ru": "Удалить приём «{name}» ({time})?",
                           "en": "Delete the «{name}» entry ({time})?"},
    "med_name_prompt": {"ru": "Напишите название лекарства одним сообщением.",
                        "en": "Send the medication name in one message.",
},
    "med_list_stale": {"ru": "Список устарел", "en": "List is out of date",
},

    # --- напоминания и забытые эпизоды ---
    # Без заголовка «Эпизод #N идёт уже…»: вопрос приклеен к тексту самой
    # карточки, а она это уже говорит.
    "remind_ask": {
        "ru": "Отпустило?\n"
              "Если не ответить, через {minutes} мин запишу эпизод без времени "
              "окончания.",
        "en": "Has it stopped?\n"
              "If you don't answer, in {minutes} min I'll log the episode with "
              "no end time.",
    },
    # Короткое сообщение-«плашка»: карточка правится на месте (инлайн), а
    # нижнее меню меняется только вместе с НОВЫМ сообщением. При переходе в
    # «забыт» эпизод перестаёт быть активным, и плашка должна вернуться к
    # «⚡️ Аритмия» — иначе остаётся «✅ Отпустило», которое уже не сработает.
    "plate_after_stale": {
        "ru": "Эпизод без отметки окончания — отметьте длительность на карточке "
              "выше, если помните. Начнётся снова — жмите «{btn_start}».",
        "en": "The episode has no end time — set its duration on the card above "
              "if you remember. If it starts again, tap «{btn_start}».",
    },
    "remind_stale_tail": {
        "ru": "Если помните, сколько длилось — отметьте, и запись станет полной.",
        "en": "If you remember how long it lasted, mark it and the record is "
              "complete.",
    },
    "forgotten_mention": {
        "ru": "Кстати, эпизод #{id} ({day}, {time}) остался без окончания{tail}. "
              "Сколько он длился?",
        "en": "By the way, episode #{id} ({day}, {time}) has no end time{tail}. "
              "How long did it last?",
    },
    "forgotten_tail": {"ru": " (и ещё {n} таких)", "en": " (and {n} more like it)",
},
    "still_on": {"ru": "⏳ Эпизод #{id} продолжается, идёт {dur}.",
                 "en": "⏳ Episode #{id} continues, {dur} so far.",
},
    "still_on_next": {"ru": "\nСпрошу снова через {minutes} мин.",
                      "en": "\nI'll ask again in {minutes} min.",
},
    "will_mark_end": {"ru": "Отмечу окончание, когда нажмёте «{btn_end}».",
                      "en": "I'll close it when you tap «{btn_end}».",
},
    "approx_saved": {"ru": "✅ Эпизод #{id}: примерно {dur}.",
                     "en": "✅ Episode #{id}: roughly {dur}.",
},
    "left_unknown": {
        "ru": "Оставил эпизод #{id} без окончания: сам приступ записан ({day}, {time}), "
              "длительность — неизвестна. В отчёте он отдельной строкой.",
        "en": "Episode #{id} stays without an end time: the episode itself is logged "
              "({day}, {time}), its duration is unknown. The report lists it separately.",
    },

    # --- удаление, подтверждения, ошибки ---
    "delete_confirm": {"ru": "Удалить эпизод #{id} ({time})?",
                       "en": "Delete episode #{id} ({time})?",
},
    "deleted": {"ru": "🗑 Эпизод #{id} удалён.", "en": "🗑 Episode #{id} deleted.",
},
    "done": {"ru": "Готово.", "en": "Done."},
    "cancelled": {"ru": "Отменил ввод.", "en": "Input cancelled."},
    "nothing_to_cancel": {"ru": "Нечего отменять.", "en": "Nothing to cancel.",
},
    "error_generic": {
        "ru": "Что-то сломалось на моей стороне. Запись не потерялась — "
              "проверьте «{btn_today}».",
        "en": "Something broke on my side. Your data is safe — check «{btn_today}».",
    },
    "gate_denied": {
        "ru": "Это личный дневник. Ваш user_id: {id} — передайте его владельцу бота, "
              "если доступ нужен.",
        "en": "This is a private diary. Your user_id is {id} — send it to the bot owner "
              "if you need access.",
    },

    # --- экспорт ---
    # Файл присылать некуда, но и молчать нельзя: без ответа человек решает,
    # что бот сломан. Вложения намеренно не делаем — см. CLAUDE.md.
    "file_declined": {
        "ru": "Файлы я не храню — расскажите словами, что было, или пришлите "
              "число, если это пульс. Сам файл остался у вас в чате, он не "
              "потерялся.",
        "en": "I don't keep files — tell me in words what happened, or send a "
              "number if that was your pulse. The file itself is still in this "
              "chat, it is not lost.",
    },
    "forget_confirm": {
        "ru": "Удалить все мои записи?\n\nЭпизодов: {episodes}, отметок о лекарствах: "
              "{meds}. Удалится всё: времена, тяжесть, пульс, симптомы, заметки, "
              "места, выбранный язык. Это необратимо, и отменить я не смогу.\n"
              "Если нужна копия — сначала /export.",
        "en": "Delete everything I have logged?\n\nEpisodes: {episodes}, medication "
              "entries: {meds}. All of it goes: times, severity, pulse, symptoms, "
              "notes, places, language choice. This cannot be undone.\n"
              "If you want a copy, run /export first.",
    },
    "btn_forget_yes": {"ru": "🗑 Да, удалить всё", "en": "🗑 Yes, delete everything"},
    "forget_done": {"ru": "Удалил всё: эпизодов {episodes}, отметок о лекарствах "
                          "{meds}. Дневник пуст.",
                    "en": "Deleted everything: {episodes} episodes, {meds} medication "
                          "entries. The diary is empty."},
    "forget_empty": {"ru": "Удалять нечего — записей нет.",
                     "en": "Nothing to delete — there are no records."},
    "too_fast": {"ru": "Слишком часто — подождите секунду.",
                 "en": "Too fast — give it a second."},
    "too_many_episodes": {
        "ru": "В дневнике уже {n} эпизодов — это предел. Выгрузите и удалите старые "
              "(/export, затем /forget), и можно писать дальше.",
        "en": "The diary already holds {n} episodes, which is the limit. Export and "
              "delete the old ones (/export, then /forget) to carry on.",
    },
    "too_many_open": {
        "ru": "Уже {n} незакрытых эпизодов. Закройте или уточните длительность хотя "
              "бы у одного — иначе дневник превращается в кашу.",
        "en": "There are already {n} unclosed episodes. Close one or give it a "
              "duration — otherwise the diary turns to mush.",
    },
    "export_cooldown": {"ru": "Выгрузку только что делали — подождите минуту.",
                        "en": "You exported just now — give it a minute."},
    "cmd_forget": {"ru": "удалить все мои записи", "en": "delete all my records"},
    # --- ретроспективная запись: приступ, который уже прошёл ---
    # Решения владельца: только кнопки (ручного ввода дат и времени нет),
    # только сегодня/вчера/позавчера, окончание — примерной длительностью.
    "cmd_earlier": {"ru": "записать прошедший приступ", "en": "log a past episode"},
    "btn_backfill": {"ru": "➕ Приступ в этот день", "en": "➕ Episode on this day"},
    "btn_day_today": {"ru": "Сегодня", "en": "Today"},
    "btn_day_yesterday": {"ru": "Вчера", "en": "Yesterday"},
    "btn_day_before": {"ru": "Позавчера", "en": "2 days ago"},
    "btn_back_step": {"ru": "← Назад", "en": "← Back"},
    "back_intro": {
        "ru": "Запишем приступ, который уже прошёл. Когда это было?",
        "en": "Let's log an episode that is already over. When was it?",
    },
    "back_ask_hour": {"ru": "{date}. В каком часу начался приступ?",
                      "en": "{date}. What hour did the episode start?"},
    "back_today_hours_note": {"ru": "Показываю только часы, которые уже прошли.",
                              "en": "Only hours that have already passed are shown."},
    "back_ask_minute": {"ru": "Около какого времени? Выберите ближайшее.",
                        "en": "Roughly what time? Pick the closest."},
    "back_ask_duration": {
        "ru": "Начало — {date} в {time}. Сколько примерно длился приступ?",
        "en": "Started {date} at {time}. Roughly how long did it last?",
    },
    "back_duplicate": {
        "ru": "Этот приступ уже записан — вот его карточка.",
        "en": "That episode is already recorded — here is its card.",
    },
    "back_saved": {
        "ru": "✅ Записал приступ: {date}, {start}–{end}, примерно {dur}. "
              "Остальное можно уточнить на карточке.",
        "en": "✅ Logged an episode: {date}, {start}–{end}, roughly {dur}. "
              "You can fill in the rest on the card.",
    },
    "back_saved_unknown": {
        "ru": "✅ Записал приступ: {date} в {time}. Длительность неизвестна — "
              "в отчёте он отдельной строкой.",
        "en": "✅ Logged an episode: {date} at {time}. Duration unknown — "
              "the report lists it separately.",
    },
    "nothing_to_export": {"ru": "Пока нечего выгружать — записей нет.",
                          "en": "Nothing to export yet — no records.",
},
    "csv_caption": {"ru": "Эпизодов: {n}. Открывается в Excel и Google Sheets.",
                    "en": "Episodes: {n}. Opens in Excel and Google Sheets.",
},
    "csv_preparing": {"ru": "Готовлю файл…", "en": "Preparing the file…",
},
    "csv_type_episode": {"ru": "эпизод", "en": "episode"},
    "csv_type_med": {"ru": "лекарство", "en": "medication"},
    "csv_yes": {"ru": "да", "en": "yes"},

    # --- короткие всплывающие подтверждения (query.answer) ---
    "ack_saved": {"ru": "Записал", "en": "Saved"},
    "ack_deleted": {"ru": "Удалено", "en": "Deleted"},
    "ack_ok": {"ru": "Ок", "en": "OK"},
    "ack_lasted": {"ru": "Длилось {dur}", "en": "Lasted {dur}"},
    "ack_reopened": {"ru": "Эпизод снова открыт", "en": "Episode reopened",
},
    "ack_start_set": {"ru": "Начало: {time}", "en": "Start: {time}"},
    "ack_extended": {"ru": "Продлил на {minutes} мин", "en": "Extended by {minutes} min",
},
    "ack_approx": {"ru": "Записал ~{dur}", "en": "Saved ~{dur}"},

    # --- длительности ---
    "dur_sec": {"ru": "{n} сек", "en": "{n} sec"},
    "dur_min": {"ru": "{n} мин", "en": "{n} min"},
    "dur_h": {"ru": "{h} ч", "en": "{h} h"},
    "dur_h_m": {"ru": "{h} ч {m} мин", "en": "{h} h {m} min"},
    "dur_d": {"ru": "{d} дн", "en": "{d} d"},
    "dur_d_h": {"ru": "{d} дн {h} ч", "en": "{d} d {h} h"},

    # Дата «день + месяц»: в португальском между ними нужен предлог.
    "date_day_month": {"ru": "{day} {month}", "en": "{day} {month}",
},

    # --- карточка эпизода ---
    "day_today": {"ru": "сегодня", "en": "today"},
    "day_yesterday": {"ru": "вчера", "en": "yesterday"},
    "card_open_fresh": {"ru": "⚡️ Эпизод #{id} — идёт", "en": "⚡️ Episode #{id} — ongoing"},
    "card_open": {"ru": "⚡️ Эпизод #{id} — идёт, уже {dur}",
                  "en": "⚡️ Episode #{id} — ongoing, {dur} so far",
},
    "card_stale": {"ru": "⚠️ Эпизод #{id} — окончание не отмечено",
                   "en": "⚠️ Episode #{id} — no end time",
},
    "card_closed": {"ru": "✅ Эпизод #{id} — {dur}", "en": "✅ Episode #{id} — {dur}",
},
    "card_start": {"ru": "Начало: {day} в {time}", "en": "Started: {day} at {time}",
},
    "card_range": {"ru": "{day}, {start}–{end}", "en": "{day}, {start}–{end}",
},
    "card_approx_suffix": {"ru": " (время окончания примерное)",
                           "en": " (end time is approximate)",
},
    "card_unknown_dur": {"ru": "Длительность неизвестна (прошло {dur}).",
                         "en": "Duration unknown ({dur} elapsed).",
},
    "card_stale_hint": {"ru": "Если помните, сколько длилось — отметьте кнопкой ниже.",
                        "en": "If you remember how long it lasted, mark it below.",
},
    "card_severity": {"ru": "Тяжесть: {value}", "en": "Severity: {value}",
},
    "card_pulse": {"ru": "Пульс: {value}", "en": "Pulse: {value}"},
    "card_symptoms": {"ru": "Симптомы: {list}", "en": "Symptoms: {list}",
},
    "card_triggers": {"ru": "Перед этим: {list}", "en": "Before it: {list}",
},
    "card_note": {"ru": "Заметка: {text}", "en": "Note: {text}"},
    "card_place": {"ru": "Место: {link}", "en": "Place: {link}"},
    "btn_place_clear": {"ru": "📍 Убрать место", "en": "📍 Remove the place"},
    "place_saved": {"ru": "📍 Место отмечено для эпизода #{id}.",
                    "en": "📍 Place noted for episode #{id}."},
    "place_cleared": {"ru": "📍 Место убрано.", "en": "📍 Place removed."},
    "place_needs_episode": {
        "ru": "Место привязывается к эпизоду, а сейчас активного нет. "
              "Отметьте приступ — и пришлите геопозицию снова.",
        "en": "A place attaches to an episode, and none is running right now. "
              "Log an episode and send the location again.",
    },
    "dash": {"ru": "—", "en": "—"},

    # --- сводка за день ---
    "day_header": {"ru": "📋 {title} ({date})", "en": "📋 {title} ({date})",
},
    "day_title_today": {"ru": "Сегодня", "en": "Today"},
    "day_empty": {"ru": "\nЗаписей нет. Хороший день 🙂",
                  "en": "\nNothing logged. A good day 🙂",
},
    "day_count": {"ru": "⚡️ Аритмии: {n}", "en": "⚡️ Arrhythmias: {n}"},
    "day_no_episodes": {"ru": "Аритмии не регистрировали",
                        "en": "No arrhythmias recorded"},
    "day_part_morning": {"ru": "🌅 Утро", "en": "🌅 Morning"},
    "day_part_day": {"ru": "☀️ День", "en": "☀️ Afternoon"},
    "day_part_evening": {"ru": "🌆 Вечер", "en": "🌆 Evening"},
    "day_part_night": {"ru": "🌙 Ночь", "en": "🌙 Night"},
    "day_total": {"ru": " · суммарно {dur}", "en": " · {dur} in total",
},
    "line_stale": {"ru": "⚠️ {time} — окончание не отмечено",
                   "en": "⚠️ {time} — no end time"},
    "line_open": {"ru": "⚡️ {time} — идёт ({dur})", "en": "⚡️ {time} — ongoing ({dur})",
},
    "line_closed": {"ru": "• {start}–{end} ({dur})", "en": "• {start}–{end} ({dur})",
},
    "line_symptoms": {"ru": "    🫁 {list}", "en": "    🫁 {list}"},
    "line_triggers": {"ru": "    ☕️ перед этим: {list}", "en": "    ☕️ before: {list}",
},
    "line_med": {"ru": "💊 {time} — {name}", "en": "💊 {time} — {name}",
},
    "med_default": {"ru": "лекарство", "en": "medication"},

    # --- отчёт за период ---
    "rep_header": {"ru": "📈 Отчёт за {days} дн. ({start} — {end})",
                   "en": "📈 Report for {days} days ({start} — {end})",
},
    "rep_empty": {"ru": "Эпизодов не зарегистрировано.", "en": "No episodes logged.",
},
    "rep_count": {"ru": "Эпизодов: {n} · {per_day} в день",
                  "en": "Episodes: {n} · {per_day} per day",
},
    "rep_duration": {"ru": "Длительность: всего {total} · в среднем {avg} · "
                           "максимум {max} ({day})",
                     "en": "Duration: {total} in total · {avg} on average · "
                           "{max} longest ({day})",
                           },
    "rep_approx": {"ru": "  из них с примерной длительностью: {n}",
                   "en": "  of those, approximate duration: {n}",
},
    "rep_stale": {"ru": "Без отметки окончания: {n} — длительность неизвестна, "
                        "в средние значения не входят",
                  "en": "Without an end time: {n} — duration unknown, excluded from "
                        "the averages",
},
    "rep_ongoing": {"ru": "Идёт прямо сейчас: 1", "en": "Ongoing right now: 1",
},
    # {range} вместо min–max: при единственном замере «148–148» выглядит глупо.
    # «замеров: 1» вместо «по 1 записям» — чтобы не согласовывать число в трёх языках.
    "rep_pulse": {"ru": "Пульс: {range} · в среднем {avg} (замеров: {n})",
                  "en": "Pulse: {range} · {avg} on average (readings: {n})",
},
    "rep_severity": {"ru": "Тяжесть: {parts}", "en": "Severity: {parts}",
},
    "rep_symptoms": {"ru": "Симптомы:", "en": "Symptoms:"},
    "rep_triggers": {"ru": "Перед эпизодом:", "en": "Before the episode:",
},
    "rep_hours": {"ru": "По времени суток:", "en": "By time of day:",
},
    "rep_weekdays": {"ru": "По дням недели: {parts}", "en": "By weekday: {parts}",
},
    "rep_meds": {"ru": "Приёмов лекарств: {n}", "en": "Medication taken: {n}",
},

    # --- описания команд в меню Telegram ---
    "cmd_log": {"ru": "⚡️ записать эпизод аритмии", "en": "⚡️ log an arrhythmia episode",
},
    "cmd_stop": {"ru": "✅ отпустило", "en": "✅ it stopped"},
    "cmd_last": {"ru": "карточка последнего эпизода", "en": "card of the latest episode",
},
    "cmd_med": {"ru": "💊 отметить лекарство", "en": "💊 log medication",
},
    "cmd_today": {"ru": "сводка за сегодня", "en": "today's summary",
},
    "cmd_yesterday": {"ru": "сводка за вчера", "en": "yesterday's summary",
},
    "cmd_week": {"ru": "отчёт за 7 дней", "en": "7-day report",
},
    "cmd_month": {"ru": "отчёт за 30 дней", "en": "30-day report",
},
    "cmd_export": {"ru": "выгрузить CSV", "en": "export CSV"},
    "cmd_lang": {"ru": "язык бота", "en": "bot language"},
    "cmd_cancel": {"ru": "отменить ввод", "en": "cancel input"},
    "cmd_help": {"ru": "справка", "en": "help"},
}

LANG_NAMES = {"ru": "Русский", "en": "English"}


def utf16_len(text: str) -> int:
    """Длина так, как её считает Telegram: в UTF-16-единицах.

    Эмодзи вне BMP — это одна «буква» для `len()`, но ДВЕ единицы для
    Telegram. Поэтому `len()` завышает запас вдвое: заметка из 3000 эмодзи
    весит 6000 единиц, и сообщение перестаёт отправляться, хотя по `len()`
    всё в порядке.
    """
    return len(text.encode("utf-16-le")) // 2


def trim_utf16(text: str, limit: int) -> str:
    """Обрезает текст так, чтобы он влез в `limit` UTF-16-единиц.

    Режет по кодовым пунктам (двоичным поиском), поэтому не разрывает
    эмодзи на половинки суррогатной пары.
    """
    if limit <= 0 or not text:
        return ""
    if utf16_len(text) <= limit:
        return text
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if utf16_len(text[:middle]) <= limit:
            low = middle
        else:
            high = middle - 1
    return text[:low]


def tail_lines_utf16(text: str, limit: int) -> tuple[str, bool]:
    """Оставляет конец текста, отбрасывая СТРОКИ с начала, пока не влезет.

    Отбрасываются целые строки, а не середина слова: заметка собирается
    построчно, и так понятно, что именно пропало. Возвращает (текст, обрезано).
    """
    if utf16_len(text) <= limit:
        return text, False
    lines = text.split("\n")
    while lines and utf16_len("\n".join(lines)) > limit:
        lines.pop(0)
    return "\n".join(lines), True


def normalize(code: str | None) -> str | None:
    """«en-US» → «en»; неподдерживаемый язык → None."""
    if not code:
        return None
    base = code.split("-")[0].split("_")[0].lower()
    return base if base in SUPPORTED else None


def resolve(stored: str | None, telegram_code: str | None, default: str) -> str:
    """Явный выбор пользователя важнее профиля Telegram, профиль — важнее дефолта."""
    return normalize(stored) or normalize(telegram_code) or normalize(default) or FALLBACK


def t(lang: str, key: str, **kwargs) -> str:
    bundle = STRINGS.get(key)
    if bundle is None:
        return key
    text = bundle.get(lang) or bundle.get(FALLBACK) or next(iter(bundle.values()))
    return text.format(**kwargs) if kwargs else text


def months(lang: str) -> tuple[str, ...]:
    return MONTHS.get(lang, MONTHS[FALLBACK])


def weekdays(lang: str) -> tuple[str, ...]:
    return WEEKDAYS.get(lang, WEEKDAYS[FALLBACK])


def csv_header(lang: str) -> list[str]:
    return CSV_HEADER.get(lang, CSV_HEADER[FALLBACK])
