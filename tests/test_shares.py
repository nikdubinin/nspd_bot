"""Доли в предложении: обязательное уточнение и площадь всего объекта."""
import asyncio
import pickle
import unittest
from unittest import mock

from nspdbot import handlers
from nspdbot.offer_form import OfferForm
from nspdbot.formatting import format_pledge_offer
from tests.test_bot import load
from tests import test_bot


class ShareTests(unittest.TestCase):
    cad = "77:03:0002007:12000"

    def form(self, numbers=None):
        return OfferForm("loan", numbers or [self.cad], True, collect_assets=True)

    def test_share_branch_and_validation(self):
        form = self.form()
        self.assertIn(self.cad, form.current.prompt)
        form.answer("доля")
        for bad in ("0/130", "130/130", "131/130", "23/0", "-1/2", "комната", "0.5", "1/2x"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                form.answer(bad)
        self.assertEqual(form.answer("23 / 130"), "23/130")
        self.assertIn("комнат", form.current.prompt)
        form.answer("Да, в комнату")
        self.assertEqual(form.current.key, "amount")
        self.assertEqual(pickle.loads(pickle.dumps(form)).answers, form.answers)

    def test_old_message_cannot_change_or_cancel_new_form(self):
        async def run():
            context = test_bot.PledgeDialogTests._context(self, {})
            form = self.form()
            form.message_id = 20
            context.chat_data[handlers.FORM_KEY] = form
            old_message = mock.Mock(message_id=10)
            for action, payload in (("fo", "0.1"), ("fx", "-")):
                query = mock.Mock(message=old_message, answer=mock.AsyncMock())
                await handlers._form_callback(query, action, payload, context)
            self.assertIs(context.chat_data[handlers.FORM_KEY], form)
            self.assertEqual(form.answers, {})
        asyncio.run(run())

    def test_double_click_does_not_answer_next_asset(self):
        async def run():
            context = test_bot.PledgeDialogTests._context(self, {})
            context.bot.edit_message_text = mock.AsyncMock()
            message, _ = test_bot.PledgeDialogTests._message(self)
            message.chat_id = 1
            form = self.form([self.cad, "50:01:0000001:1"])
            form.message_id = message.message_id
            context.chat_data[handlers.FORM_KEY] = form
            ready = asyncio.Event()
            count = 0

            async def acknowledge(*args, **kwargs):
                nonlocal count
                count += 1
                if count == 2:
                    ready.set()
                await ready.wait()

            queries = [mock.Mock(message=message, answer=acknowledge) for _ in range(2)]
            await asyncio.gather(*(handlers._form_callback(q, "fo", "0.0", context) for q in queries))
            self.assertEqual(form.step, 1)
            self.assertIn("50:01:0000001:1", form.current.prompt)
            self.assertNotIn("asset:50:01:0000001:1:scope", form.answers)
        asyncio.run(run())

    def test_cannot_skip_or_finish_unresolved_asset(self):
        form = self.form()
        with self.assertRaises(ValueError):
            form.skip()
        with self.assertRaises(ValueError):
            form.finish_now()
        form.answer("Доля")
        with self.assertRaises(ValueError):
            form.finish_now()
        form.answer("1/2")
        form.answer("Нет")
        form.finish_now()
        self.assertTrue(form.done)

    def test_each_object_and_back(self):
        form = self.form([self.cad, "50:01:0000001:1"])
        form.answer("Доля")
        form.answer("23/130")
        form.answer("Нет")
        self.assertIn("50:01:0000001:1", form.current.prompt)
        form.back()
        form.back()
        form.back()
        form.answer("Целый объект")
        self.assertNotIn(f"asset:{self.cad}:share", form.answers)
        self.assertIn("50:01:0000001:1", form.current.prompt)

    def test_room_offer_does_not_claim_full_area_or_price(self):
        flat = load("flat", self.cad)
        flat.options["area"] = 251.1
        answers = {f"asset:{self.cad}:scope": "Доля", f"asset:{self.cad}:share": "23/130",
                   f"asset:{self.cad}:allocated": "Да, в комнату"}
        text = "\n".join(format_pledge_offer([flat], answers))
        self.assertIn("комната", text.lower())
        self.assertIn("23/130", text)
        self.assertIn("в квартире", text)
        self.assertIn("Площадь всего объекта", text)
        self.assertIn("Кадастровая стоимость всего объекта", text)
        self.assertIn("со слов", text.lower())
        self.assertNotIn("• Площадь:", text)

    def test_land_and_house_share_and_mixed_pledge(self):
        land = load("land", "77:01:0001001:1000")
        house = load("building", "77:01:0001001:1020")
        house.options["purpose"] = "Жилое"
        answers = {}
        for obj in (land, house):
            answers.update({f"asset:{obj.cad_number}:scope": "Доля",
                            f"asset:{obj.cad_number}:share": "1/3",
                            f"asset:{obj.cad_number}:allocated": "Нет"})
        text = "\n".join(format_pledge_offer([land, house], answers))
        self.assertIn("в земельном участке", text)
        self.assertIn("в жилом доме", text)
        self.assertNotIn("• Суммарная кадастровая стоимость:", text)

    def test_bad_text_keeps_same_question(self):
        async def run():
            flat = load("flat", self.cad)
            context = test_bot.PledgeDialogTests._context(self, {self.cad: flat})
            context.bot.edit_message_text = mock.AsyncMock()
            message, _ = test_bot.PledgeDialogTests._message(self)
            await handlers._start_form(message, context, "loan", [self.cad], True)
            form = context.chat_data[handlers.FORM_KEY]
            form.answer("Доля")
            message.text = "23/0"
            await handlers._answer_form_text(message, context, form)
            self.assertTrue(form.current.key.endswith(":share"))
            self.assertIn("23/130", message.reply_text.call_args.args[0])
        asyncio.run(run())

    def test_full_room_dialog_and_stale_step(self):
        async def run():
            flat = load("flat", self.cad)
            context = test_bot.PledgeDialogTests._context(self, {self.cad: flat})
            context.bot.edit_message_text = mock.AsyncMock()
            message, _ = test_bot.PledgeDialogTests._message(self)
            message.chat_id = 1
            await handlers._start_form(message, context, "loan", [self.cad], True)
            form = context.chat_data[handlers.FORM_KEY]

            async def button(action, payload):
                query = mock.Mock(message=message)
                query.answer = mock.AsyncMock()
                await handlers._form_callback(query, action, payload, context)

            # Пропуск и досрочный выпуск через старые/поддельные кнопки запрещены.
            await button("fs", "0")
            await button("ff", "0")
            self.assertEqual(form.step, 0)
            await button("fo", "0.1")  # Доля
            message.text = "23/130"
            await handlers._answer_form_text(message, context, form)
            await button("fo", "0.0")  # Старый вопрос: ничего не меняет.
            self.assertTrue(form.current.key.endswith(":allocated"))
            await button("fo", "2.0")  # Комната
            self.assertEqual(form.current.key, "amount")
            await button("ff", "3")
            self.assertNotIn(handlers.FORM_KEY, context.chat_data)
            offer = message.reply_text.call_args.args[0]
            self.assertIn("залог доли", offer)
            self.assertIn("комната", offer)
            self.assertIn("23/130", offer)
        asyncio.run(run())

    def test_whole_asset_keeps_regular_description(self):
        flat = load("flat", self.cad)
        form = self.form()
        form.answer("Целый объект")
        self.assertEqual(form.current.key, "amount")
        text = "\n".join(format_pledge_offer([flat], form.answers))
        self.assertIn("предлагается квартира", text)
        self.assertIn("• Площадь:", text)
        self.assertNotIn("Размер доли", text)

    def test_mixed_pledge_only_relabels_share(self):
        flat = load("flat", self.cad)
        land = load("land", "77:01:0001001:1000")
        answers = {f"asset:{self.cad}:scope": "Доля", f"asset:{self.cad}:share": "1/2",
                   f"asset:{self.cad}:allocated": "Нет", f"asset:{land.cad_number}:scope": "Целый объект"}
        text = "\n".join(format_pledge_offer([flat, land], answers))
        self.assertIn("доля 1/2 в квартире", text)
        self.assertEqual(text.count("• Площадь всего объекта:"), 1)
        self.assertEqual(text.count("• Площадь:"), 1)
        self.assertIn("не стоимость залога", text)

    def test_older_saved_form_without_new_slot(self):
        form = OfferForm("loan", [self.cad], True)
        del form.collect_assets  # Так выглядит сохранённое состояние прежнего класса.
        restored = pickle.loads(pickle.dumps(form))
        self.assertEqual(restored.current.key, "amount")

    def test_missing_object_does_not_silently_drop_pledge(self):
        async def run():
            flat = load("flat", self.cad)
            objects = {self.cad: flat}
            context = test_bot.PledgeDialogTests._context(self, objects)
            message, _ = test_bot.PledgeDialogTests._message(self)
            form = self.form([self.cad, "50:01:0000001:1"])
            form.answer("Целый объект")
            form.answer("Целый объект")
            form.finish_now()
            context.chat_data[handlers.FORM_KEY] = form
            await handlers._finish_form(message, context, form)
            self.assertIn(handlers.FORM_KEY, context.chat_data)
            self.assertIn("Анкета сохранена", message.reply_text.call_args.args[0])
            land = load("land", "77:01:0001001:1000")
            land.cad_number = "50:01:0000001:1"
            objects[land.cad_number] = land
            message.text = "Повторить"
            await handlers._answer_form_text(message, context, form)
            self.assertNotIn(handlers.FORM_KEY, context.chat_data)
            self.assertIn(land.cad_number, message.reply_text.call_args.args[0])
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
