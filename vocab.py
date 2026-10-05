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

TRIGGER_CODES = ("coffee", "alcohol", "stress", "effort", "food", "nosleep",
                 "lying", "misseddose", "ill", "unknown")

SYMPTOMS: dict[str, dict[str, str]] = {
    "ru": {
        "irregular": "неровный пульс, вразнобой",
        "regular": "ровный, но частый",
        "abrupt_on": "началось резко, как выключатель",
        "gradual_on": "нарастало постепенно",
        "abrupt_off": "кончилось резко",
        "gradual_off": "угасало постепенно",
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
        "irregular": "irregular, uneven beat",
        "regular": "steady but fast",
        "abrupt_on": "started abruptly, like a switch",
        "gradual_on": "built up gradually",
        "abrupt_off": "stopped abruptly",
        "gradual_off": "faded gradually",
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
