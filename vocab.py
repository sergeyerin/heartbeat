#!/usr/bin/env python3
"""Справочники симптомов, причин и оценок тяжести на двух языках.

В БД хранятся только коды, подписи — для отображения. Переименование подписи или
добавление языка не ломает старые записи; коды менять нельзя, только добавлять.

Порядок кодов = порядок кнопок в меню (самое частое сверху).
"""
from __future__ import annotations

from i18n import FALLBACK

# Характер ритма идёт первым: для кардиолога это самый информативный датум из
# всего, что пациент может сообщить сам (регулярный или неровный, началось как
# выключатель или нарастало, кончилось резко или угасло) — по нему различают
# фибрилляцию, тахикардию и экстрасистолию. Технически это обычные коды
# симптомов, поэтому меню, агрегатор топа и колонка CSV подхватывают их сами.
SYMPTOM_CODES = ("irregular", "regular", "abrupt_on", "gradual_on",
                 "abrupt_off", "gradual_off",
                 "skip", "fast", "short", "weak", "dizzy", "chest", "faint",
                 "anxiety", "sweat", "cold", "nausea")

# «Близость» и «баня» стоят сразу за «нагрузкой» намеренно, не по частоте:
# весь смысл их добавления в том, что люди отправляют это в «нагрузку», значит
# верный ответ должен попадаться глазу следующим.
TRIGGER_CODES = ("coffee", "alcohol", "stress", "effort", "sex", "heat", "food",
                 "nosleep", "lying", "misseddose", "ill", "unknown")

# Характер ритма — это три вопроса с двумя ответами каждый. Отсюда и две
# колонки (пара в ряду = вопрос с двумя ответами), и взаимное исключение:
# «началось резко» и «нарастало» одновременно — не более полный ответ, а
# противоречие. Одно объявление задаёт и то, и другое, поэтому они не разъедутся.
SYMPTOM_PAIRS = (
    ("irregular", "regular"),
    ("abrupt_on", "gradual_on"),
    ("abrupt_off", "gradual_off"),
)

PAIRED_CODES = {code: pair for pair in SYMPTOM_PAIRS for code in pair}


def sibling(code: str) -> str | None:
    """Противоположный ответ в паре, если код парный."""
    pair = PAIRED_CODES.get(code)
    if pair is None:
        return None
    return pair[1] if pair[0] == code else pair[0]


SYMPTOMS: dict[str, dict[str, str]] = {
    "ru": {
        "irregular": "пульс вразнобой",
        "regular": "пульс ровный",
        "abrupt_on": "началось резко",
        "gradual_on": "нарастало",
        "abrupt_off": "кончилось резко",
        "gradual_off": "угасало",
        "skip": "перебои, замирания",
        "fast": "частое, сильное сердцебиение",
        "short": "одышка",
        "weak": "слабость",
        "dizzy": "головокружение",
        "chest": "давит в груди",
        "faint": "темнеет в глазах, предобморок",
        "anxiety": "тревога, страх",
        "sweat": "потливость",
        "cold": "холодные руки/ноги",
        "nausea": "тошнота",
    },
    "en": {
        "irregular": "irregular pulse",
        "regular": "steady pulse",
        "abrupt_on": "sudden start",
        "gradual_on": "built up slowly",
        "abrupt_off": "sudden stop",
        "gradual_off": "faded away",
        "skip": "skipped beats, pauses",
        "fast": "racing or pounding heartbeat",
        "short": "shortness of breath",
        "weak": "weakness",
        "dizzy": "dizziness",
        "chest": "chest pressure",
        "faint": "feeling I might faint",
        "anxiety": "anxiety, fear",
        "sweat": "sweating",
        "cold": "cold hands/feet",
        "nausea": "nausea",
    },
}

TRIGGERS: dict[str, dict[str, str]] = {
    "ru": {
        "coffee": "кофе",
        "alcohol": "алкоголь",
        "stress": "стресс",
        "effort": "нагрузка",
        "sex": "интимная близость",
        "heat": "баня, горячий душ",
        "food": "после еды",
        "nosleep": "недосып",
        "lying": "лёжа, ночью",
        "misseddose": "пропустил лекарство",
        "ill": "болезнь, простуда",
        "unknown": "на ровном месте",
    },
    "en": {
        "coffee": "coffee",
        "alcohol": "alcohol",
        "stress": "stress",
        "effort": "physical exertion",
        "sex": "sexual activity",
        "heat": "sauna, hot shower",
        "food": "after a meal",
        "nosleep": "lack of sleep",
        "lying": "lying down, at night",
        "misseddose": "missed a dose",
        "ill": "illness, cold",
        "unknown": "out of nowhere",
    },
}

SEVERITY: dict[str, dict[int, str]] = {
    "ru": {1: "🙂 терпимо", 2: "😕 средне", 3: "😣 тяжело"},
    "en": {1: "🙂 mild", 2: "😕 moderate", 3: "😣 severe"},
}

# Без эмодзи — для CSV, который читает врач или Excel
SEVERITY_PLAIN: dict[str, dict[int, str]] = {
    "ru": {1: "терпимо", 2: "средне", 3: "тяжело"},
    "en": {1: "mild", 2: "moderate", 3: "severe"},
}


def symptoms(lang: str) -> dict[str, str]:
    return SYMPTOMS.get(lang, SYMPTOMS[FALLBACK])


def triggers(lang: str) -> dict[str, str]:
    return TRIGGERS.get(lang, TRIGGERS[FALLBACK])


def severity(lang: str) -> dict[int, str]:
    return SEVERITY.get(lang, SEVERITY[FALLBACK])


def severity_plain(lang: str) -> dict[int, str]:
    return SEVERITY_PLAIN.get(lang, SEVERITY_PLAIN[FALLBACK])


def labels(codes: list[str], vocabulary: dict[str, str]) -> list[str]:
    """Подписи для кодов; неизвестный код показываем как есть, а не теряем."""
    return [vocabulary.get(code, code) for code in codes]
