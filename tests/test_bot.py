"""Тесты разбора номеров, моделей и форматирования. Запуск: python -m unittest"""

from __future__ import annotations

import asyncio
import json
import logging
import unittest
from pathlib import Path
from unittest import mock

from nspdbot import handlers
from nspdbot.activity import (
    ACTIVITY_LOGGER,
    Activity,
    ConsoleFilter,
    ConsoleFormatter,
    format_duration,
    user_label,
)
from nspdbot.cadastral import extract_kad_numbers, normalize_kad_number
from nspdbot.config import Settings
from nspdbot.formatting import (
    build_csv,
    fit_message,
    fmt_number,
    format_card,
    format_offer,
    format_pledge_offer,
)
from nspdbot.models import NspdObject, ObjectKind
from nspdbot.offer_form import LOAN_QUESTIONS, OfferForm, normalize_answer, questions_for
from nspdbot.nspd_service import NspdNotFound, NspdService, NspdUnavailable

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str, cad: str) -> NspdObject:
    raw = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return NspdObject.from_response(raw, cad)


class CadastralTests(unittest.TestCase):
    def test_normalize_variants(self):
        expected = "77:03:0002007:7190"
        for raw in (
            "77:03:0002007:7190",
            " 77 : 03 : 0002007 : 7190 ",
            "77 03 0002007 7190",
            "77-03-0002007-7190",
            "77030002007 7190",
            "770300020077190",
            "кн 77:03:0002007:7190.",
        ):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_kad_number(raw), expected)

    def test_normalize_rejects_garbage(self):
        for raw in ("", "привет", "123", "00:03:0002007:7190", "дом 5 кв 10", "7:03:0002007:7190"):
            with self.subTest(raw=raw):
                self.assertIsNone(normalize_kad_number(raw))

    def test_extract_many(self):
        text = "Проверь 77:01:0001001:1000, 77:01:0001001:1020\nи ещё 77:01:0001001:1000"
        self.assertEqual(extract_kad_numbers(text), ["77:01:0001001:1000", "77:01:0001001:1020"])

    def test_extract_mixed_formats(self):
        text = "77:03:0002007:12000\n77 01 0001001 1000\n77030002007 7190 77:01:0001001:1020"
        self.assertEqual(
            extract_kad_numbers(text),
            ["77:03:0002007:12000", "77:01:0001001:1000", "77:01:0001001:1020"],
        )

    def test_extract_many_without_colons(self):
        text = "77 01 0001001 1000\n77030002007 7190"
        self.assertEqual(extract_kad_numbers(text), ["77:01:0001001:1000", "77:03:0002007:7190"])


class ModelTests(unittest.TestCase):
    def test_land(self):
        obj = load("land", "77:01:0001001:1000")
        self.assertIs(obj.kind, ObjectKind.LAND)
        self.assertEqual(obj.area, 4844)
        self.assertEqual(obj.land_category, "Земли населенных пунктов")
        self.assertEqual(obj.right_type, "Постоянное (бессрочное) пользование")
        self.assertEqual(obj.noun_phrase()[0], "земельный участок")
        self.assertTrue(obj.has_related_lookup)
        lat, lon = obj.centroid
        self.assertAlmostEqual(lat, 55.75, delta=0.05)
        self.assertAlmostEqual(lon, 37.61, delta=0.05)

    def test_building(self):
        obj = load("building", "77:01:0001001:1020")
        self.assertIs(obj.kind, ObjectKind.BUILDING)
        self.assertEqual(obj.area, 371.7)
        self.assertEqual(obj.noun_phrase()[0], "нежилое здание")
        self.assertIsNone(obj.underground_floors)  # пустая строка -> None

    def test_flat(self):
        obj = load("flat", "77:03:0002007:12000")
        self.assertIs(obj.kind, ObjectKind.PREMISES)
        self.assertEqual(obj.type_label, "Помещение (квартира)")
        self.assertEqual(obj.noun_phrase(), ("квартира", "расположенная"))
        self.assertEqual(obj.floor, "4")
        self.assertEqual(obj.parent_cad_number, "77:03:0002007:11157")
        self.assertEqual(obj.status, "Учтенный")

    def test_parking(self):
        obj = load("parking", "77:03:0002007:11000")
        self.assertIs(obj.kind, ObjectKind.PARKING)
        self.assertEqual(obj.floor, "-1")
        self.assertEqual(obj.noun_phrase()[0], "машино-место")


class FormattingTests(unittest.TestCase):
    def test_fmt_number(self):
        self.assertEqual(fmt_number(12451453.56), "12 451 453,56")
        self.assertEqual(fmt_number(10), "10")
        self.assertEqual(fmt_number(371.7), "371,7")
        self.assertEqual(fmt_number(None), "")

    def test_cards_fit_and_contain_key_fields(self):
        for name, cad in (
            ("land", "77:01:0001001:1000"),
            ("building", "77:01:0001001:1020"),
            ("flat", "77:03:0002007:12000"),
            ("parking", "77:03:0002007:11000"),
        ):
            obj = load(name, cad)
            for short in (False, True):
                with self.subTest(name=name, short=short):
                    text = format_card(obj, short=short)
                    self.assertIn(cad, text)
                    self.assertLessEqual(len(text), 4096)
            for kind in ("loan", "buyout"):
                with self.subTest(name=name, kind=kind):
                    self.assertIn(cad, format_offer(obj, kind))

    def test_offer_texts(self):
        flat = load("flat", "77:03:0002007:12000")
        self.assertIn("квартира с кадастровым номером", format_offer(flat, "loan"))
        self.assertIn("предмета залога", format_offer(flat, "loan"))
        self.assertNotIn("залога", format_offer(flat, "buyout"))

    def test_html_is_escaped(self):
        obj = load("flat", "77:03:0002007:12000")
        obj.options["readable_address"] = "<b>bad</b> & co"
        self.assertIn("&lt;b&gt;bad&lt;/b&gt; &amp; co", format_card(obj))

    def test_fit_message(self):
        text = "\n".join(f"<b>строка {i}</b>" for i in range(1000))
        fitted = fit_message(text)
        self.assertLessEqual(len(fitted), 4096)
        self.assertEqual(fitted.count("<b>"), fitted.count("</b>"))

    def test_csv(self):
        obj = load("land", "77:01:0001001:1000")
        data = build_csv([(obj.cad_number, obj, None), ("77:00:0000000:1", None, "не найден")])
        text = data.decode("utf-8-sig")
        self.assertEqual(len(text.strip().splitlines()), 3)
        self.assertIn("77:01:0001001:1000;Земельный участок", text)


class PledgeOfferTests(unittest.TestCase):
    def setUp(self):
        self.objects = [
            load("land", "77:01:0001001:1000"),
            load("building", "77:01:0001001:1020"),
            load("flat", "77:03:0002007:12000"),
        ]

    def test_single_object_is_regular_offer(self):
        self.assertEqual(format_pledge_offer(self.objects[:1]), [format_offer(self.objects[0], "loan")])

    def test_several_objects(self):
        messages = format_pledge_offer(self.objects)
        text = "\n".join(messages)
        for obj in self.objects:
            self.assertIn(obj.cad_number, text)
        self.assertIn("займ под залог 3 объектов", text)
        self.assertIn("<b>Объект 3.</b> Квартира", text)
        total = sum(o.cost_value for o in self.objects)
        self.assertIn(fmt_number(total), text)
        self.assertIn("предметов залога", text)
        self.assertIn("Зарегистрированные лица", text)  # есть квартира

    def test_long_offer_is_split(self):
        objects = []
        for i in range(20):
            obj = load("land", "77:01:0001001:1000")
            obj.cad_number = f"77:01:0001001:{1000 + i}"
            objects.append(obj)
        messages = format_pledge_offer(objects)
        self.assertGreater(len(messages), 1)
        self.assertTrue(all(len(m) <= 4096 for m in messages))
        text = "\n".join(messages)
        for obj in objects:
            self.assertIn(obj.cad_number, text)
        self.assertTrue(messages[-1].endswith("Ждём ваших предложений и вопросов."))


class PledgeDialogTests(unittest.TestCase):
    def _context(self, objects: dict):
        async def get_object(kad, force=False):
            if kad in objects:
                return objects[kad]
            raise NspdNotFound(kad)

        service = mock.Mock()
        service.get_object = get_object
        context = mock.Mock()
        context.chat_data = {}
        context.application.bot_data = {"nspd": service, "settings": Settings(token="x"), "activity": Activity()}
        return context

    def _message(self):
        message = mock.Mock()
        status = mock.Mock()
        status.edit_text = mock.AsyncMock()
        message.reply_text = mock.AsyncMock(return_value=status)
        return message, status

    def test_flow_from_card(self):
        land = load("land", "77:01:0001001:1000")
        flat = load("flat", "77:03:0002007:12000")
        context = self._context({land.cad_number: land, flat.cad_number: flat})
        message, status = self._message()

        asyncio.run(handlers._ask_pledge_numbers(message, context, first=land.cad_number))
        self.assertEqual(context.chat_data[handlers.PLEDGE_KEY], [land.cad_number])

        numbers = extract_kad_numbers("77:01:0001001:1000\n77:03:0002007:12000\n77:01:0001001:999999")
        asyncio.run(handlers._finish_pledge(message, numbers, context))

        self.assertNotIn(handlers.PLEDGE_KEY, context.chat_data)
        report = status.edit_text.call_args.args[0]
        self.assertIn("объектов 2 из 3", report)
        self.assertIn("77:01:0001001:999999", report)
        # Дальше — анкета: первый вопрос, в списке есть квартира, поэтому 8 вопросов
        form = context.chat_data[handlers.FORM_KEY]
        self.assertEqual(form.numbers, [land.cad_number, flat.cad_number])
        self.assertIn("вопрос 1/8", message.reply_text.call_args.args[0])

    def test_no_numbers_keeps_waiting(self):
        context = self._context({})
        context.chat_data[handlers.PLEDGE_KEY] = []
        message, _ = self._message()
        asyncio.run(handlers._finish_pledge(message, [], context))
        self.assertIn(handlers.PLEDGE_KEY, context.chat_data)


class OfferFormTests(unittest.TestCase):
    def test_questions_depend_on_premises(self):
        loan = [q.key for q in questions_for("loan", has_premises=False)]
        self.assertEqual(loan, ["amount", "term", "purpose", "job", "income", "business", "encumbrances"])
        self.assertEqual(questions_for("loan", has_premises=True)[-1].key, "registered")
        self.assertEqual([q.key for q in questions_for("buyout", False)], ["price", "deadline", "encumbrances"])

    def test_business_options(self):
        business = next(q for q in LOAN_QUESTIONS if q.key == "business")
        self.assertEqual(
            business.options,
            ("ИП", "Самозанятость", "ООО", "Откроет при необходимости СЗ", "Откроет при необходимости ИП", "Нет ничего"),
        )

    def test_money_normalization(self):
        amount = LOAN_QUESTIONS[0]
        self.assertEqual(normalize_answer(amount, "5000000"), "5 000 000 руб.")
        self.assertEqual(normalize_answer(amount, "1 500 000 руб"), "1 500 000 руб.")
        self.assertEqual(normalize_answer(amount, "5 млн"), "5 млн")
        self.assertEqual(normalize_answer(LOAN_QUESTIONS[2], "  на   ремонт "), "на ремонт")

    def test_form_navigation(self):
        form = OfferForm(kind="loan", numbers=["x"], has_premises=False)
        form.answer("3000000")
        form.skip()
        form.back()
        self.assertEqual(form.current.key, "term")
        form.answer("12 месяцев")
        self.assertEqual(form.answers, {"amount": "3 000 000 руб.", "term": "12 месяцев"})
        form.finish_now()
        self.assertTrue(form.done)
        self.assertEqual(form.filled, 2)

    def test_offer_uses_answers(self):
        flat = load("flat", "77:03:0002007:12000")
        answers = {"amount": "3 млн руб.", "business": "Самозанятость", "registered": "Нет", "job": "<ООО «Ромашка»>"}
        text = format_offer(flat, "loan", answers)
        self.assertIn("• Требуемая сумма: 3 млн руб.", text)
        self.assertIn("• Наличие ИП / СЗ / ООО: Самозанятость", text)
        self.assertIn("• Зарегистрированные лица: Нет", text)
        self.assertIn("&lt;ООО «Ромашка»&gt;", text)
        self.assertIn("• Срок: ______", text)  # не отвечено — пустая графа

    def test_dialog_with_buttons_and_text(self):
        flat = load("flat", "77:03:0002007:12000")
        context = PledgeDialogTests._context(self, {flat.cad_number: flat})
        context.bot.edit_message_text = mock.AsyncMock()
        message, _ = PledgeDialogTests._message(self)
        message.chat_id = 1

        def query(data):
            q = mock.Mock(data=data, message=message)
            q.answer = mock.AsyncMock()
            update = mock.Mock(callback_query=q)
            return update

        async def run():
            await handlers._start_form(message, context, "buyout", [flat.cad_number], has_premises=True)
            form = context.chat_data[handlers.FORM_KEY]
            message.text = "12000000"
            await handlers._answer_form_text(message, context, form)               # цена
            await handlers.callback_handler(query("fo:1.1"), context)              # срок: 2 недели
            await handlers.callback_handler(query("fo:1.0"), context)              # устаревшая кнопка
            await handlers.callback_handler(query("fs:2"), context)                # обременения: пропуск
            await handlers.callback_handler(query("fo:3.0"), context)              # зарег. лица: Нет

        asyncio.run(run())
        self.assertNotIn(handlers.FORM_KEY, context.chat_data)
        offer = message.reply_text.call_args.args[0]
        self.assertIn("• Ожидаемая цена выкупа: 12 000 000 руб.", offer)
        self.assertIn("• Желаемый срок сделки: 2 недели", offer)
        self.assertIn("• Обременения: ______", offer)
        self.assertIn("• Зарегистрированные лица: Нет", offer)


def _update(user_id=1, first="Иван", last="Петров", username="ivan", chat_type="private", title=None, text=None):
    user = mock.Mock(id=user_id, first_name=first, last_name=last, username=username)
    chat = mock.Mock(type=chat_type, title=title)
    message = mock.Mock(text=text) if text is not None else None
    return mock.Mock(effective_user=user, effective_chat=chat, message=message)


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.activity = Activity()

    def _lines(self, fn):
        with self.assertLogs(ACTIVITY_LOGGER, level="INFO") as logs:
            fn()
        return [r.getMessage() for r in logs.records]

    def test_user_label(self):
        self.assertEqual(user_label(_update().effective_user), "Иван Петров (@ivan)")
        anon = _update(user_id=5, last=None, username=None)
        self.assertEqual(user_label(anon.effective_user), "Иван (id 5)")
        group = _update(chat_type="supergroup", title="Брокеры")
        self.assertEqual(user_label(group.effective_user, group.effective_chat), "Иван Петров (@ivan) в «Брокеры»")

    def test_new_user_and_command(self):
        def run():
            self.activity.track(_update(text="/start"))
            self.activity.track(_update(text="/help"))

        lines = self._lines(run)
        self.assertEqual(len(lines), 3)  # НОВЫЙ один раз + две команды
        self.assertTrue(lines[0].startswith("НОВЫЙ"))
        self.assertIn("id 1", lines[0])
        self.assertIn("Иван Петров (@ivan): /help", lines[2])

    def test_lookup_lines_use_current_user(self):
        obj = load("land", "77:01:0001001:1000")

        def run():
            self.activity.track(_update(text="77:01:0001001:1000"))
            self.activity.lookup(obj.cad_number, obj=obj, seconds=1.234)
            self.activity.lookup(obj.cad_number, obj=obj, cached=True)
            self.activity.lookup("77:01:0001001:999", not_found=True, seconds=0.5)

        lines = self._lines(run)
        self.assertIn("Иван Петров (@ivan) → 77:01:0001001:1000 · Земельный участок · г. Москва", lines[1])
        self.assertTrue(lines[1].endswith("1,2 с"))
        self.assertTrue(lines[2].endswith("из кэша"))
        self.assertIn("не найден в НСПД", lines[3])
        summary = self.activity.summary()
        self.assertIn("пользователей 1", summary)
        self.assertIn("запросов 3 (найдено 2, не найдено 1, сбоев 0)", summary)

    def test_console_formatter_and_filter(self):
        formatter, flt = ConsoleFormatter(), ConsoleFilter()
        act = logging.LogRecord(ACTIVITY_LOGGER, logging.INFO, "", 0, "ЗАПРОС    текст", None, None)
        self.assertRegex(formatter.format(act), r"^\d\d:\d\d:\d\d  ЗАПРОС    текст$")
        self.assertTrue(flt.filter(act))

        info = logging.LogRecord("telegram.ext", logging.INFO, "", 0, "шум", None, None)
        self.assertFalse(flt.filter(info))

        warn = logging.LogRecord("nspdbot.nspd_service", logging.WARNING, "", 0, "попытка 1/3", None, None)
        self.assertTrue(flt.filter(warn))
        self.assertIn("ВНИМАНИЕ  попытка 1/3", formatter.format(warn))

        hidden = logging.LogRecord("nspdbot.handlers", logging.ERROR, "", 0, "трассировка", None, None)
        hidden.console = False
        self.assertFalse(flt.filter(hidden))

    def test_format_duration(self):
        self.assertEqual(format_duration(45), "45 с")
        self.assertEqual(format_duration(252), "4 мин 12 с")
        self.assertEqual(format_duration(3900), "1 ч 5 мин")


class ServiceTests(unittest.TestCase):
    def _service(self, responses):
        service = NspdService(retries=3, cache_ttl=60)
        service._client = mock.Mock()
        service._client.search_by_cadastral_number.side_effect = responses
        return service

    def test_cache_and_dedup(self):
        raw = json.loads((FIXTURES / "land.json").read_text(encoding="utf-8"))
        service = self._service([raw])

        async def run():
            a, b = await asyncio.gather(
                service.get_object("77:01:0001001:1000"), service.get_object("77:01:0001001:1000")
            )
            c = await service.get_object("77:01:0001001:1000")
            return a, b, c

        a, b, c = asyncio.run(run())
        self.assertIs(a, b)
        self.assertIs(a, c)
        self.assertEqual(service._client.search_by_cadastral_number.call_count, 1)

    def test_not_found_is_not_retried(self):
        service = self._service([{"error": "Ошибка HTTP 404 для кадастрового номера x"}])
        with self.assertRaises(NspdNotFound):
            asyncio.run(service.get_object("77:01:0001001:9999"))
        self.assertEqual(service._client.search_by_cadastral_number.call_count, 1)

    def test_retry_then_fail(self):
        service = self._service([{"error": "Read timed out"}] * 3)
        with mock.patch("nspdbot.nspd_service.asyncio.sleep", new=mock.AsyncMock()):
            with self.assertRaises(NspdUnavailable):
                asyncio.run(service.get_object("77:01:0001001:1000"))
        self.assertEqual(service._client.search_by_cadastral_number.call_count, 3)


if __name__ == "__main__":
    unittest.main()
