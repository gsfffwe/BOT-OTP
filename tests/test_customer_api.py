"""Offline customer API tests: temporary wallets, simulated stock and Telegram."""
import asyncio
import hashlib
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx

with patch.dict("os.environ", {
    "BOT_TOKEN": "123456:offline-test-token", "ADMIN_ID": "999",
    "OTP_API_KEY": "offline", "OTP_BASE_URL": "https://example.invalid",
    "FIREBASE_DB_URL": "https://example.invalid", "SEPAY_WEBHOOK_TOKEN": "",
}):
    import test_otp as shop

from customer_api import RentalRequest

USER = SimpleNamespace(id=101, full_name="Khách API", username="api_test")
OTHER = SimpleNamespace(id=202, full_name="Khách khác", username="other_test")
CATALOG = {"ResponseCode": 0, "Result": [
    {"Id": 1001, "Name": "Facebook", "Cost": 5, "category": "social"},
    {"Id": 1195, "Name": "Dịch Vụ Khác", "Cost": 6},
]}
ASSIGNED = {"ResponseCode": 0, "Result": {"Id": "stock-request-1", "Number": "912345678"}}


def message(user=USER, chat_type="private"):
    m = SimpleNamespace(from_user=user, chat=SimpleNamespace(type=chat_type),
                        answer=AsyncMock(), edit_text=AsyncMock(), answer_document=AsyncMock())
    m.answer.return_value = m
    return m


def callback(data, user=USER, chat_type="private"):
    return SimpleNamespace(data=data, from_user=user, message=message(user, chat_type), answer=AsyncMock())


class CustomerApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        db_patch = patch.object(shop, "DB_NAME", str(Path(self.temp.name) / "shop.db"))
        db_patch.start()
        self.addCleanup(db_patch.stop)
        shop.init_db()
        for user in (USER, OTHER):
            shop.save_user(user)
            shop.set_balance(user.id, 10000)
        shop.PURCHASE_LOCKS.clear()
        shop.OTP_WATCH_TASKS.clear()
        self.api = shop.customer_api
        self.api.buckets.clear()
        self.api.allocating_limit = asyncio.Semaphore(5)
        self.api.rate_limit = 120
        self.catalog = AsyncMock(return_value=CATALOG)
        self.rent = AsyncMock(return_value=ASSIGNED)
        self.watch = Mock()
        self.real_watcher = shop.start_otp_watcher
        self.bot = SimpleNamespace(send_message=AsyncMock())
        for p in (patch.object(shop, "get_fixed_apps_from_api", self.catalog),
                  patch.object(shop.otp_api, "request_number", self.rent),
                  patch.object(shop.otp_api, "get_otp_code", AsyncMock(side_effect=AssertionError("Unexpected OTP poll"))),
                  patch.object(shop, "start_otp_watcher", self.watch),
                  patch.object(shop, "bot", self.bot),
                  patch.dict(shop.RUNTIME_CONFIG, {"price_mul": 1000})):
            p.start()
            self.addCleanup(p.stop)
        self.key = self.api.issue_key(USER.id)
        self.other_key = self.api.issue_key(OTHER.id)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=shop.app), base_url="https://test.invalid")

    async def asyncTearDown(self):
        await self.api.close()
        await shop.stop_otp_watchers()
        await self.client.aclose()
        shop.PURCHASE_LOCKS.clear()
        self.api.rate_limit = 120

    async def get(self, path, key=None):
        return await self.client.get("/api/v1" + path, headers={"Authorization": "Bearer " + (key or self.key)})

    async def buy(self, key="test-order-0001", body=None, api_key=None):
        return await self.client.post("/api/v1/orders", json=body if body is not None else {"app_id": 1001},
                                      headers={"Authorization": "Bearer " + (api_key or self.key), "Idempotency-Key": key})

    def logs(self, user_id=USER.id):
        with closing(shop.db()) as conn:
            return conn.execute("SELECT change_amount FROM balance_logs WHERE user_id = ? AND note LIKE '%API%'", (user_id,)).fetchall()

    async def test_authentication_required_and_query_key_not_accepted(self):
        for path in ("/balance", "/services", "/orders", "/orders/unknown"):
            response = await self.client.get("/api/v1" + path, params={"api_key": self.key})
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.headers["www-authenticate"], "Bearer")
            self.assertNotIn(self.key, response.text)
        self.assertEqual((await self.get("/balance", key="invalid")).status_code, 401)

    async def test_key_hash_rotation_revocation_and_restart(self):
        with closing(shop.db()) as conn:
            stored = conn.execute("SELECT * FROM customer_api_keys WHERE user_id = ?", (USER.id,)).fetchone()
        self.assertEqual(stored["key_hash"], hashlib.sha256(self.key.encode()).hexdigest())
        self.assertEqual(stored["suffix"], self.key[-6:])
        self.assertNotIn(self.key, str(dict(stored)))
        with self.assertRaises(ValueError):
            self.api.issue_key(USER.id)
        shop.init_db()
        self.assertEqual((await self.get("/balance")).status_code, 200)
        new_key = self.api.issue_key(USER.id, rotate=True)
        self.assertEqual((await self.get("/balance")).status_code, 401)
        self.assertEqual((await self.get("/balance", key=new_key)).status_code, 200)
        self.api.revoke_key(USER.id)
        self.assertEqual((await self.get("/balance", key=new_key)).status_code, 401)
        self.assertEqual(shop.get_balance(USER.id), 10000)

    async def test_existing_wallet_and_bot_topup_are_shared(self):
        self.assertEqual((await self.get("/balance")).json()["data"]["balance"], 10000)
        shop.update_balance(USER.id, 3500, note="Offline topup")
        self.assertEqual((await self.get("/balance")).json()["data"]["balance"], 13500)
        await self.buy()
        self.assertEqual(shop.get_balance(USER.id), 8500)
        self.assertEqual(shop.get_balance(OTHER.id), 10000)

    async def test_catalog_only_exposes_retail_prices(self):
        response = await self.get("/services")
        service = response.json()["data"]["services"][0]
        self.assertEqual(service, {"app_id": 1001, "name": "Facebook", "price": 5000,
                                   "currency": "VND", "category": "social"})
        self.assertNotIn("Cost", response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")

    async def test_catalog_failure_does_not_debit_or_leak_details(self):
        self.catalog.side_effect = RuntimeError("private upstream details")
        response = await self.buy()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private upstream", response.text)
        self.assertEqual(shop.get_balance(USER.id), 10000)
        self.rent.assert_not_awaited()

    async def test_purchase_uses_retail_price_and_durable_history(self):
        response = await self.buy(body={"app_id": 1001, "carrier": "Viettel", "max_price": 5000})
        self.assertEqual(response.status_code, 200)
        order = response.json()["data"]
        self.assertEqual((order["status"], order["price"], order["balance"]), ("waiting", 5000, 5000))
        self.assertEqual(order["phone"], "0912345678")
        self.assertIsNone(order["otp"])
        self.assertIsInstance(order["expires_at"], int)
        self.assertIn("T", order["created_at"])
        self.assertNotIn("stock-request", response.text)
        self.watch.assert_called_once_with(USER.id, "stock-request-1")
        self.rent.assert_awaited_once_with(1001, carrier="Viettel", number=None)
        shop.init_db()
        self.assertEqual((await self.get("/orders/" + order["order_id"])).json()["data"]["price"], 5000)
        self.assertEqual(shop.get_otp_history_by_req(USER.id, "stock-request-1")["source"], "api")

    async def test_idempotent_replay_no_second_purchase_even_after_price_change(self):
        first = (await self.buy()).json()["data"]
        self.catalog.side_effect = RuntimeError("unavailable now")
        second = (await self.buy()).json()["data"]
        self.assertEqual(second["order_id"], first["order_id"])
        self.rent.assert_awaited_once()
        self.assertEqual([r[0] for r in self.logs()], [-5000])

    async def test_concurrent_same_request_only_buys_once(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed(*args, **kwargs):
            entered.set()
            await release.wait()
            return ASSIGNED
        self.rent.side_effect = delayed
        first = asyncio.create_task(self.buy())
        await asyncio.wait_for(entered.wait(), 1)
        second = asyncio.create_task(self.buy())
        release.set()
        responses = await asyncio.gather(first, second)
        self.assertEqual(responses[0].json()["data"]["order_id"], responses[1].json()["data"]["order_id"])
        self.rent.assert_awaited_once()
        self.assertEqual(shop.get_balance(USER.id), 5000)

    async def test_concurrent_distinct_requests_cannot_overdraw(self):
        shop.set_balance(USER.id, 5000)
        responses = await asyncio.gather(self.buy("request-a"), self.buy("request-b"))
        self.assertEqual(sorted(r.status_code for r in responses), [200, 402])
        self.rent.assert_awaited_once()
        self.assertEqual(shop.get_balance(USER.id), 0)

    async def test_api_waits_for_telegram_purchase_using_same_wallet(self):
        shop.set_balance(USER.id, 5000)
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed(*args, **kwargs):
            entered.set()
            await release.wait()
            return ASSIGNED
        self.rent.side_effect = delayed
        telegram = asyncio.create_task(shop.otp_buy_confirmed_callback(callback("buy_confirm|1001|5000|Facebook")))
        await asyncio.wait_for(entered.wait(), 1)
        api_request = asyncio.create_task(self.buy())
        release.set()
        await telegram
        self.assertEqual((await api_request).status_code, 402)
        self.rent.assert_awaited_once()
        self.assertEqual(shop.get_balance(USER.id), 0)

    async def test_changed_body_conflicts_without_new_debit(self):
        await self.buy()
        response = await self.buy(body={"app_id": 1195})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "IDEMPOTENCY_CONFLICT")
        self.rent.assert_awaited_once()
        self.assertEqual(shop.get_balance(USER.id), 5000)

    async def test_invalid_requests_and_price_cap_never_contact_stock(self):
        cases = [({}, 422), ({"app_id": "1001"}, 422), ({"app_id": 0}, 422),
                 ({"app_id": 1001, "user_id": OTHER.id}, 422),
                 ({"app_id": 1001, "price": 1}, 422),
                 ({"app_id": 1001, "phone": "091234567899"}, 422),
                 ({"app_id": 1001, "carrier": "unknown"}, 422),
                 ({"app_id": 1001, "max_price": 4999}, 409), ({"app_id": 98765}, 404)]
        for body, expected in cases:
            with self.subTest(body=body):
                self.assertEqual((await self.buy(body=body)).status_code, expected)
        response = await self.client.post("/api/v1/orders", json={"app_id": 1001},
                                          headers={"Authorization": "Bearer " + self.key})
        self.assertEqual(response.status_code, 400)
        self.assertEqual((await self.buy(key="short")).status_code, 400)
        self.rent.assert_not_awaited()
        self.assertEqual(shop.get_balance(USER.id), 10000)

    async def test_orders_are_owner_scoped_even_with_a_valid_other_key(self):
        order = (await self.buy()).json()["data"]
        self.assertEqual((await self.get("/orders/" + order["order_id"], self.other_key)).status_code, 404)
        self.assertEqual((await self.get("/orders", self.other_key)).json()["data"]["orders"], [])
        own = (await self.get("/orders")).json()["data"]["orders"]
        self.assertEqual([o["order_id"] for o in own], [order["order_id"]])
        new_key = self.api.issue_key(USER.id, rotate=True)
        self.assertEqual((await self.get("/orders/" + order["order_id"], new_key)).status_code, 200)

    async def test_explicit_rejection_refunds_once_and_hides_supplier_message(self):
        self.rent.return_value = {"ResponseCode": 4, "Msg": "Private stock balance 3000"}
        response = await self.buy()
        self.assertEqual(response.json()["data"]["status"], "failed")
        self.assertEqual(response.json()["data"]["error_code"], "NUMBER_UNAVAILABLE")
        self.assertNotIn("Private stock", response.text)
        await self.buy()
        self.assertEqual(shop.get_balance(USER.id), 10000)
        self.assertEqual([r[0] for r in self.logs()], [-5000, 5000])
        self.rent.assert_awaited_once()
        self.watch.assert_not_called()

    async def test_specific_phone_is_normalized_and_wrong_assignment_refunded(self):
        response = await self.buy(body={"app_id": 1001, "phone": "84912345678"})
        self.assertEqual(response.json()["data"]["phone"], "0912345678")
        self.rent.assert_awaited_once_with(1001, carrier=None, number="912345678")
        response = await self.buy(key="another-request", body={"app_id": 1001, "phone": "0987654321"})
        self.assertEqual(response.json()["data"]["status"], "failed")
        self.assertEqual(shop.get_balance(USER.id), 5000)

    async def test_transport_uncertainty_holds_money_no_automatic_retry(self):
        self.rent.return_value = {"ResponseCode": 1, "_transport_error": True}
        response = await self.buy()
        order = response.json()["data"]
        self.assertEqual((response.status_code, order["status"]), (202, "review"))
        self.assertEqual(shop.get_balance(USER.id), 5000)
        await self.buy()
        self.rent.assert_awaited_once()
        self.bot.send_message.assert_awaited_once()
        self.assertEqual(self.bot.send_message.call_args.args[0], shop.ADMIN_ID)
        self.assertEqual(self.api.pending_reviews()[0]["order_id"], order["order_id"])
        unauthorized = callback("api_refund_confirm|" + order["order_id"])
        await shop.api_review_refund_callback(unauthorized)
        self.assertEqual(shop.get_balance(USER.id), 5000)
        admin = SimpleNamespace(id=shop.ADMIN_ID)
        await shop.api_review_refund_callback(callback("api_refund_check|" + order["order_id"], admin))
        self.assertEqual(shop.get_balance(USER.id), 5000)
        for _ in range(2):
            await shop.api_review_refund_callback(callback("api_refund_confirm|" + order["order_id"], admin))
        self.assertEqual(shop.get_balance(USER.id), 10000)
        self.assertEqual(self.api.order(USER.id, order["order_id"])["error_code"], "ADMIN_REFUNDED")
        self.assertEqual([r[0] for r in self.logs()], [-5000, 5000])

    async def test_unexpected_or_incomplete_allocation_is_review_not_refund(self):
        for n, value in enumerate((None, {}, {"ResponseCode": 0, "Result": {}},
                                    {"ResponseCode": 0, "Result": {"Id": "r", "Number": "invalid"}})):
            shop.set_balance(USER.id, 10000)
            self.rent.return_value = value
            order = (await self.buy(key=f"malformed-{n}")).json()["data"]
            self.assertEqual(order["status"], "review")
            self.assertEqual(shop.get_balance(USER.id), 5000)
        self.watch.assert_not_called()

    async def test_timeout_exception_and_duplicate_assignment_never_give_free_otp(self):
        self.rent.side_effect = httpx.ReadTimeout("private endpoint")
        response = await self.buy()
        self.assertEqual(response.json()["data"]["status"], "review")
        self.assertNotIn("private endpoint", response.text)
        self.rent.side_effect = None
        shop.set_balance(USER.id, 15000)
        await self.buy(key="valid-request")
        duplicate = await self.buy(key="duplicate-stock-id")
        self.assertEqual(duplicate.json()["data"]["status"], "review")
        self.watch.assert_called_once()

    async def test_restart_marks_inflight_for_review_preserving_debit_and_idempotency(self):
        payload = RentalRequest(app_id=1001)
        import json
        fingerprint = hashlib.sha256(json.dumps(payload.model_dump(), sort_keys=True).encode()).hexdigest()
        order_id, _ = self.api.reserve(USER.id, "test-order-0001", fingerprint,
                                       {"app_id": 1001, "name": "Facebook", "price": 5000})
        shop.init_db()
        self.api.recover_allocations()
        self.assertEqual(self.api.order(USER.id, order_id)["status"], "review")
        self.assertEqual((await self.buy()).json()["data"]["order_id"], order_id)
        self.rent.assert_not_awaited()
        self.assertEqual(shop.get_balance(USER.id), 5000)

    async def test_cancelled_http_request_does_not_cancel_or_duplicate_purchase(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed(*args, **kwargs):
            entered.set()
            await release.wait()
            return ASSIGNED
        self.rent.side_effect = delayed
        client_request = asyncio.create_task(self.buy())
        await asyncio.wait_for(entered.wait(), 1)
        client_request.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await client_request
        replay = await self.buy()
        self.assertEqual(replay.json()["data"]["status"], "allocating")
        self.assertEqual(shop.get_balance(USER.id), 5000)
        release.set()
        await asyncio.gather(*list(self.api.tasks))
        self.assertEqual((await self.buy()).json()["data"]["status"], "waiting")
        self.rent.assert_awaited_once()

    async def test_otp_success_visible_through_api_without_telegram_spam_or_extra_charge(self):
        order = (await self.buy()).json()["data"]
        with patch.object(shop.otp_api, "get_otp_code", AsyncMock(return_value={"ResponseCode": 0, "Result": {"Code": "001234"}})):
            await self.real_watcher(USER.id, "stock-request-1")
        result = (await self.get("/orders/" + order["order_id"])).json()["data"]
        self.assertEqual((result["status"], result["otp"]), ("success", "001234"))
        self.assertEqual(shop.get_balance(USER.id), 5000)
        self.bot.send_message.assert_not_awaited()

    async def test_api_admin_is_charged_and_expired_order_refunds_once(self):
        admin = SimpleNamespace(id=shop.ADMIN_ID, full_name="Admin", username="admin")
        shop.save_user(admin)
        shop.set_balance(admin.id, 5000)
        key = self.api.issue_key(admin.id)
        order = (await self.buy(api_key=key)).json()["data"]
        self.assertEqual(shop.get_balance(admin.id), 0)
        with patch.object(shop.otp_api, "get_otp_code", AsyncMock(return_value={"ResponseCode": 2})):
            await self.real_watcher(admin.id, "stock-request-1")
        self.assertEqual(self.api.order(admin.id, order["order_id"])["status"], "refunded")
        self.assertEqual(shop.get_balance(admin.id), 5000)
        self.assertIsNone(shop.refund_waiting_otp_once(admin.id, req_id="stock-request-1"))
        self.bot.send_message.assert_not_awaited()

    async def test_network_outage_during_otp_poll_keeps_paid_order_pending(self):
        order = (await self.buy()).json()["data"]
        entered = asyncio.Event()
        async def disconnected(_):
            entered.set()
            return {"ResponseCode": 1, "_transport_error": True}
        with patch.object(shop.otp_api, "get_otp_code", side_effect=disconnected):
            self.real_watcher(USER.id, "stock-request-1")
            await asyncio.wait_for(entered.wait(), 1)
            await shop.stop_otp_watchers()
        self.assertEqual(self.api.order(USER.id, order["order_id"])["status"], "waiting")
        self.assertEqual(shop.get_balance(USER.id), 5000)

    async def test_api_completed_history_survives_bot_history_retention(self):
        order = (await self.buy()).json()["data"]
        shop.update_otp_history_status(USER.id, "stock-request-1", "success", "123456")
        for i in range(shop.OTP_HISTORY_MAX + 2):
            shop.save_otp_history(USER.id, 1001, "Facebook", "0912345678", 5000, req_id=f"bot-{i}")
            shop.update_otp_history_status(USER.id, f"bot-{i}", "success", "654321")
        self.assertEqual(self.api.order(USER.id, order["order_id"])["otp"], "123456")

    async def test_rate_limit_is_shared_across_endpoints_and_key_rotation(self):
        self.api.rate_limit = 2
        self.assertEqual((await self.get("/balance")).status_code, 200)
        self.assertEqual((await self.get("/services")).status_code, 200)
        key = self.api.issue_key(USER.id, rotate=True)
        response = await self.get("/orders", key)
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["retry-after"], "60")
        self.assertEqual((await self.get("/balance", self.other_key)).status_code, 200)

    async def test_private_menu_key_creation_confirmation_and_guide(self):
        self.api.revoke_key(USER.id)
        group = callback("customer_api_create", chat_type="group")
        await shop.customer_api_key_callback(group)
        self.assertIsNone(self.api.key_info(USER.id))
        c = callback("customer_api_create")
        await shop.customer_api_key_callback(c)
        self.assertTrue(c.message.answer.call_args.kwargs["protect_content"])
        self.assertIn("otp_live_", c.message.answer.call_args.args[0])
        before = dict(self.api.key_info(USER.id))
        await shop.customer_api_key_callback(callback("customer_api_rotate"))
        self.assertEqual(dict(self.api.key_info(USER.id)), before)
        m = message()
        with patch.dict("os.environ", {"CUSTOMER_API_BASE_URL": "https://otp.example.invalid"}):
            await shop.customer_api_command(m, AsyncMock())
            self.assertIn("https://otp.example.invalid/api/v1", m.answer.call_args.args[0])
            self.assertNotIn("otp_live_", m.answer.call_args.args[0])
            c = callback("customer_api_guide")
            await shop.customer_api_guide_callback(c)
            guide = c.message.answer_document.call_args.args[0].data.decode("utf-8")
            self.assertIn("https://otp.example.invalid/api/v1", guide)
            self.assertNotIn("{{BASE_URL}}", guide)
            self.assertIn("Idempotency-Key", guide)
        markup = shop.main_menu_keyboard(USER.id)
        self.assertIn("customer_api", [b.callback_data for row in markup.inline_keyboard for b in row])

    def test_display_base_url_does_not_use_forward_destination(self):
        with patch.dict("os.environ", {"CUSTOMER_API_BASE_URL": "", "RAILWAY_PUBLIC_DOMAIN": "otp.example.invalid",
                                       "SEPAY_FORWARD_URL": "https://other.example.invalid"}):
            self.assertEqual(shop.customer_api_base_url(), "https://otp.example.invalid/api/v1")
        with patch.dict("os.environ", {"CUSTOMER_API_BASE_URL": "https://custom.example.invalid/api/v1/"}):
            self.assertEqual(shop.customer_api_base_url(), "https://custom.example.invalid/api/v1")

    def test_wallet_guard_rejects_overdraft_without_balance_log(self):
        self.assertIsNone(shop.update_balance(USER.id, -10001, note="API attempted overdraft"))
        self.assertEqual(shop.get_balance(USER.id), 10000)
        self.assertEqual(self.logs(), [])

    def test_old_database_migrates_without_resetting_users_or_history(self):
        legacy_file = str(Path(self.temp.name) / "legacy.db")
        with closing(sqlite3.connect(legacy_file)) as conn:
            conn.executescript("""
                CREATE TABLE users(user_id INTEGER PRIMARY KEY, full_name TEXT, username TEXT, balance INTEGER DEFAULT 0);
                INSERT INTO users VALUES(77, 'Existing user', 'existing', 12345);
                CREATE TABLE otp_history(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, app_id INTEGER,
                    app_name TEXT, phone TEXT, sell_price INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
                INSERT INTO otp_history(user_id, app_id, app_name, phone, sell_price) VALUES(77, 1001, 'Facebook', '0912345678', 5000);
            """)
            conn.commit()
        with patch.object(shop, "DB_NAME", legacy_file):
            shop.init_db()
            shop.init_db()
            self.assertEqual(shop.get_balance(77), 12345)
            with closing(shop.db()) as conn:
                row = conn.execute("SELECT * FROM otp_history").fetchone()
                self.assertEqual((row["status"], row["source"], row["phone"]), ("expired", "bot", "0912345678"))
            self.assertTrue(self.api.issue_key(77).startswith("otp_live_"))


if __name__ == "__main__":
    unittest.main()
