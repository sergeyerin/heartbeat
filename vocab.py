#!/usr/bin/env python3
"""Справочники симптомов, причин и оценок тяжести на трёх языках.

В БД хранятся только коды, подписи — для отображения. Переименование подписи или
добавление языка не ломает старые записи; коды менять нельзя, только добавлять.

Порядок кодов = порядок кнопок в меню (самое частое сверху).
"""
from __future__ import annotations

from i18n import FALLBACK

SYMPTOM_CODES = ("skip", "fast", "short", "weak", "dizzy", "chest", "faint",
                 "anxiety", "sweat", "cold")

TRIGGER_CODES = ("coffee", "alcohol", "stress", "effort", "food", "nosleep",
                 "lying", "ill", "unknown")

SYMPTOMS: dict[str, dict[str, str]] = {
    "ru": {
        "skip": "перебои, замирания",
        "fast": "сильное сердцебиение",
        "short": "одышка",
        "weak": "слабость",
        "dizzy": "головокружение",
        "chest": "давит в груди",
        "faint": "предобморок",
        "anxiety": "тревога",
        "sweat": "потливость",
        "cold": "холодные руки/ноги",
    },
    "en": {
        "skip": "skipped beats, pauses",
        "fast": "pounding heartbeat",
        "short": "shortness of breath",
        "weak": "weakness",
        "dizzy": "dizziness",
        "chest": "chest pressure",
        "faint": "near-fainting",
        "anxiety": "anxiety",
        "sweat": "sweating",
        "cold": "cold hands/feet",
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
        "ill": "простуда",
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
