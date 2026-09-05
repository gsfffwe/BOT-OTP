import asyncio
import logging
import sqlite3
import html
import os
import unicodedata
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from io import BytesIO

import httpx
import uvicorn
from fastapi import FastAPI, Request
from PIL import Image
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    BufferedInputFile,
    FSInputFile,
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    MenuButtonDefault,
    CopyTextButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    KeyboardButton,
)

_AiogramInlineKeyboardButton = InlineKeyboardButton
BUTTON_STYLES_ENABLED = os.getenv("TELEGRAM_BUTTON_STYLES", "1") != "0"


def InlineKeyboardButton(*args, **kwargs):
    """Tạo nút có màu và tự bỏ màu khi môi trường Telegram chưa hỗ trợ."""
    if not BUTTON_STYLES_ENABLED:
        kwargs.pop("style", None)
    return _AiogramInlineKeyboardButton(*args, **kwargs)

# --- CẤU HÌNH ---
# Ưu tiên biến môi trường (Railway), fallback giá trị cũ để không vỡ khi chưa set.
# Khi đổi Railway: chỉ cần set BOT_TOKEN, ADMIN_ID, FIREBASE_DB_URL (+ FIREBASE_SECRET nếu muốn heartbeat).
# OTP key/URL nên để trống và cấu hình trong web admin (settings/config) để đồng bộ.
BOT_TOKEN = os.getenv("BOT_TOKEN", "8762970436:AAEKJgyYfsMMQT711OKMl3z0n_Nl5ztwkL4")
ADMIN_ID = int(os.getenv("ADMIN_ID", "7078570432"))
OTP_API_KEY = os.getenv("OTP_API_KEY", "8fc8e078133cde11")
OTP_BASE_URL = os.getenv("OTP_BASE_URL", "https://chaycodeso3.com/api")
# Hệ số giá bán: giá bán = giá gốc (Cost) × PRICE_MUL. Đồng bộ từ web qua settings/config.
PRICE_MUL = int(os.getenv("PRICE_MUL", "3000"))
FIREBASE_DB_URL = os.getenv("FIREBASE_DB_URL", "https://accstore-47e37-default-rtdb.asia-southeast1.firebasedatabase.app")
# Firebase legacy secret — chỉ cần để bot GHI heartbeat lên settings/botStatus (tùy chọn).
FIREBASE_SECRET = os.getenv("FIREBASE_SECRET", "")

# Cấu hình động đọc từ Firebase settings/config (đồng bộ với web admin).
# Khởi tạo từ env/hardcode ở trên, sau đó refresh_runtime_config() sẽ ghi đè định kỳ.
RUNTIME_CONFIG = {
    "otp_api_key": OTP_API_KEY,
    "otp_base_url": OTP_BASE_URL,
    "price_mul": PRICE_MUL,
}

BANK_BIN = "970422"

# Nhóm app OTP
CATEGORY_EMOJI = {
    'social':   '💬',
    'shopping': '🛍️',
    'finance':  '💰',
    'local':    '🇻🇳',
    'ai':       '🤖',
    'game':     '🎮',
    'other':    '📦',
}
CATEGORY_LABEL = {
    'social':   'Mạng xã hội',
    'shopping': 'Mua sắm',
    'finance':  'Ví & Crypto',
    'local':    'Việt Nam',
    'ai':       'AI',
    'game':     'Game',
    'other':    'Khác',
}

# Cache danh sách app từ Firebase (TTL 5 phút)
import time as _time
_apps_cache: list = []
_apps_cache_ts: float = 0.0
APPS_CACHE_TTL = 300
BANK_ACCOUNT = "346641789567"
ACCOUNT_NAME = "VU VAN CUONG"

BASE_DIR = Path(__file__).resolve().parent
# Dùng Railway Volume (/data) nếu có, fallback về thư mục code khi chạy local
_DATA_DIR = Path(os.getenv("RAILWAY_VOLUME_MOUNT_PATH", str(BASE_DIR)))
DB_NAME = str(_DATA_DIR / "shop_bot.db")
PORT = int(os.getenv("PORT", "8000"))

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
app = FastAPI()

HTTP_CLIENT = httpx.AsyncClient(
    timeout=httpx.Timeout(15.0, connect=5.0),
    limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
    follow_redirects=True
)

BALANCE_LOCK = asyncio.Lock()
DEFAULT_NOTE = "📌 Ghi chú: OTP về sẽ tính tiền. Nếu sau thời gian chờ không có OTP thì hệ thống sẽ hoàn tiền."
QR_TEMPLATE_PATH = BASE_DIR / "qr_mau_nguoi_cam_giay.jpg"

# Tọa độ vùng tờ giấy theo ảnh bạn đã gửi
QR_PASTE_X = 220
QR_PASTE_Y = 500
QR_PASTE_W = 270
QR_PASTE_H = 270

# --- REFERRAL ---
REFERRAL_FIRST_BONUS = 3000
REFERRAL_PERCENT = 0.10
REFERRAL_MIN_DEPOSIT = 20000
BOT_USERNAME_CACHE = None
QR_EXPIRE_MINUTES = 30

# --- DANH SÁCH APP CỐ ĐỊNH HIỂN THỊ TRONG BOT ---
FIXED_APP_LIST = [
    {"Id": 1095, "Name": "Amazon"},
    {"Id": 1561, "Name": "Binance"},
    {"Id": 1869, "Name": "Claude"},
    {"Id": 1195, "Name": "Dịch Vụ Khác"},
    {"Id": 1001, "Name": "Facebook"},
    {"Id": 1160, "Name": "Garena"},
    {"Id": 1005, "Name": "Gmail/Google"},
    {"Id": 1021, "Name": "Grab"},
    {"Id": 1432, "Name": "Highlands"},
    {"Id": 1247, "Name": "Id Apple"},
    {"Id": 1010, "Name": "Instagram"},
    {"Id": 1656, "Name": "Katinat"},
    {"Id": 1007, "Name": "Lazada"},
    {"Id": 1034, "Name": "Momo"},
    {"Id": 1102, "Name": "My Viettel"},
    {"Id": 1301, "Name": "MY VNPT/ DIGILIFE/MYTV/VNPT Money"},
    {"Id": 1289, "Name": "Netflix"},
    {"Id": 1090, "Name": "Paypal"},
    {"Id": 1136, "Name": "Roblox"},
    {"Id": 1002, "Name": "Shopee/shopee pay"},
    {"Id": 1472, "Name": "Shopee Food"},
    {"Id": 1006, "Name": "Telegram"},
    {"Id": 1097, "Name": "Tiki"},
    {"Id": 1032, "Name": "TikTok"},
    {"Id": 1030, "Name": "Twitter"},
    {"Id": 1477, "Name": "VNPAY"},
    {"Id": 1022, "Name": "wechat"},
    {"Id": 1024, "Name": "WhatsApp"},
    {"Id": 1425, "Name": "Youtube"},
    {"Id": 1176, "Name": "ZaloPay"},
]

# --- FSM ---
class DepositState(StatesGroup):
    waiting_for_amount = State()

class BuySpecificState(StatesGroup):
    waiting_for_phone = State()

class SearchServiceState(StatesGroup):
    waiting_for_query = State()

# --- DATABASE ---
def db():
    conn = sqlite3.connect(DB_NAME, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn

def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY,
            full_name TEXT,
            username TEXT,
            balance INTEGER DEFAULT 0
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS app_notes(
            keyword TEXT PRIMARY KEY,
            note TEXT NOT NULL
        )
    """)

    cur.execute("PRAGMA table_info(users)")
    columns = [column[1] for column in cur.fetchall()]
    if 'balance' not in columns:
        cur.execute("ALTER TABLE users ADD COLUMN balance INTEGER DEFAULT 0")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS balance_logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            change_amount INTEGER NOT NULL,
            balance_after INTEGER NOT NULL,
            note TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS deposit_orders(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            memo TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'pending',
            provider TEXT DEFAULT 'sepay',
            transaction_id TEXT,
            raw_payload TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            paid_at TEXT
        )
    """)

    # Bảng referral: lưu ai giới thiệu ai
    cur.execute("""
        CREATE TABLE IF NOT EXISTS referrals(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_id INTEGER NOT NULL,
            invited_user_id INTEGER NOT NULL UNIQUE,
            invited_full_name TEXT,
            invited_username TEXT,
            ref_code TEXT,
            first_bonus_amount INTEGER NOT NULL DEFAULT 0,
            first_bonus_paid INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # FIX DB CŨ: nếu bảng referrals đã tồn tại từ trước mà thiếu cột first_bonus_amount / first_bonus_paid
    cur.execute("PRAGMA table_info(referrals)")
    referral_columns = [column[1] for column in cur.fetchall()]
    if referral_columns and 'first_bonus_amount' not in referral_columns:
        cur.execute("ALTER TABLE referrals ADD COLUMN first_bonus_amount INTEGER NOT NULL DEFAULT 0")
    if referral_columns and 'first_bonus_paid' not in referral_columns:
        cur.execute("ALTER TABLE referrals ADD COLUMN first_bonus_paid INTEGER NOT NULL DEFAULT 0")

    # Bảng log hoa hồng 10% theo từng lần nạp
    cur.execute("""
        CREATE TABLE IF NOT EXISTS referral_commissions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_id INTEGER NOT NULL,
            invited_user_id INTEGER NOT NULL,
            deposit_amount INTEGER NOT NULL,
            commission_amount INTEGER NOT NULL,
            source TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS otp_history(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            app_id INTEGER NOT NULL,
            app_name TEXT NOT NULL,
            phone TEXT NOT NULL,
            raw_phone TEXT,
            req_id TEXT,
            sell_price INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'waiting',
            otp_code TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Migration: thêm cột mới cho DB cũ
    cur.execute("PRAGMA table_info(otp_history)")
    otp_hist_cols = [c[1] for c in cur.fetchall()]
    if otp_hist_cols and 'raw_phone' not in otp_hist_cols:
        cur.execute("ALTER TABLE otp_history ADD COLUMN raw_phone TEXT")
    if otp_hist_cols and 'req_id' not in otp_hist_cols:
        cur.execute("ALTER TABLE otp_history ADD COLUMN req_id TEXT")
    if otp_hist_cols and 'status' not in otp_hist_cols:
        # Dữ liệu cũ không còn phiên poll đang chạy, nên đánh dấu hết hạn thay vì chờ giả.
        cur.execute("ALTER TABLE otp_history ADD COLUMN status TEXT NOT NULL DEFAULT 'expired'")
    if otp_hist_cols and 'otp_code' not in otp_hist_cols:
        cur.execute("ALTER TABLE otp_history ADD COLUMN otp_code TEXT")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_favorites(
            user_id  INTEGER NOT NULL,
            app_id   INTEGER NOT NULL,
            app_name TEXT    NOT NULL,
            created_at TEXT  DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, app_id)
        )
    """)

    conn.commit()
    conn.close()

def get_user(user_id):
    conn = db()
    user = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return user

def get_balance(user_id):
    conn = db()
    try:
        row = conn.execute(
            "SELECT balance FROM users WHERE user_id = ?",
            (user_id,)
        ).fetchone()
        return int(row["balance"]) if row else 0
    finally:
        conn.close()

def update_balance(user_id, amount, full_name=None, username=None, note=""):
    conn = db()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO users (user_id, full_name, username, balance)
            VALUES (?, ?, ?, 0)
            ON CONFLICT(user_id) DO UPDATE SET
                full_name = COALESCE(excluded.full_name, users.full_name),
                username = COALESCE(excluded.username, users.username)
        """, (user_id, full_name, username))

        cur.execute("""
            UPDATE users
            SET balance = balance + ?
            WHERE user_id = ?
        """, (amount, user_id))

        cur.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        new_balance = int(row["balance"]) if row else None

        if new_balance is not None:
            cur.execute("""
                INSERT INTO balance_logs(user_id, change_amount, balance_after, note)
                VALUES (?, ?, ?, ?)
            """, (user_id, amount, new_balance, note))

        conn.commit()
        return new_balance
    except Exception:
        conn.rollback()
        logging.exception("Lỗi update_balance")
        return None
    finally:
        conn.close()

def set_balance(user_id, new_balance, full_name=None, username=None, note=""):
    conn = db()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO users (user_id, full_name, username, balance)
            VALUES (?, ?, ?, 0)
            ON CONFLICT(user_id) DO UPDATE SET
                full_name = COALESCE(excluded.full_name, users.full_name),
                username = COALESCE(excluded.username, users.username)
        """, (user_id, full_name, username))

        cur.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
        old_row = cur.fetchone()
        old_balance = int(old_row["balance"]) if old_row else 0

        cur.execute("""
            UPDATE users
            SET balance = ?
            WHERE user_id = ?
        """, (new_balance, user_id))

        cur.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        final_balance = int(row["balance"]) if row else None

        if final_balance is not None:
            change_amount = final_balance - old_balance
            cur.execute("""
                INSERT INTO balance_logs(user_id, change_amount, balance_after, note)
                VALUES (?, ?, ?, ?)
            """, (user_id, change_amount, final_balance, note))

        conn.commit()
        return final_balance
    except Exception:
        conn.rollback()
        logging.exception("Lỗi set_balance")
        return None
    finally:
        conn.close()

def save_user(user):
    conn = db()
    conn.execute("""
        INSERT INTO users (user_id, full_name, username, balance)
        VALUES (?, ?, ?, 0)
        ON CONFLICT(user_id) DO UPDATE SET
            full_name = excluded.full_name,
            username = excluded.username
    """, (user.id, user.full_name, user.username))
    conn.commit()
    conn.close()

def get_users_with_balance():
    conn = db()
    users = conn.execute("""
        SELECT user_id, full_name, username, balance
        FROM users
        WHERE balance > 0
        ORDER BY balance DESC, user_id ASC
    """).fetchall()
    conn.close()
    return users

def create_deposit_order(user_id: int, amount: int, memo: str):
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO deposit_orders(user_id, amount, memo, status, provider)
            VALUES (?, ?, ?, 'pending', 'sepay')
        """, (user_id, amount, memo))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()

def get_deposit_order_by_id(order_id: int):
    conn = db()
    try:
        row = conn.execute("""
            SELECT * FROM deposit_orders
            WHERE id = ?
            LIMIT 1
        """, (order_id,)).fetchone()
        return row
    finally:
        conn.close()


def get_user_deposit_orders(user_id: int, limit: int = 10):
    conn = db()
    try:
        return conn.execute("""
            SELECT id, user_id, amount, memo, status, created_at, paid_at
            FROM deposit_orders
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT ?
        """, (user_id, limit)).fetchall()
    finally:
        conn.close()


def cancel_user_deposit_order(order_id: int, user_id: int) -> bool:
    conn = db()
    try:
        conn.execute("""
            UPDATE deposit_orders
            SET status = 'cancelled'
            WHERE id = ? AND user_id = ? AND status = 'pending'
        """, (order_id, user_id))
        conn.commit()
        return conn.total_changes > 0
    finally:
        conn.close()

def expire_old_pending_orders(minutes: int = QR_EXPIRE_MINUTES):
    conn = db()
    try:
        conn.execute("""
            UPDATE deposit_orders
            SET status = 'expired'
            WHERE status = 'pending'
              AND datetime(created_at, '+' || ? || ' minutes') <= datetime('now')
        """, (minutes,))
        conn.commit()
    finally:
        conn.close()

def is_order_expired(order_row, minutes: int = QR_EXPIRE_MINUTES):
    conn = db()
    try:
        row = conn.execute("""
            SELECT CASE
                WHEN datetime(?, '+' || ? || ' minutes') <= datetime('now') THEN 1
                ELSE 0
            END AS expired
        """, (order_row['created_at'], minutes)).fetchone()
        return bool(row['expired']) if row else False
    finally:
        conn.close()

def mark_order_expired(order_id: int):
    conn = db()
    try:
        conn.execute("""
            UPDATE deposit_orders
            SET status = 'expired'
            WHERE id = ? AND status = 'pending'
        """, (order_id,))
        conn.commit()
        return conn.total_changes > 0
    finally:
        conn.close()

def mark_order_rejected(order_id: int):
    conn = db()
    try:
        conn.execute("""
            UPDATE deposit_orders
            SET status = 'rejected'
            WHERE id = ? AND status = 'pending'
        """, (order_id,))
        conn.commit()
        return conn.total_changes > 0
    finally:
        conn.close()

async def auto_expire_deposit_order_later(order_id: int, user_id: int, amount: int, memo: str):
    await asyncio.sleep(QR_EXPIRE_MINUTES * 60)
    try:
        expired = mark_order_expired(order_id)
        if not expired:
            return
        try:
            await bot.send_message(
                user_id,
                f"⏰ Mã QR nạp tiền đã hết hạn sau <b>{QR_EXPIRE_MINUTES} phút</b>.\n"
                f"💰 Số tiền: <b>{amount:,}đ</b>\n"
                f"📝 Nội dung cũ: <code>{memo}</code>\n\n"
                "Vui lòng tạo lại mã QR mới nếu bạn vẫn muốn nạp tiền.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text=f"↻ Tạo lại đơn {amount:,}đ", callback_data=f"deposit_repeat|{amount}", style="success")],
                    [InlineKeyboardButton(text="📥 Xem các đơn nạp", callback_data="deposit_orders", style="primary")],
                ])
            )
        except Exception:
            logging.exception("Không gửi được thông báo hết hạn QR cho khách")
    except Exception:
        logging.exception("Lỗi auto_expire_deposit_order_later")

def get_pending_orders():
    conn = db()
    try:
        rows = conn.execute("""
            SELECT * FROM deposit_orders
            WHERE status = 'pending'
            ORDER BY id DESC
        """).fetchall()
        return rows
    finally:
        conn.close()


def get_payment_matchable_orders():
    """Đối soát cả đơn chờ lẫn đơn khách vừa huỷ nếu tiền thực sự đã về."""
    conn = db()
    try:
        return conn.execute("""
            SELECT * FROM deposit_orders
            WHERE status IN ('pending', 'cancelled')
            ORDER BY id DESC
        """).fetchall()
    finally:
        conn.close()

def mark_order_paid(order_id: int, transaction_id: str = "", raw_payload: str = ""):
    conn = db()
    try:
        conn.execute("""
            UPDATE deposit_orders
            SET status = 'paid',
                transaction_id = ?,
                raw_payload = ?,
                paid_at = CURRENT_TIMESTAMP
            WHERE id = ? AND status IN ('pending', 'cancelled')
        """, (transaction_id, raw_payload, order_id))
        conn.commit()
        return conn.total_changes > 0
    finally:
        conn.close()

# --- REFERRAL DATABASE ---
def get_referral_by_invited(invited_user_id: int):
    conn = db()
    try:
        row = conn.execute("""
            SELECT * FROM referrals
            WHERE invited_user_id = ?
            LIMIT 1
        """, (invited_user_id,)).fetchone()
        return row
    finally:
        conn.close()

def get_referral_stats(user_id: int):
    conn = db()
    try:
        row1 = conn.execute("""
            SELECT COUNT(*) AS total_invited
            FROM referrals
            WHERE referrer_id = ?
        """, (user_id,)).fetchone()

        row2 = conn.execute("""
            SELECT COALESCE(SUM(first_bonus_amount), 0) AS total_first_bonus
            FROM referrals
            WHERE referrer_id = ?
        """, (user_id,)).fetchone()

        row3 = conn.execute("""
            SELECT COALESCE(SUM(commission_amount), 0) AS total_commission
            FROM referral_commissions
            WHERE referrer_id = ?
        """, (user_id,)).fetchone()

        total_invited = int(row1["total_invited"]) if row1 else 0
        total_first_bonus = int(row2["total_first_bonus"]) if row2 else 0
        total_commission = int(row3["total_commission"]) if row3 else 0
        total_bonus = total_first_bonus + total_commission

        return total_invited, total_bonus
    finally:
        conn.close()

def get_referral_history(user_id: int, limit: int = 20):
    conn = db()
    try:
        rows = conn.execute("""
            SELECT invited_user_id, invited_full_name, invited_username, ref_code, first_bonus_amount, created_at
            FROM referrals
            WHERE referrer_id = ?
            ORDER BY id DESC
            LIMIT ?
        """, (user_id, limit)).fetchall()
        return rows
    finally:
        conn.close()

def get_referral_commission_history(user_id: int, limit: int = 20):
    conn = db()
    try:
        rows = conn.execute("""
            SELECT invited_user_id, deposit_amount, commission_amount, source, created_at
            FROM referral_commissions
            WHERE referrer_id = ?
            ORDER BY id DESC
            LIMIT ?
        """, (user_id, limit)).fetchall()
        return rows
    finally:
        conn.close()

def build_ref_code(referrer_id: int) -> str:
    return f"ref_{referrer_id}"

def extract_referrer_id_from_start(text: str):
    try:
        parts = (text or "").split(maxsplit=1)
        if len(parts) < 2:
            return None

        payload = parts[1].strip()
        if not payload.startswith("ref_"):
            return None

        referrer_id = int(payload.replace("ref_", "", 1))
        return referrer_id
    except Exception:
        return None

def register_referral_atomic(referrer_id: int, invited_user):
    """
    Chỉ ghi nhận quan hệ giới thiệu, KHÔNG cộng thưởng ngay.
    Thưởng người mới + hoa hồng chỉ được trả khi user nạp >= REFERRAL_MIN_DEPOSIT.
    """
    if not referrer_id:
        return ("error", None, 0)

    if int(referrer_id) == int(invited_user.id):
        return ("self_ref", None, 0)

    conn = db()
    cur = conn.cursor()

    try:
        cur.execute("BEGIN IMMEDIATE")

        referrer = cur.execute("""
            SELECT user_id
            FROM users
            WHERE user_id = ?
            LIMIT 1
        """, (referrer_id,)).fetchone()

        if not referrer:
            conn.rollback()
            return ("referrer_not_found", None, 0)

        existed = cur.execute("""
            SELECT id
            FROM referrals
            WHERE invited_user_id = ?
            LIMIT 1
        """, (invited_user.id,)).fetchone()

        if existed:
            conn.rollback()
            return ("already_referred", None, 0)

        ref_code = build_ref_code(referrer_id)

        cur.execute("""
            INSERT INTO referrals(
                referrer_id,
                invited_user_id,
                invited_full_name,
                invited_username,
                ref_code,
                first_bonus_amount,
                first_bonus_paid
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            referrer_id,
            invited_user.id,
            invited_user.full_name,
            invited_user.username,
            ref_code,
            0,
            0
        ))

        conn.commit()
        return ("registered_pending", None, 0)

    except sqlite3.IntegrityError:
        conn.rollback()
        return ("already_referred", None, 0)
    except Exception:
        conn.rollback()
        logging.exception("Lỗi register_referral_atomic")
        return ("error", None, 0)
    finally:
        conn.close()


def apply_referral_commission_atomic(invited_user_id: int, deposit_amount: int, source: str = ""):
    """
    Logic referral đúng theo yêu cầu:
    - Lần nạp đầu tiên của user được giới thiệu phải >= REFERRAL_MIN_DEPOSIT
      => referrer nhận REFERRAL_FIRST_BONUS + 10% tiền nạp
    - Từ lần nạp thứ 2 trở đi: nạp bao nhiêu cũng được, referrer luôn nhận 10%
    """
    if deposit_amount <= 0:
        return {
            "status": "ignored",
            "referrer_id": None,
            "commission_amount": 0,
            "first_bonus_amount": 0,
            "referrer_new_balance": 0
        }

    conn = db()
    cur = conn.cursor()

    try:
        cur.execute("BEGIN IMMEDIATE")

        ref = cur.execute("""
            SELECT referrer_id, invited_user_id, first_bonus_paid
            FROM referrals
            WHERE invited_user_id = ?
            LIMIT 1
        """, (invited_user_id,)).fetchone()

        if not ref:
            conn.rollback()
            return {
                "status": "no_referrer",
                "referrer_id": None,
                "commission_amount": 0,
                "first_bonus_amount": 0,
                "referrer_new_balance": 0
            }

        referrer_id = int(ref["referrer_id"])
        is_first_qualified_reward = int(ref["first_bonus_paid"] or 0) == 0

        if is_first_qualified_reward and int(deposit_amount) < int(REFERRAL_MIN_DEPOSIT):
            conn.rollback()
            return {
                "status": "first_deposit_not_enough",
                "referrer_id": referrer_id,
                "commission_amount": 0,
                "first_bonus_amount": 0,
                "referrer_new_balance": 0
            }

        commission = int(deposit_amount * REFERRAL_PERCENT)
        first_bonus_amount = 0

        if is_first_qualified_reward:
            first_bonus_amount = int(REFERRAL_FIRST_BONUS)
            cur.execute("""
                UPDATE referrals
                SET first_bonus_paid = 1,
                    first_bonus_amount = ?
                WHERE invited_user_id = ?
            """, (first_bonus_amount, invited_user_id))

        total_reward = commission + first_bonus_amount

        if total_reward <= 0:
            conn.rollback()
            return {
                "status": "ignored",
                "referrer_id": referrer_id,
                "commission_amount": 0,
                "first_bonus_amount": 0,
                "referrer_new_balance": 0
            }

        cur.execute("""
            INSERT INTO users (user_id, full_name, username, balance)
            VALUES (?, ?, ?, 0)
            ON CONFLICT(user_id) DO NOTHING
        """, (referrer_id, None, None))

        cur.execute("""
            UPDATE users
            SET balance = balance + ?
            WHERE user_id = ?
        """, (total_reward, referrer_id))

        row = cur.execute("""
            SELECT balance
            FROM users
            WHERE user_id = ?
            LIMIT 1
        """, (referrer_id,)).fetchone()

        new_balance = int(row["balance"]) if row else 0

        if first_bonus_amount > 0:
            cur.execute("""
                INSERT INTO balance_logs(user_id, change_amount, balance_after, note)
                VALUES (?, ?, ?, ?)
            """, (
                referrer_id,
                first_bonus_amount,
                new_balance,
                f"Thưởng người mới referral từ user {invited_user_id} đạt lần nạp đầu tiên >= {REFERRAL_MIN_DEPOSIT}đ | nạp {deposit_amount}đ | source={source}"
            ))

        if commission > 0:
            cur.execute("""
                INSERT INTO balance_logs(user_id, change_amount, balance_after, note)
                VALUES (?, ?, ?, ?)
            """, (
                referrer_id,
                commission,
                new_balance,
                f"Hoa hồng referral 10% từ user {invited_user_id} nạp {deposit_amount}đ | source={source}"
            ))

            cur.execute("""
                INSERT INTO referral_commissions(
                    referrer_id,
                    invited_user_id,
                    deposit_amount,
                    commission_amount,
                    source
                )
                VALUES (?, ?, ?, ?, ?)
            """, (
                referrer_id,
                invited_user_id,
                deposit_amount,
                commission,
                source
            ))

        conn.commit()
        return {
            "status": "credited",
            "referrer_id": referrer_id,
            "commission_amount": commission,
            "first_bonus_amount": first_bonus_amount,
            "referrer_new_balance": new_balance
        }

    except Exception:
        conn.rollback()
        logging.exception("Lỗi apply_referral_commission_atomic")
        return {
            "status": "error",
            "referrer_id": None,
            "commission_amount": 0,
            "first_bonus_amount": 0,
            "referrer_new_balance": 0
        }
    finally:
        conn.close()

async def get_bot_username_cached():
    global BOT_USERNAME_CACHE
    if BOT_USERNAME_CACHE:
        return BOT_USERNAME_CACHE

    me = await bot.get_me()
    BOT_USERNAME_CACHE = me.username
    return BOT_USERNAME_CACHE

async def build_referral_link(referrer_id: int) -> str:
    username = await get_bot_username_cached()
    return f"https://t.me/{username}?start={build_ref_code(referrer_id)}"

# --- APP NOTES DATABASE ---
def set_app_note(keyword, note):
    conn = db()
    conn.execute("""
        INSERT INTO app_notes(keyword, note)
        VALUES(?, ?)
        ON CONFLICT(keyword) DO UPDATE SET note=excluded.note
    """, (keyword.lower().strip(), note.strip()))
    conn.commit()
    conn.close()

def delete_app_note(keyword):
    conn = db()
    cur = conn.cursor()
    cur.execute("DELETE FROM app_notes WHERE keyword = ?", (keyword.lower().strip(),))
    affected = cur.rowcount
    conn.commit()
    conn.close()
    return affected > 0

def get_all_app_notes():
    conn = db()
    rows = conn.execute("SELECT keyword, note FROM app_notes ORDER BY keyword ASC").fetchall()
    conn.close()
    return rows

def get_app_note(app_name: str):
    conn = db()
    rows = conn.execute("SELECT keyword, note FROM app_notes ORDER BY LENGTH(keyword) DESC").fetchall()
    conn.close()

    app_name_lower = app_name.lower()
    for row in rows:
        if row["keyword"] in app_name_lower:
            return row["note"]

    return DEFAULT_NOTE

# --- OTP HISTORY DATABASE ---
OTP_HISTORY_MAX = 20

def save_otp_history(user_id, app_id, app_name, phone, sell_price, raw_phone=None, req_id=None):
    conn = db()
    try:
        cur = conn.execute("""
            INSERT INTO otp_history(user_id, app_id, app_name, phone, raw_phone, req_id, sell_price, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'waiting')
        """, (user_id, int(app_id), app_name, phone, raw_phone, req_id, sell_price))
        history_id = cur.lastrowid
        # Giữ chỉ OTP_HISTORY_MAX bản ghi mới nhất, xóa bản ghi cũ hơn
        conn.execute("""
            DELETE FROM otp_history
            WHERE user_id = ? AND id NOT IN (
                SELECT id FROM otp_history
                WHERE user_id = ?
                ORDER BY id DESC
                LIMIT ?
            )
        """, (user_id, user_id, OTP_HISTORY_MAX))
        conn.commit()
        return history_id
    except Exception:
        logging.exception("Lỗi save_otp_history")
        return None
    finally:
        conn.close()

def get_otp_history_by_id(history_id: int, user_id: int):
    conn = db()
    try:
        row = conn.execute("""
            SELECT * FROM otp_history WHERE id = ? AND user_id = ?
        """, (history_id, user_id)).fetchone()
        return row
    finally:
        conn.close()


def get_otp_history_by_req(user_id: int, req_id):
    conn = db()
    try:
        return conn.execute("""
            SELECT * FROM otp_history
            WHERE user_id = ? AND req_id = ?
            ORDER BY id DESC
            LIMIT 1
        """, (user_id, str(req_id))).fetchone()
    finally:
        conn.close()

def get_otp_history(user_id):
    conn = db()
    try:
        rows = conn.execute("""
            SELECT id, app_id, app_name, phone, sell_price, status, otp_code, created_at
            FROM otp_history
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT ?
        """, (user_id, OTP_HISTORY_MAX)).fetchall()
        return rows
    finally:
        conn.close()


def get_active_otp_history(user_id: int, limit: int = 10):
    conn = db()
    try:
        return conn.execute("""
            SELECT id, user_id, app_id, app_name, phone, raw_phone, req_id,
                   sell_price, status, otp_code, created_at
            FROM otp_history
            WHERE user_id = ? AND status = 'waiting' AND req_id IS NOT NULL
            ORDER BY id DESC
            LIMIT ?
        """, (user_id, limit)).fetchall()
    finally:
        conn.close()


def update_otp_history_status(user_id: int, req_id, status: str, otp_code=None):
    if not req_id:
        return False
    conn = db()
    try:
        conn.execute("""
            UPDATE otp_history
            SET status = ?, otp_code = COALESCE(?, otp_code)
            WHERE user_id = ? AND req_id = ? AND status = 'waiting'
        """, (status, str(otp_code) if otp_code is not None else None, user_id, str(req_id)))
        conn.commit()
        return conn.total_changes > 0
    finally:
        conn.close()


def refund_waiting_otp_once(user_id: int, *, history_id: int | None = None, req_id=None):
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if history_id is not None:
            row = conn.execute("""
                SELECT id, sell_price, phone, app_name, status
                FROM otp_history
                WHERE id = ? AND user_id = ?
            """, (history_id, user_id)).fetchone()
        else:
            row = conn.execute("""
                SELECT id, sell_price, phone, app_name, status
                FROM otp_history
                WHERE user_id = ? AND req_id = ?
                ORDER BY id DESC LIMIT 1
            """, (user_id, str(req_id))).fetchone()

        if not row or row["status"] != "waiting":
            conn.rollback()
            return None

        refund_amount = int(row["sell_price"] or 0)
        conn.execute("UPDATE otp_history SET status = 'refunded' WHERE id = ? AND status = 'waiting'", (row["id"],))
        if conn.total_changes <= 0:
            conn.rollback()
            return None

        conn.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (refund_amount, user_id))
        balance_row = conn.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,)).fetchone()
        new_balance = int(balance_row["balance"]) if balance_row else None
        if new_balance is None:
            conn.rollback()
            return None

        conn.execute("""
            INSERT INTO balance_logs(user_id, change_amount, balance_after, note)
            VALUES (?, ?, ?, ?)
        """, (
            user_id,
            refund_amount,
            new_balance,
            f"Hoàn tiền OTP hết hạn app {row['app_name']} - {row['phone']}"
        ))
        conn.commit()
        return {"amount": refund_amount, "balance": new_balance, "history_id": int(row["id"])}
    except Exception:
        conn.rollback()
        logging.exception("Lỗi hoàn tiền OTP theo lịch sử")
        return None
    finally:
        conn.close()

FAV_MAX = 5

def get_user_favorites(user_id: int) -> list:
    conn = db()
    try:
        rows = conn.execute("""
            SELECT app_id, app_name FROM user_favorites
            WHERE user_id = ?
            ORDER BY created_at DESC
        """, (user_id,)).fetchall()
        return rows
    finally:
        conn.close()

def is_favorite(user_id: int, app_id: int) -> bool:
    conn = db()
    try:
        row = conn.execute("""
            SELECT 1 FROM user_favorites WHERE user_id = ? AND app_id = ?
        """, (user_id, app_id)).fetchone()
        return row is not None
    finally:
        conn.close()

def toggle_favorite(user_id: int, app_id: int, app_name: str) -> str:
    """Trả về 'added' hoặc 'removed' hoặc 'full'."""
    conn = db()
    try:
        exists = conn.execute("""
            SELECT 1 FROM user_favorites WHERE user_id = ? AND app_id = ?
        """, (user_id, app_id)).fetchone()
        if exists:
            conn.execute("DELETE FROM user_favorites WHERE user_id = ? AND app_id = ?", (user_id, app_id))
            conn.commit()
            return "removed"
        count = conn.execute("SELECT COUNT(*) FROM user_favorites WHERE user_id = ?", (user_id,)).fetchone()[0]
        if count >= FAV_MAX:
            return "full"
        conn.execute("""
            INSERT INTO user_favorites(user_id, app_id, app_name) VALUES (?, ?, ?)
        """, (user_id, app_id, app_name))
        conn.commit()
        return "added"
    finally:
        conn.close()

def normalize_phone_vn(phone: str) -> str:
    """Chuẩn hóa về dạng 0xxxxxxxxx (10 chữ số) để hiển thị và lưu DB."""
    s = "".join(ch for ch in str(phone) if ch.isdigit())
    if s.startswith("84") and len(s) == 11:
        s = "0" + s[2:]
    elif not s.startswith("0"):
        s = "0" + s
    return s

def to_api_phone(phone: str) -> str:
    """Chuẩn hóa về dạng 9 chữ số không có 0 đầu (vd: 816373462) — đúng format API chaycodeso3."""
    s = "".join(ch for ch in str(phone) if ch.isdigit())
    if s.startswith("84") and len(s) == 11:
        return s[2:]   # 84816373462 → 816373462
    if s.startswith("0") and len(s) == 10:
        return s[1:]   # 0816373462 → 816373462
    return s

def is_valid_phone_vn(phone: str) -> bool:
    s = normalize_phone_vn(phone)
    return s.isdigit() and len(s) == 10 and s.startswith("0")


# --- API OTP ---
class ChayCodeAPI:
    def __init__(self, api_key):
        self.api_key = api_key

    async def _get(self, params):
        # Dùng cấu hình động (đồng bộ từ Firebase settings/config), không dùng self.api_key cố định.
        params['apik'] = RUNTIME_CONFIG["otp_api_key"]
        try:
            response = await HTTP_CLIENT.get(RUNTIME_CONFIG["otp_base_url"], params=params)
            return response.json()
        except Exception:
            logging.exception("Lỗi gọi OTP API")
            return {"ResponseCode": 1, "Msg": "Lỗi kết nối Server"}

    async def get_apps(self):
        return await self._get({'act': 'app'})

    async def request_number(self, app_id, carrier=None, prefix=None, number=None):
        params = {'act': 'number', 'appId': app_id}
        if carrier:
            params['carrier'] = carrier
        if prefix:
            params['prefix'] = prefix
        if number:
            params['number'] = number
        return await self._get(params)

    async def get_otp_code(self, request_id):
        return await self._get({'act': 'code', 'id': request_id})

otp_api = ChayCodeAPI(OTP_API_KEY)
QR_TEMPLATE_CACHE = None

def _build_qr_sync(qr_bytes: bytes) -> bytes:
    global QR_TEMPLATE_CACHE
    if QR_TEMPLATE_CACHE is None:
        QR_TEMPLATE_CACHE = Image.open(QR_TEMPLATE_PATH).convert("RGBA")
    template = QR_TEMPLATE_CACHE.copy()
    qr_img = Image.open(BytesIO(qr_bytes)).convert("RGBA")
    qr_size = min(QR_PASTE_W, QR_PASTE_H)
    qr_img = qr_img.resize((qr_size, qr_size))
    white_bg = Image.new("RGBA", (qr_size + 20, qr_size + 20), (255, 255, 255, 255))
    white_bg.paste(qr_img, (10, 10))
    template.paste(white_bg, (QR_PASTE_X, QR_PASTE_Y))
    output = BytesIO()
    template.save(output, format="PNG")
    return output.getvalue()

async def build_qr_on_paper_image(qr_url: str) -> BufferedInputFile:
    resp = await HTTP_CLIENT.get(qr_url)
    resp.raise_for_status()
    result = await asyncio.to_thread(_build_qr_sync, resp.content)
    return BufferedInputFile(file=result, filename="qr_thanh_toan.png")

async def _get_selected_apps_list() -> list:
    """Lấy danh sách app đã chọn từ Firebase. Fallback về FIXED_APP_LIST nếu lỗi."""
    global _apps_cache, _apps_cache_ts
    now = _time.time()
    if _apps_cache and (now - _apps_cache_ts) < APPS_CACHE_TTL:
        return _apps_cache
    try:
        url = f"{FIREBASE_DB_URL}/settings/selectedApps.json"
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(url)
        data = resp.json()
        if isinstance(data, list) and len(data) > 0:
            _apps_cache = data
            _apps_cache_ts = now
            return data
    except Exception:
        pass
    _apps_cache = FIXED_APP_LIST
    _apps_cache_ts = now
    return FIXED_APP_LIST


async def refresh_runtime_config():
    """Đọc settings/config từ Firebase và cập nhật OTP key/base URL khi admin đổi trên web.
    Đọc công khai (không cần auth), giống _get_selected_apps_list."""
    try:
        url = f"{FIREBASE_DB_URL}/settings/config.json"
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(url)
        data = resp.json()
        if not isinstance(data, dict):
            return
        changed = []
        new_key = data.get("otpApiKey")
        new_url = data.get("otpBaseUrl")
        new_mul = data.get("priceMultiplier")
        if new_key and new_key != RUNTIME_CONFIG["otp_api_key"]:
            RUNTIME_CONFIG["otp_api_key"] = new_key
            changed.append("otp_api_key")
        if new_url and new_url != RUNTIME_CONFIG["otp_base_url"]:
            RUNTIME_CONFIG["otp_base_url"] = new_url
            changed.append("otp_base_url")
        try:
            if new_mul is not None and int(new_mul) > 0 and int(new_mul) != RUNTIME_CONFIG["price_mul"]:
                RUNTIME_CONFIG["price_mul"] = int(new_mul)
                changed.append("price_mul")
        except (TypeError, ValueError):
            pass
        if changed:
            logging.info("Đã đồng bộ cấu hình từ Firebase settings/config: %s", ", ".join(changed))
    except Exception:
        logging.exception("Lỗi refresh_runtime_config")


async def write_bot_heartbeat():
    """Ghi trạng thái 'bot còn sống' lên Firebase để web admin hiển thị. Cần FIREBASE_SECRET."""
    if not FIREBASE_SECRET:
        return
    try:
        url = f"{FIREBASE_DB_URL}/settings/botStatus.json?auth={FIREBASE_SECRET}"
        payload = {"lastSeen": int(_time.time() * 1000), "online": True}
        async with httpx.AsyncClient(timeout=8) as client:
            await client.put(url, json=payload)
    except Exception:
        logging.exception("Lỗi write_bot_heartbeat")


async def config_refresh_loop():
    """Vòng lặp nền: cập nhật cấu hình + ghi heartbeat mỗi 60s (hot-reload, không cần redeploy)."""
    while True:
        await refresh_runtime_config()
        await write_bot_heartbeat()
        await asyncio.sleep(60)


async def get_fixed_apps_from_api():
    res = await otp_api.get_apps()
    if res.get("ResponseCode") != 0:
        return res

    selected_list = await _get_selected_apps_list()
    api_apps = res.get("Result", [])
    api_map = {int(app["Id"]): app for app in api_apps if "Id" in app}

    filtered_apps = []
    for item in selected_list:
        app_id = int(item["Id"])
        if app_id in api_map:
            api_item = api_map[app_id]
            filtered_apps.append({
                "Id": app_id,
                "Name": item["Name"],
                "Cost": api_item.get("Cost", 0),
                "category": item.get("category", "other") or "other",
            })

    return {
        "ResponseCode": 0,
        "Msg": "OK",
        "Result": filtered_apps
    }

# --- ADMIN / STATS HELPERS ---
def get_balance_history(user_id: int, limit: int = 20):
    conn = db()
    try:
        rows = conn.execute("""
            SELECT change_amount, balance_after, note, created_at
            FROM balance_logs
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT ?
        """, (user_id, limit)).fetchall()
        return rows
    finally:
        conn.close()


def get_revenue_stats():
    conn = db()
    try:
        total_users = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        paid_users = conn.execute("SELECT COUNT(DISTINCT user_id) AS c FROM deposit_orders WHERE status = 'paid'").fetchone()["c"]
        pending_orders = conn.execute("SELECT COUNT(*) AS c FROM deposit_orders WHERE status = 'pending'").fetchone()["c"]
        paid_orders = conn.execute("SELECT COUNT(*) AS c FROM deposit_orders WHERE status = 'paid'").fetchone()["c"]
        total_revenue = conn.execute("SELECT COALESCE(SUM(amount), 0) AS s FROM deposit_orders WHERE status = 'paid'").fetchone()["s"]
        today_revenue = conn.execute("""
            SELECT COALESCE(SUM(amount), 0) AS s
            FROM deposit_orders
            WHERE status = 'paid'
              AND DATE(COALESCE(paid_at, created_at), '+7 hours') = DATE('now', '+7 hours')
        """).fetchone()["s"]
        month_revenue = conn.execute("""
            SELECT COALESCE(SUM(amount), 0) AS s
            FROM deposit_orders
            WHERE status = 'paid'
              AND strftime('%Y-%m', COALESCE(paid_at, created_at), '+7 hours') = strftime('%Y-%m', 'now', '+7 hours')
        """).fetchone()["s"]
        total_referral_paid = conn.execute("SELECT COALESCE(SUM(first_bonus_amount), 0) AS s FROM referrals").fetchone()["s"]
        total_referral_commission = conn.execute("SELECT COALESCE(SUM(commission_amount), 0) AS s FROM referral_commissions").fetchone()["s"]
        return {
            'total_users': int(total_users or 0),
            'paid_users': int(paid_users or 0),
            'pending_orders': int(pending_orders or 0),
            'paid_orders': int(paid_orders or 0),
            'total_revenue': int(total_revenue or 0),
            'today_revenue': int(today_revenue or 0),
            'month_revenue': int(month_revenue or 0),
            'total_referral_paid': int(total_referral_paid or 0),
            'total_referral_commission': int(total_referral_commission or 0),
        }
    finally:
        conn.close()


UI_DIVIDER = "━━━━━━━━━━━━━━━━━━━━"


def format_balance(user_id: int) -> str:
    user = get_user(user_id)
    if user_id == ADMIN_ID:
        return "Không giới hạn"
    return f"{int(user['balance']) if user else 0:,}đ"


def get_latest_otp_safe(user_id: int):
    try:
        rows = get_otp_history(user_id)
        return rows[0] if rows else None
    except Exception:
        logging.exception("Không đọc được giao dịch OTP gần nhất")
        return None


def get_active_otp_count(user_id: int) -> int:
    try:
        return len(get_active_otp_history(user_id))
    except Exception:
        logging.exception("Không đếm được phiên OTP đang chờ")
        return 0


def normalize_search_text(value: str) -> str:
    normalized = unicodedata.normalize("NFD", (value or "").lower().strip())
    return "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")


def main_menu_text(user_id: int, full_name: str) -> str:
    recent = get_latest_otp_safe(user_id)
    recent_text = ""
    if recent:
        recent_text = (
            "\n\n🕘 Gần đây: "
            f"<b>{html.escape(recent['app_name'])}</b> · <code>{recent['phone']}</code>"
        )
    return (
        "⚡ <b>OTP SHOP</b>\n"
        f"{UI_DIVIDER}\n"
        f"Xin chào <b>{html.escape(full_name)}</b> 👋\n\n"
        f"💰 Số dư khả dụng: <b>{format_balance(user_id)}</b>\n"
        f"⏳ OTP đang chờ: <b>{get_active_otp_count(user_id)}</b>\n"
        "🛡 Không nhận được OTP, hệ thống tự hoàn tiền."
        f"{recent_text}\n\n"
        "Bạn muốn làm gì?"
    )


def deposit_prompt_text() -> str:
    return (
        "💳 <b>NẠP TIỀN</b>\n"
        f"{UI_DIVIDER}\n"
        "Chọn nhanh một mệnh giá bên dưới hoặc nhập số tiền bạn muốn nạp.\n\n"
        "• Tối thiểu: <b>10.000đ</b>\n"
        "• Có thể nhập: <code>20000</code>, <code>20.000</code> hoặc <code>20k</code>\n"
        "• Tiền được cộng tự động sau khi ngân hàng xác nhận\n\n"
        "Gửi <code>/cancel</code> để huỷ."
    )


def deposit_prompt_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="20.000đ", callback_data="deposit_quick|20000", style="success"),
            InlineKeyboardButton(text="50.000đ", callback_data="deposit_quick|50000", style="success"),
        ],
        [
            InlineKeyboardButton(text="100.000đ", callback_data="deposit_quick|100000", style="success"),
            InlineKeyboardButton(text="200.000đ", callback_data="deposit_quick|200000", style="success"),
        ],
        [InlineKeyboardButton(text="← Về trang chủ", callback_data="menu")],
    ])


def quick_access_keyboard(use_styles: bool = True):
    def quick_button(text: str, style: str | None = None):
        kwargs = {"text": text}
        if use_styles and style:
            kwargs["style"] = style
        return KeyboardButton(**kwargs)

    return ReplyKeyboardMarkup(
        keyboard=[
            [
                quick_button("🔎 Tìm dịch vụ", "primary"),
                quick_button("⏳ OTP đang chờ", "success"),
            ],
            [
                quick_button("⚡ Thuê OTP", "primary"),
                quick_button("💳 Nạp tiền", "success"),
            ],
            [
                quick_button("🧾 Lịch sử", "primary"),
                quick_button("📥 Đơn nạp", "primary"),
            ],
            [
                quick_button("❤️ Yêu thích", "primary"),
                quick_button("🎁 Nhận thưởng", "success"),
            ],
            [
                quick_button("⌂ Trang chủ", "primary"),
                quick_button("⌨️ Ẩn phím nhanh", "danger"),
            ],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Chọn thao tác nhanh...",
    )


async def send_quick_keyboard(message: Message):
    text = "⌨️ <b>Đã bật phím nhanh</b> · Bạn có thể dùng các nút bên dưới bất cứ lúc nào."
    try:
        await message.answer(text, reply_markup=quick_access_keyboard(use_styles=True))
    except Exception:
        logging.warning("Telegram từ chối màu của phím nhanh; chuyển sang bàn phím mặc định")
        await message.answer(text, reply_markup=quick_access_keyboard(use_styles=False))


def standard_navigation_keyboard(*, include_history: bool = False):
    rows = []
    if include_history:
        rows.append([InlineKeyboardButton(text="🧾 Xem lịch sử", callback_data="otp_history|0", style="primary")])
    rows.append([
        InlineKeyboardButton(text="📱 Thuê số", callback_data="otp_list", style="primary"),
        InlineKeyboardButton(text="⌂ Trang chủ", callback_data="menu"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def deposit_navigation_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 Xem các đơn nạp", callback_data="deposit_orders", style="primary")],
        [
            InlineKeyboardButton(text="⚡ Thuê số", callback_data="otp_list", style="primary"),
            InlineKeyboardButton(text="⌂ Trang chủ", callback_data="menu"),
        ],
    ])


def waiting_otp_keyboard(phone: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="📋 Sao chép số điện thoại",
            copy_text=CopyTextButton(text=str(phone)),
            style="primary"
        )],
        [InlineKeyboardButton(text="🧾 Xem lịch sử", callback_data="otp_history|0", style="primary")],
        [InlineKeyboardButton(text="⌂ Trang chủ", callback_data="menu")],
    ])


def otp_result_keyboard(code: str, phone: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="📋 Sao chép mã OTP",
            copy_text=CopyTextButton(text=str(code)),
            style="success"
        )],
        [InlineKeyboardButton(
            text="📱 Sao chép số điện thoại",
            copy_text=CopyTextButton(text=str(phone)),
            style="primary"
        )],
        [
            InlineKeyboardButton(text="🧾 Lịch sử", callback_data="otp_history|0", style="primary"),
            InlineKeyboardButton(text="⌂ Trang chủ", callback_data="menu"),
        ],
    ])


async def render_screen(message: Message, text: str, reply_markup: InlineKeyboardMarkup):
    """Cập nhật màn hình hiện tại; với tin ảnh thì mở màn hình mới."""
    try:
        if message.text is not None:
            await message.edit_text(text, reply_markup=reply_markup)
        else:
            await message.answer(text, reply_markup=reply_markup)
    except Exception:
        fallback_markup = disable_button_styles(reply_markup)
        if fallback_markup is None:
            raise
        logging.warning("Telegram từ chối kiểu nút màu; đã tự chuyển sang nút mặc định")
        if message.text is not None:
            await message.edit_text(text, reply_markup=fallback_markup)
        else:
            await message.answer(text, reply_markup=fallback_markup)


def disable_button_styles(reply_markup: InlineKeyboardMarkup) -> InlineKeyboardMarkup | None:
    global BUTTON_STYLES_ENABLED
    found_style = False
    fallback_rows = []
    for row in reply_markup.inline_keyboard:
        fallback_row = []
        for button in row:
            data = button.model_dump(exclude_none=True)
            if data.pop("style", None):
                found_style = True
            fallback_row.append(_AiogramInlineKeyboardButton(**data))
        fallback_rows.append(fallback_row)
    if found_style:
        BUTTON_STYLES_ENABLED = False
        return InlineKeyboardMarkup(inline_keyboard=fallback_rows)
    return None


async def send_new_screen(message: Message, text: str, reply_markup: InlineKeyboardMarkup):
    try:
        return await message.answer(text, reply_markup=reply_markup)
    except Exception:
        fallback_markup = disable_button_styles(reply_markup)
        if fallback_markup is None:
            raise
        logging.warning("Telegram từ chối kiểu nút màu; gửi lại trang chủ bằng nút mặc định")
        return await message.answer(text, reply_markup=fallback_markup)


def parse_vnd_amount(value: str) -> int | None:
    raw = (value or "").strip().lower().replace("₫", "").replace("đ", "").replace("vnd", "")
    multiplier = 1000 if raw.endswith("k") else 1
    if multiplier == 1000:
        raw = raw[:-1]
    normalized = raw.replace(".", "").replace(",", "").replace(" ", "").replace("_", "")
    if not normalized.isdigit():
        return None
    return int(normalized) * multiplier


def admin_menu_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="👥 Khách hàng", callback_data="admin_users", style="primary"),
            InlineKeyboardButton(text="📊 Thống kê", callback_data="admin_stats", style="primary")
        ],
        [
            InlineKeyboardButton(text="💰 Số dư khách", callback_data="admin_positive_balance"),
            InlineKeyboardButton(text="💾 Sao lưu", callback_data="admin_backup_menu", style="success")
        ],
        [InlineKeyboardButton(text="🧾 Tra cứu biến động số dư", callback_data="admin_history_help")],
        [InlineKeyboardButton(text="← Về trang chủ", callback_data="menu")]
    ])


def format_stats_text():
    s = get_revenue_stats()
    return (
        "📊 <b>TỔNG QUAN KINH DOANH</b>\n"
        f"{UI_DIVIDER}\n"
        f"👥 Khách hàng: <b>{s['total_users']}</b> · Đã nạp: <b>{s['paid_users']}</b>\n"
        f"✅ Đơn thành công: <b>{s['paid_orders']}</b> · Đang chờ: <b>{s['pending_orders']}</b>\n\n"
        f"Hôm nay: <b>{s['today_revenue']:,}đ</b>\n"
        f"Tháng này: <b>{s['month_revenue']:,}đ</b>\n"
        f"Tổng doanh thu: <b>{s['total_revenue']:,}đ</b>\n\n"
        f"🎁 Thưởng người mới: <b>{s['total_referral_paid']:,}đ</b>\n"
        f"🤝 Hoa hồng giới thiệu: <b>{s['total_referral_commission']:,}đ</b>"
    )

# --- KEYBOARDS ---
def main_menu_keyboard(user_id):
    rows = [
        [InlineKeyboardButton(text="⚡ Thuê số nhận OTP", callback_data="otp_list", style="primary")],
        [
            InlineKeyboardButton(text="🔎 Tìm nhanh", callback_data="search_service", style="primary"),
            InlineKeyboardButton(
                text=f"⏳ OTP đang chờ · {get_active_otp_count(user_id)}",
                callback_data="active_otp",
                style="success"
            ),
        ],
    ]

    recent = get_latest_otp_safe(user_id)
    if recent:
        rows.append([InlineKeyboardButton(
            text=f"↻ Thuê lại {recent['app_name']} · {recent['phone']}",
            callback_data=f"rebuy_preview|{recent['id']}",
            style="success"
        )])

    rows.extend([
        [
            InlineKeyboardButton(text="💳 Nạp tiền", callback_data="deposit", style="success"),
            InlineKeyboardButton(text="📥 Đơn nạp", callback_data="deposit_orders", style="primary"),
        ],
        [
            InlineKeyboardButton(text="🧾 Lịch sử", callback_data="otp_history|0", style="primary"),
            InlineKeyboardButton(text="❤️ Yêu thích", callback_data="otp_fav", style="primary"),
        ],
        [
            InlineKeyboardButton(text="🎁 Nhận thưởng", callback_data="referral_menu", style="success"),
            InlineKeyboardButton(text="💬 Hỗ trợ", callback_data="contact", style="primary"),
        ],
        [
            InlineKeyboardButton(text="↻ Làm mới", callback_data="refresh_bal"),
            InlineKeyboardButton(text="⌨️ Phím nhanh", callback_data="enable_quick_keyboard", style="primary"),
        ],
    ])

    if user_id == ADMIN_ID:
        rows.append([InlineKeyboardButton(text="👑 Trung tâm quản trị", callback_data="admin_menu", style="primary")])

    return InlineKeyboardMarkup(inline_keyboard=rows)

# --- HANDLERS ---
async def reset_bot_on_startup():
    """Đặt menu lệnh gọn cho khách và menu đầy đủ riêng cho admin."""
    try:
        default_scope = BotCommandScopeDefault()
        admin_scope = BotCommandScopeChat(chat_id=ADMIN_ID)
        await bot.delete_my_commands(scope=default_scope)
        await bot.delete_my_commands(scope=admin_scope)
        await bot.set_my_commands([
            BotCommand(command="start", description="Mở trang chủ"),
            BotCommand(command="quick", description="Bật phím nhanh"),
            BotCommand(command="hidequick", description="Ẩn phím nhanh"),
            BotCommand(command="help", description="Hướng dẫn sử dụng"),
        ], scope=default_scope)
        await bot.set_my_commands([
            BotCommand(command="start",       description="Mở trang chủ"),
            BotCommand(command="quick",       description="Bật phím nhanh"),
            BotCommand(command="hidequick",   description="Ẩn phím nhanh"),
            BotCommand(command="help",        description="Hướng dẫn và lệnh quản trị"),
            BotCommand(command="thongbao",    description="Gửi thông báo"),
            BotCommand(command="setnote",     description="Cập nhật ghi chú dịch vụ"),
            BotCommand(command="khachdangdu", description="Khách còn số dư"),
            BotCommand(command="congtien",    description="Cộng tiền cho khách"),
            BotCommand(command="trutien",     description="Trừ tiền của khách"),
        ], scope=admin_scope)
        await bot.set_chat_menu_button(menu_button=MenuButtonDefault())
        logging.info("Đã reset commands và menu button thành công.")
    except Exception:
        logging.exception("Không reset được commands/menu button")


@dp.message(Command("start"))
async def show_menu(m: Message, state: FSMContext):
    await state.clear()
    save_user(m.from_user)

    referral_notice = ""
    referrer_id = extract_referrer_id_from_start(m.text)

    if referrer_id:
        async with BALANCE_LOCK:
            status, referrer_new_balance, first_bonus = register_referral_atomic(
                referrer_id=referrer_id,
                invited_user=m.from_user
            )

        if status == "registered_pending":
            referral_notice = (
                "\n\n🎉 <b>Đã ghi nhận lời mời!</b> "
                f"Người giới thiệu sẽ nhận <b>{REFERRAL_FIRST_BONUS:,}đ + 10%</b> khi bạn nạp lần đầu từ "
                f"<b>{REFERRAL_MIN_DEPOSIT:,}đ</b>."
            )

            try:
                await bot.send_message(
                    referrer_id,
                    "📌 <b>ĐÃ GHI NHẬN 1 REFERRAL MỚI</b>\n\n"
                    f"👤 Người dùng: <b>{html.escape(m.from_user.full_name)}</b>\n"
                    f"🆔 ID: <code>{m.from_user.id}</code>\n\n"
                    f"⏳ Chưa cộng thưởng ngay.\n"
                    f"Người này cần nạp từ <b>{REFERRAL_MIN_DEPOSIT:,}đ</b> trở lên để bạn nhận:\n"
                    f"- Thưởng người mới: <b>{REFERRAL_FIRST_BONUS:,}đ</b>\n"
                    "- Hoa hồng: <b>10%</b> tiền nạp"
                )
            except Exception:
                logging.exception("Không gửi được thông báo referral cho referrer")

            try:
                await bot.send_message(
                    ADMIN_ID,
                    "📣 <b>PHÁT SINH REFERRAL MỚI</b>\n\n"
                    f"👤 Referrer ID: <code>{referrer_id}</code>\n"
                    f"👥 User mới: <b>{html.escape(m.from_user.full_name)}</b>\n"
                    f"🆔 Invited ID: <code>{m.from_user.id}</code>\n"
                    f"🛡 Chế độ chống spam: chỉ trả thưởng khi user nạp từ <b>{REFERRAL_MIN_DEPOSIT:,}đ</b> trở lên."
                )
            except Exception:
                logging.exception("Không gửi được thông báo referral cho admin")

        elif status == "self_ref":
            referral_notice = "\n\n⚠️ Bạn không thể tự dùng link giới thiệu của chính mình."
        elif status == "already_referred":
            referral_notice = "\n\nℹ️ Tài khoản này đã được ghi nhận referral từ trước."
        elif status == "referrer_not_found":
            referral_notice = "\n\nℹ️ Link giới thiệu không hợp lệ."
        else:
            referral_notice = "\n\n⚠️ Có lỗi khi xử lý giới thiệu, vui lòng thử lại."

    try:
        await send_new_screen(
            m,
            main_menu_text(m.from_user.id, m.from_user.full_name) + referral_notice,
            main_menu_keyboard(m.from_user.id)
        )
    except Exception:
        logging.exception("Không hiển thị được trang chủ")
        await m.answer(
            "⚠️ Chưa thể tải đầy đủ menu. Vui lòng thử lại sau ít phút hoặc liên hệ @tai_khoan_xin."
        )


@dp.message(Command("quick"))
async def quick_keyboard_command(m: Message, state: FSMContext):
    await state.clear()
    save_user(m.from_user)
    await send_quick_keyboard(m)


@dp.message(Command("hidequick"))
async def hide_quick_keyboard_command(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Đã ẩn phím nhanh.", reply_markup=ReplyKeyboardRemove())
    await send_new_screen(
        m,
        main_menu_text(m.from_user.id, m.from_user.full_name),
        main_menu_keyboard(m.from_user.id)
    )


@dp.callback_query(F.data == "enable_quick_keyboard")
async def enable_quick_keyboard_callback(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await send_quick_keyboard(c.message)
    await c.answer("Đã bật phím nhanh")


@dp.message(Command("help"))
async def help_command(m: Message, state: FSMContext):
    await state.clear()
    text = (
        "📖 <b>HƯỚNG DẪN NHANH</b>\n"
        f"{UI_DIVIDER}\n"
        "<b>Thuê số:</b> Chọn dịch vụ → chọn nhà mạng hoặc số cụ thể → chờ OTP.\n\n"
        "<b>Tìm nhanh:</b> Gõ tên dịch vụ, có thể nhập không dấu.\n\n"
        "<b>OTP đang chờ:</b> Xem mọi phiên đang chạy và tự làm mới trạng thái.\n\n"
        "<b>Thanh toán:</b> Chọn Nạp tiền → chọn mệnh giá → quét QR. Số dư được cộng tự động.\n\n"
        "<b>Đơn nạp:</b> Theo dõi trạng thái, sao chép nội dung hoặc tạo lại đơn hết hạn.\n\n"
        "<b>Bảo hành:</b> Không có OTP trong thời gian chờ, hệ thống tự hoàn tiền. Mã sai không thuộc chính sách hoàn.\n\n"
        f"<b>Giới thiệu:</b> Nhận <b>{REFERRAL_FIRST_BONUS:,}đ + 10%</b> khi bạn bè nạp lần đầu từ <b>{REFERRAL_MIN_DEPOSIT:,}đ</b>."
    )
    if m.from_user.id == ADMIN_ID:
        text += (
            "\n\n👑 <b>LỆNH QUẢN TRỊ</b>\n"
            "/users · /sodu · /khachdangdu\n"
            "/congtien · /trutien · /setsodu\n"
            "/thongbao · /refstats · /backup\n"
            "/setnote · /delnote · /notes"
        )
    await m.answer(text, reply_markup=main_menu_keyboard(m.from_user.id))

@dp.message(F.text.in_({
    "🏠 Trang chủ", "⌂ Trang chủ",
    "📱 Thuê số OTP", "⚡ Thuê OTP",
    "💳 Nạp tiền",
    "📋 Lịch sử thuê số", "🧾 Lịch sử",
    "🎁 Giới thiệu bạn bè", "🎁 Nhận thưởng",
    "☎️ Hỗ trợ", "💬 Hỗ trợ",
    "💰 Số dư", "❤️ Yêu thích",
    "🔎 Tìm dịch vụ", "⏳ OTP đang chờ", "📥 Đơn nạp",
    "⌨️ Ẩn phím nhanh",
}))
async def reply_kb_handler(m: Message, state: FSMContext):
    save_user(m.from_user)
    text = m.text
    if text != "💳 Nạp tiền":
        await state.clear()
    if text == "🔎 Tìm dịch vụ":
        await state.set_state(SearchServiceState.waiting_for_query)
        await m.answer(
            "🔎 <b>TÌM DỊCH VỤ</b>\n"
            f"{UI_DIVIDER}\n"
            "Nhập tên dịch vụ bạn cần. Có thể tìm không dấu.\n"
            "Ví dụ: <code>Facebook</code>, <code>gmail</code>, <code>shopee</code>."
        )
    elif text == "⏳ OTP đang chờ":
        await m.answer(
            "⏳ <b>TRUNG TÂM OTP</b>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="⏳ Mở OTP đang chờ", callback_data="active_otp", style="success")
            ]])
        )
    elif text == "📥 Đơn nạp":
        await m.answer(
            "📥 <b>QUẢN LÝ ĐƠN NẠP</b>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="📥 Xem các đơn nạp", callback_data="deposit_orders", style="primary")
            ]])
        )
    elif text in {"🏠 Trang chủ", "⌂ Trang chủ"}:
        await send_new_screen(
            m,
            main_menu_text(m.from_user.id, m.from_user.full_name),
            main_menu_keyboard(m.from_user.id)
        )
    elif text in {"📱 Thuê số OTP", "⚡ Thuê OTP"}:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⚡ Chọn dịch vụ", callback_data="otp_list", style="primary")
        ]])
        await m.answer("📱 <b>THUÊ SỐ NHẬN OTP</b>\nChọn dịch vụ để bắt đầu.", reply_markup=kb)
    elif text == "💳 Nạp tiền":
        await state.set_state(DepositState.waiting_for_amount)
        await m.answer(deposit_prompt_text(), reply_markup=deposit_prompt_keyboard())
    elif text in {"📋 Lịch sử thuê số", "🧾 Lịch sử"}:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="📋 Xem lịch sử", callback_data="otp_history|0")
        ]])
        await m.answer("🧾 <b>LỊCH SỬ GIAO DỊCH</b>\nXem lại và thuê lại số đã dùng.", reply_markup=kb)
    elif text in {"🎁 Giới thiệu bạn bè", "🎁 Nhận thưởng"}:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🎁 Xem chi tiết", callback_data="referral_menu")
        ]])
        await m.answer("🎁 <b>GIỚI THIỆU & NHẬN THƯỞNG</b>", reply_markup=kb)
    elif text == "❤️ Yêu thích":
        await m.answer(
            "❤️ <b>DỊCH VỤ YÊU THÍCH</b>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="❤️ Mở danh sách yêu thích", callback_data="otp_fav", style="primary")
            ]])
        )
    elif text in {"☎️ Hỗ trợ", "💬 Hỗ trợ"}:
        await m.answer(
            "💬 <b>TRUNG TÂM HỖ TRỢ</b>\n"
            f"{UI_DIVIDER}\n"
            "Admin: @tai_khoan_xin\n"
            "Hoạt động hàng ngày · 08:00–22:00"
        )
    elif text == "💰 Số dư":
        await m.answer(
            "💰 <b>SỐ DƯ KHẢ DỤNG</b>\n"
            f"{UI_DIVIDER}\n"
            f"<b>{format_balance(m.from_user.id)}</b>",
            reply_markup=main_menu_keyboard(m.from_user.id)
        )
    elif text == "⌨️ Ẩn phím nhanh":
        await m.answer("Đã ẩn phím nhanh.", reply_markup=ReplyKeyboardRemove())
        await send_new_screen(
            m,
            main_menu_text(m.from_user.id, m.from_user.full_name),
            main_menu_keyboard(m.from_user.id)
        )


@dp.callback_query(F.data == "noop_bal")
async def noop_bal(c: CallbackQuery):
    await c.answer(f"💰 Số dư: {format_balance(c.from_user.id)}", show_alert=False)

@dp.callback_query(F.data == "refresh_bal")
async def refresh_bal(c: CallbackQuery):
    save_user(c.from_user)
    try:
        await c.message.edit_text(
            main_menu_text(c.from_user.id, c.from_user.full_name),
            reply_markup=main_menu_keyboard(c.from_user.id)
        )
    except Exception:
        pass
    await c.answer(f"Đã cập nhật · {format_balance(c.from_user.id)}")

@dp.callback_query(F.data == "contact")
async def contact_callback(c: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Nhắn admin ngay", url="https://t.me/tai_khoan_xin")],
        [InlineKeyboardButton(text="← Về trang chủ", callback_data="menu")],
    ])
    await c.message.edit_text(
        "💬 <b>TRUNG TÂM HỖ TRỢ</b>\n"
        f"{UI_DIVIDER}\n"
        "Bạn cần trợ giúp về giao dịch hay OTP?\n\n"
        "👤 Admin: <b>@tai_khoan_xin</b>\n"
        "🕐 Thời gian: <b>08:00–22:00</b> hàng ngày\n\n"
        "Khi liên hệ, hãy gửi kèm <b>mã đơn</b> hoặc <b>số điện thoại đã thuê</b> để được xử lý nhanh hơn.",
        reply_markup=kb
    )
    await c.answer()

@dp.callback_query(F.data == "referral_menu")
async def referral_menu_callback(c: CallbackQuery):
    save_user(c.from_user)

    total_invited, total_bonus = get_referral_stats(c.from_user.id)
    ref_link = await build_referral_link(c.from_user.id)
    share_text = "Mời bạn dùng OTP SHOP để thuê số nhận mã nhanh:"
    share_url = (
        "https://t.me/share/url"
        f"?url={quote(ref_link, safe='')}"
        f"&text={quote(share_text, safe='')}"
    )

    text = (
        "🎁 <b>GIỚI THIỆU & NHẬN THƯỞNG</b>\n"
        f"{UI_DIVIDER}\n"
        f"Bạn nhận <b>{REFERRAL_FIRST_BONUS:,}đ + 10%</b> khi bạn bè nạp lần đầu từ "
        f"<b>{REFERRAL_MIN_DEPOSIT:,}đ</b>. Các lần nạp sau bạn tiếp tục nhận <b>10%</b>.\n\n"
        f"👥 Bạn đã mời: <b>{total_invited} người</b>\n"
        f"💰 Tổng thưởng: <b>{total_bonus:,}đ</b>\n\n"
        "🔗 <b>Link cá nhân</b> · chạm để sao chép\n"
        f"<code>{html.escape(ref_link)}</code>"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↗️ Chia sẻ với bạn bè", url=share_url)],
        [InlineKeyboardButton(text="← Về trang chủ", callback_data="menu")],
    ])

    await c.message.edit_text(text, reply_markup=kb)
    await c.answer()

@dp.callback_query(F.data == "admin_menu")
async def admin_menu_callback(c: CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        return await c.answer("❌ Bạn không có quyền!", show_alert=True)
    await c.message.edit_text(
        "👑 <b>TRUNG TÂM QUẢN TRỊ</b>\n"
        f"{UI_DIVIDER}\n"
        "Theo dõi hoạt động, khách hàng và dữ liệu hệ thống.",
        reply_markup=admin_menu_keyboard()
    )
    await c.answer()


@dp.callback_query(F.data == "admin_stats")
async def admin_stats_callback(c: CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        return await c.answer("❌ Bạn không có quyền!", show_alert=True)
    await c.message.edit_text(format_stats_text(), reply_markup=admin_menu_keyboard())
    await c.answer()


@dp.callback_query(F.data == "admin_users")
async def admin_users_callback(c: CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        return await c.answer("❌ Bạn không có quyền!", show_alert=True)

    conn = db()
    try:
        users = conn.execute("""
            SELECT * FROM users
            ORDER BY user_id DESC
        """).fetchall()
    finally:
        conn.close()

    if not users:
        await c.message.edit_text("📭 Trống.", reply_markup=admin_menu_keyboard())
        return await c.answer()

    header = f"👥 <b>TỔNG SỐ NGƯỜI DÙNG:</b> <b>{len(users)}</b>\n\n"
    chunks = []
    current = header

    for i, u in enumerate(users, 1):
        full_name = html.escape(u["full_name"] or "Không rõ tên")
        username = f"@{html.escape(u['username'])}" if u["username"] else "không username"
        line = (
            f"{i}. {full_name} | {username} | "
            f"ID: <code>{u['user_id']}</code> | "
            f"Số dư: <b>{int(u['balance']):,}đ</b>\n"
        )
        if len(current) + len(line) > 3500:
            chunks.append(current)
            current = line
        else:
            current += line

    if current.strip():
        chunks.append(current)

    await c.message.edit_text(chunks[0], reply_markup=admin_menu_keyboard())
    for idx, chunk in enumerate(chunks[1:], 2):
        await c.message.answer(f"<b>📄 Trang {idx}/{len(chunks)}</b>\n\n{chunk}")
    await c.answer()


@dp.callback_query(F.data == "admin_positive_balance")
async def admin_positive_balance_callback(c: CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        return await c.answer("❌ Bạn không có quyền!", show_alert=True)
    users = get_users_with_balance()
    if not users:
        await c.message.edit_text("Không có khách nào dư tiền.", reply_markup=admin_menu_keyboard())
        return await c.answer()
    res = ["💰 <b>KHÁCH CÒN DƯ TIỀN</b>"]
    for u in users[:100]:
        full_name = html.escape(u['full_name'] or 'Không rõ tên')
        res.append(f"- {full_name}: {int(u['balance']):,}đ | ID <code>{u['user_id']}</code>")
    if len(users) > 100:
        res.append(f"\n... và còn <b>{len(users) - 100}</b> khách nữa")
    await c.message.edit_text("\n".join(res), reply_markup=admin_menu_keyboard())
    await c.answer()


@dp.callback_query(F.data == "admin_backup_menu")
async def admin_backup_menu_callback(c: CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        return await c.answer("❌ Bạn không có quyền!", show_alert=True)

    db_path = Path(DB_NAME)
    if not db_path.exists():
        return await c.answer("❌ Không tìm thấy file database.", show_alert=True)

    try:
        file_size = db_path.stat().st_size
        time_text = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        backup_file = FSInputFile(str(db_path))
        await bot.send_document(
            chat_id=ADMIN_ID,
            document=backup_file,
            caption=(
                "✅ <b>BACKUP DATABASE THÀNH CÔNG</b>\n\n"
                f"📁 Tên file: <b>{html.escape(db_path.name)}</b>\n"
                f"📦 Dung lượng: <b>{file_size:,} bytes</b>\n"
                f"🕒 Thời gian: <b>{time_text}</b>\n"
                f"📂 Đường dẫn: <code>{html.escape(str(db_path))}</code>"
            )
        )
        await c.answer("Đã gửi file backup về Telegram admin")
    except Exception:
        logging.exception("Lỗi backup database từ menu admin")
        await c.answer("❌ Backup thất bại", show_alert=True)


@dp.callback_query(F.data == "admin_history_help")
async def admin_history_help_callback(c: CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        return await c.answer("❌ Bạn không có quyền!", show_alert=True)
    await c.message.edit_text(
        "🧾 <b>XEM LỊCH SỬ SỐ DƯ</b>\n\n"
        "Dùng lệnh:\n"
        "<code>/lichsu [user_id]</code>\n\n"
        "Ví dụ:\n"
        "<code>/lichsu 123456789</code>",
        reply_markup=admin_menu_keyboard()
    )
    await c.answer()


# --- ADMIN HANDLERS ---
@dp.message(Command("lichsu"))
async def admin_balance_history(m: Message):
    if m.from_user.id != ADMIN_ID:
        return await m.answer("❌ Bạn không có quyền!")

    parts = m.text.split()
    if len(parts) < 2:
        return await m.answer("Sử dụng: /lichsu [user_id]")

    try:
        user_id = int(parts[1])
    except Exception:
        return await m.answer("❌ user_id phải là số.")

    user = get_user(user_id)
    if not user:
        return await m.answer("❌ Không tìm thấy user này.")

    rows = get_balance_history(user_id, limit=20)
    if not rows:
        return await m.answer(
            f"🧾 <b>LỊCH SỬ SỐ DƯ</b>\n\n"
            f"👤 User: <b>{html.escape(user['full_name'] or 'Không rõ')}</b>\n"
            f"🆔 ID: <code>{user_id}</code>\n\n"
            "Chưa có biến động số dư nào."
        )

    lines = [
        "🧾 <b>LỊCH SỬ BIẾN ĐỘNG SỐ DƯ</b>",
        f"👤 User: <b>{html.escape(user['full_name'] or 'Không rõ')}</b>",
        f"🆔 ID: <code>{user_id}</code>",
        ""
    ]

    for i, row in enumerate(rows, 1):
        change_amount = int(row["change_amount"] or 0)
        balance_after = int(row["balance_after"] or 0)
        note = html.escape(row["note"] or "Không có ghi chú")

        sign = "+" if change_amount >= 0 else ""
        lines.append(
            f"{i}. Biến động: <b>{sign}{change_amount:,}đ</b>\n"
            f"   Số dư sau: <b>{balance_after:,}đ</b>\n"
            f"   Ghi chú: {note}\n"
            f"   Thời gian: {row['created_at']}\n"
        )

    await m.answer("\n".join(lines))

@dp.message(Command("refreshapps"))
async def cmd_refresh_apps(m: Message):
    if m.from_user.id != ADMIN_ID:
        return await m.answer("❌ Bạn không có quyền!")
    global _apps_cache, _apps_cache_ts
    _apps_cache = []
    _apps_cache_ts = 0.0
    # Tải lại ngay để kiểm tra
    apps = await _get_selected_apps_list()
    await m.answer(f"✅ Đã làm mới cache. Hiện đang dùng <b>{len(apps)}</b> app OTP từ Firebase.", parse_mode="HTML")

@dp.message(Command("users"))
async def admin_list_users(m: Message):
    if m.from_user.id != ADMIN_ID:
        return await m.answer("❌ Bạn không có quyền!")
    users = db().execute("SELECT * FROM users").fetchall()
    if not users:
        return await m.answer("📭 Trống.")
    lines = ["👥 <b>DANH SÁCH NGƯỜI DÙNG</b>\n"]
    for i, u in enumerate(users, 1):
        lines.append(f"{i}. {u['full_name']} (ID: <code>{u['user_id']}</code>) - <b>{u['balance']:,}đ</b>")
    await m.answer("\n".join(lines))

@dp.message(Command("backup"))
async def admin_backup_db(m: Message):
    if m.from_user.id != ADMIN_ID:
        return await m.answer("❌ Bạn không có quyền!")

    db_path = Path(DB_NAME)

    if not db_path.exists():
        return await m.answer(
            "❌ Không tìm thấy file database.\n"
            f"📂 Đường dẫn hiện tại: <code>{html.escape(str(db_path))}</code>"
        )

    try:
        file_size = db_path.stat().st_size
        time_text = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        backup_file = FSInputFile(str(db_path))

        await bot.send_document(
            chat_id=ADMIN_ID,
            document=backup_file,
            caption=(
                "✅ <b>BACKUP DATABASE THÀNH CÔNG</b>\n\n"
                f"📁 Tên file: <b>{html.escape(db_path.name)}</b>\n"
                f"📦 Dung lượng: <b>{file_size:,} bytes</b>\n"
                f"🕒 Thời gian: <b>{time_text}</b>\n"
                f"📂 Đường dẫn: <code>{html.escape(str(db_path))}</code>"
            )
        )

        await m.answer("✅ Bot đã gửi file shop_bot.db về Telegram admin.")
    except Exception as e:
        logging.exception("Lỗi backup database")
        await m.answer(
            "❌ Backup thất bại.\n"
            f"Lỗi: <code>{html.escape(str(e))}</code>"
        )

@dp.message(Command("thongbao"))
async def admin_broadcast(m: Message):
    await _do_admin_broadcast(m)

@dp.message(F.photo, F.caption.startswith("/thongbao"))
async def admin_broadcast_photo(m: Message):
    await _do_admin_broadcast(m)

async def _do_admin_broadcast(m: Message):
    if m.from_user.id != ADMIN_ID:
        return await m.answer("❌ Bạn không có quyền!")

    caption_text = (m.caption or m.text or "").replace("/thongbao", "", 1).strip()
    photo_file_id = None

    # Cách 1: Admin gửi ảnh trực tiếp kèm caption /thongbao [nội dung]
    if m.photo:
        photo_file_id = m.photo[-1].file_id  # Lấy ảnh chất lượng cao nhất

    # Cách 2: Admin reply một tin nhắn ảnh rồi gõ /thongbao [nội dung]
    elif m.reply_to_message and m.reply_to_message.photo:
        photo_file_id = m.reply_to_message.photo[-1].file_id
        if not caption_text and m.reply_to_message.caption:
            caption_text = m.reply_to_message.caption

    # Kiểm tra có nội dung không
    if not caption_text and not photo_file_id:
        return await m.answer(
            "📌 <b>HƯỚNG DẪN GỬI THÔNG BÁO</b>\n\n"
            "1️⃣ <b>Chỉ text:</b> /thongbao [nội dung]\n"
            "2️⃣ <b>Ảnh + text:</b> Gửi ảnh, ghi caption là /thongbao [nội dung]\n"
            "3️⃣ <b>Reply ảnh:</b> Reply một ảnh rồi gõ /thongbao [nội dung]"
        )

    broadcast_caption = f"🔔 <b>THÔNG BÁO</b>\n\n{caption_text}" if caption_text else "🔔 <b>THÔNG BÁO</b>"

    users = db().execute("SELECT user_id FROM users").fetchall()
    sent = 0
    for u in users:
        try:
            if photo_file_id:
                await bot.send_photo(
                    u['user_id'],
                    photo=photo_file_id,
                    caption=broadcast_caption
                )
            else:
                await bot.send_message(u['user_id'], broadcast_caption)
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass

    mode = "ảnh + text" if photo_file_id else "text"
    await m.answer(f"✅ Đã gửi thông báo ({mode}) tới {sent} người.")

@dp.message(Command("sodu"))
async def admin_check_one_balance(m: Message):
    if m.from_user.id != ADMIN_ID:
        return await m.answer("❌ Bạn không có quyền!")
    parts = m.text.split()
    if len(parts) < 2:
        return await m.answer("Sử dụng: /sodu [user_id]")

    try:
        user_id = int(parts[1])
    except Exception:
        return await m.answer("❌ user_id phải là số.")

    user = get_user(user_id)
    if not user:
        return await m.answer("Không tìm thấy.")

    balance = get_balance(user_id)
    await m.answer(f"👤 {user['full_name']}\n💰 Số dư: <b>{balance:,}đ</b>")

@dp.message(Command("khachdangdu"))
async def admin_list_positive_balance(m: Message):
    if m.from_user.id != ADMIN_ID:
        return await m.answer("❌ Bạn không có quyền!")
    users = get_users_with_balance()
    if not users:
        return await m.answer("Không có khách nào dư tiền.")
    res = ["💰 <b>KHÁCH CÒN DƯ TIỀN</b>"]
    for u in users:
        res.append(f"- {u['full_name']}: {u['balance']:,}đ")
    await m.answer("\n".join(res))

@dp.message(Command("refstats"))
async def admin_refstats(m: Message):
    if m.from_user.id != ADMIN_ID:
        return await m.answer("❌ Bạn không có quyền!")

    parts = m.text.split()
    if len(parts) < 2:
        return await m.answer("Sử dụng: /refstats [user_id]")

    try:
        user_id = int(parts[1])
    except Exception:
        return await m.answer("❌ user_id phải là số.")

    user = get_user(user_id)
    if not user:
        return await m.answer("❌ Không tìm thấy user này.")

    total_invited, total_bonus = get_referral_stats(user_id)
    history = get_referral_history(user_id, limit=20)
    commission_history = get_referral_commission_history(user_id, limit=20)

    lines = [
        "🎁 <b>THỐNG KÊ REFERRAL</b>",
        f"👤 User: <b>{html.escape(user['full_name'] or 'Không rõ')}</b>",
        f"🆔 ID: <code>{user_id}</code>",
        f"👥 Tổng số người đã giới thiệu: <b>{total_invited}</b>",
        f"💰 Tổng thưởng + hoa hồng đã nhận: <b>{total_bonus:,}đ</b>",
        "",
        "<b>🕒 20 user được giới thiệu gần nhất:</b>"
    ]

    if not history:
        lines.append("Chưa có referral nào.")
    else:
        for i, row in enumerate(history, 1):
            invited_name = row["invited_full_name"] or "Không rõ tên"
            invited_username = f"@{row['invited_username']}" if row["invited_username"] else "không username"
            first_bonus_amount = int(row["first_bonus_amount"]) if row["first_bonus_amount"] else 0
            lines.append(
                f"{i}. {html.escape(invited_name)} | {invited_username} | "
                f"ID <code>{row['invited_user_id']}</code> | "
                f"Thưởng mới <b>{first_bonus_amount:,}đ</b> | {row['created_at']}"
            )

    lines.append("")
    lines.append("<b>💸 20 lượt hoa hồng gần nhất:</b>")

    if not commission_history:
        lines.append("Chưa có hoa hồng nào.")
    else:
        for i, row in enumerate(commission_history, 1):
            lines.append(
                f"{i}. User <code>{row['invited_user_id']}</code> nạp <b>{int(row['deposit_amount']):,}đ</b> | "
                f"HH <b>{int(row['commission_amount']):,}đ</b> | "
                f"{row['created_at']}"
            )

    await m.answer("\n".join(lines))

@dp.message(Command("setnote"))
async def admin_set_note(m: Message):
    if m.from_user.id != ADMIN_ID:
        return await m.answer("❌ Bạn không có quyền!")
    raw = m.text.replace("/setnote", "", 1).strip()
    if "|" not in raw:
        return await m.answer("Sử dụng: /setnote app | nội dung")
    kw, nt = raw.split("|", 1)
    set_app_note(kw, nt)
    await m.answer("✅ Đã lưu.")

@dp.message(Command("delnote"))
async def admin_delete_note(m: Message):
    if m.from_user.id != ADMIN_ID:
        return await m.answer("❌ Bạn không có quyền!")
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2:
        return await m.answer("Sử dụng: /delnote keyword")
    if delete_app_note(parts[1]):
        await m.answer("✅ Đã xóa.")
    else:
        await m.answer("❌ Không tìm thấy.")

@dp.message(Command("notes"))
async def admin_list_notes(m: Message):
    if m.from_user.id != ADMIN_ID:
        return await m.answer("❌ Bạn không có quyền!")
    rows = get_all_app_notes()
    if not rows:
        return await m.answer("Trống.")
    res = ["📝 <b>DANH SÁCH GHI CHÚ</b>"]
    for r in rows:
        res.append(f"- <code>{r['keyword']}</code>: {r['note']}")
    await m.answer("\n".join(res))

@dp.message(Command("congtien"))
async def admin_add_balance(m: Message):
    if m.from_user.id != ADMIN_ID:
        return await m.answer("❌ Bạn không có quyền!")

    parts = m.text.split()
    if len(parts) < 3:
        return await m.answer("Sử dụng: /congtien [user_id] [so_tien]")

    try:
        user_id = int(parts[1])
        amount = int(parts[2])
    except Exception:
        return await m.answer("❌ User ID và số tiền phải là số.")

    if amount <= 0:
        return await m.answer("❌ Số tiền phải lớn hơn 0.")

    async with BALANCE_LOCK:
        new_balance = update_balance(
            user_id,
            amount,
            note=f"Admin cộng tiền bởi {m.from_user.id}"
        )

    if new_balance is None:
        return await m.answer("❌ Không cộng được số dư.")

    await m.answer(
        f"✅ Đã cộng <b>{amount:,}đ</b> cho user <code>{user_id}</code>\n"
        f"💰 Số dư mới: <b>{new_balance:,}đ</b>"
    )

    try:
        await bot.send_message(
            user_id,
            f"💰 Admin vừa cộng thêm <b>{amount:,}đ</b> cho bạn.\n"
            f"💳 Số dư hiện tại: <b>{new_balance:,}đ</b>"
        )
    except Exception:
        logging.exception("Không gửi được thông báo cộng tiền cho khách")

@dp.message(Command("trutien"))
async def admin_sub_balance(m: Message):
    if m.from_user.id != ADMIN_ID:
        return await m.answer("❌ Bạn không có quyền!")

    parts = m.text.split()
    if len(parts) < 3:
        return await m.answer("Sử dụng: /trutien [user_id] [so_tien]")

    try:
        user_id = int(parts[1])
        amount = int(parts[2])
    except Exception:
        return await m.answer("❌ User ID và số tiền phải là số.")

    if amount <= 0:
        return await m.answer("❌ Số tiền phải lớn hơn 0.")

    async with BALANCE_LOCK:
        current_balance = get_balance(user_id)
        if amount > current_balance:
            return await m.answer(
                f"❌ Không thể trừ {amount:,}đ vì khách chỉ còn {current_balance:,}đ."
            )

        new_balance = update_balance(
            user_id,
            -amount,
            note=f"Admin trừ tiền bởi {m.from_user.id}"
        )

    if new_balance is None:
        return await m.answer("❌ Không trừ được số dư.")

    await m.answer(
        f"✅ Đã trừ <b>{amount:,}đ</b> của user <code>{user_id}</code>\n"
        f"💰 Số dư mới: <b>{new_balance:,}đ</b>"
    )

    try:
        await bot.send_message(
            user_id,
            f"💸 Admin vừa trừ <b>{amount:,}đ</b> khỏi số dư của bạn.\n"
            f"💳 Số dư hiện tại: <b>{new_balance:,}đ</b>"
        )
    except Exception:
        logging.exception("Không gửi được thông báo trừ tiền cho khách")

@dp.message(Command("setsodu"))
async def admin_set_user_balance(m: Message):
    if m.from_user.id != ADMIN_ID:
        return await m.answer("❌ Bạn không có quyền!")

    parts = m.text.split()
    if len(parts) < 3:
        return await m.answer("Sử dụng: /setsodu [user_id] [so_du_moi]")

    try:
        user_id = int(parts[1])
        new_balance_input = int(parts[2])
    except Exception:
        return await m.answer("❌ User ID và số dư phải là số.")

    if new_balance_input < 0:
        return await m.answer("❌ Số dư không được âm.")

    async with BALANCE_LOCK:
        final_balance = set_balance(
            user_id,
            new_balance_input,
            note=f"Admin đặt số dư bởi {m.from_user.id}"
        )

    if final_balance is None:
        return await m.answer("❌ Không đặt được số dư.")

    await m.answer(
        f"✅ Đã đặt số dư user <code>{user_id}</code> thành <b>{final_balance:,}đ</b>"
    )

    try:
        await bot.send_message(
            user_id,
            f"💳 Admin vừa cập nhật số dư của bạn.\n"
            f"💰 Số dư hiện tại: <b>{final_balance:,}đ</b>"
        )
    except Exception:
        logging.exception("Không gửi được thông báo set số dư cho khách")

# --- XỬ LÝ NẠP TIỀN ---
@dp.callback_query(F.data == "deposit")
async def deposit_start(c: CallbackQuery, state: FSMContext):
    await state.set_state(DepositState.waiting_for_amount)
    await c.message.edit_text(deposit_prompt_text(), reply_markup=deposit_prompt_keyboard())
    await c.answer()


async def send_deposit_checkout(message: Message, user, amount: int, loading_msg: Message | None = None):
    expire_old_pending_orders()

    memo = f"NAP{user.id}_{int(datetime.now().timestamp() * 1000)}"
    order_id = create_deposit_order(user.id, amount, memo)

    qr_url = (
        "https://img.vietqr.io/image/"
        f"{BANK_BIN}-{BANK_ACCOUNT}-compact2.jpg"
        f"?amount={amount}&addInfo={quote(memo)}&accountName={quote(ACCOUNT_NAME)}"
    )

    customer_caption = (
        "💳 <b>QUÉT QR ĐỂ NẠP TIỀN</b>\n"
        f"{UI_DIVIDER}\n"
        f"Số tiền: <b>{amount:,}đ</b>\n"
        f"Ngân hàng: <b>MB Bank</b>\n"
        f"Số tài khoản: <code>{BANK_ACCOUNT}</code>\n"
        f"Chủ tài khoản: <b>{ACCOUNT_NAME}</b>\n"
        f"Nội dung: <code>{memo}</code>\n"
        f"Mã đơn: <code>{order_id}</code>\n\n"
        "1️⃣ Quét QR bằng ứng dụng ngân hàng\n"
        "2️⃣ Giữ nguyên số tiền và nội dung\n"
        "3️⃣ Chờ bot tự cộng số dư\n\n"
        f"⏱ QR hết hạn sau <b>{QR_EXPIRE_MINUTES} phút</b>."
    )

    admin_keyboard = InlineKeyboardMarkup(inline_keyboard=[[ 
        InlineKeyboardButton(text="✓ Duyệt thủ công", callback_data=f"admin_approve|{order_id}", style="success"),
        InlineKeyboardButton(text="✕ Từ chối", callback_data=f"admin_reject|{order_id}", style="danger")
    ]])

    admin_caption = (
        "💳 <b>YÊU CẦU NẠP TIỀN MỚI</b>\n"
        f"{UI_DIVIDER}\n"
        f"Mã đơn: <code>{order_id}</code>\n"
        f"Khách: <b>{html.escape(user.full_name)}</b>\n"
        f"ID: <code>{user.id}</code>\n"
        f"Số tiền: <b>{amount:,}đ</b>\n"
        f"Nội dung: <code>{memo}</code>\n"
        f"Hết hạn: <b>{QR_EXPIRE_MINUTES} phút</b>\n\n"
        "Đơn đang chờ SePay tự động xác nhận."
    )

    if loading_msg is None:
        loading_msg = await message.answer("⏳ Đang tạo mã thanh toán an toàn...")
    else:
        await loading_msg.edit_text("⏳ Đang tạo mã thanh toán an toàn...")

    try:
        final_img = await build_qr_on_paper_image(qr_url)
        await message.answer_photo(
            photo=final_img,
            caption=customer_caption,
            reply_markup=deposit_navigation_keyboard()
        )
        await loading_msg.delete()
    except Exception:
        logging.exception("Lỗi tạo ảnh QR thanh toán")
        await loading_msg.edit_text(
            "⚠️ <b>KHÔNG TẢI ĐƯỢC ẢNH QR</b>\n"
            f"{UI_DIVIDER}\n"
            "Bạn vẫn có thể chuyển khoản thủ công bằng thông tin sau:\n\n"
            f"Số tiền: <b>{amount:,}đ</b>\n"
            f"Số tài khoản: <code>{BANK_ACCOUNT}</code>\n"
            f"Chủ tài khoản: <b>{ACCOUNT_NAME}</b>\n"
            f"Nội dung: <code>{memo}</code>\n"
            f"Mã đơn: <code>{order_id}</code>\n\n"
            f"⏱ Đơn hết hạn sau <b>{QR_EXPIRE_MINUTES} phút</b>.",
            reply_markup=deposit_navigation_keyboard()
        )

    try:
        await bot.send_message(ADMIN_ID, admin_caption, reply_markup=admin_keyboard)
    except Exception:
        logging.exception("Không gửi được thông báo duyệt nạp tiền cho admin")

    asyncio.create_task(auto_expire_deposit_order_later(order_id, user.id, amount, memo))


@dp.callback_query(F.data.startswith("deposit_quick|"))
async def deposit_quick_amount(c: CallbackQuery, state: FSMContext):
    try:
        amount = int(c.data.split("|", 1)[1])
    except Exception:
        return await c.answer("Mệnh giá không hợp lệ.", show_alert=True)
    if amount < 10000:
        return await c.answer("Số tiền nạp tối thiểu là 10.000đ.", show_alert=True)

    await state.clear()
    await c.answer(f"Đã chọn {amount:,}đ")
    await send_deposit_checkout(c.message, c.from_user, amount, loading_msg=c.message)


@dp.message(DepositState.waiting_for_amount)
async def deposit_amount_received(m: Message, state: FSMContext):
    if m.text and m.text.strip().lower() == "/cancel":
        await state.clear()
        return await m.answer(
            "Đã huỷ yêu cầu nạp tiền.",
            reply_markup=main_menu_keyboard(m.from_user.id)
        )

    amount = parse_vnd_amount(m.text or "")
    if amount is None:
        return await m.answer(
            "Mình chưa đọc được số tiền. Hãy nhập như <code>20000</code>, <code>20.000</code> hoặc <code>20k</code>.\n"
            "Gửi <code>/cancel</code> để huỷ.",
            reply_markup=deposit_prompt_keyboard()
        )

    if amount < 10000:
        return await m.answer(
            "⚠️ Số tiền nạp tối thiểu là <b>10.000đ</b>. Vui lòng chọn hoặc nhập mệnh giá lớn hơn.",
            reply_markup=deposit_prompt_keyboard()
        )

    await state.clear()
    await send_deposit_checkout(m, m.from_user, amount)


DEPOSIT_STATUS_UI = {
    "pending": ("⏳ Đang chờ", "primary"),
    "paid": ("✅ Thành công", "success"),
    "expired": ("⌛ Hết hạn", "danger"),
    "rejected": ("✕ Đã huỷ", "danger"),
    "cancelled": ("✕ Bạn đã huỷ", "danger"),
}


@dp.callback_query(F.data == "deposit_orders")
async def deposit_orders_callback(c: CallbackQuery):
    expire_old_pending_orders()
    rows = get_user_deposit_orders(c.from_user.id)
    if not rows:
        await render_screen(
            c.message,
            "📥 <b>ĐƠN NẠP TIỀN</b>\n"
            f"{UI_DIVIDER}\n"
            "Bạn chưa tạo đơn nạp tiền nào.",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💳 Tạo đơn nạp", callback_data="deposit", style="success")],
                [InlineKeyboardButton(text="⌂ Trang chủ", callback_data="menu")],
            ])
        )
        return await c.answer()

    btns = []
    for row in rows:
        status_text, status_style = DEPOSIT_STATUS_UI.get(row["status"], (row["status"], "primary"))
        btns.append([InlineKeyboardButton(
            text=f"{status_text} · #{row['id']} · {int(row['amount']):,}đ",
            callback_data=f"deposit_order|{row['id']}",
            style=status_style
        )])
    btns.append([InlineKeyboardButton(text="💳 Tạo đơn nạp mới", callback_data="deposit", style="success")])
    btns.append([InlineKeyboardButton(text="⌂ Trang chủ", callback_data="menu")])
    await render_screen(
        c.message,
        "📥 <b>ĐƠN NẠP TIỀN</b>\n"
        f"{UI_DIVIDER}\n"
        f"Hiển thị <b>{len(rows)}</b> đơn gần nhất · chọn một đơn để xem chi tiết:",
        InlineKeyboardMarkup(inline_keyboard=btns)
    )
    await c.answer()


@dp.callback_query(F.data.startswith("deposit_order|"))
async def deposit_order_detail_callback(c: CallbackQuery):
    try:
        order_id = int(c.data.split("|", 1)[1])
    except Exception:
        return await c.answer("Mã đơn không hợp lệ.", show_alert=True)

    expire_old_pending_orders()
    order = get_deposit_order_by_id(order_id)
    if not order or int(order["user_id"]) != c.from_user.id:
        return await c.answer("Không tìm thấy đơn nạp này.", show_alert=True)

    status_text, _ = DEPOSIT_STATUS_UI.get(order["status"], (order["status"], "primary"))
    lines = [
        "📥 <b>CHI TIẾT ĐƠN NẠP</b>",
        UI_DIVIDER,
        f"Mã đơn: <code>{order_id}</code>",
        f"Số tiền: <b>{int(order['amount']):,}đ</b>",
        f"Nội dung: <code>{order['memo']}</code>",
        f"Trạng thái: <b>{status_text}</b>",
        f"Tạo lúc: <b>{order['created_at']}</b>",
    ]
    btns = []
    if order["status"] == "pending":
        try:
            created_at = datetime.strptime(order["created_at"], "%Y-%m-%d %H:%M:%S")
            remaining = max(0, QR_EXPIRE_MINUTES * 60 - int((datetime.utcnow() - created_at).total_seconds()))
            lines.append(f"Còn hiệu lực: <b>{remaining // 60:02d}:{remaining % 60:02d}</b>")
        except Exception:
            pass
        btns.append([InlineKeyboardButton(
            text="📋 Sao chép nội dung chuyển khoản",
            copy_text=CopyTextButton(text=str(order["memo"])),
            style="primary"
        )])
        btns.append([InlineKeyboardButton(
            text="✕ Huỷ đơn đang chờ",
            callback_data=f"confirm_cancel_deposit|{order_id}",
            style="danger"
        )])
    elif order["status"] in {"expired", "rejected", "cancelled"}:
        btns.append([InlineKeyboardButton(
            text=f"↻ Tạo lại đơn {int(order['amount']):,}đ",
            callback_data=f"deposit_repeat|{int(order['amount'])}",
            style="success"
        )])
    elif order["status"] == "paid":
        btns.append([InlineKeyboardButton(text="⚡ Thuê số ngay", callback_data="otp_list", style="success")])

    btns.append([InlineKeyboardButton(text="← Danh sách đơn nạp", callback_data="deposit_orders", style="primary")])
    btns.append([InlineKeyboardButton(text="⌂ Trang chủ", callback_data="menu")])
    await c.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))
    await c.answer()


@dp.callback_query(F.data.startswith("confirm_cancel_deposit|"))
async def confirm_cancel_deposit_callback(c: CallbackQuery):
    try:
        order_id = int(c.data.split("|", 1)[1])
    except Exception:
        return await c.answer("Mã đơn không hợp lệ.", show_alert=True)
    order = get_deposit_order_by_id(order_id)
    if not order or int(order["user_id"]) != c.from_user.id or order["status"] != "pending":
        return await c.answer("Đơn không còn ở trạng thái chờ.", show_alert=True)
    await c.message.edit_text(
        "⚠️ <b>XÁC NHẬN HUỶ ĐƠN</b>\n"
        f"{UI_DIVIDER}\n"
        f"Mã đơn: <code>{order_id}</code> · <b>{int(order['amount']):,}đ</b>\n\n"
        "Chỉ huỷ nếu bạn <b>chưa chuyển khoản</b>. Nếu ngân hàng đang xử lý giao dịch, hãy quay lại và chờ hệ thống xác nhận.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✕ Tôi chưa chuyển khoản · Huỷ đơn", callback_data=f"cancel_deposit|{order_id}", style="danger")],
            [InlineKeyboardButton(text="← Tiếp tục chờ thanh toán", callback_data=f"deposit_order|{order_id}", style="primary")],
        ])
    )
    await c.answer()


@dp.callback_query(F.data.startswith("cancel_deposit|"))
async def cancel_deposit_callback(c: CallbackQuery):
    try:
        order_id = int(c.data.split("|", 1)[1])
    except Exception:
        return await c.answer("Mã đơn không hợp lệ.", show_alert=True)

    order = get_deposit_order_by_id(order_id)
    if not order or int(order["user_id"]) != c.from_user.id:
        return await c.answer("Không tìm thấy đơn nạp này.", show_alert=True)
    if not cancel_user_deposit_order(order_id, c.from_user.id):
        return await c.answer("Đơn đã được xử lý hoặc không thể huỷ.", show_alert=True)

    await c.message.edit_text(
        "✕ <b>ĐÃ HUỶ ĐƠN NẠP</b>\n"
        f"{UI_DIVIDER}\n"
        f"Mã đơn: <code>{order_id}</code>\n"
        f"Số tiền: <b>{int(order['amount']):,}đ</b>\n\n"
        "Bạn có thể tạo lại một mã QR mới.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=f"↻ Tạo lại đơn {int(order['amount']):,}đ",
                callback_data=f"deposit_repeat|{int(order['amount'])}",
                style="success"
            )],
            [InlineKeyboardButton(text="← Danh sách đơn nạp", callback_data="deposit_orders", style="primary")],
            [InlineKeyboardButton(text="⌂ Trang chủ", callback_data="menu")],
        ])
    )
    await c.answer("Đã huỷ đơn")


@dp.callback_query(F.data.startswith("deposit_repeat|"))
async def repeat_deposit_callback(c: CallbackQuery, state: FSMContext):
    try:
        amount = int(c.data.split("|", 1)[1])
    except Exception:
        return await c.answer("Số tiền không hợp lệ.", show_alert=True)
    if amount < 10000:
        return await c.answer("Số tiền nạp tối thiểu là 10.000đ.", show_alert=True)
    await state.clear()
    await c.answer(f"Đang tạo lại đơn {amount:,}đ")
    await send_deposit_checkout(c.message, c.from_user, amount, loading_msg=c.message)


@dp.callback_query(F.data.startswith("admin_"))
async def admin_action_handler(c: CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        return await c.answer("❌ Bạn không có quyền.", show_alert=True)

    expire_old_pending_orders()

    parts = c.data.split("|")
    action = parts[0]

    if len(parts) < 2:
        return await c.answer("❌ Dữ liệu không hợp lệ.", show_alert=True)

    try:
        order_id = int(parts[1])
    except Exception:
        return await c.answer("❌ Order ID không hợp lệ.", show_alert=True)

    order = get_deposit_order_by_id(order_id)
    if not order:
        return await c.answer("❌ Không tìm thấy đơn nạp.", show_alert=True)

    if action == "admin_approve":
        if order["status"] != "pending":
            return await c.answer(f"❌ Đơn này đã ở trạng thái: {order['status']}", show_alert=True)

        if is_order_expired(order):
            mark_order_expired(order_id)
            try:
                await bot.send_message(
                    order["user_id"],
                    f"⏰ Đơn nạp <code>{order_id}</code> đã hết hạn sau <b>{QR_EXPIRE_MINUTES} phút</b>, nên admin không thể duyệt nữa.\n"
                    "Vui lòng tạo mã QR mới nếu bạn vẫn muốn nạp tiền."
                )
            except Exception:
                logging.exception("Không gửi được thông báo đơn hết hạn cho khách")
            await c.message.edit_text(c.message.text + f"\n\n⏰ Đơn {order_id} đã hết hạn, không thể duyệt.")
            return await c.answer("Đơn đã hết hạn!", show_alert=True)

        user_id = int(order["user_id"])
        amount = int(order["amount"])

        async with BALANCE_LOCK:
            updated = mark_order_paid(
                order_id,
                transaction_id=f"manual_admin_{c.from_user.id}",
                raw_payload=f"manual approve by {c.from_user.id}"
            )

            if not updated:
                return await c.answer("❌ Đơn không còn ở trạng thái chờ.", show_alert=True)

            new_balance = update_balance(
                user_id,
                amount,
                note=f"Duyệt nạp tiền thủ công order {order_id} số tiền {amount}đ bởi admin {c.from_user.id}"
            )

            referral_result = apply_referral_commission_atomic(
                invited_user_id=user_id,
                deposit_amount=amount,
                source=f"manual_admin_approve_order_{order_id}_by_{c.from_user.id}"
            )

        commission_status = referral_result.get("status")
        referrer_id = referral_result.get("referrer_id")
        commission_amount = int(referral_result.get("commission_amount", 0) or 0)
        first_bonus_amount = int(referral_result.get("first_bonus_amount", 0) or 0)
        referrer_new_balance = int(referral_result.get("referrer_new_balance", 0) or 0)

        if new_balance is None:
            await c.message.edit_text(
                c.message.text + f"\n\n❌ Duyệt thất bại: không cộng được tiền cho khách."
            )
            return await c.answer("Không cộng được tiền!", show_alert=True)

        try:
            await bot.send_message(
                user_id,
                "✅ <b>NẠP TIỀN THÀNH CÔNG</b>\n"
                f"{UI_DIVIDER}\n"
                f"Đã cộng: <b>+{amount:,}đ</b>\n"
                f"Mã đơn: <code>{order_id}</code>\n"
                f"Số dư mới: <b>{new_balance:,}đ</b>",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="⚡ Thuê số ngay", callback_data="otp_list", style="success")],
                    [InlineKeyboardButton(text="⌂ Trang chủ", callback_data="menu")],
                ])
            )
        except Exception:
            logging.exception("Không gửi được tin nhắn cộng tiền cho khách")

        if commission_status == "credited" and referrer_id:
            try:
                await bot.send_message(
                    referrer_id,
                    "🎁 <b>BẠN VỪA NHẬN HOA HỒNG GIỚI THIỆU</b>\n\n"
                    f"👤 Người được giới thiệu vừa nạp: <code>{user_id}</code>\n"
                    f"💵 Số tiền nạp: <b>{amount:,}đ</b>\n"
                    f"💰 Hoa hồng 10%: <b>{commission_amount:,}đ</b>\n"
                    f"💳 Số dư mới: <b>{referrer_new_balance:,}đ</b>"
                )
            except Exception:
                logging.exception("Không gửi được thông báo hoa hồng referral cho referrer")

            try:
                await bot.send_message(
                    ADMIN_ID,
                    "💸 <b>ĐÃ CỘNG HOA HỒNG REFERRAL</b>\n\n"
                    f"👤 Referrer: <code>{referrer_id}</code>\n"
                    f"👥 Invited: <code>{user_id}</code>\n"
                    f"💰 Tiền nạp: <b>{amount:,}đ</b>\n"
                    f"🎁 Hoa hồng: <b>{commission_amount:,}đ</b>"
                )
            except Exception:
                logging.exception("Không gửi được log hoa hồng cho admin")

        await c.message.edit_text(
            c.message.text + f"\n\n✅ Đã duyệt tay order <code>{order_id}</code> và cộng {amount:,}đ"
        )
        await c.answer("Đã duyệt.")

    elif action == "admin_reject":
        if order["status"] != "pending":
            return await c.answer(f"❌ Đơn này đã ở trạng thái: {order['status']}", show_alert=True)

        if is_order_expired(order):
            mark_order_expired(order_id)
            await c.message.edit_text(c.message.text + f"\n\n⏰ Đơn {order_id} đã hết hạn.")
            return await c.answer("Đơn đã hết hạn!", show_alert=True)

        rejected = mark_order_rejected(order_id)
        if not rejected:
            return await c.answer("❌ Không hủy được đơn.", show_alert=True)

        user_id = int(order["user_id"])
        amount = int(order["amount"])

        try:
            await bot.send_message(
                user_id,
                "✕ <b>YÊU CẦU NẠP TIỀN ĐÃ BỊ HUỶ</b>\n"
                f"{UI_DIVIDER}\n"
                f"Số tiền: <b>{amount:,}đ</b>\n"
                f"Mã đơn: <code>{order_id}</code>\n\n"
                "Nếu vẫn muốn nạp, hãy tạo một mã QR mới.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="💳 Tạo mã nạp mới", callback_data="deposit", style="success")],
                    [InlineKeyboardButton(text="⌂ Trang chủ", callback_data="menu")],
                ])
            )
        except Exception:
            logging.exception("Không gửi được tin nhắn từ chối cho khách")

        await c.message.edit_text(
            c.message.text + f"\n\n❌ Đã hủy yêu cầu nạp order <code>{order_id}</code>"
        )
        await c.answer("Đã hủy.")

# --- TÌM KIẾM DỊCH VỤ ---
@dp.callback_query(F.data == "search_service")
async def search_service_start(c: CallbackQuery, state: FSMContext):
    await state.set_state(SearchServiceState.waiting_for_query)
    await c.message.edit_text(
        "🔎 <b>TÌM DỊCH VỤ</b>\n"
        f"{UI_DIVIDER}\n"
        "Nhập tên dịch vụ bạn cần, ví dụ: <code>Facebook</code>, <code>Gmail</code> hoặc <code>Shopee</code>.\n\n"
        "Có thể tìm không dấu · Gửi <code>/cancel</code> để huỷ.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="← Về danh mục", callback_data="otp_list")],
            [InlineKeyboardButton(text="⌂ Trang chủ", callback_data="menu")],
        ])
    )
    await c.answer()


@dp.message(SearchServiceState.waiting_for_query)
async def search_service_query(m: Message, state: FSMContext):
    if m.text and m.text.strip().lower() == "/cancel":
        await state.clear()
        return await send_new_screen(
            m,
            main_menu_text(m.from_user.id, m.from_user.full_name),
            main_menu_keyboard(m.from_user.id)
        )

    query = normalize_search_text(m.text or "")
    if len(query) < 2:
        return await m.answer("Hãy nhập ít nhất <b>2 ký tự</b> để tìm kiếm.")

    res = await get_fixed_apps_from_api()
    if res.get("ResponseCode") != 0:
        return await m.answer("⚠️ Chưa kết nối được kho dịch vụ. Vui lòng thử lại sau.")

    matches = [
        app_item for app_item in res.get("Result", [])
        if query in normalize_search_text(app_item.get("Name", ""))
    ]
    matches.sort(key=lambda item: (
        not normalize_search_text(item.get("Name", "")).startswith(query),
        normalize_search_text(item.get("Name", ""))
    ))

    if not matches:
        return await m.answer(
            f"Không tìm thấy dịch vụ cho <code>{html.escape(m.text or '')}</code>.\n"
            "Hãy thử tên ngắn hơn, ví dụ <code>face</code> hoặc <code>shop</code>.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📱 Xem tất cả dịch vụ", callback_data="otp_cat|all", style="primary")],
                [InlineKeyboardButton(text="⌂ Trang chủ", callback_data="menu")],
            ])
        )

    await state.clear()
    btns = []
    for app_item in matches[:15]:
        try:
            cost = float(app_item.get("Cost", 0))
        except Exception:
            cost = 0.0
        sell_price = int(cost * RUNTIME_CONFIG["price_mul"])
        app_id = int(app_item["Id"])
        app_name = app_item["Name"]
        btns.append([InlineKeyboardButton(
            text=f"{app_name} · {sell_price:,}đ",
            callback_data=f"appinfo|{app_id}|{sell_price}|{app_name}",
            style="success"
        )])

    btns.append([InlineKeyboardButton(text="🔎 Tìm từ khoá khác", callback_data="search_service", style="primary")])
    btns.append([InlineKeyboardButton(text="⌂ Trang chủ", callback_data="menu")])
    await m.answer(
        "🔎 <b>KẾT QUẢ TÌM KIẾM</b>\n"
        f"{UI_DIVIDER}\n"
        f"Tìm thấy <b>{len(matches)}</b> dịch vụ cho <code>{html.escape(m.text or '')}</code>:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=btns)
    )


# --- XỬ LÝ OTP ---
@dp.callback_query(F.data == "otp_list")
async def otp_list_callback(c: CallbackQuery, state: FSMContext):
    save_user(c.from_user)
    await state.clear()
    res = await get_fixed_apps_from_api()
    if res.get("ResponseCode") != 0:
        return await c.answer("Lỗi kết nối API", show_alert=True)

    apps = res["Result"]

    # Đếm app theo nhóm (chỉ hiện nhóm có app)
    cat_counts: dict[str, int] = {}
    for a in apps:
        cat = a.get("category") or "other"
        cat_counts[cat] = cat_counts.get(cat, 0) + 1

    btns = []
    favs = get_user_favorites(c.from_user.id)
    btns.append([InlineKeyboardButton(
        text=f"❤️ Yêu thích · {len(favs)}/{FAV_MAX}",
        callback_data="otp_fav",
        style="primary"
    )])
    btns.append([InlineKeyboardButton(
        text=f"🔎 Xem tất cả {len(apps)} dịch vụ",
        callback_data="otp_cat|all",
        style="primary"
    )])
    # Các nhóm có app, xếp 2 cột
    row: list = []
    for key in CATEGORY_LABEL:
        count = cat_counts.get(key, 0)
        if count == 0:
            continue
        emoji = CATEGORY_EMOJI.get(key, "📦")
        label = CATEGORY_LABEL[key]
        row.append(InlineKeyboardButton(
            text=f"{emoji} {label} · {count}",
            callback_data=f"otp_cat|{key}",
            style="primary"
        ))
        if len(row) == 2:
            btns.append(row); row = []
    if row:
        btns.append(row)

    btns.append([InlineKeyboardButton(text="← Về trang chủ", callback_data="menu")])

    await render_screen(
        c.message,
        "<b>BƯỚC 1/4 · CHỌN NHÓM DỊCH VỤ</b>\n"
        f"{UI_DIVIDER}\n"
        f"Số dư: <b>{format_balance(c.from_user.id)}</b>\n\n"
        "Chọn một nhóm để tìm dịch vụ nhanh hơn.\n"
        "<i>Không có OTP → tự động hoàn tiền · OTP sai → không hoàn</i>",
        InlineKeyboardMarkup(inline_keyboard=btns)
    )
    await c.answer()


@dp.callback_query(F.data.startswith("otp_cat|"))
async def otp_cat_callback(c: CallbackQuery):
    save_user(c.from_user)
    category = c.data.split("|", 1)[1]
    res = await get_fixed_apps_from_api()
    if res.get("ResponseCode") != 0:
        return await c.answer("Lỗi kết nối API", show_alert=True)

    all_apps = res["Result"]
    if category == "all":
        apps = all_apps
        title = "🔍 Tất cả dịch vụ"
    else:
        apps = [a for a in all_apps if (a.get("category") or "other") == category]
        emoji = CATEGORY_EMOJI.get(category, "📦")
        title = f"{emoji} {CATEGORY_LABEL.get(category, category)}"

    if not apps:
        return await c.answer("Nhóm này chưa có dịch vụ!", show_alert=True)

    btns = []
    for app_item in apps:
        try:
            cost = float(app_item.get("Cost", 0))
        except Exception:
            cost = 0.0
        sell_price = int(cost * RUNTIME_CONFIG["price_mul"])
        app_id = int(app_item["Id"])
        btns.append([InlineKeyboardButton(
            text=f"{app_item['Name']}  ·  {sell_price:,}đ",
            callback_data=f"appinfo|{app_id}|{sell_price}|{app_item['Name']}",
            style="success"
        )])

    btns.append([
        InlineKeyboardButton(text="← Nhóm dịch vụ", callback_data="otp_list"),
        InlineKeyboardButton(text="⌂ Trang chủ", callback_data="menu"),
    ])

    await c.message.edit_text(
        f"<b>BƯỚC 1/4 · {title}</b>\n"
        f"{UI_DIVIDER}\n"
        f"Có <b>{len(apps)}</b> dịch vụ · giá hiển thị là giá cuối.\n"
        "Chọn dịch vụ bạn cần:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=btns)
    )
    await c.answer()

# --- XEM GHI CHÚ VÀ CHỌN NHÀ MẠNG ---
@dp.callback_query(F.data.startswith("appinfo|"))
async def app_info_callback(c: CallbackQuery, state: FSMContext):
    save_user(c.from_user)
    await state.clear()
    try:
        _, app_id, sell_price, app_name = c.data.split("|", 3)
    except Exception:
        return await c.answer("Lỗi dữ liệu!")

    carriers = ["Viettel", "Mobi", "Vina", "VNMB", "ITelecom"]
    btns = [[InlineKeyboardButton(text="⚡ Thuê số ngẫu nhiên", callback_data=f"buy|{app_id}|{sell_price}|{app_name}", style="success")]]

    row = []
    for net in carriers:
        row.append(InlineKeyboardButton(text=net, callback_data=f"buy|{app_id}|{sell_price}|{app_name}|{net}", style="primary"))
        if len(row) == 3:
            btns.append(row)
            row = []
    if row:
        btns.append(row)

    btns.append([InlineKeyboardButton(text="🔎 Tìm số điện thoại cụ thể", callback_data=f"buy_specific|{app_id}|{sell_price}|{app_name}", style="primary")])
    favorite = is_favorite(c.from_user.id, int(app_id))
    fav_icon = "💔 Bỏ yêu thích" if favorite else "❤️ Thêm yêu thích"
    fav_style = "danger" if favorite else "success"
    btns.append([InlineKeyboardButton(text=fav_icon, callback_data=f"toggle_fav|{app_id}|{sell_price}|{app_name}", style=fav_style)])
    btns.append([
        InlineKeyboardButton(text="← Danh mục", callback_data="otp_list"),
        InlineKeyboardButton(text="⌂ Trang chủ", callback_data="menu"),
    ])

    note = get_app_note(app_name)
    await c.message.edit_text(
        "<b>BƯỚC 2/4 · CHỌN CÁCH THUÊ</b>\n"
        f"{UI_DIVIDER}\n"
        f"Dịch vụ: <b>{html.escape(app_name)}</b>\n"
        f"Giá thuê: <b>{int(sell_price):,}đ</b>\n"
        f"Số dư: <b>{format_balance(c.from_user.id)}</b>\n\n"
        f"{note}\n\n"
        "<b>Cách thuê</b>\n"
        "• Thuê ngẫu nhiên để nhận số nhanh nhất\n"
        "• Chọn nhà mạng nếu bạn có nhu cầu\n"
        "• Tìm số cụ thể nếu muốn dùng lại số cũ",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=btns)
    )
    await c.answer()

@dp.callback_query(F.data.startswith("buy|"))
async def otp_buy_preview_callback(c: CallbackQuery):
    save_user(c.from_user)
    parts = c.data.split("|")
    app_id, sell_price, app_name = parts[1], int(parts[2]), parts[3]
    carrier = parts[4] if len(parts) > 4 else None

    user_id = c.from_user.id
    user = get_user(user_id)
    current_balance = int(user["balance"]) if user else 0
    if user_id != ADMIN_ID and current_balance < sell_price:
        await c.message.edit_text(
            "💳 <b>SỐ DƯ CHƯA ĐỦ</b>\n"
            f"{UI_DIVIDER}\n"
            f"Dịch vụ: <b>{html.escape(app_name)}</b>\n"
            f"Giá thuê: <b>{sell_price:,}đ</b>\n"
            f"Số dư: <b>{current_balance:,}đ</b>\n"
            f"Cần nạp thêm: <b>{sell_price - current_balance:,}đ</b>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💳 Nạp tiền ngay", callback_data="deposit", style="success")],
                [InlineKeyboardButton(text="← Quay lại dịch vụ", callback_data=f"appinfo|{app_id}|{sell_price}|{app_name}")],
            ])
        )
        return await c.answer("Bạn cần nạp thêm tiền để tiếp tục.")

    carrier_label = carrier or "Ngẫu nhiên"
    balance_after = "Không giới hạn" if user_id == ADMIN_ID else f"{current_balance - sell_price:,}đ"
    confirm_data = f"buy_confirm|{app_id}|{sell_price}|{app_name}"
    if carrier:
        confirm_data += f"|{carrier}"

    await c.message.edit_text(
        "<b>BƯỚC 3/4 · XÁC NHẬN THUÊ</b>\n"
        f"{UI_DIVIDER}\n"
        f"Dịch vụ: <b>{html.escape(app_name)}</b>\n"
        f"Nhà mạng: <b>{html.escape(carrier_label)}</b>\n"
        f"Giá thuê: <b>{sell_price:,}đ</b>\n"
        f"Số dư sau giao dịch: <b>{balance_after}</b>\n\n"
        "Chỉ trừ tiền khi hệ thống lấy được số thành công.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=f"✓ Xác nhận thuê · {sell_price:,}đ",
                callback_data=confirm_data,
                style="success"
            )],
            [InlineKeyboardButton(
                text="← Đổi cách thuê",
                callback_data=f"appinfo|{app_id}|{sell_price}|{app_name}",
                style="primary"
            )],
            [InlineKeyboardButton(text="✕ Huỷ giao dịch", callback_data="menu", style="danger")],
        ])
    )
    await c.answer()


@dp.callback_query(F.data.startswith("buy_confirm|"))
async def otp_buy_confirmed_callback(c: CallbackQuery):
    save_user(c.from_user)
    parts = c.data.split("|")
    app_id, sell_price, app_name = parts[1], int(parts[2]), parts[3]
    carrier = parts[4] if len(parts) > 4 else None

    user_id = c.from_user.id
    if user_id != ADMIN_ID:
        user = get_user(user_id)
        if not user or user['balance'] < sell_price:
            current_balance = int(user['balance']) if user else 0
            await c.message.edit_text(
                "💳 <b>SỐ DƯ CHƯA ĐỦ</b>\n"
                f"{UI_DIVIDER}\n"
                f"Dịch vụ: <b>{html.escape(app_name)}</b>\n"
                f"Giá thuê: <b>{sell_price:,}đ</b>\n"
                f"Số dư: <b>{current_balance:,}đ</b>\n"
                f"Cần nạp thêm: <b>{sell_price - current_balance:,}đ</b>",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="💳 Nạp tiền ngay", callback_data="deposit", style="success")],
                    [InlineKeyboardButton(text="← Quay lại dịch vụ", callback_data=f"appinfo|{app_id}|{sell_price}|{app_name}")],
                ])
            )
            return await c.answer("Bạn cần nạp thêm tiền để tiếp tục.")

    carrier_text = f" · {carrier}" if carrier else ""
    await c.message.edit_text(
        "⏳ <b>ĐANG TÌM SỐ PHÙ HỢP</b>\n"
        f"{UI_DIVIDER}\n"
        f"Dịch vụ: <b>{html.escape(app_name)}</b>{carrier_text}\n"
        "Vui lòng chờ trong giây lát..."
    )
    await c.answer()
    res = await otp_api.request_number(app_id, carrier=carrier)

    if res.get("ResponseCode") == 0:
        if user_id != ADMIN_ID:
            async with BALANCE_LOCK:
                new_balance = update_balance(
                    user_id,
                    -sell_price,
                    full_name=c.from_user.full_name,
                    username=c.from_user.username,
                    note=f"Mua số OTP app {app_name}"
                )
            if new_balance is None:
                return await c.message.edit_text("❌ Trừ tiền thất bại, vui lòng thử lại.")

        phone = res["Result"]["Number"]
        req_id = res["Result"]["Id"]
        display_phone = normalize_phone_vn(phone)
        save_otp_history(user_id, int(app_id), app_name, display_phone, sell_price, raw_phone=phone, req_id=req_id)
        await c.message.edit_text(
            "<b>BƯỚC 4/4 · ĐANG CHỜ OTP</b>\n"
            f"{UI_DIVIDER}\n"
            f"Dịch vụ: <b>{html.escape(app_name)}</b>\n"
            f"Số điện thoại: <code>{display_phone}</code>\n"
            f"Đã thanh toán: <b>{sell_price:,}đ</b>\n\n"
            "Bạn có thể chạm vào số để sao chép. OTP sẽ được gửi ngay khi có.\n"
            "<i>Nếu hết thời gian mà chưa có OTP, tiền sẽ tự hoàn.</i>",
            reply_markup=waiting_otp_keyboard(display_phone)
        )
        asyncio.create_task(wait_for_otp(user_id, req_id, display_phone, sell_price, (user_id == ADMIN_ID), app_name))
    else:
        await c.message.edit_text(
            "⚠️ <b>CHƯA TÌM ĐƯỢC SỐ</b>\n"
            f"{UI_DIVIDER}\n"
            f"{html.escape(str(res.get('Msg') or 'Kho số đang bận.'))}\n\n"
            "Bạn có thể thử lại hoặc chọn dịch vụ khác.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="↻ Thử lại", callback_data=c.data, style="primary")],
                [
                    InlineKeyboardButton(text="← Danh mục", callback_data="otp_list"),
                    InlineKeyboardButton(text="⌂ Trang chủ", callback_data="menu"),
                ],
            ])
        )


# --- MUA SỐ CỤ THỂ ---
@dp.callback_query(F.data.startswith("buy_specific|"))
async def buy_specific_callback(c: CallbackQuery, state: FSMContext):
    save_user(c.from_user)
    try:
        _, app_id, sell_price, app_name = c.data.split("|", 3)
    except Exception:
        return await c.answer("Lỗi dữ liệu!")

    await state.set_state(BuySpecificState.waiting_for_phone)
    await state.update_data(app_id=app_id, sell_price=int(sell_price), app_name=app_name)

    await c.message.edit_text(
        "<b>BƯỚC 3/4 · NHẬP SỐ ĐIỆN THOẠI</b>\n"
        f"{UI_DIVIDER}\n"
        f"Dịch vụ: <b>{html.escape(app_name)}</b>\n"
        f"Giá thuê: <b>{int(sell_price):,}đ</b>\n\n"
        "Nhập số Việt Nam dạng <code>0xxxxxxxxx</code> để xác nhận thuê.\n"
        "<i>Số chỉ được tính tiền khi tìm thấy trong kho.</i>\n\n"
        "Gửi <code>/cancel</code> để huỷ.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="← Quay lại dịch vụ", callback_data=f"appinfo|{app_id}|{sell_price}|{app_name}")],
            [InlineKeyboardButton(text="⌂ Trang chủ", callback_data="menu")],
        ])
    )
    await c.answer()


@dp.message(BuySpecificState.waiting_for_phone)
async def buy_specific_phone_handler(m: Message, state: FSMContext):
    if m.text and m.text.strip().lower() == "/cancel":
        await state.clear()
        return await m.answer(
            "Đã huỷ tìm số cụ thể.",
            reply_markup=main_menu_keyboard(m.from_user.id)
        )

    phone_input = (m.text or "").strip()
    phone_number = normalize_phone_vn(phone_input)

    if not is_valid_phone_vn(phone_number):
        return await m.answer(
            "⚠️ Số điện thoại chưa đúng. Hãy nhập đủ 10 số, bắt đầu bằng <code>0</code>.\n"
            "Ví dụ: <code>0912345678</code> · Gửi <code>/cancel</code> để huỷ."
        )

    data = await state.get_data()
    app_id    = data["app_id"]
    sell_price = int(data["sell_price"])
    app_name  = data["app_name"]
    await state.clear()

    user_id  = m.from_user.id
    is_admin = (user_id == ADMIN_ID)

    if not is_admin:
        user = get_user(user_id)
        current_balance = int(user["balance"]) if user else 0
        if current_balance < sell_price:
            return await m.answer(
                "💳 <b>SỐ DƯ CHƯA ĐỦ</b>\n"
                f"{UI_DIVIDER}\n"
                f"Giá thuê: <b>{sell_price:,}đ</b>\n"
                f"Số dư: <b>{current_balance:,}đ</b>\n"
                f"Cần nạp thêm: <b>{sell_price - current_balance:,}đ</b>",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="💳 Nạp tiền ngay", callback_data="deposit", style="success")],
                    [InlineKeyboardButton(text="⌂ Trang chủ", callback_data="menu")],
                ])
            )

    api_phone = to_api_phone(phone_number)
    msg = await m.answer(
        "⏳ <b>ĐANG KIỂM TRA KHO SỐ</b>\n"
        f"{UI_DIVIDER}\n"
        f"Số cần tìm: <code>{phone_number}</code>\n"
        f"Dịch vụ: <b>{html.escape(app_name)}</b>"
    )

    res = await otp_api.request_number(app_id, number=api_phone)

    if res.get("ResponseCode") == 0:
        raw_phone    = str(res["Result"].get("Number", ""))
        actual_phone = normalize_phone_vn(raw_phone) if raw_phone else phone_number
        req_id       = res["Result"]["Id"]

        # API cấp số khác → báo ngay, không trừ tiền
        if to_api_phone(actual_phone) != to_api_phone(phone_number):
            return await msg.edit_text(
                "⚠️ <b>SỐ NÀY KHÔNG CÓ TRONG KHO</b>\n"
                f"{UI_DIVIDER}\n"
                f"Số điện thoại: <code>{phone_number}</code>\n"
                f"Dịch vụ: <b>{html.escape(app_name)}</b>\n\n"
                "Bạn có thể thuê số ngẫu nhiên hoặc thử lại với số khác.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="⚡ Thuê số ngẫu nhiên", callback_data=f"buy|{app_id}|{sell_price}|{app_name}")],
                    [InlineKeyboardButton(text="← Quay lại dịch vụ", callback_data=f"appinfo|{app_id}|{sell_price}|{app_name}")],
                    [InlineKeyboardButton(text="⌂ Trang chủ", callback_data="menu")]
                ])
            )

        if not is_admin:
            async with BALANCE_LOCK:
                new_balance = update_balance(
                    user_id, -sell_price,
                    full_name=m.from_user.full_name,
                    username=m.from_user.username,
                    note=f"Mua số cụ thể {actual_phone} app {app_name}"
                )
            if new_balance is None:
                return await msg.edit_text("❌ Trừ tiền thất bại, vui lòng thử lại.")

        save_otp_history(user_id, int(app_id), app_name, actual_phone, sell_price,
                         raw_phone=raw_phone, req_id=req_id)

        await msg.edit_text(
            "<b>BƯỚC 4/4 · ĐANG CHỜ OTP</b>\n"
            f"{UI_DIVIDER}\n"
            f"Dịch vụ: <b>{html.escape(app_name)}</b>\n"
            f"Số điện thoại: <code>{actual_phone}</code>\n"
            f"Đã thanh toán: <b>{sell_price:,}đ</b>\n\n"
            "OTP sẽ được gửi ngay khi có. Nếu hết thời gian chờ, tiền sẽ tự hoàn.",
            reply_markup=waiting_otp_keyboard(actual_phone)
        )
        asyncio.create_task(
            wait_for_otp(user_id, req_id, actual_phone, sell_price, is_admin, app_name)
        )
    else:
        await msg.edit_text(
            "⚠️ <b>CHƯA TÌM ĐƯỢC SỐ</b>\n"
            f"{UI_DIVIDER}\n"
            f"Số điện thoại: <code>{phone_number}</code>\n"
            f"{html.escape(str(res.get('Msg') or 'Kho số đang bận.'))}\n\n"
            "Bạn có thể thuê số ngẫu nhiên hoặc quay lại chọn dịch vụ.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⚡ Thuê số ngẫu nhiên", callback_data=f"buy|{app_id}|{sell_price}|{app_name}")],
                [InlineKeyboardButton(text="← Quay lại dịch vụ", callback_data=f"appinfo|{app_id}|{sell_price}|{app_name}")],
                [InlineKeyboardButton(text="⌂ Trang chủ", callback_data="menu")]
            ])
        )


async def wait_for_otp(user_id, req_id, phone, sell_price, is_admin, app_name):
    for _ in range(60):
        await asyncio.sleep(7)
        res = await otp_api.get_otp_code(req_id)
        if res.get("ResponseCode") == 0:
            updated = update_otp_history_status(user_id, req_id, "success", res["Result"]["Code"])
            if not updated:
                # Phiên đã được làm mới hoặc hoàn ở thao tác khác, không gửi thông báo trùng.
                return
            await bot.send_message(
                user_id,
                "🔐 <b>OTP ĐÃ SẴN SÀNG</b>\n"
                f"{UI_DIVIDER}\n"
                f"<code>{html.escape(str(res['Result']['Code']))}</code>\n\n"
                f"Dịch vụ: <b>{html.escape(app_name)}</b>\n"
                f"Số điện thoại: <code>{phone}</code>\n\n"
                "Chạm vào mã để sao chép.",
                reply_markup=otp_result_keyboard(str(res["Result"]["Code"]), phone)
            )
            return
        elif res.get("ResponseCode") == 2:
            break

    if not is_admin:
        async with BALANCE_LOCK:
            refund_result = refund_waiting_otp_once(user_id, req_id=req_id)

        if refund_result is not None:
            await bot.send_message(
                user_id,
                "⌛ <b>HẾT THỜI GIAN CHỜ OTP</b>\n"
                f"{UI_DIVIDER}\n"
                f"Số điện thoại: <code>{phone}</code>\n"
                f"Đã hoàn: <b>{refund_result['amount']:,}đ</b>\n"
                f"Số dư mới: <b>{refund_result['balance']:,}đ</b>",
                reply_markup=standard_navigation_keyboard()
            )
        else:
            current = get_otp_history_by_req(user_id, req_id)
            if current and current["status"] == "waiting":
                await bot.send_message(
                    user_id,
                    f"⚠️ Số <code>{phone}</code> đã hết hạn nhưng chưa thể hoàn tiền. Vui lòng liên hệ admin.",
                    reply_markup=standard_navigation_keyboard()
                )
    elif sell_price == 0:
        update_otp_history_status(user_id, req_id, "expired")
        await bot.send_message(
            user_id,
            "⌛ <b>SESSION ĐÃ HẾT HẠN</b>\n"
            f"Số điện thoại: <code>{phone}</code>\n"
            "Bạn không bị trừ tiền cho lần kết nối lại này.",
            reply_markup=standard_navigation_keyboard(include_history=True)
        )
    else:
        update_otp_history_status(user_id, req_id, "expired")
        await bot.send_message(
            user_id,
            f"⌛ Số <code>{phone}</code> đã hết thời gian chờ (tài khoản admin).",
            reply_markup=standard_navigation_keyboard()
        )

async def wait_for_otp_charge_later(user_id, req_id, phone, sell_price, is_admin, app_name):
    """Poll OTP — trừ tiền khi OTP về, không trừ nếu hết hạn."""
    for _ in range(60):
        await asyncio.sleep(7)
        res = await otp_api.get_otp_code(req_id)
        if res.get("ResponseCode") == 0:
            otp_code = res["Result"]["Code"]
            update_otp_history_status(user_id, req_id, "success", otp_code)
            if not is_admin and sell_price > 0:
                async with BALANCE_LOCK:
                    new_balance = update_balance(
                        user_id, -sell_price,
                        note=f"Thuê lại OTP {app_name} - {phone}"
                    )
                if new_balance is None:
                    await bot.send_message(
                        user_id,
                        "🔐 <b>OTP ĐÃ SẴN SÀNG</b>\n"
                        f"{UI_DIVIDER}\n"
                        f"<code>{html.escape(str(otp_code))}</code>\n\n"
                        f"Dịch vụ: <b>{html.escape(app_name)}</b>\n"
                        f"Số điện thoại: <code>{phone}</code>\n\n"
                        "⚠️ Chưa thể trừ số dư. Vui lòng liên hệ admin.",
                        reply_markup=otp_result_keyboard(str(otp_code), phone)
                    )
                    return
                await bot.send_message(
                    user_id,
                    "🔐 <b>OTP ĐÃ SẴN SÀNG</b>\n"
                    f"{UI_DIVIDER}\n"
                    f"<code>{html.escape(str(otp_code))}</code>\n\n"
                    f"Dịch vụ: <b>{html.escape(app_name)}</b>\n"
                    f"Số điện thoại: <code>{phone}</code>\n"
                    f"Đã thanh toán: <b>{sell_price:,}đ</b> · Số dư: <b>{new_balance:,}đ</b>",
                    reply_markup=otp_result_keyboard(str(otp_code), phone)
                )
            else:
                await bot.send_message(
                    user_id,
                    "🔐 <b>OTP ĐÃ SẴN SÀNG</b>\n"
                    f"{UI_DIVIDER}\n"
                    f"<code>{html.escape(str(otp_code))}</code>\n\n"
                    f"Dịch vụ: <b>{html.escape(app_name)}</b>\n"
                    f"Số điện thoại: <code>{phone}</code>",
                    reply_markup=otp_result_keyboard(str(otp_code), phone)
                )
            return
        elif res.get("ResponseCode") == 2:
            break

    update_otp_history_status(user_id, req_id, "expired")
    await bot.send_message(
        user_id,
        "⌛ <b>HẾT THỜI GIAN CHỜ OTP</b>\n"
        f"{UI_DIVIDER}\n"
        f"Số điện thoại: <code>{phone}</code>\n"
        f"Dịch vụ: <b>{html.escape(app_name)}</b>\n"
        "Bạn không bị trừ tiền cho lần này.",
        reply_markup=standard_navigation_keyboard(include_history=True)
    )


# --- TRUNG TÂM OTP ĐANG CHỜ ---
@dp.callback_query(F.data == "active_otp")
async def active_otp_list_callback(c: CallbackQuery):
    rows = get_active_otp_history(c.from_user.id)
    if not rows:
        await render_screen(
            c.message,
            "⏳ <b>OTP ĐANG CHỜ</b>\n"
            f"{UI_DIVIDER}\n"
            "Hiện không có phiên OTP nào đang hoạt động.",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⚡ Thuê số mới", callback_data="otp_list", style="success")],
                [InlineKeyboardButton(text="🧾 Xem lịch sử", callback_data="otp_history|0", style="primary")],
                [InlineKeyboardButton(text="⌂ Trang chủ", callback_data="menu")],
            ])
        )
        return await c.answer()

    btns = []
    for row in rows:
        btns.append([InlineKeyboardButton(
            text=f"⏳ {row['app_name']} · {row['phone']}",
            callback_data=f"active_otp|{row['id']}",
            style="success"
        )])
    btns.append([InlineKeyboardButton(text="↻ Làm mới", callback_data="active_otp", style="primary")])
    btns.append([InlineKeyboardButton(text="⌂ Trang chủ", callback_data="menu")])
    await render_screen(
        c.message,
        "⏳ <b>OTP ĐANG CHỜ</b>\n"
        f"{UI_DIVIDER}\n"
        f"Có <b>{len(rows)}</b> phiên đang hoạt động. Chọn một phiên để kiểm tra ngay:",
        InlineKeyboardMarkup(inline_keyboard=btns)
    )
    await c.answer()


@dp.callback_query(F.data.startswith("active_otp|"))
async def active_otp_detail_callback(c: CallbackQuery):
    try:
        history_id = int(c.data.split("|", 1)[1])
    except Exception:
        return await c.answer("Dữ liệu không hợp lệ.", show_alert=True)

    row = get_otp_history_by_id(history_id, c.from_user.id)
    if not row:
        return await c.answer("Không tìm thấy phiên OTP.", show_alert=True)

    if row["status"] == "success" and row["otp_code"]:
        await c.message.edit_text(
            "🔐 <b>OTP ĐÃ SẴN SÀNG</b>\n"
            f"{UI_DIVIDER}\n"
            f"<code>{html.escape(str(row['otp_code']))}</code>\n\n"
            f"Dịch vụ: <b>{html.escape(row['app_name'])}</b>\n"
            f"Số điện thoại: <code>{row['phone']}</code>",
            reply_markup=otp_result_keyboard(str(row["otp_code"]), row["phone"])
        )
        return await c.answer()

    if row["status"] != "waiting" or not row["req_id"]:
        return await c.answer("Phiên OTP này không còn hoạt động.", show_alert=True)

    res = await otp_api.get_otp_code(row["req_id"])
    response_code = res.get("ResponseCode")
    if response_code == 0:
        otp_code = str(res["Result"]["Code"])
        updated = update_otp_history_status(c.from_user.id, row["req_id"], "success", otp_code)
        if not updated:
            current = get_otp_history_by_id(history_id, c.from_user.id)
            if current and current["status"] == "success" and current["otp_code"]:
                otp_code = str(current["otp_code"])
            else:
                await c.answer("Phiên đã được xử lý ở thao tác khác.", show_alert=True)
                return
        await c.message.edit_text(
            "🔐 <b>OTP ĐÃ SẴN SÀNG</b>\n"
            f"{UI_DIVIDER}\n"
            f"<code>{html.escape(otp_code)}</code>\n\n"
            f"Dịch vụ: <b>{html.escape(row['app_name'])}</b>\n"
            f"Số điện thoại: <code>{row['phone']}</code>",
            reply_markup=otp_result_keyboard(otp_code, row["phone"])
        )
        return await c.answer("Đã nhận OTP")

    if response_code == 2:
        if c.from_user.id == ADMIN_ID:
            update_otp_history_status(c.from_user.id, row["req_id"], "expired")
            refund_result = None
        else:
            async with BALANCE_LOCK:
                refund_result = refund_waiting_otp_once(c.from_user.id, history_id=history_id)

        if refund_result:
            await c.message.edit_text(
                "⌛ <b>PHIÊN OTP ĐÃ HẾT HẠN</b>\n"
                f"{UI_DIVIDER}\n"
                f"Số điện thoại: <code>{row['phone']}</code>\n"
                f"Đã hoàn: <b>{refund_result['amount']:,}đ</b>\n"
                f"Số dư mới: <b>{refund_result['balance']:,}đ</b>",
                reply_markup=standard_navigation_keyboard(include_history=True)
            )
        else:
            await c.message.edit_text(
                "⌛ <b>PHIÊN OTP ĐÃ KẾT THÚC</b>\n"
                f"{UI_DIVIDER}\n"
                f"Số điện thoại: <code>{row['phone']}</code>\n"
                "Trạng thái giao dịch đã được xử lý.",
                reply_markup=standard_navigation_keyboard(include_history=True)
            )
        return await c.answer()

    try:
        created_at = datetime.strptime(row["created_at"], "%Y-%m-%d %H:%M:%S")
        elapsed = max(0, int((datetime.utcnow() - created_at).total_seconds()))
        remaining = max(0, 420 - elapsed)
        remaining_text = f"{remaining // 60:02d}:{remaining % 60:02d}"
    except Exception:
        remaining_text = "đang cập nhật"

    await c.message.edit_text(
        "⏳ <b>ĐANG CHỜ OTP</b>\n"
        f"{UI_DIVIDER}\n"
        f"Dịch vụ: <b>{html.escape(row['app_name'])}</b>\n"
        f"Số điện thoại: <code>{row['phone']}</code>\n"
        f"Thời gian dự kiến còn lại: <b>{remaining_text}</b>\n\n"
        "Bấm Làm mới để kiểm tra OTP ngay.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="↻ Làm mới trạng thái", callback_data=f"active_otp|{history_id}", style="primary")],
            [InlineKeyboardButton(
                text="📋 Sao chép số điện thoại",
                copy_text=CopyTextButton(text=str(row["phone"])),
                style="primary"
            )],
            [InlineKeyboardButton(text="← Danh sách đang chờ", callback_data="active_otp")],
            [InlineKeyboardButton(text="⌂ Trang chủ", callback_data="menu")],
        ])
    )
    await c.answer()

@dp.callback_query(F.data == "otp_fav")
async def otp_fav_callback(c: CallbackQuery):
    save_user(c.from_user)
    favs = get_user_favorites(c.from_user.id)
    if not favs:
        await c.message.edit_text(
            "❤️ <b>DỊCH VỤ YÊU THÍCH</b>\n"
            f"{UI_DIVIDER}\n"
            "Bạn chưa lưu dịch vụ nào.\n\n"
            "Khi xem một dịch vụ, bấm <b>Thêm yêu thích</b> để mở lại nhanh từ trang chủ.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📱 Khám phá dịch vụ", callback_data="otp_list", style="primary")],
                [InlineKeyboardButton(text="← Về trang chủ", callback_data="menu")],
            ])
        )
        return await c.answer()

    res = await get_fixed_apps_from_api()
    api_map = {}
    if res.get("ResponseCode") == 0:
        api_map = {int(a["Id"]): a for a in res["Result"]}

    btns = []
    for fav in favs:
        app_id  = int(fav["app_id"])
        app_name = fav["app_name"]
        api_item = api_map.get(app_id)
        if api_item:
            sell_price = int(float(api_item.get("Cost", 0)) * RUNTIME_CONFIG["price_mul"])
            btns.append([InlineKeyboardButton(
                text=f"{app_name}  ·  {sell_price:,}đ",
                callback_data=f"appinfo|{app_id}|{sell_price}|{app_name}",
                style="success"
            )])
        else:
            btns.append([InlineKeyboardButton(
                text=f"{app_name} · Tạm hết",
                callback_data="noop_bal",
                style="danger"
            )])

    btns.append([
        InlineKeyboardButton(text="← Danh mục", callback_data="otp_list"),
        InlineKeyboardButton(text="⌂ Trang chủ", callback_data="menu"),
    ])

    await c.message.edit_text(
        "❤️ <b>DỊCH VỤ YÊU THÍCH</b>\n"
        f"{UI_DIVIDER}\n"
        f"Đã lưu <b>{len(favs)}/{FAV_MAX}</b> dịch vụ · chọn để thuê nhanh:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=btns)
    )
    await c.answer()


@dp.callback_query(F.data.startswith("toggle_fav|"))
async def toggle_fav_callback(c: CallbackQuery):
    parts = c.data.split("|", 3)
    app_id, sell_price, app_name = int(parts[1]), parts[2], parts[3]
    result = toggle_favorite(c.from_user.id, app_id, app_name)

    if result == "added":
        await c.answer(f"❤️ Đã thêm {app_name} vào yêu thích!", show_alert=False)
    elif result == "removed":
        await c.answer(f"💔 Đã bỏ {app_name} khỏi yêu thích.", show_alert=False)
    else:
        return await c.answer(f"❌ Tối đa {FAV_MAX} app yêu thích. Bỏ bớt 1 app trước.", show_alert=True)

    # Cập nhật lại nút trên card app
    fav_icon = "💔 Bỏ yêu thích" if result == "added" else "❤️ Thêm yêu thích"
    carriers = ["Viettel", "Mobi", "Vina", "VNMB", "ITelecom"]
    btns = [[InlineKeyboardButton(text="⚡ Thuê số ngẫu nhiên", callback_data=f"buy|{app_id}|{sell_price}|{app_name}", style="success")]]
    row = []
    for net in carriers:
        row.append(InlineKeyboardButton(text=net, callback_data=f"buy|{app_id}|{sell_price}|{app_name}|{net}", style="primary"))
        if len(row) == 3:
            btns.append(row); row = []
    if row:
        btns.append(row)
    btns.append([InlineKeyboardButton(text="🔎 Tìm số điện thoại cụ thể", callback_data=f"buy_specific|{app_id}|{sell_price}|{app_name}", style="primary")])
    fav_style = "danger" if result == "added" else "success"
    btns.append([InlineKeyboardButton(text=fav_icon, callback_data=f"toggle_fav|{app_id}|{sell_price}|{app_name}", style=fav_style)])
    btns.append([
        InlineKeyboardButton(text="← Danh mục", callback_data="otp_list"),
        InlineKeyboardButton(text="⌂ Trang chủ", callback_data="menu"),
    ])
    try:
        await c.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))
    except Exception:
        pass


@dp.callback_query(F.data == "menu")
async def menu_back(c: CallbackQuery, state: FSMContext):
    save_user(c.from_user)
    await state.clear()
    await render_screen(
        c.message,
        main_menu_text(c.from_user.id, c.from_user.full_name),
        main_menu_keyboard(c.from_user.id)
    )
    await c.answer()

# --- LỊCH SỬ THUÊ SỐ ---
HISTORY_PAGE_SIZE = 5

@dp.callback_query(F.data.startswith("otp_history|"))
async def otp_history_callback(c: CallbackQuery):
    save_user(c.from_user)
    user_id = c.from_user.id

    try:
        page = int(c.data.split("|")[1])
    except Exception:
        page = 0

    rows = get_otp_history(user_id)

    if not rows:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⚡ Thuê số đầu tiên", callback_data="otp_list", style="success")],
            [InlineKeyboardButton(text="← Về trang chủ", callback_data="menu")],
        ])
        await render_screen(
            c.message,
            "🧾 <b>LỊCH SỬ THUÊ SỐ</b>\n"
            f"{UI_DIVIDER}\n"
            "Chưa có giao dịch nào. Khi bạn thuê số, thông tin sẽ được lưu tại đây để dễ dàng thuê lại.",
            kb
        )
        return await c.answer()

    total      = len(rows)
    total_pages = (total + HISTORY_PAGE_SIZE - 1) // HISTORY_PAGE_SIZE
    page       = max(0, min(page, total_pages - 1))
    page_rows  = rows[page * HISTORY_PAGE_SIZE : (page + 1) * HISTORY_PAGE_SIZE]

    # --- Xây nội dung text ---
    lines = [
        "🧾 <b>LỊCH SỬ THUÊ SỐ</b>",
        UI_DIVIDER,
        f"Trang <b>{page + 1}/{total_pages}</b> · {total} giao dịch gần nhất",
        "",
    ]
    for i, row in enumerate(page_rows, start=page * HISTORY_PAGE_SIZE + 1):
        app_name   = html.escape(row["app_name"])
        phone      = row["phone"]
        sell_price = int(row["sell_price"])
        date_str   = (row["created_at"] or "")[:10]
        status_label = {
            "waiting": "⏳ Đang chờ",
            "success": "✅ Thành công",
            "refunded": "↩️ Đã hoàn",
            "expired": "⌛ Hết hạn",
        }.get(row["status"], "• Đã xử lý")
        lines.append(
            f"<b>{i}. {app_name}</b>\n"
            f"<code>{phone}</code> · {sell_price:,}đ · {date_str}\n"
            f"{status_label}"
        )
        if i < page * HISTORY_PAGE_SIZE + len(page_rows):
            lines.append("")

    # Nút một cột để tên dịch vụ và số điện thoại không bị cắt trên màn hình nhỏ.
    btns = []
    for i, row in enumerate(page_rows, start=page * HISTORY_PAGE_SIZE + 1):
        if row["status"] == "waiting":
            btns.append([InlineKeyboardButton(
                text=f"⏳ Kiểm tra OTP {i} · {row['app_name']}",
                callback_data=f"active_otp|{row['id']}",
                style="primary"
            )])
        else:
            btns.append([InlineKeyboardButton(
                text=f"↻ Thuê lại {i} · {row['app_name']}",
                callback_data=f"rebuy_preview|{row['id']}",
                style="success"
            )])

    # --- Nút phân trang ---
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="← Trước", callback_data=f"otp_history|{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"· {page + 1}/{total_pages} ·", callback_data="noop_bal"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="Tiếp →", callback_data=f"otp_history|{page + 1}"))
    if nav:
        btns.append(nav)

    btns.append([InlineKeyboardButton(text="← Về trang chủ", callback_data="menu")])

    await render_screen(
        c.message,
        "\n".join(lines),
        InlineKeyboardMarkup(inline_keyboard=btns)
    )
    await c.answer()


@dp.callback_query(F.data.startswith("rebuy_preview|"))
async def rebuy_preview_callback(c: CallbackQuery):
    try:
        history_id = int(c.data.split("|", 1)[1])
    except Exception:
        return await c.answer("Dữ liệu không hợp lệ.", show_alert=True)

    row = get_otp_history_by_id(history_id, c.from_user.id)
    if not row:
        return await c.answer("Không tìm thấy giao dịch này.", show_alert=True)

    await c.message.edit_text(
        "<b>XÁC NHẬN THUÊ LẠI SỐ</b>\n"
        f"{UI_DIVIDER}\n"
        f"Dịch vụ: <b>{html.escape(row['app_name'])}</b>\n"
        f"Số điện thoại: <code>{row['phone']}</code>\n"
        f"Phí tối đa: <b>{int(row['sell_price']):,}đ</b>\n\n"
        "Bot sẽ kiểm tra phiên cũ trước. Nếu phiên vẫn hoạt động, bạn tiếp tục chờ OTP miễn phí; "
        "nếu phiên đã hết hạn, bot chỉ trừ tiền khi lấy lại đúng số.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="✓ Xác nhận thuê lại",
                callback_data=f"rebuy_confirm|{history_id}",
                style="success"
            )],
            [InlineKeyboardButton(text="← Về lịch sử", callback_data="otp_history|0", style="primary")],
            [InlineKeyboardButton(text="✕ Huỷ", callback_data="menu", style="danger")],
        ])
    )
    await c.answer()


@dp.callback_query(F.data.startswith("rebuy_confirm|"))
async def rebuy_callback(c: CallbackQuery):
    save_user(c.from_user)
    parts = c.data.split("|")

    try:
        hid = int(parts[1])
    except Exception:
        return await c.answer("❌ Dữ liệu không hợp lệ!", show_alert=True)

    user_id  = c.from_user.id
    is_admin = (user_id == ADMIN_ID)

    row = get_otp_history_by_id(hid, user_id)
    if not row:
        return await c.answer("❌ Không tìm thấy lịch sử này!", show_alert=True)

    app_id        = int(row["app_id"])
    app_name      = row["app_name"]
    phone_number  = row["phone"]          # dạng 0xxxxxxxxx (hiển thị)
    sell_price    = int(row["sell_price"])
    stored_req_id = row["req_id"] if row["req_id"] else None

    # Chuẩn hóa về 9 chữ số (format API): dù raw_phone lưu dạng gì cũng ra đúng
    src = row["raw_phone"] if row["raw_phone"] else phone_number
    api_phone = to_api_phone(src)

    await c.message.edit_text(
        "⏳ <b>ĐANG KIỂM TRA SỐ CŨ</b>\n"
        f"{UI_DIVIDER}\n"
        f"Số điện thoại: <code>{phone_number}</code>\n"
        f"Dịch vụ: <b>{html.escape(app_name)}</b>"
    )

    # ── BƯỚC 1: session đang chờ OTP (chưa nhận) → reconnect miễn phí ──
    if stored_req_id:
        code_res = await otp_api.get_otp_code(stored_req_id)
        rc = code_res.get("ResponseCode")

        if rc == 1:
            # Session còn sống, đang chờ OTP → poll tiếp, không trừ tiền
            await c.message.edit_text(
                "⏳ <b>TIẾP TỤC CHỜ OTP · KHÔNG MẤT PHÍ</b>\n"
                f"{UI_DIVIDER}\n"
                f"Số điện thoại: <code>{phone_number}</code>\n"
                f"Dịch vụ: <b>{html.escape(app_name)}</b>\n\n"
                "Phiên cũ vẫn còn hiệu lực. OTP sẽ được gửi ngay khi có.",
                reply_markup=waiting_otp_keyboard(phone_number)
            )
            asyncio.create_task(
                wait_for_otp(
                    user_id=user_id, req_id=stored_req_id,
                    phone=phone_number, sell_price=0,
                    is_admin=True, app_name=app_name
                )
            )
            return await c.answer()

        # rc == 2/3: session hết hạn → tiếp tục tạo session mới bên dưới

    # ── BƯỚC 2: session cũ hết hạn hoặc không có req_id → tạo session mới ──
    if not is_admin:
        user = get_user(user_id)
        current_balance = int(user["balance"]) if user else 0
        if current_balance < sell_price:
            await c.message.edit_text(
                "💳 <b>SỐ DƯ CHƯA ĐỦ</b>\n"
                f"{UI_DIVIDER}\n"
                f"Giá thuê lại: <b>{sell_price:,}đ</b>\n"
                f"Số dư: <b>{current_balance:,}đ</b>\n"
                f"Cần nạp thêm: <b>{sell_price - current_balance:,}đ</b>",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="💳 Nạp tiền ngay", callback_data="deposit", style="success")],
                    [InlineKeyboardButton(text="← Về lịch sử", callback_data="otp_history|0")],
                ])
            )
            return await c.answer("Bạn cần nạp thêm tiền để thuê lại số.")

    res = await otp_api.request_number(app_id, number=api_phone)

    if res.get("ResponseCode") == 0:
        new_req_id   = res["Result"]["Id"]
        raw_phone    = str(res["Result"].get("Number", ""))
        actual_phone = normalize_phone_vn(raw_phone) if raw_phone else phone_number

        # So sánh bằng format 9 chữ số để tránh nhầm 0xxx vs 84xxx vs xxx
        if to_api_phone(actual_phone) != to_api_phone(phone_number):
            await c.message.edit_text(
                "⚠️ <b>SỐ CŨ KHÔNG CÒN TRONG KHO</b>\n"
                f"{UI_DIVIDER}\n"
                f"Số điện thoại: <code>{phone_number}</code>\n"
                f"Dịch vụ: <b>{html.escape(app_name)}</b>\n\n"
                "Số có thể đã được thu hồi. Bạn có thể thuê một số mới cho cùng dịch vụ.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="⚡ Thuê số mới", callback_data=f"appinfo|{app_id}|{sell_price}|{app_name}")],
                    [InlineKeyboardButton(text="← Về lịch sử", callback_data="otp_history|0")],
                    [InlineKeyboardButton(text="⌂ Trang chủ", callback_data="menu")]
                ])
            )
            return await c.answer()

        # Trừ tiền ngay khi lấy được đúng số cũ (hoàn nếu hết hạn không có OTP)
        if not is_admin:
            async with BALANCE_LOCK:
                new_balance = update_balance(
                    user_id, -sell_price,
                    full_name=c.from_user.full_name,
                    username=c.from_user.username,
                    note=f"Thuê lại số {actual_phone} app {app_name}"
                )
            if new_balance is None:
                return await c.message.edit_text("❌ Trừ tiền thất bại, vui lòng thử lại.")

        save_otp_history(user_id, app_id, app_name, actual_phone, sell_price,
                         raw_phone=raw_phone, req_id=new_req_id)

        await c.message.edit_text(
            "<b>BƯỚC 4/4 · ĐANG CHỜ OTP</b>\n"
            f"{UI_DIVIDER}\n"
            f"Số điện thoại: <code>{actual_phone}</code>\n"
            f"Dịch vụ: <b>{html.escape(app_name)}</b>\n"
            f"Đã thanh toán: <b>{sell_price:,}đ</b>\n\n"
            "OTP sẽ được gửi ngay khi có. Không có OTP, tiền sẽ tự hoàn.",
            reply_markup=waiting_otp_keyboard(actual_phone)
        )

        asyncio.create_task(
            wait_for_otp(
                user_id=user_id, req_id=new_req_id,
                phone=actual_phone, sell_price=sell_price,
                is_admin=is_admin, app_name=app_name
            )
        )
        await c.answer()
    else:
        msg = res.get("Msg", "Lỗi không xác định")
        await c.message.edit_text(
            "⚠️ <b>KHÔNG THỂ THUÊ LẠI SỐ CŨ</b>\n"
            f"{UI_DIVIDER}\n"
            f"Số điện thoại: <code>{phone_number}</code>\n"
            f"Dịch vụ: <b>{html.escape(app_name)}</b>\n"
            f"{html.escape(str(msg))}\n\n"
            "Bạn có thể thuê số mới cho cùng dịch vụ.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⚡ Thuê số mới", callback_data=f"appinfo|{app_id}|{sell_price}|{app_name}")],
                [InlineKeyboardButton(text="← Về lịch sử", callback_data="otp_history|0")],
                [InlineKeyboardButton(text="⌂ Trang chủ", callback_data="menu")]
            ])
        )
        await c.answer()

# --- SEPAY WEBHOOK ---
def normalize_payment_text(text: str) -> str:
    if not text:
        return ""
    return "".join(ch.lower() for ch in str(text) if ch.isalnum())

def _flatten_payload(payload):
    if isinstance(payload, dict):
        if isinstance(payload.get("data"), dict):
            return payload["data"]
        if isinstance(payload.get("transfer"), dict):
            return payload["transfer"]
    return payload if isinstance(payload, dict) else {}

def _extract_amount_content_txn(payload):
    data = _flatten_payload(payload)

    amount = 0
    content = ""
    txn_id = ""

    amount_keys = [
        "transferAmount", "amount", "transfer_amount", "creditAmount",
        "transactionAmount", "incomingAmount"
    ]
    content_keys = [
        "content", "description", "transferContent", "transactionContent",
        "referenceCode"
    ]
    txn_keys = [
        "id", "transaction_id", "transactionId", "reference", "code"
    ]

    for key in amount_keys:
        value = data.get(key)
        if value is None:
            continue
        try:
            amount = int(float(str(value).replace(",", "").strip()))
            if amount > 0:
                break
        except Exception:
            pass

    for key in content_keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            content = value.strip()
            break

    for key in txn_keys:
        value = data.get(key)
        if value is not None and str(value).strip():
            txn_id = str(value).strip()
            break

    return amount, content, txn_id

# --- FIREBASE WEB INTEGRATION ---
async def process_firebase_deposit(amount: int, normalized_content: str, txn_id: str) -> bool:
    try:
        res = await HTTP_CLIENT.get(f"{FIREBASE_DB_URL}/deposit_requests.json")
        if res.status_code != 200:
            return False
        
        requests = res.json()
        if not requests:
            return False

        for memo, req in requests.items():
            if req.get("status") == "Chờ duyệt":
                norm_memo = normalize_payment_text(memo)
                req_amount = int(req.get("amount", 0))
                
                # Khớp memo và số tiền
                if norm_memo in normalized_content and req_amount == amount:
                    username = req.get("username")
                    
                    # 1. Cập nhật trạng thái đơn nạp Web
                    await HTTP_CLIENT.patch(
                        f"{FIREBASE_DB_URL}/deposit_requests/{memo}.json",
                        json={"status": "Đã duyệt (Auto SePay)"}
                    )
                    
                    # 2. Lấy số dư hiện tại
                    user_res = await HTTP_CLIENT.get(f"{FIREBASE_DB_URL}/users/{username}/balance.json")
                    current_balance = user_res.json() or 0
                    
                    # 3. Cộng tiền
                    new_balance = current_balance + amount
                    await HTTP_CLIENT.put(f"{FIREBASE_DB_URL}/users/{username}/balance.json", json=new_balance)
                    
                    # 4. Thông báo cho Admin qua Telegram
                    try:
                        await bot.send_message(
                            ADMIN_ID,
                            f"🌐 <b>WEB: TỰ ĐỘNG DUYỆT NẠP TIỀN</b>\n"
                            f"👤 User Web: <code>{username}</code>\n"
                            f"💰 Số tiền: <b>{amount:,}đ</b>\n"
                            f"📝 Memo: <code>{memo}</code>\n"
                            f"💳 Số dư mới: <b>{new_balance:,}đ</b>\n"
                            f"🏦 Txn: <code>{html.escape(txn_id or 'N/A')}</code>"
                        )
                    except Exception:
                        logging.exception("Không gửi được thông báo Firebase Deposit cho admin")
                    
                    return True
    except Exception as e:
        logging.error(f"Error processing Firebase deposit: {e}")
    return False

@app.get("/")
async def root():
    return {"ok": True, "message": "Bot + SePay webhook is running"}

@app.get("/sepay/webhook")
async def sepay_webhook_get():
    return {"ok": True, "message": "SePay webhook endpoint is alive. Use POST."}

@app.post("/sepay/webhook")
async def sepay_webhook_post(request: Request):
    try:
        payload = await request.json()
    except Exception:
        raw_text = await request.body()
        logging.warning(f"SEPAY WEBHOOK non-json body: {raw_text!r}")
        return {"ok": False, "message": "invalid json"}

    logging.info(f"SEPAY WEBHOOK payload: {payload}")

    amount, content, txn_id = _extract_amount_content_txn(payload)

    if amount <= 0 or not content:
        return {"ok": True, "message": "ignored"}

    expire_old_pending_orders()
    orders = get_payment_matchable_orders()
    matched = None

    normalized_content = normalize_payment_text(content)

    for order in orders:
        normalized_memo = normalize_payment_text(order["memo"])

        if normalized_memo in normalized_content and int(order["amount"]) == int(amount):
            matched = order
            break

    if not matched:
        # Thử tìm đơn nạp bên phía Web App qua Firebase
        web_matched = await process_firebase_deposit(amount, normalized_content, txn_id)
        if web_matched:
            return {"ok": True, "message": "processed for web"}

        logging.info(
            f"SEPAY no match | amount={amount} | content={content} | normalized={normalized_content}"
        )
        return {"ok": True, "message": "no match"}

    if is_order_expired(matched):
        mark_order_expired(int(matched["id"]))
        try:
            await bot.send_message(
                matched["user_id"],
                f"⏰ Đơn nạp <code>{matched['id']}</code> đã quá hạn {QR_EXPIRE_MINUTES} phút nên hệ thống không cộng tiền tự động.\n"
                "Vui lòng tạo mã QR mới và chuyển khoản lại đúng đơn mới."
            )
        except Exception:
            logging.exception("Không gửi được thông báo order hết hạn khi webhook tới")
        return {"ok": True, "message": "order expired"}

    async with BALANCE_LOCK:
        updated = mark_order_paid(
            matched["id"],
            transaction_id=txn_id,
            raw_payload=str(payload)
        )

        if not updated:
            return {"ok": True, "message": "already paid"}

        new_balance = update_balance(
            matched["user_id"],
            matched["amount"],
            note=f"SePay auto nạp tiền - order={matched['id']} - memo={matched['memo']} - txn={txn_id}"
        )

        referral_result = apply_referral_commission_atomic(
            invited_user_id=matched["user_id"],
            deposit_amount=matched["amount"],
            source=f"sepay:{txn_id}"
        )

    commission_status = referral_result.get("status")
    referrer_id = referral_result.get("referrer_id")
    commission_amount = int(referral_result.get("commission_amount", 0) or 0)
    first_bonus_amount = int(referral_result.get("first_bonus_amount", 0) or 0)
    referrer_new_balance = int(referral_result.get("referrer_new_balance", 0) or 0)

    if new_balance is None:
        return {"ok": False, "message": "balance update failed"}

    try:
        await bot.send_message(
            matched["user_id"],
            "✅ <b>NẠP TIỀN THÀNH CÔNG</b>\n"
            f"{UI_DIVIDER}\n"
            f"Đã cộng: <b>+{matched['amount']:,}đ</b>\n"
            f"Mã đơn: <code>{matched['id']}</code>\n"
            f"Số dư mới: <b>{new_balance:,}đ</b>\n\n"
            "Giao dịch đã được ngân hàng xác nhận tự động.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⚡ Thuê số ngay", callback_data="otp_list", style="success")],
                [InlineKeyboardButton(text="⌂ Trang chủ", callback_data="menu")],
            ])
        )
    except Exception:
        logging.exception("Không gửi được thông báo nạp tiền cho khách")

    try:
        await bot.send_message(
            ADMIN_ID,
            f"💸 <b>TỰ ĐỘNG DUYỆT NẠP TIỀN</b>\n"
            f"🧾 Order ID: <code>{matched['id']}</code>\n"
            f"👤 User: <code>{matched['user_id']}</code>\n"
            f"💰 Số tiền: <b>{matched['amount']:,}đ</b>\n"
            f"📝 Memo: <code>{matched['memo']}</code>\n"
            f"🏦 Txn: <code>{html.escape(txn_id or 'N/A')}</code>"
        )
    except Exception:
        logging.exception("Không gửi được thông báo cho admin")

    if commission_status == "credited" and referrer_id and commission_amount > 0:
        try:
            await bot.send_message(
                referrer_id,
                "🎁 <b>BẠN VỪA NHẬN HOA HỒNG GIỚI THIỆU</b>\n\n"
                f"👤 Người được giới thiệu: <code>{matched['user_id']}</code>\n"
                f"💵 Số tiền nạp: <b>{matched['amount']:,}đ</b>\n"
                f"💰 Hoa hồng 10%: <b>{commission_amount:,}đ</b>\n"
                f"💳 Số dư mới: <b>{referrer_new_balance:,}đ</b>"
            )
        except Exception:
            logging.exception("Không gửi được thông báo referral commission cho referrer")

        try:
            await bot.send_message(
                ADMIN_ID,
                "💸 <b>REFERRAL HOA HỒNG TỰ ĐỘNG</b>\n\n"
                f"👤 Referrer: <code>{referrer_id}</code>\n"
                f"👥 Invited: <code>{matched['user_id']}</code>\n"
                f"💰 Tiền nạp: <b>{matched['amount']:,}đ</b>\n"
                f"🎁 Hoa hồng: <b>{commission_amount:,}đ</b>\n"
                f"🏦 Txn: <code>{html.escape(txn_id or 'N/A')}</code>"
            )
        except Exception:
            logging.exception("Không gửi được log referral auto cho admin")

    return {"ok": True, "message": "processed"}

# --- RUN ---
async def run_bot():
    await dp.start_polling(bot)

async def run_web():
    config = uvicorn.Config(app, host="0.0.0.0", port=PORT, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()

async def main():
    init_db()
    await refresh_runtime_config()   # đọc OTP key/URL từ Firebase trước khi chạy
    await reset_bot_on_startup()
    print("Bot + SePay webhook is running...")
    try:
        await asyncio.gather(
            run_bot(),
            run_web(),
            config_refresh_loop()
        )
    finally:
        await HTTP_CLIENT.aclose()

if __name__ == "__main__":
    asyncio.run(main())
