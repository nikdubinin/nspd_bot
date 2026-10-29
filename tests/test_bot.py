"""Тесты разбора номеров, моделей и форматирования. Запуск: python -m unittest"""

from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from unittest import mock

from nspdbot import handlers
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
        context.application.bot_data = {"nspd": service, "settings": Settings(token="x")}
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
        offer = message.reply_text.call_args.args[0]
        self.assertIn("займ под залог 2 объектов", offer)

    def test_no_numbers_keeps_waiting(self):
        context = self._context({})
        context.chat_data[handlers.PLEDGE_KEY] = []
        message, _ = self._message()
        asyncio.run(handlers._finish_pledge(message, [], context))
        self.assertIn(handlers.PLEDGE_KEY, context.chat_data)


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
