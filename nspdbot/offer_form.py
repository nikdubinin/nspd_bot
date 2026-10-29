"""Анкета для предложения инвесторам: вопросы вместо пустых граф «______».

Бот задаёт вопросы по очереди; ответ можно написать текстом или выбрать кнопкой.
Пропущенный вопрос остаётся в тексте предложения пустой графой.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

OfferKind = Literal["loan", "buyout"]

MAX_ANSWER_LEN = 300


@dataclass(frozen=True, slots=True)
class Question:
    key: str
    label: str  # подпись графы в тексте предложения
    prompt: str  # вопрос пользователю
    options: tuple[str, ...] = ()  # варианты для кнопок
    money: bool = False  # «5000000» -> «5 000 000 руб.»
    premises_only: bool = False  # только если среди объектов есть помещение


LOAN_QUESTIONS: tuple[Question, ...] = (
    Question("amount", "Требуемая сумма", "Какая сумма займа нужна?", money=True),
    Question(
        "term", "Срок", "На какой срок нужен займ?",
        options=("6 месяцев", "12 месяцев", "24 месяца", "36 месяцев"),
    ),
    Question("purpose", "Цель", "Цель займа?", options=("Бизнес", "Рефинансирование", "Личные нужды")),
    Question("job", "Место работы / должность", "Место работы и должность заёмщика?"),
    Question("income", "Ежемесячный доход", "Ежемесячный доход заёмщика?", money=True),
    Question(
        "business", "Наличие ИП / СЗ / ООО", "Что есть у заёмщика: ИП, самозанятость, ООО?",
        options=(
            "ИП",
            "Самозанятость",
            "ООО",
            "Откроет при необходимости СЗ",
            "Откроет при необходимости ИП",
            "Нет ничего",
        ),
    ),
    Question("encumbrances", "Обременения", "Есть ли обременения (ипотека, арест, залог)?", options=("Нет",)),
    Question(
        "registered", "Зарегистрированные лица", "Есть ли зарегистрированные лица?",
        options=("Нет",), premises_only=True,
    ),
)

BUYOUT_QUESTIONS: tuple[Question, ...] = (
    Question("price", "Ожидаемая цена выкупа", "Какая ожидаемая цена выкупа?", money=True),
    Question(
        "deadline", "Желаемый срок сделки", "Желаемый срок сделки?",
        options=("1 неделя", "2 недели", "1 месяц"),
    ),
    Question("encumbrances", "Обременения", "Есть ли обременения (ипотека, арест, залог)?", options=("Нет",)),
    Question(
        "registered", "Зарегистрированные лица", "Есть ли зарегистрированные лица?",
        options=("Нет",), premises_only=True,
    ),
)


def questions_for(kind: OfferKind, has_premises: bool) -> list[Question]:
    source = LOAN_QUESTIONS if kind == "loan" else BUYOUT_QUESTIONS
    return [q for q in source if has_premises or not q.premises_only]


_MONEY_RE = re.compile(r"^\s*(\d[\d\s]*(?:[.,]\d+)?)\s*(?:р|р\.|руб|руб\.|рублей|₽)?\s*$", re.IGNORECASE)


def normalize_answer(question: Question, text: str) -> str:
    """Чистит ответ; голое число для денежных вопросов оформляет как сумму в рублях."""
    text = " ".join(text.split())[:MAX_ANSWER_LEN]
    if question.money and (match := _MONEY_RE.match(text)):
        digits = match.group(1).replace(" ", "").replace(",", ".")
        try:
            value = float(digits)
        except ValueError:
            return text
        whole = f"{value:,.2f}".replace(",", " ").replace(".", ",").removesuffix(",00")
        return f"{whole} руб."
    return text


@dataclass(slots=True)
class OfferForm:
    """Состояние анкеты в chat_data (сериализуется вместе с ним)."""

    kind: OfferKind
    numbers: list[str]
    has_premises: bool
    step: int = 0
    answers: dict[str, str] = field(default_factory=dict)
    message_id: int | None = None  # сообщение с текущим вопросом — чтобы убрать с него кнопки

    @property
    def questions(self) -> list[Question]:
        return questions_for(self.kind, self.has_premises)

    @property
    def current(self) -> Question | None:
        questions = self.questions
        return questions[self.step] if self.step < len(questions) else None

    @property
    def done(self) -> bool:
        return self.current is None

    def answer(self, text: str) -> str:
        question = self.current
        assert question is not None
        value = normalize_answer(question, text)
        self.answers[question.key] = value
        self.step += 1
        return value

    def skip(self) -> None:
        question = self.current
        if question is not None:
            self.answers.pop(question.key, None)
        self.step += 1

    def back(self) -> None:
        self.step = max(0, self.step - 1)

    def finish_now(self) -> None:
        self.step = len(self.questions)

    @property
    def progress(self) -> str:
        return f"{min(self.step + 1, len(self.questions))}/{len(self.questions)}"

    @property
    def filled(self) -> int:
        return sum(1 for q in self.questions if self.answers.get(q.key))
