"""Offline regression checks. Uses temporary SQLite and mocks all network calls."""
import asyncio
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

with patch.dict("os.environ", {
    "BOT_TOKEN": "123456:offline-test-token", "ADMIN_ID": "999",
    "OTP_API_KEY": "offline", "OTP_BASE_URL": "https://example.invalid",
    "FIREBASE_DB_URL": "https://example.invalid", "SEPAY_WEBHOOK_TOKEN": "",
}):
    import test_otp as shop


USER = SimpleNamespace(id=101, full_name="Khách thử", username="test_user")
CATALOG = {"ResponseCode": 0, "Result": [
    {"Id": 1001, "Name": "Facebook", "Cost": 5},
    {"Id": 1005, "Name": "Gmail/Google", "Cost": 5},
    {"Id": 1195, "Name": "Dịch Vụ Khác", "Cost": 5},
]}


def fake_message():
    message = SimpleNamespace(from_user=USER, text="", answer=AsyncMock(), edit_text=AsyncMock(),
                              answer_photo=AsyncMock(), delete=AsyncMock())
    message.answer.return_value = message
    message.model_copy = lambda *, update: SimpleNamespace(**{**vars(message), **update})
    return message


def fake_callback(data, user=USER):
    callback = SimpleNamespace(data=data, from_user=user, message=fake_message(), answer=AsyncMock())
    callback.model_copy = lambda *, update: SimpleNamespace(**{**vars(callback), **update})
    return callback


def tokens(markup):
    return [button.callback_data for row in markup.inline_keyboard for button in row]


class ShopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_patch = patch.object(shop, "DB_NAME", str(Path(self.temp.name) / "shop.db"))
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        shop.init_db()
        shop.save_user(USER)
        shop.set_balance(USER.id, 2000)
        shop.RENTAL_LOCKS.clear()
        shop.PURCHASE_LOCKS.clear()
        shop.OTP_WATCH_TASKS.clear()
        self.catalog = AsyncMock(return_value=CATALOG)
        self.bot = SimpleNamespace(send_message=AsyncMock())
        for p in (patch.object(shop, "get_fixed_apps_from_api", self.catalog),
                  patch.object(shop, "bot", self.bot),
                  patch.object(shop, "process_firebase_deposit", AsyncMock(return_value=False)),
                  patch.object(shop, "forward_sepay_event", AsyncMock(return_value=False)),
                  patch.dict(shop.RUNTIME_CONFIG, {"price_mul": 1000})):
            p.start()
            self.addCleanup(p.stop)

    async def asyncTearDown(self):
        await shop.stop_otp_watchers()

    def make_intent(self, **kwargs):
        markup = shop.rental_shortfall_keyboard(USER.id, 1001, "Facebook", 5000, 2000, **kwargs)
        self.assertIn("3,000đ", markup.inline_keyboard[0][0].text)
        return tokens(markup)[0].split("|")[1]

    def waiting_order(self, req="request-1", user=USER.id, age=0):
        history_id = shop.save_otp_history(user, 1001, "Facebook", "0912345678", 5000, req_id=req)
        if age:
            created = datetime.fromtimestamp(datetime.now(timezone.utc).timestamp() - age, timezone.utc)
            with closing(shop.db()) as conn:
                conn.execute("UPDATE otp_history SET created_at = ? WHERE id = ?",
                             (created.strftime("%Y-%m-%d %H:%M:%S"), history_id))
                conn.commit()
        return history_id

    def test_search_aliases_and_accentless_text(self):
        for query, expected in [("FB", 1001), ("gg", 1005), ("dich vu khac", 1195), ("DỊCH VỤ KHÁC", 1195)]:
            with self.subTest(query=query):
                self.assertEqual(shop.search_services(CATALOG["Result"], query)[0]["Id"], expected)
        self.assertEqual(shop.search_services(CATALOG["Result"], "unknown-service"), [])

    async def test_missing_service_has_other_button(self):
        message = fake_message()
        state = AsyncMock()
        await shop.render_service_search(message, state, "unknown-service")
        self.assertIn("search_other", tokens(message.answer.call_args.kwargs["reply_markup"]))
        state.clear.assert_not_awaited()

    async def test_other_button_opens_live_catalog_result(self):
        callback = fake_callback("search_other")
        await shop.search_other_callback(callback, AsyncMock())
        buttons = tokens(callback.message.answer.call_args.kwargs["reply_markup"])
        self.assertTrue(any(value.startswith("appinfo|1195|") for value in buttons))

    def test_intents_persist_and_are_user_scoped(self):
        token = self.make_intent(carrier="Viettel")
        shop.init_db()  # Repeat migration/restart, without resetting balance or intents.
        self.assertEqual(shop.get_balance(USER.id), 2000)
        self.assertEqual(shop.get_rental_intent(token, USER.id)["carrier"], "Viettel")
        self.assertIsNone(shop.get_rental_intent(token, 202))

    async def test_topup_recalculates_exact_shortfall_below_minimum(self):
        token = self.make_intent()
        shop.set_balance(USER.id, 3500)
        callback = fake_callback(f"rental_topup|{token}")
        with patch.object(shop, "send_deposit_checkout", new_callable=AsyncMock) as checkout:
            await shop.rental_topup_callback(callback, AsyncMock())
            self.assertEqual(checkout.call_args.args[2], 1500)
            self.assertEqual(checkout.call_args.kwargs["rental_token"], token)

    async def test_topup_uses_new_price_and_rejects_removed_services(self):
        token = self.make_intent()
        self.catalog.return_value = {"ResponseCode": 0, "Result": [{"Id": 1001, "Name": "Facebook", "Cost": 6}]}
        with patch.object(shop, "send_deposit_checkout", new_callable=AsyncMock) as checkout:
            await shop.rental_topup_callback(fake_callback(f"rental_topup|{token}"), AsyncMock())
            self.assertEqual(checkout.call_args.args[2], 4000)
            checkout.reset_mock()
            self.catalog.return_value = {"ResponseCode": 0, "Result": []}
            await shop.rental_topup_callback(fake_callback(f"rental_topup|{token}"), AsyncMock())
            checkout.assert_not_awaited()

    async def test_paid_deposit_links_to_confirmation_and_reuses_pending_qr(self):
        token = self.make_intent()
        message = fake_message()
        with patch.object(shop, "build_qr_on_paper_image", new_callable=AsyncMock), \
             patch.object(shop, "auto_expire_deposit_order_later", new_callable=AsyncMock):
            await shop.send_deposit_checkout(message, USER, 3000, rental_token=token)
            await shop.send_deposit_checkout(message, USER, 3000, rental_token=token)
            await asyncio.sleep(0)
        with closing(shop.db()) as conn:
            orders = conn.execute("SELECT * FROM deposit_orders").fetchall()
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["amount"], 3000)
        self.assertIn(f"rental_resume|{token}", tokens(shop.deposit_paid_keyboard(orders[0]["id"], USER.id)))
        self.assertNotIn(f"rental_resume|{token}", tokens(shop.deposit_paid_keyboard(orders[0]["id"], 202)))

    async def test_sepay_exact_shortfall_credits_once_and_does_not_rent(self):
        token = self.make_intent()
        order = shop.create_deposit_order(USER.id, 3000, "NAP101_test")
        with closing(shop.db()) as conn:
            conn.execute("UPDATE rental_intents SET deposit_id = ? WHERE token = ?", (order, token))
            conn.commit()
        request = SimpleNamespace(headers={}, json=AsyncMock(return_value={
            "id": "txn-test-1", "transferAmount": 3000, "content": "NAP101_test", "transferType": "in",
        }))
        with patch.object(shop.otp_api, "request_number", new_callable=AsyncMock) as rent:
            result = await shop.sepay_webhook_post(request)
            self.assertTrue(result["ok"])
            self.assertEqual(shop.get_balance(USER.id), 5000)
            result = await shop.sepay_webhook_post(request)
            self.assertEqual(shop.get_balance(USER.id), 5000)
            rent.assert_not_awaited()
        customer_calls = [c for c in self.bot.send_message.call_args_list if c.args[0] == USER.id]
        self.assertIn(f"rental_resume|{token}", tokens(customer_calls[0].kwargs["reply_markup"]))

    async def test_qr_amount_changes_with_wallet_balance(self):
        token = self.make_intent()
        message = fake_message()
        with patch.object(shop, "build_qr_on_paper_image", new_callable=AsyncMock), \
             patch.object(shop, "auto_expire_deposit_order_later", new_callable=AsyncMock):
            await shop.send_deposit_checkout(message, USER, 3000, rental_token=token)
            await shop.send_deposit_checkout(message, USER, 1500, rental_token=token)
            await asyncio.sleep(0)
        intent = shop.get_rental_intent(token, USER.id)
        self.assertEqual(shop.get_deposit_order_by_id(intent["deposit_id"])["amount"], 1500)

    async def test_resume_before_payment_and_wrong_user_cannot_rent(self):
        token = self.make_intent()
        with patch.object(shop, "otp_buy_confirmed_callback", new_callable=AsyncMock) as rent:
            callback = fake_callback(f"rental_resume|{token}")
            await shop.rental_resume_callback(callback, AsyncMock())
            self.assertIn("Cần nạp thêm", callback.message.answer.call_args.args[0])
            other = SimpleNamespace(id=202)
            await shop.rental_confirm_callback(fake_callback(f"rental_confirm|{token}|5000", other), AsyncMock())
            rent.assert_not_awaited()
        self.assertIsNotNone(shop.get_rental_intent(token, USER.id))

    async def test_manual_approval_has_resume_button(self):
        token = self.make_intent()
        order = shop.create_deposit_order(USER.id, 3000, "NAP101_manual")
        with closing(shop.db()) as conn:
            conn.execute("UPDATE rental_intents SET deposit_id = ? WHERE token = ?", (order, token))
            conn.commit()
        admin = SimpleNamespace(id=shop.ADMIN_ID)
        await shop.admin_action_handler(fake_callback(f"admin_approve|{order}", admin))
        self.assertEqual(shop.get_balance(USER.id), 5000)
        customer_call = next(c for c in self.bot.send_message.call_args_list if c.args[0] == USER.id)
        self.assertIn(f"rental_resume|{token}", tokens(customer_call.kwargs["reply_markup"]))

    async def test_confirmation_only_rents_after_explicit_click_and_once(self):
        token = self.make_intent(carrier="Viettel")
        shop.set_balance(USER.id, 5000)
        with patch.object(shop, "otp_buy_confirmed_callback", new_callable=AsyncMock) as rent:
            await shop.rental_resume_callback(fake_callback(f"rental_resume|{token}"), AsyncMock())
            rent.assert_not_awaited()
            callback = fake_callback(f"rental_confirm|{token}|5000")
            await shop.rental_confirm_callback(callback, AsyncMock())
            await shop.rental_confirm_callback(callback, AsyncMock())
            rent.assert_awaited_once()
            self.assertTrue(rent.call_args.args[0].data.endswith("|Viettel"))

    async def test_price_change_requires_confirmation_again(self):
        token = self.make_intent()
        shop.set_balance(USER.id, 9000)
        self.catalog.return_value = {"ResponseCode": 0, "Result": [{"Id": 1001, "Cost": 6}]}
        with patch.object(shop, "otp_buy_confirmed_callback", new_callable=AsyncMock) as rent:
            callback = fake_callback(f"rental_confirm|{token}|5000")
            await shop.rental_confirm_callback(callback, AsyncMock())
            rent.assert_not_awaited()
            self.assertIsNotNone(shop.get_rental_intent(token, USER.id))
            self.assertIn(f"rental_confirm|{token}|6000", tokens(callback.message.answer.call_args.kwargs["reply_markup"]))

    async def test_specific_number_continues_with_same_user_and_number(self):
        token = self.make_intent(phone="0912345678")
        shop.set_balance(USER.id, 5000)
        with patch.object(shop, "buy_specific_phone_handler", new_callable=AsyncMock) as rent:
            await shop.rental_confirm_callback(fake_callback(f"rental_confirm|{token}|5000"), AsyncMock())
            rent.assert_awaited_once()
            self.assertEqual(rent.call_args.args[0].text, "0912345678")
            self.assertEqual(rent.call_args.args[0].from_user.id, USER.id)

    async def test_completed_and_refunded_orders_not_restored(self):
        self.waiting_order("complete")
        self.waiting_order("refund")
        shop.update_otp_history_status(USER.id, "complete", "success", "123456")
        shop.refund_waiting_otp_once(USER.id, req_id="refund")
        self.assertEqual(await shop.restore_waiting_otp(), 0)

    async def test_restored_overdue_order_checks_code_before_refund(self):
        self.waiting_order(age=800)
        with patch.object(shop.otp_api, "get_otp_code", AsyncMock(return_value={"ResponseCode": 0, "Result": {"Code": "123456"}})):
            self.assertEqual(await shop.restore_waiting_otp(), 1)
            await asyncio.gather(*list(shop.OTP_WATCH_TASKS.values()))
        self.assertEqual(shop.get_balance(USER.id), 2000)
        self.assertEqual(shop.get_otp_history_by_req(USER.id, "request-1")["status"], "success")
        self.bot.send_message.assert_awaited_once()

    async def test_overdue_pending_order_refunds_once_without_new_wait_period(self):
        self.waiting_order(age=800)
        poll = AsyncMock(return_value={"ResponseCode": 1})
        with patch.object(shop.otp_api, "get_otp_code", poll):
            first = shop.start_otp_watcher(USER.id, "request-1")
            self.assertIs(first, shop.start_otp_watcher(USER.id, "request-1"))
            await asyncio.wait_for(first, timeout=1)
            self.assertEqual(await shop.restore_waiting_otp(), 0)
        self.assertEqual(shop.get_balance(USER.id), 7000)
        poll.assert_awaited_once()
        self.assertIsNone(shop.refund_waiting_otp_once(USER.id, req_id="request-1"))

    async def test_network_outage_keeps_order_waiting_without_refund(self):
        self.waiting_order(age=800)
        entered = asyncio.Event()
        async def failed_poll(_):
            entered.set()
            return {"ResponseCode": 1, "_transport_error": True}
        with patch.object(shop.otp_api, "get_otp_code", side_effect=failed_poll):
            task = shop.start_otp_watcher(USER.id, "request-1")
            await asyncio.wait_for(entered.wait(), 1)
            self.assertFalse(task.done())
            self.assertEqual(shop.get_balance(USER.id), 2000)
            self.assertEqual(shop.get_otp_history_by_req(USER.id, "request-1")["status"], "waiting")
            await shop.stop_otp_watchers()

    async def test_reconnect_does_not_lose_original_refund(self):
        history_id = self.waiting_order(age=800)
        responses = AsyncMock(side_effect=[{"ResponseCode": 1}, {"ResponseCode": 2}])
        with patch.object(shop.otp_api, "get_otp_code", responses):
            await shop.rebuy_callback(fake_callback(f"rebuy_confirm|{history_id}"))
            await asyncio.gather(*list(shop.OTP_WATCH_TASKS.values()))
        self.assertEqual(shop.get_balance(USER.id), 7000)
        self.assertEqual(shop.get_otp_history_by_id(history_id, USER.id)["status"], "refunded")

    def test_pending_orders_not_deleted_by_history_limit(self):
        for i in range(shop.OTP_HISTORY_MAX + 5):
            self.waiting_order(req=f"request-{i}")
        self.assertEqual(len(shop.get_all_waiting_otp()), shop.OTP_HISTORY_MAX + 5)

    async def test_admin_expired_order_never_receives_refund(self):
        self.waiting_order(user=shop.ADMIN_ID, age=800)
        with patch.object(shop.otp_api, "get_otp_code", AsyncMock(return_value={"ResponseCode": 2})):
            await shop.start_otp_watcher(shop.ADMIN_ID, "request-1")
        self.assertEqual(shop.get_balance(shop.ADMIN_ID), 0)
        self.assertEqual(shop.get_otp_history_by_req(shop.ADMIN_ID, "request-1")["status"], "expired")


def tearDownModule():
    asyncio.run(shop.HTTP_CLIENT.aclose())


if __name__ == "__main__":
    unittest.main()
