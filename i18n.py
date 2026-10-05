#!/usr/bin/env python3
"""Строки интерфейса на русском, английском и португальском.

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

SUPPORTED = ("ru", "en", "pt")
FALLBACK = "en"

MONTHS = {
    "ru": ("января", "февраля", "марта", "апреля", "мая", "июня",
           "июля", "августа", "сентября", "октября", "ноября", "декабря"),
    "en": ("January", "February", "March", "April", "May", "June",
           "July", "August", "September", "October", "November", "December"),
    "pt": ("janeiro", "fevereiro", "março", "abril", "maio", "junho",
           "julho", "agosto", "setembro", "outubro", "novembro", "dezembro"),
}

WEEKDAYS = {
    "ru": ("пн", "вт", "ср", "чт", "пт", "сб", "вс"),
    "en": ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"),
    "pt": ("seg", "ter", "qua", "qui", "sex", "sáb", "dom"),
}

# Заголовки CSV — отдельно от обычных строк, это список колонок.
CSV_HEADER = {
    "ru": ["тип", "id", "дата", "начало", "конец", "длительность_мин",
           "длительность_примерная", "тяжесть", "пульс", "симптомы",
           "перед_эпизодом", "заметка"],
    "en": ["type", "id", "date", "start", "end", "duration_min",
           "duration_approx", "severity", "pulse", "symptoms",
           "before_episode", "note"],
    "pt": ["tipo", "id", "data", "inicio", "fim", "duracao_min",
           "duracao_aproximada", "intensidade", "pulso", "sintomas",
           "antes_do_episodio", "nota"],
}

STRINGS: dict[str, dict[str, str]] = {
    # --- кнопки нижней плашки (текст приходит обратно от Telegram, поэтому
    # должен быть уникальным среди всех языков) ---
    "btn_start": {"ru": "⚡️ Аритмия", "en": "⚡️ Arrhythmia", "pt": "⚡️ Arritmia"},
    "btn_end": {"ru": "✅ Отпустило", "en": "✅ It stopped", "pt": "✅ Já passou"},
    "btn_med": {"ru": "💊 Лекарство", "en": "💊 Medication", "pt": "💊 Medicamento"},
    "btn_today": {"ru": "📋 Сегодня", "en": "📋 Today", "pt": "📋 Hoje"},
    "btn_report": {"ru": "📈 Отчёт", "en": "📈 Report", "pt": "📈 Relatório"},
    "btn_export": {"ru": "📤 Выгрузить", "en": "📤 Export", "pt": "📤 Exportar"},

    # --- кнопки карточки ---
    "btn_pulse": {"ru": "💓 Пульс", "en": "💓 Pulse", "pt": "💓 Pulso"},
    "btn_note": {"ru": "📝 Заметка", "en": "📝 Note", "pt": "📝 Nota"},
    "btn_symptoms": {"ru": "🫁 Симптомы", "en": "🫁 Symptoms", "pt": "🫁 Sintomas"},
    "btn_triggers": {"ru": "☕️ Перед этим", "en": "☕️ Before it", "pt": "☕️ Antes disto"},
    "btn_shift": {"ru": "⏪ начало −{minutes} мин", "en": "⏪ start −{minutes} min",
                  "pt": "⏪ início −{minutes} min"},
    "btn_reopen": {"ru": "↩️ Ещё идёт", "en": "↩️ Still ongoing", "pt": "↩️ Ainda a decorrer"},
    "btn_delete": {"ru": "🗑 Удалить", "en": "🗑 Delete", "pt": "🗑 Eliminar"},
    "btn_done": {"ru": "← Готово", "en": "← Done", "pt": "← Pronto"},
    "btn_back": {"ru": "← Отмена", "en": "← Cancel", "pt": "← Cancelar"},
    "btn_no": {"ru": "Нет", "en": "No", "pt": "Não"},
    "btn_how_long": {"ru": "⏱ Сколько длилось?", "en": "⏱ How long did it last?",
                     "pt": "⏱ Quanto tempo durou?"},
    "btn_still_on": {"ru": "⏳ Ещё идёт", "en": "⏳ Still ongoing", "pt": "⏳ Ainda a decorrer"},
    "btn_dunno": {"ru": "🤷 Не знаю", "en": "🤷 Don't know", "pt": "🤷 Não sei"},
    "btn_dont_remember": {"ru": "🤷 Не помню, когда прошло",
                          "en": "🤷 Don't remember when it stopped",
                          "pt": "🤷 Não me lembro quando passou"},
    "btn_med_other": {"ru": "✏️ Другое", "en": "✏️ Other", "pt": "✏️ Outro"},
    "btn_med_cancel": {"ru": "🗑 Отменить", "en": "🗑 Undo", "pt": "🗑 Anular"},
    "btn_csv": {"ru": "📤 Выгрузить в CSV", "en": "📤 Export to CSV",
                "pt": "📤 Exportar para CSV"},
    "btn_period": {"ru": "{days} дн.", "en": "{days} d", "pt": "{days} dias"},
    "btn_yes_episode": {"ru": "⚡️ Да, эпизод сейчас", "en": "⚡️ Yes, log it now",
                        "pt": "⚡️ Sim, registar agora"},
    "btn_log_with_pulse": {"ru": "⚡️ Записать эпизод и пульс {pulse}",
                           "en": "⚡️ Log an episode with pulse {pulse}",
                           "pt": "⚡️ Registar episódio com pulso {pulse}"},

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
              "Записал не сразу? На карточке есть «⏪ начало −5/−15/−30 мин».\n"
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
              "Logged it late? The card has «⏪ start −5/−15/−30 min».\n"
              "Language: /lang",
        "pt": "Olá! Sou o seu diário de arritmia.\n\n"
              "Quando começar um episódio, toque em «{btn_start}» aqui abaixo — "
              "registo a hora de imediato. Quando passar, toque em «{btn_end}».\n"
              "O resto (intensidade, pulso, sintomas, causa, nota) fica no cartão do "
              "episódio, em botões opcionais — pode preencher depois, ou não "
              "preencher.\n\n"
              "Atalhos:\n"
              "• envie apenas um número — é o pulso;\n"
              "• envie texto simples — fica como nota do episódio atual;\n"
              "• «{btn_today}» mostra o dia, «{btn_report}» as estatísticas, "
              "«{btn_export}» um CSV para o médico.\n\n"
              "Registou tarde? O cartão tem «⏪ início −5/−15/−30 min».\n"
              "Idioma: /lang",
    },
    "help": {
        "ru": "Команды:\n"
              "/log — записать начало эпизода\n"
              "/stop — отметить, что отпустило\n"
              "/last — карточка последнего эпизода\n"
              "/med — отметить приём лекарства\n"
              "/today, /yesterday — сводка за день\n"
              "/week, /month — отчёт за 7 и 30 дней\n"
              "/export — CSV со всеми записями\n"
              "/lang — язык бота\n"
              "/cancel — отменить ожидание ввода\n\n"
              "Время хранится в UTC, показывается в зоне {tz}.",
        "en": "Commands:\n"
              "/log — log the start of an episode\n"
              "/stop — mark that it stopped\n"
              "/last — card of the latest episode\n"
              "/med — log medication\n"
              "/today, /yesterday — daily summary\n"
              "/week, /month — 7- and 30-day report\n"
              "/export — CSV with every record\n"
              "/lang — bot language\n"
              "/cancel — cancel a pending input\n\n"
              "Times are stored in UTC and shown in the {tz} zone.",
        "pt": "Comandos:\n"
              "/log — registar o início de um episódio\n"
              "/stop — marcar que já passou\n"
              "/last — cartão do último episódio\n"
              "/med — registar medicamento\n"
              "/today, /yesterday — resumo do dia\n"
              "/week, /month — relatório de 7 e 30 dias\n"
              "/export — CSV com todos os registos\n"
              "/lang — idioma do bot\n"
              "/cancel — cancelar uma entrada pendente\n\n"
              "As horas são guardadas em UTC e mostradas no fuso {tz}.",
    },

    # --- выбор языка ---
    "lang_prompt": {"ru": "Язык бота:", "en": "Bot language:", "pt": "Idioma do bot:"},
    "lang_set": {"ru": "Готово, говорю по-русски.", "en": "Done, I'll speak English.",
                 "pt": "Pronto, vou falar português."},

    # --- эпизоды ---
    "ep_already": {
        "ru": "Эпизод #{id} уже идёт с {time} — не стал создавать второй. "
              "Если отпустило, нажмите «{btn_end}».",
        "en": "Episode #{id} has been running since {time} — I didn't start a second "
              "one. If it has stopped, tap «{btn_end}».",
        "pt": "O episódio #{id} está a decorrer desde as {time} — não criei outro. "
              "Se já passou, toque em «{btn_end}».",
    },
    "ep_logged": {"ru": "⚡️ Записал: эпизод #{id}, начало {time}.",
                  "en": "⚡️ Logged: episode #{id}, started at {time}.",
                  "pt": "⚡️ Registado: episódio #{id}, início às {time}."},
    "ep_closed": {"ru": "✅ Эпизод #{id} закрыт: {dur}.",
                  "en": "✅ Episode #{id} closed: {dur}.",
                  "pt": "✅ Episódio #{id} fechado: {dur}."},
    "ep_recorded": {"ru": "⚡️ Эпизод #{id} записан.", "en": "⚡️ Episode #{id} logged.",
                    "pt": "⚡️ Episódio #{id} registado."},
    "ep_recorded_pulse": {"ru": "⚡️ Эпизод #{id}, пульс {pulse}.",
                          "en": "⚡️ Episode #{id}, pulse {pulse}.",
                          "pt": "⚡️ Episódio #{id}, pulso {pulse}."},
    "ep_gone": {"ru": "Эпизод #{id} уже удалён.", "en": "Episode #{id} is already deleted.",
                "pt": "O episódio #{id} já foi eliminado."},
    "ep_not_found": {"ru": "Эпизод не найден", "en": "Episode not found",
                     "pt": "Episódio não encontrado"},
    "ep_deleted_msg": {"ru": "Эпизод удалён.", "en": "Episode deleted.",
                       "pt": "Episódio eliminado."},
    "no_active": {"ru": "Сейчас нет активного эпизода. Начался приступ — «{btn_start}».",
                  "en": "No episode is running right now. When one starts — «{btn_start}».",
                  "pt": "Não há episódio a decorrer. Quando começar — «{btn_start}»."},
    "no_records": {"ru": "Записей ещё нет. Начался приступ — «{btn_start}».",
                   "en": "Nothing logged yet. When an episode starts — «{btn_start}».",
                   "pt": "Ainda não há registos. Quando começar um episódio — «{btn_start}»."},

    # --- уточнения ---
    "pulse_prompt": {"ru": "Пришлите пульс числом (эпизод #{id}). /cancel — отменить.",
                     "en": "Send your pulse as a number (episode #{id}). /cancel to abort.",
                     "pt": "Envie o pulso como número (episódio #{id}). /cancel para anular."},
    "pulse_bad": {"ru": "Нужно число от {min} до {max}. Или /cancel, чтобы не вводить.",
                  "en": "I need a number between {min} and {max}. Or /cancel to skip.",
                  "pt": "Preciso de um número entre {min} e {max}. Ou /cancel para desistir."},
    "pulse_saved": {"ru": "💓 Пульс {pulse} записан.", "en": "💓 Pulse {pulse} saved.",
                    "pt": "💓 Pulso {pulse} guardado."},
    "pulse_to_ep": {"ru": "💓 Пульс {pulse} → эпизод #{id}.",
                    "en": "💓 Pulse {pulse} → episode #{id}.",
                    "pt": "💓 Pulso {pulse} → episódio #{id}."},
    "pulse_needs_episode": {
        "ru": "Это пульс? Сначала отметьте эпизод — тогда число привяжется к нему.",
        "en": "Is that a pulse? Log an episode first and the number will attach to it.",
        "pt": "É o pulso? Registe primeiro um episódio e o número fica ligado a ele.",
    },
    "note_prompt": {"ru": "Напишите заметку к эпизоду #{id} одним сообщением. "
                          "/cancel — отменить.",
                    "en": "Send a note for episode #{id} in one message. /cancel to abort.",
                    "pt": "Escreva uma nota para o episódio #{id} numa só mensagem. "
                          "/cancel para anular."},
    "note_saved": {"ru": "📝 Заметка добавлена.", "en": "📝 Note added.",
                   "pt": "📝 Nota adicionada."},
    "note_appended": {"ru": "📝 Дописал в эпизод #{id}.",
                      "en": "📝 Appended to episode #{id}.",
                      "pt": "📝 Acrescentado ao episódio #{id}."},
    "ask_text_as_episode": {"ru": "Записать это как новый эпизод?",
                            "en": "Log this as a new episode?",
                            "pt": "Registar isto como um novo episódio?"},
    "not_logged": {"ru": "Ок, не записал.", "en": "Fine, nothing logged.",
                   "pt": "Certo, não registei."},
    "menu_symptoms": {"ru": "Эпизод #{id}. Что чувствуете? Жмите всё подходящее.",
                      "en": "Episode #{id}. What do you feel? Tap everything that applies.",
                      "pt": "Episódio #{id}. O que sente? Toque em tudo o que se aplica."},
    "menu_triggers": {"ru": "Эпизод #{id}. Что было перед эпизодом? Жмите всё подходящее.",
                      "en": "Episode #{id}. What came before it? Tap everything that applies.",
                      "pt": "Episódio #{id}. O que houve antes? Toque em tudo o que se aplica."},

    # --- лекарства ---
    "med_logged": {"ru": "💊 Отметил приём в {time}. Что приняли?",
                   "en": "💊 Logged at {time}. What did you take?",
                   "pt": "💊 Registado às {time}. O que tomou?"},
    "med_name_prompt": {"ru": "Напишите название лекарства одним сообщением.",
                        "en": "Send the medication name in one message.",
                        "pt": "Escreva o nome do medicamento numa só mensagem."},
    "med_saved": {"ru": "💊 Записал: {name}.", "en": "💊 Saved: {name}.",
                  "pt": "💊 Guardado: {name}."},
    "med_deleted": {"ru": "Отметку о лекарстве удалил.", "en": "Medication entry removed.",
                    "pt": "Registo do medicamento removido."},
    "med_list_stale": {"ru": "Список устарел", "en": "List is out of date",
                       "pt": "A lista está desatualizada"},

    # --- напоминания и забытые эпизоды ---
    "remind_ask": {
        "ru": "Эпизод #{id} идёт уже {dur}. Отпустило?\n"
              "Если не ответить, через {minutes} мин запишу его без времени окончания.",
        "en": "Episode #{id} has been running for {dur}. Has it stopped?\n"
              "If you don't answer, in {minutes} min I'll log it with no end time.",
        "pt": "O episódio #{id} já dura {dur}. Já passou?\n"
              "Se não responder, dentro de {minutes} min registo-o sem hora de fim.",
    },
    "remind_stale": {
        "ru": "⚠️ Эпизод #{id} (начало {time}, {day}) остался без отметки окончания.\n"
              "Длительность пока неизвестна — если помните, отметьте примерно.",
        "en": "⚠️ Episode #{id} ({day}, started {time}) has no end time.\n"
              "Its duration is unknown — mark it roughly if you remember.",
        "pt": "⚠️ O episódio #{id} ({day}, início às {time}) ficou sem hora de fim.\n"
              "A duração é desconhecida — indique-a por aproximação, se se lembrar.",
    },
    "forgotten_mention": {
        "ru": "Кстати, эпизод #{id} ({day}, {time}) остался без окончания{tail}. "
              "Сколько он длился?",
        "en": "By the way, episode #{id} ({day}, {time}) has no end time{tail}. "
              "How long did it last?",
        "pt": "A propósito, o episódio #{id} ({day}, {time}) ficou sem fim{tail}. "
              "Quanto tempo durou?",
    },
    "forgotten_tail": {"ru": " (и ещё {n} таких)", "en": " (and {n} more like it)",
                       "pt": " (e mais {n} iguais)"},
    "still_on": {"ru": "⏳ Эпизод #{id} продолжается, идёт {dur}.",
                 "en": "⏳ Episode #{id} continues, {dur} so far.",
                 "pt": "⏳ O episódio #{id} continua, já {dur}."},
    "still_on_next": {"ru": "\nСпрошу снова через {minutes} мин.",
                      "en": "\nI'll ask again in {minutes} min.",
                      "pt": "\nVolto a perguntar dentro de {minutes} min."},
    "will_mark_end": {"ru": "Отмечу окончание, когда нажмёте «{btn_end}».",
                      "en": "I'll close it when you tap «{btn_end}».",
                      "pt": "Fecho-o quando tocar em «{btn_end}»."},
    "approx_saved": {"ru": "✅ Эпизод #{id}: примерно {dur}.",
                     "en": "✅ Episode #{id}: roughly {dur}.",
                     "pt": "✅ Episódio #{id}: cerca de {dur}."},
    "left_unknown": {
        "ru": "Оставил эпизод #{id} без окончания: сам приступ записан ({day}, {time}), "
              "длительность — неизвестна. В отчёте он отдельной строкой.",
        "en": "Episode #{id} stays without an end time: the episode itself is logged "
              "({day}, {time}), its duration is unknown. The report lists it separately.",
        "pt": "O episódio #{id} fica sem hora de fim: o episódio está registado "
              "({day}, {time}), a duração é desconhecida. No relatório aparece à parte.",
    },

    # --- удаление, подтверждения, ошибки ---
    "delete_confirm": {"ru": "Удалить эпизод #{id} ({time})?",
                       "en": "Delete episode #{id} ({time})?",
                       "pt": "Eliminar o episódio #{id} ({time})?"},
    "deleted": {"ru": "🗑 Эпизод #{id} удалён.", "en": "🗑 Episode #{id} deleted.",
                "pt": "🗑 Episódio #{id} eliminado."},
    "done": {"ru": "Готово.", "en": "Done.", "pt": "Pronto."},
    "cancelled": {"ru": "Отменил ввод.", "en": "Input cancelled.", "pt": "Entrada anulada."},
    "nothing_to_cancel": {"ru": "Нечего отменять.", "en": "Nothing to cancel.",
                          "pt": "Nada para anular."},
    "error_generic": {
        "ru": "Что-то сломалось на моей стороне. Запись не потерялась — "
              "проверьте «{btn_today}».",
        "en": "Something broke on my side. Your data is safe — check «{btn_today}».",
        "pt": "Algo falhou do meu lado. Os dados estão guardados — veja «{btn_today}».",
    },
    "gate_denied": {
        "ru": "Это личный дневник. Ваш user_id: {id} — передайте его владельцу бота, "
              "если доступ нужен.",
        "en": "This is a private diary. Your user_id is {id} — send it to the bot owner "
              "if you need access.",
        "pt": "Este diário é privado. O seu user_id é {id} — envie-o ao proprietário do "
              "bot se precisar de acesso.",
    },

    # --- экспорт ---
    "nothing_to_export": {"ru": "Пока нечего выгружать — записей нет.",
                          "en": "Nothing to export yet — no records.",
                          "pt": "Ainda não há nada para exportar — sem registos."},
    "csv_caption": {"ru": "Эпизодов: {n}. Открывается в Excel и Google Sheets.",
                    "en": "Episodes: {n}. Opens in Excel and Google Sheets.",
                    "pt": "Episódios: {n}. Abre no Excel e no Google Sheets."},
    "csv_preparing": {"ru": "Готовлю файл…", "en": "Preparing the file…",
                      "pt": "A preparar o ficheiro…"},
    "csv_type_episode": {"ru": "эпизод", "en": "episode", "pt": "episódio"},
    "csv_type_med": {"ru": "лекарство", "en": "medication", "pt": "medicamento"},
    "csv_yes": {"ru": "да", "en": "yes", "pt": "sim"},

    # --- короткие всплывающие подтверждения (query.answer) ---
    "ack_saved": {"ru": "Записал", "en": "Saved", "pt": "Guardado"},
    "ack_deleted": {"ru": "Удалено", "en": "Deleted", "pt": "Eliminado"},
    "ack_ok": {"ru": "Ок", "en": "OK", "pt": "OK"},
    "ack_lasted": {"ru": "Длилось {dur}", "en": "Lasted {dur}", "pt": "Durou {dur}"},
    "ack_reopened": {"ru": "Эпизод снова открыт", "en": "Episode reopened",
                     "pt": "Episódio reaberto"},
    "ack_start_set": {"ru": "Начало: {time}", "en": "Start: {time}", "pt": "Início: {time}"},
    "ack_extended": {"ru": "Продлил на {minutes} мин", "en": "Extended by {minutes} min",
                     "pt": "Prolongado {minutes} min"},
    "ack_approx": {"ru": "Записал ~{dur}", "en": "Saved ~{dur}", "pt": "Guardado ~{dur}"},

    # --- длительности ---
    "dur_sec": {"ru": "{n} сек", "en": "{n} sec", "pt": "{n} s"},
    "dur_min": {"ru": "{n} мин", "en": "{n} min", "pt": "{n} min"},
    "dur_h": {"ru": "{h} ч", "en": "{h} h", "pt": "{h} h"},
    "dur_h_m": {"ru": "{h} ч {m} мин", "en": "{h} h {m} min", "pt": "{h} h {m} min"},
    "dur_d": {"ru": "{d} дн", "en": "{d} d", "pt": "{d} d"},
    "dur_d_h": {"ru": "{d} дн {h} ч", "en": "{d} d {h} h", "pt": "{d} d {h} h"},

    # Дата «день + месяц»: в португальском между ними нужен предлог.
    "date_day_month": {"ru": "{day} {month}", "en": "{day} {month}",
                       "pt": "{day} de {month}"},

    # --- карточка эпизода ---
    "day_today": {"ru": "сегодня", "en": "today", "pt": "hoje"},
    "day_yesterday": {"ru": "вчера", "en": "yesterday", "pt": "ontem"},
    "card_open": {"ru": "⚡️ Эпизод #{id} — идёт, уже {dur}",
                  "en": "⚡️ Episode #{id} — ongoing, {dur} so far",
                  "pt": "⚡️ Episódio #{id} — a decorrer, já {dur}"},
    "card_stale": {"ru": "⚠️ Эпизод #{id} — окончание не отмечено",
                   "en": "⚠️ Episode #{id} — no end time",
                   "pt": "⚠️ Episódio #{id} — sem hora de fim"},
    "card_closed": {"ru": "✅ Эпизод #{id} — {dur}", "en": "✅ Episode #{id} — {dur}",
                    "pt": "✅ Episódio #{id} — {dur}"},
    "card_start": {"ru": "Начало: {day} в {time}", "en": "Started: {day} at {time}",
                   "pt": "Início: {day} às {time}"},
    "card_range": {"ru": "{day}, {start}–{end}", "en": "{day}, {start}–{end}",
                   "pt": "{day}, {start}–{end}"},
    "card_approx_suffix": {"ru": " (время окончания примерное)",
                           "en": " (end time is approximate)",
                           "pt": " (hora de fim aproximada)"},
    "card_unknown_dur": {"ru": "Длительность неизвестна (прошло {dur}).",
                         "en": "Duration unknown ({dur} elapsed).",
                         "pt": "Duração desconhecida (passaram {dur})."},
    "card_stale_hint": {"ru": "Если помните, сколько длилось — отметьте кнопкой ниже.",
                        "en": "If you remember how long it lasted, mark it below.",
                        "pt": "Se se lembra de quanto durou, indique abaixo."},
    "card_severity": {"ru": "Тяжесть: {value}", "en": "Severity: {value}",
                      "pt": "Intensidade: {value}"},
    "card_pulse": {"ru": "Пульс: {value}", "en": "Pulse: {value}", "pt": "Pulso: {value}"},
    "card_symptoms": {"ru": "Симптомы: {list}", "en": "Symptoms: {list}",
                      "pt": "Sintomas: {list}"},
    "card_triggers": {"ru": "Перед этим: {list}", "en": "Before it: {list}",
                      "pt": "Antes disto: {list}"},
    "card_note": {"ru": "Заметка: {text}", "en": "Note: {text}", "pt": "Nota: {text}"},
    "dash": {"ru": "—", "en": "—", "pt": "—"},

    # --- сводка за день ---
    "day_header": {"ru": "📋 {title} ({date})", "en": "📋 {title} ({date})",
                   "pt": "📋 {title} ({date})"},
    "day_title_today": {"ru": "Сегодня", "en": "Today", "pt": "Hoje"},
    "day_empty": {"ru": "\nЗаписей нет. Хороший день 🙂",
                  "en": "\nNothing logged. A good day 🙂",
                  "pt": "\nSem registos. Um bom dia 🙂"},
    "day_count": {"ru": "Эпизодов: {n}", "en": "Episodes: {n}", "pt": "Episódios: {n}"},
    "day_total": {"ru": " · суммарно {dur}", "en": " · {dur} in total",
                  "pt": " · {dur} no total"},
    "line_stale": {"ru": "⚠️ {time} — окончание не отмечено",
                   "en": "⚠️ {time} — no end time", "pt": "⚠️ {time} — sem hora de fim"},
    "line_open": {"ru": "⚡️ {time} — идёт ({dur})", "en": "⚡️ {time} — ongoing ({dur})",
                  "pt": "⚡️ {time} — a decorrer ({dur})"},
    "line_closed": {"ru": "• {start}–{end} ({dur})", "en": "• {start}–{end} ({dur})",
                    "pt": "• {start}–{end} ({dur})"},
    "line_symptoms": {"ru": "    🫁 {list}", "en": "    🫁 {list}", "pt": "    🫁 {list}"},
    "line_triggers": {"ru": "    ☕️ перед этим: {list}", "en": "    ☕️ before: {list}",
                      "pt": "    ☕️ antes: {list}"},
    "line_med": {"ru": "💊 {time} — {name}", "en": "💊 {time} — {name}",
                 "pt": "💊 {time} — {name}"},
    "med_default": {"ru": "лекарство", "en": "medication", "pt": "medicamento"},

    # --- отчёт за период ---
    "rep_header": {"ru": "📈 Отчёт за {days} дн. ({start} — {end})",
                   "en": "📈 Report for {days} days ({start} — {end})",
                   "pt": "📈 Relatório de {days} dias ({start} — {end})"},
    "rep_empty": {"ru": "Эпизодов не зарегистрировано.", "en": "No episodes logged.",
                  "pt": "Sem episódios registados."},
    "rep_count": {"ru": "Эпизодов: {n} · {per_day} в день",
                  "en": "Episodes: {n} · {per_day} per day",
                  "pt": "Episódios: {n} · {per_day} por dia"},
    "rep_duration": {"ru": "Длительность: всего {total} · в среднем {avg} · "
                           "максимум {max} ({day})",
                     "en": "Duration: {total} in total · {avg} on average · "
                           "{max} longest ({day})",
                     "pt": "Duração: {total} no total · {avg} em média · "
                           "{max} a mais longa ({day})"},
    "rep_approx": {"ru": "  из них с примерной длительностью: {n}",
                   "en": "  of those, approximate duration: {n}",
                   "pt": "  destes, com duração aproximada: {n}"},
    "rep_stale": {"ru": "Без отметки окончания: {n} — длительность неизвестна, "
                        "в средние значения не входят",
                  "en": "Without an end time: {n} — duration unknown, excluded from "
                        "the averages",
                  "pt": "Sem hora de fim: {n} — duração desconhecida, fora das médias"},
    "rep_ongoing": {"ru": "Идёт прямо сейчас: 1", "en": "Ongoing right now: 1",
                    "pt": "A decorrer agora: 1"},
    # {range} вместо min–max: при единственном замере «148–148» выглядит глупо.
    # «замеров: 1» вместо «по 1 записям» — чтобы не согласовывать число в трёх языках.
    "rep_pulse": {"ru": "Пульс: {range} · в среднем {avg} (замеров: {n})",
                  "en": "Pulse: {range} · {avg} on average (readings: {n})",
                  "pt": "Pulso: {range} · {avg} em média (medições: {n})"},
    "rep_severity": {"ru": "Тяжесть: {parts}", "en": "Severity: {parts}",
                     "pt": "Intensidade: {parts}"},
    "rep_symptoms": {"ru": "Симптомы:", "en": "Symptoms:", "pt": "Sintomas:"},
    "rep_triggers": {"ru": "Перед эпизодом:", "en": "Before the episode:",
                     "pt": "Antes do episódio:"},
    "rep_hours": {"ru": "По времени суток:", "en": "By time of day:",
                  "pt": "Por hora do dia:"},
    "rep_weekdays": {"ru": "По дням недели: {parts}", "en": "By weekday: {parts}",
                     "pt": "Por dia da semana: {parts}"},
    "rep_meds": {"ru": "Приёмов лекарств: {n}", "en": "Medication taken: {n}",
                 "pt": "Tomas de medicamento: {n}"},

    # --- описания команд в меню Telegram ---
    "cmd_log": {"ru": "⚡️ записать эпизод аритмии", "en": "⚡️ log an arrhythmia episode",
                "pt": "⚡️ registar episódio de arritmia"},
    "cmd_stop": {"ru": "✅ отпустило", "en": "✅ it stopped", "pt": "✅ já passou"},
    "cmd_last": {"ru": "карточка последнего эпизода", "en": "card of the latest episode",
                 "pt": "cartão do último episódio"},
    "cmd_med": {"ru": "💊 отметить лекарство", "en": "💊 log medication",
                "pt": "💊 registar medicamento"},
    "cmd_today": {"ru": "сводка за сегодня", "en": "today's summary",
                  "pt": "resumo de hoje"},
    "cmd_yesterday": {"ru": "сводка за вчера", "en": "yesterday's summary",
                      "pt": "resumo de ontem"},
    "cmd_week": {"ru": "отчёт за 7 дней", "en": "7-day report",
                 "pt": "relatório de 7 dias"},
    "cmd_month": {"ru": "отчёт за 30 дней", "en": "30-day report",
                  "pt": "relatório de 30 dias"},
    "cmd_export": {"ru": "выгрузить CSV", "en": "export CSV", "pt": "exportar CSV"},
    "cmd_lang": {"ru": "язык бота", "en": "bot language", "pt": "idioma do bot"},
    "cmd_cancel": {"ru": "отменить ввод", "en": "cancel input", "pt": "anular entrada"},
    "cmd_help": {"ru": "справка", "en": "help", "pt": "ajuda"},
}

LANG_NAMES = {"ru": "Русский", "en": "English", "pt": "Português"}


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
