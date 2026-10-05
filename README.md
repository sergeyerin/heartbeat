# Heartbeat — an arrhythmia diary in Telegram

*[Русская версия](README.ru.md)*

A Telegram bot for people who get arrhythmia episodes: log an episode with a
single tap, then look back at what happened, when, and how you felt.

The design goal is that logging costs almost nothing in the moment an episode
starts — because that is exactly when you have the least patience for an app.

- **The «⚡️ Arrhythmia» button always sits at the bottom of the chat**
  (a persistent reply keyboard). One tap and the start time is recorded. Nothing
  else is required.
- When it stops, the same button becomes **«✅ It stopped»** and the bot computes
  the duration itself.
- Everything else — severity, pulse, symptoms, trigger, free-form note — lives on
  the episode card as **optional** buttons. Fill it in later, when you feel
  better. Or never: the episode is already logged.
- Logged it late? The card offers **«⏪ start −5 / −15 / −30 min»**.
- The same actions are available as commands in the Telegram menu: `/log`,
  `/stop`, `/today`, …

No external services are involved: no LLM, no analytics, no third-party API.
Just Telegram and a local SQLite file.

## Languages

Russian and English. The language comes from the Telegram profile
(`language_code`): `en-GB` → English, `ru` → Russian, anything else →
`DEFAULT_LANG`. `/lang` switches it explicitly, and the choice is stored in the
database — so it also applies to the reminders the bot sends on its own (those
have no incoming update to read a profile from) and survives a restart.

Everything is translated: buttons, cards, daily feeds, reports, symptom and
trigger names, CSV headers, and the command descriptions in the Telegram menu
(`set_my_commands` per language). A test guards translation integrity — no key
and no placeholder can go missing in one of the languages.

Portuguese was built and then removed: with no actual Portuguese-speaking user,
a third language is a permanent tax on every future change. Adding a language
back means one entry per key in `i18n.py` plus `vocab.py`; the test will tell you
exactly what is missing.

## What gets recorded

**Episode:** start, end (→ duration), severity (🙂 mild / 😕 moderate /
😣 severe), pulse, symptoms (skipped beats, pounding heartbeat, shortness of
breath, weakness, dizziness, chest pressure, near-fainting, anxiety, sweating,
cold hands/feet), what preceded it (coffee, alcohol, stress, physical exertion,
after a meal, lack of sleep, lying down at night, illness, "out of nowhere") and
a free-form note.

**Medication:** the «💊 Medication» button logs the time; the name is picked with
one tap from the names you entered before.

Symptoms and triggers are toggles — tap to set, tap again to unset. The severity
rating clears when you tap the same level twice.

## If you tap «Arrhythmia» but never tap «It stopped»

Nothing is lost: the episode and its start time are already in the diary. From
there the bot does not rely on you remembering:

1. After `EPISODE_WINDOW_MIN` (30 min) it asks **once**: "Episode #12 has been
   running for 30 min. Has it stopped? If you don't answer, in 30 min I'll log it
   with no end time" — with **✅ It stopped**, **⏳ Still ongoing** and
   **🤷 Don't remember when it stopped**. There is no recurring nagging.
2. **⏳ Still ongoing** extends the episode by exactly one window and does not
   touch the start time: an atrial fibrillation episode really can run for hours,
   and the bot must not cut it short artificially. Tap it as many times as you
   need — each tap buys another window and moves the question.
3. If the question goes unanswered — silence lasting two windows
   (`STALE_AFTER_MIN`, 60 min by default) — the episode is marked
   **"no end time"**. That is deliberately a different state from "ongoing":
   - the «⚡️ Arrhythmia» button works again, so a new episode is logged normally
     (otherwise one forgotten episode would block the diary forever);
   - plain text and pulse numbers attach to the new episode, not the stale one;
   - the report counts such episodes but lists them on a separate line,
     "Without an end time: N", and **excludes** them from average and total
     duration — statistics are not skewed by invented zeros.
4. The duration can still be recovered from memory: **15 min / 30 min / 1 h /
   2 h / 4 h / 8 h**. Such a record is marked approximate — shown with "~" in the
   daily feed, flagged in the `duration_approx` CSV column, and summarised in the
   report as "of those, approximate duration: N".
5. **🤷 Don't know** leaves the episode without an end time for good and silences
   reminders for it. The episode is kept; its duration is honestly unknown.

Reminders live in the process memory, so on startup the bot re-schedules them for
every open episode — a container restart does not lose them.

## Quick input without buttons

| Send this | What happens |
|---|---|
| a number (`120`) | pulse of the current (or just-finished) episode |
| any text | a note on the current episode |
| text when no episode is running | the bot asks "log this as a new episode?" — one tap |

"Current" means the running episode, or — if none is running — one that ended no
more than `RECENT_EPISODE_MIN` minutes ago (3 hours by default).

## What you can look at

- **📋 Today** (`/today`, `/yesterday`) — the day as a feed: episodes with times,
  durations, how you felt, and medication taken; arrows page through the days.
- **📈 Report** (`/week`, `/month`) — 7 / 30 / 90 days: episode count, total and
  average duration, the longest one, pulse spread, severity breakdown, top
  symptoms and triggers, distribution by time of day and by weekday.
- **📤 Export** (`/export`) — a CSV of every record (`;`-separated with a BOM, so
  Excel and Google Sheets open it without encoding games). Handy to hand to a
  cardiologist.

## Logging an episode after the fact

An attack at night with the phone in another room still belongs in the diary.
`/earlier` — or the «➕ Episode on this day» button under a day's summary —
records it: pick the day (today, yesterday, or the day before), the hour, the
minute (buttons show the full time, like `23:40`), and roughly how long it
lasted. No typing anywhere. The entry is honestly marked as approximate, and
«don't know» keeps the episode with its duration unknown rather than inventing
one. A currently running episode is never disturbed.

## Where it happened

The «📍 Place» button on the bottom keyboard attaches your location to the
current episode — it has to live there, because requesting a location is
something only a reply keyboard can do. The card then shows an OpenStreetMap
link (no account, no tracking), and the CSV gets latitude and longitude columns.

Be aware of what this adds: on top of symptoms and pulse, the diary now knows
where you go. The card carries a «📍 Remove the place» button, `/forget` clears
coordinates with everything else, and nothing is stored unless you press the
button.

## Deleting your data

`/forget` wipes everything: episodes, medication entries, notes, places and the
language choice, after a two-step confirmation that shows the counts first. There
is no undo, so the bot suggests `/export` before. Nothing is kept about you
afterwards.

## Privacy

The bot is open to everyone, but diaries are isolated: every record is keyed by
`user_id`, and another user's episodes can neither be read nor deleted — not even
with a hand-crafted callback payload (covered by tests). The bot replies in
private chats only, so a diary cannot be coaxed into a group.

Be clear-eyed about where the data actually lives, though. **It does not stay on
the bot's machine alone.** Everything you type travels through Telegram and is
kept in your Telegram chat history, and an exported CSV is uploaded to Telegram's
file servers — bots cannot use secret chats. On top of that, whoever operates the
instance holds a plaintext SQLite file with every diary on it. If that is not
acceptable for your data, run your own instance and be your own operator — that
is what this repository is for.

Not yet implemented, and worth knowing before you start: there is no "delete
everything" command (individual episodes can be deleted), no retention limit, and
no encryption at rest.

## How data is stored

A SQLite file, `data/heartbeat.db` (WAL mode), mounted into the container as a
volume. PostgreSQL would be overkill here: the load is dozens of records per
month per person, and a backup is a file copy.

Three tables:

- `episodes` — `started_at`, `ended_at` (NULL = no end time recorded),
  `end_approx` (duration entered from memory), `confirmed_at` (last
  "still ongoing"), `severity`, `pulse`, `symptoms`, `triggers`, `note`;
- `meds` — `taken_at`, `name`;
- `user_prefs` — the language chosen with `/lang`.

Times are always written in UTC and converted to `TZ` only for display, so
changing the zone on the server does not corrupt history. The schema migrates
itself on startup (`_migrate` in `db.py`), so upgrades need no manual steps.

## Running locally (no Docker)

```bash
git clone https://github.com/sergeyerin/heartbeat.git
cd heartbeat
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env          # put your TELEGRAM_BOT_TOKEN in
.venv/bin/python bot.py
```

Checks (offline, no token needed):

```bash
.venv/bin/python smoke_test.py   # storage, formatting, keyboards, translations
.venv/bin/python flow_test.py    # whole scenarios: tap → stored → reply
```

## Deploying to a VPS (Docker)

The bot uses long polling — no public HTTPS, no inbound ports, only outbound
traffic to Telegram.

```bash
cd ~/projects
git clone https://github.com/sergeyerin/heartbeat.git
cd heartbeat
cp .env.example .env          # TELEGRAM_BOT_TOKEN
docker compose up -d --build
docker compose logs -f        # wait for "Bot started"
```

Updating after a `git push`:

```bash
git pull && docker compose up -d --build
```

If only `.env` changed, no rebuild is needed: `docker compose up -d`.

| Task | Command |
|---|---|
| Logs | `docker compose logs -f --tail=100` |
| Status | `docker compose ps` |
| Restart | `docker compose restart` |
| Stop | `docker compose down` |
| Back up the database | `docker compose stop && cp data/heartbeat.db* ~/backups/ && docker compose start` |

Things worth knowing:

- **History lives in `./data`.** Without that volume the database disappears on
  the next image rebuild. For a backup copy `heartbeat.db` together with its
  `-wal` and `-shm` companions, or you may lose the most recent records.
- **One instance per token.** A second container with the same
  `TELEGRAM_BOT_TOKEN` gets `409 Conflict` — two pollers on one token.
- **The container runs as root** on purpose: `./data` on the host belongs to
  root, and a non-root process could not open the database file. The container
  exposes no ports.
- Both test suites run during `docker build`, so a broken image fails at build
  time instead of in production.
- Reminders need APScheduler (`python-telegram-bot[job-queue]`, already in
  requirements). If the log says "Job queue недоступна" on startup, the extra did
  not install: the bot still works, but stops asking "has it stopped?".

## Configuration (.env)

| Variable | Default | Meaning |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | bot token from @BotFather |
| `DEFAULT_LANG` | `en` | language for profiles that are not ru/en |
| `TZ` | `Europe/Moscow` | zone used to display times |
| `DB_PATH` | `data/heartbeat.db` | SQLite file |
| `EPISODE_WINDOW_MIN` | `30` | episode window: when to ask "has it stopped?", and how much «⏳ Still ongoing» adds (0 — never ask) |
| `STALE_AFTER_MIN` | two windows (`60`) | minutes of silence that mean "no end time" |
| `CARD_TICK_SEC` | `60` | how often the card of an ongoing episode is redrawn (0 — never) |
| `MAX_EPISODES_PER_USER` | `5000` | cap on episodes per person |
| `MAX_OPEN_EPISODES` | `10` | cap on unclosed episodes per person |
| `MIN_ACTION_INTERVAL_SEC` | `1.5` | minimum gap between episode/medication entries |
| `EXPORT_COOLDOWN_SEC` | `60` | cooldown on /export, which builds the whole diary in memory |
| `RECENT_EPISODE_MIN` | `180` | how long after an episode ends plain input still attaches to it |

## Layout

- `bot.py` — handlers, the bottom keyboard, the episode card, inline menus
- `db.py` — SQLite: the `episodes` / `meds` / `user_prefs` tables and queries
- `report.py` — card, daily feed, period report, CSV
- `i18n.py` — every interface string in Russian and English
- `vocab.py` — codes and labels for symptoms, triggers, severity
- `config.py` — configuration from `.env`
- `smoke_test.py` — offline checks of storage, formatting, translations, and the
  hardening invariants (state guards, note limits, CSV escaping)
- `flow_test.py` — scenario run through the real handlers with a stubbed Telegram,
  including group chats and hand-crafted callback payloads

Both test suites run during `docker build`.

## Not medical advice

This is a diary, not a diagnostic tool. It records what you tell it and does not
interpret anything. Discuss what you see with your doctor.
