#!/usr/bin/env python3
"""Фаззинг разбора callback_data: ни один payload не должен ронять хендлер.

Payload кнопки управляется клиентом, и именно на нём жили два блокера:
`m:<id>` без третьего сегмента падал с IndexError, а огромные числа роняли
sqlite3 с OverflowError. Тим-лид гонял это руками; здесь то же самое, но в
сборке — чтобы не выводить заново каждым кругом ревью.
"""
from __future__ import annotations

import asyncio
import os
import tempfile

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "fuzz")
os.environ.setdefault("TZ", "Europe/Moscow")
os.environ.setdefault("MIN_ACTION_INTERVAL_SEC", "0")

import bot  # noqa: E402
import db  # noqa: E402
import flow_test as harness  # noqa: E402
import vocab  # noqa: E402

ACTIONS = ("bf",
           "s", "p", "n", "nc", "nt", "m", "ts", "tt", "c", "e", "ro", "go", "apm",
           "ap", "unk", "sh", "d", "dy", "pc", "r", "csv", "dn", "nn", "nx", "np",
           "mn", "mt", "md", "lang", "fy", "noop", "", "zzz")

ARGS = ("", "0", "1", "-1", "7", "abc", "٣", "１２３", "+5", " 5 ", "1_0", "sym", "trg",
        "junk", "a,b", "=HYPERLINK(1)", "х" * 200, "9" * 400, str(2 ** 63),
        str(-(2 ** 63)), str(10 ** 30), "2026-01-01", "0001-01-01", "not-a-date",
        "30", "45", "480", "\x00", "::", "null", "None")


def payloads(episode_id: int, med_id: int) -> list[str]:
    out: list[str] = []
    for action in ACTIONS:
        out.append(action)
        out.append(f"{action}:")
        for arg in ARGS:
            out.append(f"{action}:{arg}")
            out.append(f"{action}:{episode_id}:{arg}")
            out.append(f"{action}:{arg}:{episode_id}")
            out.append(f"{action}:{med_id}:{arg}:extra")
    return out


async def run() -> int:
    fake = harness.FakeBot()
    user_id = harness.USER.id
    ep = db.start_episode(user_id)
    db.set_severity(user_id, ep.id, 2)
    db.toggle_code(user_id, ep.id, "symptoms", vocab.SYMPTOM_CODES[0])
    db.append_note(user_id, ep.id, "эталон")
    med = db.add_med(user_id, "конкор")
    # Канарейка другого пользователя: её не должен тронуть ни один payload
    other = user_id + 1
    canary = db.start_episode(other)
    db.append_note(other, canary.id, "чужое")
    db.add_med(other, "чужое лекарство")

    failures = []
    cases = payloads(ep.id, med.id)
    for data in cases:
        try:
            await bot.on_callback(
                harness.callback_update(fake, data), harness.make_context(fake, {})
            )
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{data!r} → {type(exc).__name__}: {exc}")

    after = db.get_episode(user_id, ep.id)
    canary_now = db.get_episode(other, canary.id)
    if canary_now is None or "чужое" not in (canary_now.note or ""):
        print(" FAIL данные другого пользователя пострадали")
        return 1
    if len(db.all_meds(other)) != 1:
        print(" FAIL лекарства другого пользователя пострадали")
        return 1
    print("  ok   данные другого пользователя не тронуты")
    print(f"  payload'ов проверено: {len(cases)}")
    if failures:
        print(f" FAIL необработанных исключений: {len(failures)}")
        for line in failures[:10]:
            print("   ", line)
        return 1
    print("  ok   ни один payload не уронил хендлер")
    # Мусор не должен писаться в данные. Эталон мог измениться только там, где
    # payload был валидным (например s:<id>:1), поэтому сверяем лишь то, что
    # подделать нельзя: симптомы и заметку.
    if after is None:
        print(" FAIL свой эпизод удалён — подтверждение удаления обходится")
        return 1
    if "эталон" not in (after.note or ""):
        print(" FAIL заметка повреждена")
        return 1
    unknown = [c for c in after.symptoms if c not in vocab.SYMPTOM_CODES]
    if unknown:
        print(f" FAIL в симптомы попали неизвестные коды: {unknown}")
        return 1
    print("  ok   данные не повреждены")
    print("\nФаззинг пройден.")
    return 0


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        db.init(os.path.join(tmp, "fuzz.db"))
        return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
