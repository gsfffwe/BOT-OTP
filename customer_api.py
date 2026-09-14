"""Customer-facing API, using the Telegram shop wallet and OTP history."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import secrets
import time
from contextlib import closing
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field


class ApiProblem(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


class RentalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    app_id: int = Field(gt=0, strict=True)
    carrier: Literal["Viettel", "Mobi", "Vina", "VNMB", "ITelecom"] | None = None
    phone: str | None = Field(default=None, pattern=r"^[0-9]{9,12}$")
    max_price: int | None = Field(default=None, gt=0, strict=True)


def init_customer_api_schema(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS customer_api_keys(
            user_id INTEGER PRIMARY KEY,
            key_hash TEXT NOT NULL UNIQUE,
            suffix TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS customer_api_orders(
            order_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            idempotency_key TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            app_id INTEGER NOT NULL,
            app_name TEXT NOT NULL,
            price INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'allocating',
            error_code TEXT,
            history_id INTEGER,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, idempotency_key)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_customer_orders_user ON customer_api_orders(user_id, created_at)")


class CustomerApi:
    def __init__(self, *, db, catalog, price_multiplier, request_number, start_watcher,
                 normalize_phone, valid_phone, to_api_phone, purchase_locks, notify_review,
                 rate_limit=120):
        self.db = db
        self.catalog = catalog
        self.price_multiplier = price_multiplier
        self.request_number = request_number
        self.start_watcher = start_watcher
        self.normalize_phone = normalize_phone
        self.valid_phone = valid_phone
        self.to_api_phone = to_api_phone
        self.purchase_locks = purchase_locks
        self.notify_review = notify_review
        self.rate_limit = rate_limit
        self.buckets = {}
        self.tasks = set()
        self.allocating_limit = asyncio.Semaphore(5)

    def key_info(self, user_id):
        with closing(self.db()) as conn:
            return conn.execute("SELECT suffix, created_at FROM customer_api_keys WHERE user_id = ?", (user_id,)).fetchone()

    def issue_key(self, user_id, *, rotate=False):
        raw = "otp_live_" + secrets.token_urlsafe(32)
        digest = hashlib.sha256(raw.encode()).hexdigest()
        with closing(self.db()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not conn.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,)).fetchone():
                raise ValueError("Hãy mở /start trước khi tạo API key.")
            if not rotate and conn.execute("SELECT 1 FROM customer_api_keys WHERE user_id = ?", (user_id,)).fetchone():
                raise ValueError("Bạn đã có API key. Chọn Đổi key nếu cần tạo lại.")
            conn.execute("""
                INSERT INTO customer_api_keys(user_id, key_hash, suffix) VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET key_hash = excluded.key_hash,
                    suffix = excluded.suffix, created_at = CURRENT_TIMESTAMP
            """, (user_id, digest, raw[-6:]))
            conn.commit()
        return raw

    def revoke_key(self, user_id):
        with closing(self.db()) as conn:
            conn.execute("DELETE FROM customer_api_keys WHERE user_id = ?", (user_id,))
            conn.commit()

    async def authenticate(self, request: Request):
        auth = request.headers.get("authorization", "")
        scheme, _, raw = auth.partition(" ")
        if scheme.lower() != "bearer" or not re.fullmatch(r"otp_live_[A-Za-z0-9_-]{43}", raw):
            raise ApiProblem(401, "INVALID_API_KEY", "API key không hợp lệ hoặc đã bị thu hồi.")
        digest = hashlib.sha256(raw.encode()).hexdigest()
        with closing(self.db()) as conn:
            row = conn.execute("""
                SELECT k.user_id FROM customer_api_keys k JOIN users u ON u.user_id = k.user_id
                WHERE k.key_hash = ?
            """, (digest,)).fetchone()
        if not row:
            raise ApiProblem(401, "INVALID_API_KEY", "API key không hợp lệ hoặc đã bị thu hồi.")
        user_id = int(row["user_id"])
        now = time.monotonic()
        if len(self.buckets) > 5000:
            self.buckets = {key: value for key, value in self.buckets.items() if value[0] > now - 60}
        started, count = self.buckets.get(user_id, (now, 0))
        if now - started >= 60:
            started, count = now, 0
        if count >= self.rate_limit:
            raise ApiProblem(429, "RATE_LIMITED", "Quá nhiều yêu cầu. Vui lòng chờ tối đa 60 giây.")
        self.buckets[user_id] = started, count + 1
        return user_id

    def balance(self, user_id):
        with closing(self.db()) as conn:
            row = conn.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return int(row["balance"]) if row else 0

    async def services(self):
        try:
            result = await self.catalog()
        except Exception:
            raise ApiProblem(503, "SERVICES_UNAVAILABLE", "Chưa tải được danh sách dịch vụ. Vui lòng thử lại sau.") from None
        if (not isinstance(result, dict) or result.get("ResponseCode") != 0
                or not isinstance(result.get("Result"), list)):
            raise ApiProblem(503, "SERVICES_UNAVAILABLE", "Chưa tải được danh sách dịch vụ. Vui lòng thử lại sau.")
        services = []
        for item in result.get("Result", []):
            if not isinstance(item, dict):
                continue
            try:
                price = int(float(item.get("Cost", 0)) * self.price_multiplier())
                if price <= 0:
                    continue
                services.append({"app_id": int(item["Id"]), "name": str(item["Name"]),
                                 "price": price, "currency": "VND", "category": str(item.get("category") or "other")})
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
        return services

    def order_by_key(self, user_id, key):
        with closing(self.db()) as conn:
            return conn.execute("SELECT * FROM customer_api_orders WHERE user_id = ? AND idempotency_key = ?",
                                (user_id, key)).fetchone()

    def order(self, user_id, order_id):
        with closing(self.db()) as conn:
            row = conn.execute("""
                SELECT a.*, h.status AS otp_status, h.phone, h.otp_code, h.created_at AS otp_created_at
                FROM customer_api_orders a LEFT JOIN otp_history h ON h.id = a.history_id AND h.user_id = a.user_id
                WHERE a.user_id = ? AND a.order_id = ?
            """, (user_id, order_id)).fetchone()
        if not row:
            raise ApiProblem(404, "ORDER_NOT_FOUND", "Không tìm thấy đơn hàng.")
        status = row["otp_status"] or row["status"]
        expires_at = None
        if row["otp_created_at"]:
            created = datetime.strptime(row["otp_created_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            expires_at = int(created.timestamp()) + 420
        return {"order_id": row["order_id"], "app_id": row["app_id"], "app_name": row["app_name"],
                "price": row["price"], "currency": "VND", "status": status,
                "phone": row["phone"], "otp": row["otp_code"], "expires_at": expires_at,
                "created_at": row["created_at"].replace(" ", "T") + "Z", "error_code": row["error_code"],
                "balance": self.balance(user_id)}

    def reserve(self, user_id, key, fingerprint, service):
        """Debit and record the request in one SQLite transaction before contacting stock."""
        order_id = "API-" + secrets.token_hex(8).upper()
        with closing(self.db()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT * FROM customer_api_orders WHERE user_id = ? AND idempotency_key = ?",
                                    (user_id, key)).fetchone()
            if existing:
                if existing["fingerprint"] != fingerprint:
                    raise ApiProblem(409, "IDEMPOTENCY_CONFLICT", "Mã yêu cầu đã dùng cho nội dung khác.")
                return existing["order_id"], False
            price = service["price"]
            changed = conn.execute("UPDATE users SET balance = balance - ? WHERE user_id = ? AND balance >= ?",
                                   (price, user_id, price)).rowcount
            if not changed:
                raise ApiProblem(402, "INSUFFICIENT_BALANCE", "Số dư chưa đủ. Vui lòng nạp tiền trong bot.")
            conn.execute("""
                INSERT INTO customer_api_orders(order_id, user_id, idempotency_key, fingerprint, app_id, app_name, price)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (order_id, user_id, key, fingerprint, service["app_id"], service["name"], price))
            balance = conn.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,)).fetchone()[0]
            conn.execute("INSERT INTO balance_logs(user_id, change_amount, balance_after, note) VALUES (?, ?, ?, ?)",
                         (user_id, -price, balance, f"Thuê OTP qua API - {order_id}"))
            conn.commit()
        return order_id, True

    def refund_allocation(self, order_id, code, *, allowed_status="allocating"):
        with closing(self.db()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM customer_api_orders WHERE order_id = ? AND status = ?",
                               (order_id, allowed_status)).fetchone()
            if not row:
                return False
            changed = conn.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?",
                                   (row["price"], row["user_id"])).rowcount
            if not changed:
                raise RuntimeError("Wallet missing during API refund")
            balance = conn.execute("SELECT balance FROM users WHERE user_id = ?", (row["user_id"],)).fetchone()[0]
            conn.execute("UPDATE customer_api_orders SET status = 'failed', error_code = ? WHERE order_id = ?",
                         (code, order_id))
            conn.execute("INSERT INTO balance_logs(user_id, change_amount, balance_after, note) VALUES (?, ?, ?, ?)",
                         (row["user_id"], row["price"], balance, f"Hoàn yêu cầu OTP qua API - {order_id}"))
            conn.commit()
        return True

    def flag_review(self, order_id):
        with closing(self.db()) as conn:
            conn.execute("UPDATE customer_api_orders SET status = 'review', error_code = 'ORDER_REVIEW' WHERE order_id = ? AND status = 'allocating'", (order_id,))
            conn.commit()

    def pending_reviews(self):
        with closing(self.db()) as conn:
            return conn.execute("SELECT order_id, user_id, price, app_name FROM customer_api_orders WHERE status = 'review' ORDER BY created_at LIMIT 20").fetchall()

    def recover_allocations(self):
        # A crash may occur after stock accepted the request: never purchase again blindly.
        with closing(self.db()) as conn:
            conn.execute("UPDATE customer_api_orders SET status = 'review', error_code = 'ORDER_REVIEW' WHERE status = 'allocating'")
            conn.commit()

    async def allocate(self, user_id, order_id, payload, service):
        try:
            async with self.allocating_limit:
                result = await self.request_number(payload.app_id, carrier=payload.carrier,
                                                   number=self.to_api_phone(payload.phone) if payload.phone else None)
            if not isinstance(result, dict) or result.get("_transport_error"):
                raise RuntimeError("Uncertain allocation")
            if result.get("ResponseCode") != 0:
                # Only an explicit, structured rejection may release reserved money automatically.
                if isinstance(result.get("ResponseCode"), int):
                    self.refund_allocation(order_id, "NUMBER_UNAVAILABLE")
                    return
                raise RuntimeError("Invalid allocation response")
            details = result.get("Result")
            if not isinstance(details, dict) or not details.get("Id") or not details.get("Number"):
                raise RuntimeError("Incomplete allocation response")
            phone = self.normalize_phone(str(details["Number"]))
            if not self.valid_phone(phone):
                raise RuntimeError("Invalid assigned phone")
            if payload.phone and self.to_api_phone(payload.phone) != self.to_api_phone(phone):
                self.refund_allocation(order_id, "NUMBER_UNAVAILABLE")
                return
            with closing(self.db()) as conn:
                conn.execute("BEGIN IMMEDIATE")
                state = conn.execute("SELECT status FROM customer_api_orders WHERE order_id = ?", (order_id,)).fetchone()
                if not state or state[0] != "allocating":
                    raise RuntimeError("Allocation state changed")
                req_id = str(details["Id"])
                if conn.execute("SELECT 1 FROM otp_history WHERE req_id = ?", (req_id,)).fetchone():
                    raise RuntimeError("Duplicate assignment")
                history_id = conn.execute("""
                    INSERT INTO otp_history(user_id, app_id, app_name, phone, raw_phone, req_id, sell_price, status, source)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'waiting', 'api')
                """, (user_id, payload.app_id, service["name"], phone, str(details["Number"]), req_id, service["price"])).lastrowid
                conn.execute("UPDATE customer_api_orders SET status = 'waiting', history_id = ? WHERE order_id = ?", (history_id, order_id))
                conn.commit()
            self.start_watcher(user_id, req_id)
        except asyncio.CancelledError:
            self.flag_review(order_id)
            raise
        except Exception as exc:
            logging.error("API allocation requires review: %s (%s)", order_id, type(exc).__name__)
            self.flag_review(order_id)
            try:
                await self.notify_review(order_id, user_id, service["price"])
            except Exception:
                logging.warning("Could not notify admin about API review %s", order_id)

    async def purchase(self, user_id, payload, key):
        if not key or not re.fullmatch(r"[A-Za-z0-9_.:-]{8,128}", key):
            raise ApiProblem(400, "IDEMPOTENCY_KEY_REQUIRED", "Gửi Idempotency-Key gồm 8–128 ký tự chữ/số hoặc _ . : -.")
        if payload.phone:
            if not self.valid_phone(self.normalize_phone(payload.phone)):
                raise ApiProblem(422, "INVALID_PHONE", "Số điện thoại không hợp lệ.")
            payload = payload.model_copy(update={"phone": self.normalize_phone(payload.phone)})
        fingerprint = hashlib.sha256(json.dumps(payload.model_dump(), sort_keys=True).encode()).hexdigest()
        async with self.purchase_locks[user_id]:
            existing = self.order_by_key(user_id, key)
            if existing:
                if existing["fingerprint"] != fingerprint:
                    raise ApiProblem(409, "IDEMPOTENCY_CONFLICT", "Mã yêu cầu đã dùng cho nội dung khác.")
                return self.order(user_id, existing["order_id"])
            services = await self.services()
            service = next((item for item in services if item["app_id"] == payload.app_id), None)
            if not service:
                raise ApiProblem(404, "SERVICE_NOT_FOUND", "Dịch vụ không có sẵn.")
            if payload.max_price is not None and service["price"] > payload.max_price:
                raise ApiProblem(409, "PRICE_CHANGED", "Giá hiện tại vượt max_price. Kiểm tra lại danh sách dịch vụ.")
            order_id, created = self.reserve(user_id, key, fingerprint, service)
            if created:
                task = asyncio.create_task(self.allocate(user_id, order_id, payload, service))
                self.tasks.add(task)
                task.add_done_callback(self.tasks.discard)
                await asyncio.shield(task)
            return self.order(user_id, order_id)

    async def close(self):
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def install(self, app):
        router = APIRouter(prefix="/api/v1", tags=["Customer API"])

        @app.exception_handler(ApiProblem)
        async def api_error(_request, exc):
            headers = {"Cache-Control": "no-store"}
            if exc.status == 401:
                headers["WWW-Authenticate"] = "Bearer"
            if exc.status == 429:
                headers["Retry-After"] = "60"
            return JSONResponse(status_code=exc.status, headers=headers,
                                content={"success": False, "error": {"code": exc.code, "message": exc.message}})

        def ok(data, status=200):
            return JSONResponse(status_code=status, content={"success": True, "data": data}, headers={"Cache-Control": "no-store"})

        @router.get("/balance")
        async def get_balance(user_id: int = Depends(self.authenticate)):
            return ok({"balance": self.balance(user_id), "currency": "VND"})

        @router.get("/services")
        async def get_services(user_id: int = Depends(self.authenticate)):
            return ok({"services": await self.services()})

        @router.post("/orders")
        async def create_order(payload: RentalRequest, user_id: int = Depends(self.authenticate),
                               idempotency_key: str | None = Header(default=None)):
            result = await self.purchase(user_id, payload, idempotency_key)
            return ok(result, 202 if result["status"] in {"allocating", "review"} else 200)

        @router.get("/orders")
        async def list_orders(user_id: int = Depends(self.authenticate), limit: int = Query(20, ge=1, le=100),
                              offset: int = Query(0, ge=0)):
            with closing(self.db()) as conn:
                rows = conn.execute("SELECT order_id FROM customer_api_orders WHERE user_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?",
                                    (user_id, limit, offset)).fetchall()
            return ok({"orders": [self.order(user_id, row["order_id"]) for row in rows], "limit": limit, "offset": offset})

        @router.get("/orders/{order_id}")
        async def get_order(order_id: str, user_id: int = Depends(self.authenticate)):
            return ok(self.order(user_id, order_id))

        app.include_router(router)
