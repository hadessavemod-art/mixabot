#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram bot "ПЕРЕБИВ" на Telethon (для Bothost).
Сессия и настройки зашиты в код.
Умеет менять цену платных сообщений в группе автоматически.
Управление только кнопками. Один активный чат.
"""
import subprocess
import sys

# Автоустановка Telethon, если его нет
try:
    import telethon  # noqa
except ImportError:
    print("[startup] telethon не найден, устанавливаю...", flush=True)
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "--no-cache-dir", "telethon"]
    )
    print("[startup] telethon установлен", flush=True)

import asyncio
import html
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from contextlib import closing
from typing import Optional

from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
from telethon.errors import (
    RPCError,
    ChatAdminRequiredError,
    ChannelInvalidError,
    FloodWaitError,
    MessageNotModifiedError,
    MessageIdInvalidError,
)
from telethon.tl.types import Channel, Chat

try:
    from telethon.tl.functions.channels import UpdatePaidMessagesPriceRequest
except ImportError:
    try:
        from telethon.tl.functions.channels import UpdatePaidMessagesPrice as UpdatePaidMessagesPriceRequest  # type: ignore
    except ImportError:
        UpdatePaidMessagesPriceRequest = None  # type: ignore


# ============================================================
# НАСТРОЙКИ
# ============================================================
BOT_TOKEN = "8865782064:AAF_QRh0UpmS80C-u7bUcRi3IOM8jWB6zmk"

API_ID = 34714558
API_HASH = "335d8883ab3c4b1c8fd7662ea5219bfc"

SESSION_STRING = (
    "1AZWarzUBu4fIEQuqJFgFSEqpeO5O6WWm2_i7eOdZLUTRHbDF4R6SbyRdLHI5H2DbTr4q-jtPpQH0Z6TSqMtJw8Y3YlXvKKVmRIZFRUjv4hzZVSlI3C5oZq7kw8bXABa99aiIWo5kQ7dRo8tJC4f9jtgofJuK72x0r0Pxl_GIse_WmLETDKpaRdA3EhkEpuYDfsC6OWT0Hg_2i23vM5etvsQUHeeaJ10Ad4wowYKFgbw7IP35b_6WRQ_UAmcbz4czjn15xquCXqVtcDaOSipKmxoVQlKKOUz6_WFDgG35-yAGhC4YhgpV9HRD_u66mYOGijXVptL4KsKXrM9jEN9HjWHWx5ReSos="
)

ADMIN_IDS = [1592503829, 7831720836]
DEFAULT_CHAT_ID = -5379233619
EVENT_DURATION = 180
DEFAULT_INTERVAL = 86400
DB_PATH = "perebiv.sqlite3"
DISPLAY_UTC_OFFSET_HOURS = 0

GIFTS = [
    {"key": "bear",    "emoji": "🧸", "name": "Мишка"},
    {"key": "rose",    "emoji": "🌹", "name": "Роза"},
    {"key": "cake",    "emoji": "🎂", "name": "Торт"},
    {"key": "gift",    "emoji": "🎁", "name": "Подарок"},
    {"key": "trophy",  "emoji": "🏆", "name": "Кубок"},
    {"key": "ring",    "emoji": "💍", "name": "Кольцо"},
    {"key": "diamond", "emoji": "💎", "name": "Алмаз"},
]
GIFT_MAP = {g["key"]: g for g in GIFTS}
GIFT_INDEX = {g["key"]: i for i, g in enumerate(GIFTS)}
MAX_GIFT_INDEX = len(GIFTS) - 1
DEFAULT_GIFT_KEY = "bear"
MAX_MESSAGE_COST = 50

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("perebiv")
UTC = timezone.utc


# ============================================================
# УТИЛИТЫ
# ============================================================
def now_utc() -> datetime:
    return datetime.now(UTC)


def dt_to_str(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def str_to_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).astimezone(UTC)
    except (ValueError, TypeError):
        return None


def display_dt(value: Optional[str]) -> str:
    dt = str_to_dt(value)
    if not dt:
        return "—"
    dt = dt + timedelta(hours=DISPLAY_UTC_OFFSET_HOURS)
    return dt.strftime("%d.%m.%Y %H:%M")


def display_time(dt: Optional[datetime]) -> str:
    if not dt:
        return "—"
    dt = dt + timedelta(hours=DISPLAY_UTC_OFFSET_HOURS)
    return dt.strftime("%H:%M:%S")


def format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def format_minutes(seconds: int) -> str:
    seconds = max(0, int(seconds))
    m = seconds // 60
    if m and seconds % 60 == 0:
        return f"{m} мин"
    if m:
        return f"{m} мин {seconds % 60} сек"
    return f"{seconds} сек"


def user_mention(user_id: int, username: Optional[str], first_name: Optional[str]) -> str:
    if username:
        return "@" + html.escape(username)
    name = html.escape(first_name or "Пользователь")
    return f'<a href="tg://user?id={user_id}">{name}</a>'


def is_admin(user_id: Optional[int]) -> bool:
    return bool(user_id and user_id in ADMIN_IDS)


def esc(value: object) -> str:
    return html.escape(str(value if value is not None else ""))


def gift_display(key: Optional[str]) -> str:
    g = GIFT_MAP.get(key or DEFAULT_GIFT_KEY, GIFT_MAP[DEFAULT_GIFT_KEY])
    return f"{g['emoji']} {g['name']}"


def short_chat_title(title: Optional[str], chat_id: int, max_len: int = 28) -> str:
    t = (title or f"Чат {chat_id}").strip()
    if len(t) > max_len:
        t = t[: max_len - 1] + "…"
    return t


# ============================================================
# БАЗА ДАННЫХ
# ============================================================
class Database:
    def __init__(self, path: str):
        self.path = path
        self._lock = asyncio.Lock()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    async def initialize(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        with closing(self._connect()) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT NOT NULL DEFAULT '',
                    wins INTEGER NOT NULL DEFAULT 0,
                    participations INTEGER NOT NULL DEFAULT 0,
                    steals INTEGER NOT NULL DEFAULT 0,
                    last_win TEXT
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    winner_id INTEGER,
                    status TEXT NOT NULL DEFAULT 'active'
                );
                CREATE TABLE IF NOT EXISTS settings (
                    chat_id INTEGER PRIMARY KEY,
                    auto_start INTEGER NOT NULL DEFAULT 1,
                    interval INTEGER NOT NULL DEFAULT 86400,
                    next_event_time TEXT,
                    duration INTEGER NOT NULL DEFAULT 180,
                    prize_type TEXT NOT NULL DEFAULT 'gift',
                    prize_key TEXT NOT NULL DEFAULT 'bear',
                    nft_link TEXT,
                    nft_name TEXT,
                    message_cost INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS winners (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    username TEXT,
                    event_id INTEGER NOT NULL,
                    won_at TEXT NOT NULL,
                    reward TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bot_state (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                CREATE TABLE IF NOT EXISTS known_chats (
                    chat_id INTEGER PRIMARY KEY,
                    title TEXT,
                    added_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_chat_status
                    ON events(chat_id, status);
                CREATE INDEX IF NOT EXISTS idx_winners_won_at
                    ON winners(won_at DESC);
                """
            )
            cols = [r["name"] for r in conn.execute("PRAGMA table_info(settings)").fetchall()]

            def _add(name: str, ddl: str) -> None:
                if name not in cols:
                    conn.execute(f"ALTER TABLE settings ADD COLUMN {ddl}")

            _add("duration", "duration INTEGER NOT NULL DEFAULT 180")
            _add("prize_type", "prize_type TEXT NOT NULL DEFAULT 'gift'")
            _add("prize_key", f"prize_key TEXT NOT NULL DEFAULT '{DEFAULT_GIFT_KEY}'")
            _add("nft_link", "nft_link TEXT")
            _add("nft_name", "nft_name TEXT")
            _add("message_cost", "message_cost INTEGER NOT NULL DEFAULT 0")
            conn.commit()

    async def get_bot_state(self, key: str) -> Optional[str]:
        return await asyncio.to_thread(self._get_bot_state_sync, key)

    def _get_bot_state_sync(self, key: str) -> Optional[str]:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT value FROM bot_state WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else None

    async def set_bot_state(self, key: str, value: Optional[str]) -> None:
        async with self._lock:
            await asyncio.to_thread(self._set_bot_state_sync, key, value)

    def _set_bot_state_sync(self, key: str, value: Optional[str]) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO bot_state(key, value) VALUES (?, ?)", (key, value)
            )
            conn.commit()

    async def register_chat(self, chat_id: int, title: Optional[str]) -> None:
        async with self._lock:
            await asyncio.to_thread(self._register_chat_sync, chat_id, title)

    def _register_chat_sync(self, chat_id: int, title: Optional[str]) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO known_chats(chat_id, title, added_at) VALUES (?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET title = excluded.title
                """,
                (chat_id, title or "", dt_to_str(now_utc())),
            )
            conn.commit()

    async def list_known_chats(self):
        return await asyncio.to_thread(self._list_known_chats_sync)

    def _list_known_chats_sync(self):
        with closing(self._connect()) as conn:
            return conn.execute("SELECT * FROM known_chats ORDER BY added_at DESC").fetchall()

    async def remove_known_chat(self, chat_id: int) -> None:
        async with self._lock:
            await asyncio.to_thread(self._remove_known_chat_sync, chat_id)

    def _remove_known_chat_sync(self, chat_id: int) -> None:
        with closing(self._connect()) as conn:
            conn.execute("DELETE FROM known_chats WHERE chat_id = ?", (chat_id,))
            conn.commit()

    async def ensure_settings(self, chat_id: int) -> None:
        async with self._lock:
            await asyncio.to_thread(self._ensure_settings_sync, chat_id)

    def _ensure_settings_sync(self, chat_id: int) -> None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT chat_id FROM settings WHERE chat_id = ?", (chat_id,)
            ).fetchone()
            if row is None:
                next_time = dt_to_str(now_utc() + timedelta(seconds=DEFAULT_INTERVAL))
                conn.execute(
                    """
                    INSERT INTO settings(
                        chat_id, auto_start, interval, next_event_time, duration,
                        prize_type, prize_key, nft_link, nft_name, message_cost
                    ) VALUES (?, 1, ?, ?, ?, 'gift', ?, NULL, NULL, 0)
                    """,
                    (chat_id, DEFAULT_INTERVAL, next_time, EVENT_DURATION, DEFAULT_GIFT_KEY),
                )
                conn.commit()

    async def get_settings(self, chat_id: int) -> sqlite3.Row:
        await self.ensure_settings(chat_id)
        return await asyncio.to_thread(self._get_settings_sync, chat_id)

    def _get_settings_sync(self, chat_id: int) -> sqlite3.Row:
        with closing(self._connect()) as conn:
            return conn.execute("SELECT * FROM settings WHERE chat_id = ?", (chat_id,)).fetchone()

    async def update_settings(self, chat_id: int, **kwargs) -> None:
        async with self._lock:
            await asyncio.to_thread(self._update_settings_sync, chat_id, kwargs)

    def _update_settings_sync(self, chat_id: int, kw: dict) -> None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM settings WHERE chat_id = ?", (chat_id,)).fetchone()
            if row is None:
                current = {
                    "auto_start": 1, "interval": DEFAULT_INTERVAL,
                    "next_event_time": dt_to_str(now_utc() + timedelta(seconds=DEFAULT_INTERVAL)),
                    "duration": EVENT_DURATION, "prize_type": "gift",
                    "prize_key": DEFAULT_GIFT_KEY, "nft_link": None,
                    "nft_name": None, "message_cost": 0,
                }
            else:
                current = dict(row)

            for key in (
                "auto_start", "interval", "next_event_time", "duration",
                "prize_type", "prize_key", "nft_link", "nft_name", "message_cost",
            ):
                if key not in kw:
                    continue
                value = kw[key]
                if value is None:
                    continue
                if key == "next_event_time" and isinstance(value, datetime):
                    value = dt_to_str(value)
                if key == "auto_start" and isinstance(value, bool):
                    value = int(value)
                current[key] = value

            conn.execute(
                """
                INSERT OR REPLACE INTO settings(
                    chat_id, auto_start, interval, next_event_time, duration,
                    prize_type, prize_key, nft_link, nft_name, message_cost
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chat_id,
                    int(current["auto_start"]),
                    int(current["interval"]),
                    current["next_event_time"],
                    int(current["duration"]),
                    current["prize_type"],
                    current["prize_key"],
                    current["nft_link"],
                    current["nft_name"],
                    int(current["message_cost"] or 0),
                ),
            )
            conn.commit()
            logger.info("settings updated for chat %s: %s", chat_id, kw)

    async def upsert_user(self, user_id: int, username: Optional[str], first_name: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._upsert_user_sync, user_id, username, first_name)

    def _upsert_user_sync(self, user_id: int, username: Optional[str], first_name: str) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO users(user_id, username, first_name) VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username, first_name = excluded.first_name
                """,
                (user_id, username, first_name or ""),
            )
            conn.commit()

    async def increment_participation(self, user_id: int) -> None:
        async with self._lock:
            await asyncio.to_thread(self._increment_column_sync, user_id, "participations")

    async def increment_steal(self, user_id: int) -> None:
        async with self._lock:
            await asyncio.to_thread(self._increment_column_sync, user_id, "steals")

    def _increment_column_sync(self, user_id: int, column: str) -> None:
        if column not in {"participations", "steals"}:
            raise ValueError("Invalid column")
        with closing(self._connect()) as conn:
            conn.execute(
                f"UPDATE users SET {column} = {column} + 1 WHERE user_id = ?", (user_id,)
            )
            conn.commit()

    async def create_event(self, chat_id: int, started_at: datetime) -> int:
        async with self._lock:
            return await asyncio.to_thread(self._create_event_sync, chat_id, dt_to_str(started_at))

    def _create_event_sync(self, chat_id: int, started_at: str) -> int:
        with closing(self._connect()) as conn:
            cur = conn.execute(
                "INSERT INTO events(chat_id, started_at, status) VALUES (?, ?, 'active')",
                (chat_id, started_at),
            )
            conn.commit()
            return int(cur.lastrowid)

    async def finish_event(self, event_id: int, winner_id: Optional[int], finished_at: datetime) -> None:
        async with self._lock:
            await asyncio.to_thread(self._finish_event_sync, event_id, winner_id, dt_to_str(finished_at))

    def _finish_event_sync(self, event_id: int, winner_id: Optional[int], finished_at: str) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "UPDATE events SET status = 'finished', winner_id = ?, finished_at = ? WHERE id = ?",
                (winner_id, finished_at, event_id),
            )
            conn.commit()

    async def cancel_event(self, event_id: int, finished_at: datetime) -> None:
        async with self._lock:
            await asyncio.to_thread(self._cancel_event_sync, event_id, dt_to_str(finished_at))

    def _cancel_event_sync(self, event_id: int, finished_at: str) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "UPDATE events SET status = 'cancelled', finished_at = ? WHERE id = ?",
                (finished_at, event_id),
            )
            conn.commit()

    async def get_active_event(self, chat_id: int) -> Optional[sqlite3.Row]:
        return await asyncio.to_thread(self._get_active_event_sync, chat_id)

    def _get_active_event_sync(self, chat_id: int) -> Optional[sqlite3.Row]:
        with closing(self._connect()) as conn:
            return conn.execute(
                "SELECT * FROM events WHERE chat_id = ? AND status = 'active' ORDER BY id DESC LIMIT 1",
                (chat_id,),
            ).fetchone()

    async def add_winner(self, user_id: int, username: Optional[str], event_id: int, won_at: datetime, reward: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._add_winner_sync, user_id, username, event_id, dt_to_str(won_at), reward)

    def _add_winner_sync(self, user_id: int, username: Optional[str], event_id: int, won_at: str, reward: str) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "UPDATE users SET wins = wins + 1, last_win = ?, username = COALESCE(?, username) WHERE user_id = ?",
                (won_at, username, user_id),
            )
            conn.execute(
                "INSERT INTO winners(user_id, username, event_id, won_at, reward) VALUES (?, ?, ?, ?, ?)",
                (user_id, username, event_id, won_at, reward),
            )
            conn.commit()

    async def get_statistics(self) -> dict:
        return await asyncio.to_thread(self._get_statistics_sync)

    def _get_statistics_sync(self) -> dict:
        with closing(self._connect()) as conn:
            users = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
            wins = conn.execute("SELECT COALESCE(SUM(wins), 0) AS c FROM users").fetchone()["c"]
            steals = conn.execute("SELECT COALESCE(SUM(steals), 0) AS c FROM users").fetchone()["c"]
            events_count = conn.execute("SELECT COUNT(*) AS c FROM events").fetchone()["c"]
            top = conn.execute(
                """
                SELECT user_id, username, first_name, wins FROM users WHERE wins > 0
                ORDER BY wins DESC, last_win DESC LIMIT 3
                """
            ).fetchall()
            return {"users": users, "wins": wins, "steals": steals, "events": events_count, "top": top}

    async def get_user(self, user_id: int) -> Optional[sqlite3.Row]:
        return await asyncio.to_thread(self._get_user_sync, user_id)

    def _get_user_sync(self, user_id: int) -> Optional[sqlite3.Row]:
        with closing(self._connect()) as conn:
            return conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()

    async def get_recent_winners(self, limit: int = 10):
        return await asyncio.to_thread(self._get_recent_winners_sync, limit)

    def _get_recent_winners_sync(self, limit: int):
        with closing(self._connect()) as conn:
            return conn.execute(
                """
                SELECT w.*, u.first_name FROM winners w
                LEFT JOIN users u ON u.user_id = w.user_id
                ORDER BY w.won_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()


# ============================================================
# СОСТОЯНИЕ ИВЕНТА
# ============================================================
class EventState:
    def __init__(
        self, chat_id: int, event_id: int, started_at: datetime, duration: int,
        prize_type: str, base_gift_key: str, nft_link: Optional[str],
        nft_name: Optional[str], message_cost: int,
    ):
        self.chat_id = chat_id
        self.event_id = event_id
        self.started_at = started_at
        self.duration = duration
        self.prize_type = prize_type
        self.base_gift_key = base_gift_key
        self.current_gift_key = base_gift_key
        self.nft_link = nft_link
        self.nft_name = nft_name
        self.message_cost = message_cost

        self.current_user_id: Optional[int] = None
        self.current_username: Optional[str] = None
        self.current_first_name: Optional[str] = None
        self.crown_started_at: Optional[datetime] = None

        self.timer_task: Optional[asyncio.Task] = None
        self.warning_task: Optional[asyncio.Task] = None
        self.escalation_task: Optional[asyncio.Task] = None
        self.last_announcement_message_id: Optional[int] = None
        self.start_message_id: Optional[int] = None
        self.lock = asyncio.Lock()
        self.active = True

    def cancel_leader_tasks(self) -> None:
        cur = asyncio.current_task()
        for t in (self.timer_task, self.warning_task):
            if t and not t.done() and t is not cur:
                t.cancel()
        self.timer_task = None
        self.warning_task = None

    def cancel_all_tasks(self) -> None:
        self.cancel_leader_tasks()
        cur = asyncio.current_task()
        if self.escalation_task and not self.escalation_task.done() and self.escalation_task is not cur:
            self.escalation_task.cancel()
        self.escalation_task = None

    def current_gift(self) -> dict:
        return GIFT_MAP.get(self.current_gift_key, GIFT_MAP[DEFAULT_GIFT_KEY])

    def current_prize_text(self) -> str:
        if self.prize_type == "nft":
            return f"🖼 <b>NFT</b> — {esc(self.nft_name or 'NFT')}"
        g = self.current_gift()
        return f"{g['emoji']} <b>{esc(g['name'])}</b>"

    def current_prize_reward_string(self) -> str:
        if self.prize_type == "nft":
            return f"🖼 NFT — {self.nft_name or 'NFT'}"
        g = self.current_gift()
        return f"{g['emoji']} {g['name']}"


# ============================================================
# БОТ
# ============================================================
class PerebivBot:
    def __init__(self):
        self.db = Database(DB_PATH)
        self.client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
        self.states: dict[int, EventState] = {}
        self.global_lock = asyncio.Lock()
        self.scheduler_task: Optional[asyncio.Task] = None
        self.awaiting_input: dict[int, str] = {}
        self._active_chat_id: Optional[int] = None
        self._bot_id: Optional[int] = None

    def register_handlers(self) -> None:
        self.client.add_event_handler(
            self.handle_start,
            events.NewMessage(pattern=r"^/start(\s|$)", incoming=True),
        )
        self.client.add_event_handler(
            self.handle_callback,
            events.CallbackQuery(),
        )
        self.client.add_event_handler(
            self.handle_admin_input,
            events.NewMessage(
                incoming=True,
                func=lambda e: e.is_private and e.sender_id in ADMIN_IDS and not (e.raw_text or "").startswith("/"),
            ),
        )
        self.client.add_event_handler(
            self.handle_player_message,
            events.NewMessage(
                incoming=True,
                func=lambda e: (e.is_group or e.is_channel) and not (e.raw_text or "").startswith("/"),
            ),
        )

    async def get_active_chat_id(self) -> int:
        if self._active_chat_id is not None:
            return self._active_chat_id
        stored = await self.db.get_bot_state("active_chat_id")
        if stored:
            try:
                self._active_chat_id = int(stored)
                return self._active_chat_id
            except ValueError:
                pass
        self._active_chat_id = DEFAULT_CHAT_ID
        await self.db.set_bot_state("active_chat_id", str(DEFAULT_CHAT_ID))
        return self._active_chat_id

    async def set_active_chat_id(self, chat_id: int) -> None:
        self._active_chat_id = chat_id
        await self.db.set_bot_state("active_chat_id", str(chat_id))
        await self.db.ensure_settings(chat_id)

    # ---------- ЦЕНА ПЛАТНЫХ СООБЩЕНИЙ ----------
    async def set_paid_price(self, chat_id: int, stars: int) -> bool:
        if UpdatePaidMessagesPriceRequest is None:
            logger.error("UpdatePaidMessagesPriceRequest недоступен. Обновите telethon.")
            return False
        try:
            entity = await self.client.get_input_entity(chat_id)
            await self.client(
                UpdatePaidMessagesPriceRequest(
                    channel=entity,
                    send_paid_messages_stars=stars,
                    broadcast_messages_allowed=True,
                )
            )
            logger.info("Paid price for chat %s set to %s stars", chat_id, stars)
            return True
        except ChatAdminRequiredError:
            logger.error("Бот не админ или нет прав менять настройки группы %s", chat_id)
        except ChannelInvalidError:
            logger.error("Неверный channel/chat для %s", chat_id)
        except FloodWaitError as e:
            logger.error("FloodWait: ждать %s сек", e.seconds)
        except RPCError as e:
            logger.error("RPC error при установке цены %s: %s", chat_id, e)
        except Exception:
            logger.exception("Не удалось установить цену для %s", chat_id)
        return False

    async def reset_paid_price(self, chat_id: int) -> None:
        await self.set_paid_price(chat_id, 0)

    def event_start_text(self, state: EventState) -> str:
        mins = max(1, state.duration // 60)
        lines = [
            "<b> ИВЕНТ НАЧАЛСЯ!</b>", "",
            "<b>💬 Отправь сообщение в чат и попробуй стать ПОБЕДИТЕЛЕМ.</b>", "",
            f"<b> Твоя задача — продержаться {mins} мин.</b>", "",
        ]
        if state.prize_type == "nft":
            lines.append(f"<b>🏆 Победитель получает NFT:</b> <b>{esc(state.nft_name or 'NFT')}</b>")
            if state.nft_link:
                lines.append(f'🔗 <a href="{esc(state.nft_link)}">Ссылка на NFT</a>')
        else:
            g = state.current_gift()
            lines.append(f"<b>🏆 Победитель получает: {g['emoji']} {esc(g['name'])}</b>")
            if state.base_gift_key != "diamond":
                lines.append("<i>⏫ Каждый час без победителя приз повышается!</i>")
        if state.message_cost > 0:
            lines.append(f"<b>⭐ Стоимость 1 сообщения: {state.message_cost} звёзд</b>")
        return "\n".join(lines)

    def nft_info_text(self, state: EventState) -> str:
        lines = ["<b>🖼 ПРИЗ — NFT</b>", "", f"📦 Название: <b>{esc(state.nft_name or 'NFT')}</b>"]
        if state.nft_link:
            lines.append(f"🔗 Ссылка: {esc(state.nft_link)}")
        return "\n".join(lines)

    @staticmethod
    def crown_text(user_id, username, first_name, duration) -> str:
        mention = user_mention(user_id, username, first_name)
        mins = max(1, duration // 60)
        return (
            "<b> НОВЫЙ ЛИДЕР!</b>\n\n"
            f"<b> {mention} захватил лидерство!</b>\n\n"
            f"<b> Ему необходимо продержаться: {mins} мин</b>\n"
            "<b> УСПЕЙТЕ ПЕРЕБИТЬ ЕГО!</b>"
        )

    @staticmethod
    def steal_text(user_id, username, first_name, duration) -> str:
        mention = user_mention(user_id, username, first_name)
        mins = max(1, duration // 60)
        return f"<b> 🔄 Перебито!: {mention} До конца: {mins} мин</b>\n"

    @staticmethod
    def warning_text(user_id, username, first_name) -> str:
        mention = user_mention(user_id, username, first_name)
        return (
            "<b> ОСТАЛОСЬ 10 СЕКУНД! </b>\n\n"
            f"<b> {mention} почти победил!</b>\n\n"
            "<b> УСПЕЙ ПЕРЕБИТЬ!</b>"
        )

    def winner_text(self, state: EventState, user_id, username, first_name) -> str:
        mention = user_mention(user_id, username, first_name)
        mins = max(1, state.duration // 60)
        if state.prize_type == "nft":
            prize_block = f"<b>🖼 NFT:</b> <b>{esc(state.nft_name or 'NFT')}</b>"
            if state.nft_link:
                prize_block += f"\n🔗 {esc(state.nft_link)}"
        else:
            g = state.current_gift()
            prize_block = f"<b>{g['emoji']} {esc(g['name'])}</b>"
        return (
            f"<b>👑 ПОБЕДИТЕЛЬ!:</b>\n{mention}\n\n"
            f"<b>🔥 Он продержался целых {mins} минут!</b>\n\n"
            "<b>🎁 НАГРАДА:</b>\n"
            f"{prize_block}\n\n"
            "<b>Поздравляем с победой! ❤️‍🔥</b>"
        )

    def escalation_text(self, new_gift: dict, hours: int) -> str:
        return (
            "<b>⏫ ПРИЗ ПОВЫШЕН!</b>\n\n"
            f"<b>Ивент идёт уже {hours} ч. без победителя.</b>\n"
            f"<b>Новый приз: {new_gift['emoji']} {esc(new_gift['name'])}</b>\n\n"
            "<b>УСПЕЙТЕ ПЕРЕБИТЬ И ЗАБРАТЬ ПРИЗ!</b>"
        )

    async def initialize(self) -> None:
        await self.db.initialize()
        active = await self.get_active_chat_id()
        await self.db.ensure_settings(active)
        try:
            await self.reset_paid_price(active)
        except Exception:
            logger.exception("Не удалось сбросить цену при старте")
        await self.restore_active_events()
        self.scheduler_task = asyncio.create_task(self.scheduler_loop())
        logger.info("Bot initialized. Active chat: %s", active)

    async def shutdown(self) -> None:
        if self.scheduler_task and not self.scheduler_task.done():
            self.scheduler_task.cancel()
            try:
                await self.scheduler_task
            except asyncio.CancelledError:
                pass
        for state in list(self.states.values()):
            async with state.lock:
                state.active = False
                state.cancel_all_tasks()
        self.states.clear()

    async def restore_active_events(self) -> None:
        active = await self.get_active_chat_id()
        row = await self.db.get_active_event(active)
        if row:
            logger.warning("Unfinished event %s; cancelling.", row["id"])
            await self.db.cancel_event(row["id"], now_utc())

    async def scheduler_loop(self) -> None:
        while True:
            try:
                chat_id = await self.get_active_chat_id()
                settings = await self.db.get_settings(chat_id)
                if settings["auto_start"]:
                    next_time = str_to_dt(settings["next_event_time"])
                    current = now_utc()
                    if next_time is None:
                        next_time = current + timedelta(seconds=settings["interval"])
                        await self.db.update_settings(chat_id, next_event_time=next_time)
                    elif current >= next_time:
                        interval = max(1, int(settings["interval"]))
                        next_after = next_time
                        while next_after <= current:
                            next_after += timedelta(seconds=interval)
                        await self.db.update_settings(chat_id, next_event_time=next_after)
                        if chat_id not in self.states:
                            try:
                                await self.start_event(chat_id)
                            except Exception:
                                logger.exception("Auto start failed")
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Scheduler error")
                await asyncio.sleep(5)

    async def start_event(self, chat_id: int) -> bool:
        async with self.global_lock:
            if chat_id in self.states and self.states[chat_id].active:
                return False
            if await self.db.get_active_event(chat_id):
                return False
            settings = await self.db.get_settings(chat_id)
            duration = int(settings["duration"])
            prize_type = settings["prize_type"] or "gift"
            prize_key = settings["prize_key"] or DEFAULT_GIFT_KEY
            nft_link = settings["nft_link"]
            nft_name = settings["nft_name"]
            message_cost = int(settings["message_cost"] or 0)
            if prize_type == "nft" and not nft_link:
                prize_type = "gift"
            if prize_key not in GIFT_MAP:
                prize_key = DEFAULT_GIFT_KEY

            started = now_utc()
            event_id = await self.db.create_event(chat_id, started)
            state = EventState(
                chat_id, event_id, started, duration, prize_type, prize_key,
                nft_link, nft_name, message_cost,
            )
            self.states[chat_id] = state

            price_set = False
            if message_cost > 0:
                price_set = await self.set_paid_price(chat_id, message_cost)
                if not price_set:
                    try:
                        await self.client.send_message(
                            chat_id,
                            "<b>⚠️ Не удалось установить стоимость сообщений автоматически.</b>\n"
                            f"<i>Админ должен вручную поставить {message_cost} ⭐ в настройках группы.</i>",
                            parse_mode="html",
                        )
                    except Exception:
                        logger.exception("Не удалось отправить предупреждение о цене")

            try:
                message = await self.client.send_message(
                    chat_id, self.event_start_text(state), parse_mode="html",
                )
                state.start_message_id = message.id
                try:
                    await self.client.pin_message(chat_id, message.id, notify=False)
                except Exception:
                    logger.warning("Не удалось закрепить сообщение", exc_info=True)

                if prize_type == "nft":
                    try:
                        await self.client.send_message(
                            chat_id, self.nft_info_text(state),
                            parse_mode="html", link_preview=True,
                        )
                    except Exception:
                        logger.exception("NFT info send failed")

                if prize_type == "gift":
                    state.escalation_task = asyncio.create_task(self.prize_escalation_loop(state))
                logger.info("Event %s started in %s (cost=%s, price_set=%s)",
                            event_id, chat_id, message_cost, price_set)
                return True
            except Exception:
                logger.exception("Не удалось анонсировать ивент")
                async with state.lock:
                    state.active = False
                    state.cancel_all_tasks()
                self.states.pop(chat_id, None)
                await self.db.cancel_event(event_id, now_utc())
                if price_set:
                    await self.reset_paid_price(chat_id)
                return False

    async def stop_event(self, chat_id: int, reason: str = "admin") -> bool:
        async with self.global_lock:
            state = self.states.get(chat_id)
            if not state:
                row = await self.db.get_active_event(chat_id)
                if row:
                    await self.db.cancel_event(row["id"], now_utc())
                    await self.db.update_settings(chat_id, message_cost=0)
                    await self.reset_paid_price(chat_id)
                    return True
                return False
            async with state.lock:
                if not state.active:
                    return False
                state.active = False
                state.cancel_all_tasks()
                event_id = state.event_id
            self.states.pop(chat_id, None)
            await self.db.cancel_event(event_id, now_utc())
            await self.db.update_settings(chat_id, message_cost=0)
            await self.reset_paid_price(chat_id)
            return True

    async def handle_player_message(self, event) -> None:
        try:
            chat = await event.get_chat()
            sender = await event.get_sender()
        except Exception:
            return
        if sender is None:
            return

        chat_id = event.chat_id
        if isinstance(chat, (Channel, Chat)):
            try:
                title = getattr(chat, "title", None)
                await self.db.register_chat(chat_id, title)
            except Exception:
                logger.exception("register_chat failed")

        if chat_id not in self.states:
            return

        if self._bot_id and sender.id == self._bot_id:
            return

        username = getattr(sender, "username", None)
        first_name = getattr(sender, "first_name", None) or "Пользователь"

        state = self.states.get(chat_id)
        if not state:
            return

        await self.db.upsert_user(sender.id, username, first_name)

        async with state.lock:
            if not state.active:
                return

            if state.current_user_id is None:
                state.current_user_id = sender.id
                state.current_username = username
                state.current_first_name = first_name
                state.crown_started_at = now_utc()
                await self.db.increment_participation(sender.id)
                state.cancel_leader_tasks()
                state.warning_task = asyncio.create_task(self.warning_after_ten_seconds(state))
                state.timer_task = asyncio.create_task(self.crown_timer(state))
                text = self.crown_text(sender.id, username, first_name, state.duration)
            elif state.current_user_id == sender.id:
                return
            else:
                state.current_user_id = sender.id
                state.current_username = username
                state.current_first_name = first_name
                state.crown_started_at = now_utc()
                await self.db.increment_participation(sender.id)
                await self.db.increment_steal(sender.id)
                state.cancel_leader_tasks()
                state.warning_task = asyncio.create_task(self.warning_after_ten_seconds(state))
                state.timer_task = asyncio.create_task(self.crown_timer(state))
                text = self.steal_text(sender.id, username, first_name, state.duration)

            try:
                if state.last_announcement_message_id is not None:
                    try:
                        await self.client.delete_messages(chat_id, [state.last_announcement_message_id])
                    except Exception:
                        pass
                new_message = await self.client.send_message(
                    chat_id, text, parse_mode="html", reply_to=event.message.id,
                )
                state.last_announcement_message_id = new_message.id
            except Exception:
                logger.exception("Announce crown failed")

    async def warning_after_ten_seconds(self, state: EventState) -> None:
        try:
            await asyncio.sleep(max(0, state.duration - 10))
            async with state.lock:
                if not state.active or state.current_user_id is None:
                    return
                await self.client.send_message(
                    state.chat_id,
                    self.warning_text(
                        state.current_user_id, state.current_username,
                        state.current_first_name or "Пользователь",
                    ),
                    parse_mode="html",
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Warning task failed")

    async def crown_timer(self, state: EventState) -> None:
        try:
            await asyncio.sleep(state.duration)
            async with state.lock:
                if not state.active or state.current_user_id is None:
                    return
                winner_id = state.current_user_id
                username = state.current_username
                first_name = state.current_first_name or "Пользователь"
                event_id = state.event_id
                reward_string = state.current_prize_reward_string()
                state.active = False
                state.cancel_all_tasks()
                await self.db.add_winner(winner_id, username, event_id, now_utc(), reward_string)
                await self.db.finish_event(event_id, winner_id, now_utc())
                await self.db.update_settings(state.chat_id, message_cost=0)
                self.states.pop(state.chat_id, None)
                await self.reset_paid_price(state.chat_id)
                try:
                    await self.client.send_message(
                        state.chat_id,
                        self.winner_text(state, winner_id, username, first_name),
                        parse_mode="html", link_preview=True,
                    )
                except Exception:
                    logger.exception("Winner send failed")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Crown timer failed")

    @staticmethod
    def escalated_gift_key(base_key: str, hours: int) -> str:
        base_i = GIFT_INDEX.get(base_key, 0)
        return GIFTS[min(base_i + hours, MAX_GIFT_INDEX)]["key"]

    async def prize_escalation_loop(self, state: EventState) -> None:
        try:
            while True:
                if not state.active or state.prize_type != "gift":
                    return
                elapsed = (now_utc() - state.started_at).total_seconds()
                hours = int(elapsed // 3600)
                new_key = self.escalated_gift_key(state.base_gift_key, hours)
                if new_key != state.current_gift_key:
                    state.current_gift_key = new_key
                    gift = GIFT_MAP[new_key]
                    try:
                        await self.client.send_message(
                            state.chat_id, self.escalation_text(gift, hours),
                            parse_mode="html",
                        )
                    except Exception:
                        logger.exception("Escalation send failed")
                next_boundary = state.started_at + timedelta(hours=hours + 1)
                wait = (next_boundary - now_utc()).total_seconds()
                await asyncio.sleep(max(5.0, wait))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Escalation task failed")

    # ========================================================
    # АДМИН-ПАНЕЛЬ
    # ========================================================
    def admin_buttons(self) -> list:
        return [
            [Button.inline("📊 Статистика", b"admin:stats"),
             Button.inline("🏆 Победители", b"admin:winners")],
            [Button.inline("👑 Текущий ивент", b"admin:current"),
             Button.inline("⏰ Запуск", b"admin:schedule")],
            [Button.inline("🎁 Приз", b"admin:prize"),
             Button.inline("⭐ Цена сообщения", b"admin:cost")],
            [Button.inline("⏱ Длительность", b"admin:duration")],
            [Button.inline("▶️ Запустить", b"admin:start"),
             Button.inline("⏹ Остановить", b"admin:stop")],
            [Button.inline("🔄 Перезапустить", b"admin:restart"),
             Button.inline("💬 Чат", b"admin:chat")],
            [Button.inline("🔎 Пользователь по ID", b"admin:user")],
        ]

    @staticmethod
    def admin_text() -> str:
        return (
            "<b>╔════════════════════╗</b>\n"
            "<b>⚙️ АДМИН-ПАНЕЛЬ</b>\n"
            "<b>╚════════════════════╝</b>\n"
            "Управление ивентом <b>«ПЕРЕБИВ»</b>.\n"
            "Всё делается кнопками ниже."
        )

    async def handle_start(self, event) -> None:
        user_id = event.sender_id
        chat = await event.get_chat()
        if not event.is_private and isinstance(chat, (Channel, Chat)):
            try:
                await self.db.register_chat(chat.id, getattr(chat, "title", None))
            except Exception:
                logger.exception("register_chat failed")
        self.awaiting_input.pop(user_id, None)
        if is_admin(user_id):
            await event.respond(
                self.admin_text(),
                buttons=self.admin_buttons(),
                parse_mode="html",
            )
        else:
            await event.respond("❌ ПОШЕЛ НАХYЙ ОТ СЮДА.")

    async def handle_callback(self, event) -> None:
        user_id = event.sender_id
        if not is_admin(user_id):
            await event.answer("❌ Нет доступа", alert=True)
            return
        await event.answer()
        self.awaiting_input.pop(user_id, None)

        try:
            data = event.data.decode()
        except Exception:
            return

        try:
            if data == "admin:menu":
                await self._edit(event, self.admin_text(), self.admin_buttons())
            elif data == "admin:stats":
                await self.show_stats(event)
            elif data == "admin:winners":
                await self.show_winners(event)
            elif data == "admin:current":
                await self.show_current(event)
            elif data == "admin:schedule":
                await self.show_schedule(event)
            elif data == "admin:duration":
                await self.show_duration(event)
            elif data.startswith("admin:duration:"):
                chat_id = await self.get_active_chat_id()
                seconds = int(data.rsplit(":", 1)[1])
                await self.db.update_settings(chat_id, duration=seconds)
                await event.answer(f"⏱ {format_minutes(seconds)}", alert=True)
                await self.show_duration(event)

            elif data == "admin:prize":
                await self.show_prize_menu(event)
            elif data.startswith("admin:prize:gift:"):
                chat_id = await self.get_active_chat_id()
                key = data.rsplit(":", 1)[1]
                if key in GIFT_MAP:
                    await self.db.update_settings(chat_id, prize_type="gift", prize_key=key)
                    await event.answer(
                        f"🎁 Приз: {GIFT_MAP[key]['emoji']} {GIFT_MAP[key]['name']}",
                        alert=True,
                    )
                await self.show_prize_menu(event)
            elif data == "admin:prize:nft":
                self.awaiting_input[user_id] = "nft"
                await self._edit(
                    event,
                    "<b>🖼 НАСТРОЙКА NFT-ПРИЗА</b>\n\n"
                    "Пришлите <b>одним сообщением</b> в этот чат:\n"
                    "<code>ССЫЛКА [Название]</code>\n\n"
                    "Пример:\n<code>https://t.me/nft/SomeNFT Платиновый Кубок</code>",
                    [[Button.inline("⬅️ Отмена", b"admin:prize")]],
                )

            elif data == "admin:cost":
                await self.show_cost_menu(event)
            elif data.startswith("admin:cost:set:"):
                chat_id = await self.get_active_chat_id()
                value = max(0, min(MAX_MESSAGE_COST, int(data.rsplit(":", 1)[1])))
                await self.db.update_settings(chat_id, message_cost=value)
                state = self.states.get(chat_id)
                if state and state.active:
                    state.message_cost = value
                    await self.set_paid_price(chat_id, value)
                    try:
                        await self.client.edit_message(
                            chat_id, state.start_message_id,
                            self.event_start_text(state),
                            parse_mode="html",
                        )
                    except Exception:
                        logger.warning("Could not edit start message", exc_info=True)
                await event.answer(f"⭐ Стоимость 1 сообщения: {value}", alert=True)
                await self.show_cost_menu(event)
            elif data == "admin:cost:manual":
                self.awaiting_input[user_id] = "cost"
                await self._edit(
                    event,
                    "<b>⭐ СТОИМОСТЬ СООБЩЕНИЯ</b>\n\n"
                    f"Пришлите <b>одно число</b> от 0 до {MAX_MESSAGE_COST}.",
                    [[Button.inline("⬅️ Отмена", b"admin:cost")]],
                )

            elif data == "admin:start":
                chat_id = await self.get_active_chat_id()
                started = await self.start_event(chat_id)
                await event.answer(
                    "▶️ Ивент запущен" if started else "⚠️ Ивент уже активен", alert=True,
                )
                await self._edit(event, self.admin_text(), self.admin_buttons())
            elif data == "admin:stop":
                chat_id = await self.get_active_chat_id()
                stopped = await self.stop_event(chat_id, "admin")
                await event.answer(
                    "⏹ Ивент остановлен" if stopped else "⚠️ Активного ивента нет", alert=True,
                )
                await self._edit(event, self.admin_text(), self.admin_buttons())
            elif data == "admin:restart":
                chat_id = await self.get_active_chat_id()
                await self.stop_event(chat_id, "restart")
                started = await self.start_event(chat_id)
                await event.answer(
                    "🔄 Ивент перезапущен" if started else "⚠️ Не удалось запустить", alert=True,
                )
                await self._edit(event, self.admin_text(), self.admin_buttons())

            elif data == "admin:chat":
                await self.show_chat_menu(event)
            elif data.startswith("admin:chat:use:"):
                chat_id = int(data.rsplit(":", 1)[1])
                await self.set_active_chat_id(chat_id)
                await event.answer("✅ Активный чат изменён", alert=True)
                await self.show_chat_menu(event)
            elif data.startswith("admin:chat:del:"):
                chat_id = int(data.rsplit(":", 1)[1])
                current = await self.get_active_chat_id()
                if chat_id == current:
                    await event.answer("❌ Нельзя удалить активный чат", alert=True)
                    return
                await self.db.remove_known_chat(chat_id)
                await event.answer("🗑 Удалено", alert=True)
                await self.show_chat_menu(event)
            elif data == "admin:chat:refresh":
                await self.show_chat_menu(event)

            elif data == "admin:user":
                self.awaiting_input[user_id] = "user_lookup"
                await self._edit(
                    event,
                    "<b>🔎 ПОЛЬЗОВАТЕЛЬ ПО ID</b>\n\nПришлите в этот чат Telegram ID (число).",
                    [[Button.inline("⬅️ Отмена", b"admin:menu")]],
                )

            elif data == "admin:autoon":
                chat_id = await self.get_active_chat_id()
                await self.db.update_settings(chat_id, auto_start=True)
                await event.answer("▶️ Автозапуск включён", alert=True)
                await self.show_schedule(event)
            elif data == "admin:autooff":
                chat_id = await self.get_active_chat_id()
                await self.db.update_settings(chat_id, auto_start=False)
                await event.answer("⏹ Автозапуск выключен", alert=True)
                await self.show_schedule(event)
            elif data.startswith("admin:interval:"):
                chat_id = await self.get_active_chat_id()
                seconds = int(data.rsplit(":", 1)[1])
                await self.db.update_settings(
                    chat_id, interval=seconds,
                    next_event_time=now_utc() + timedelta(seconds=seconds),
                )
                await event.answer(f"🔄 Интервал: {seconds // 3600} ч.", alert=True)
                await self.show_schedule(event)
            elif data == "admin:next":
                self.awaiting_input[user_id] = "next_time"
                await self._edit(
                    event,
                    "<b>⏰ ИЗМЕНЕНИЕ ВРЕМЕНИ</b>\n\n"
                    "Пришлите дату и время:\n<code>ДД.ММ.ГГГГ ЧЧ:ММ</code>\n\n"
                    "Пример: <code>03.10.2026 20:00</code>\n"
                    "Время трактуется относительно UTC+0.",
                    [[Button.inline("⬅️ Отмена", b"admin:schedule")]],
                )
            else:
                logger.warning("Unknown callback: %s", data)
        except MessageNotModifiedError:
            pass
        except Exception:
            logger.exception("Admin callback failed")
            try:
                await event.answer("⚠️ Произошла ошибка", alert=True)
            except Exception:
                pass

    async def _edit(self, event, text: str, buttons: list) -> None:
        try:
            await event.edit(text, buttons=buttons, parse_mode="html")
        except MessageNotModifiedError:
            pass
        except MessageIdInvalidError:
            await event.respond(text, buttons=buttons, parse_mode="html")

    async def handle_admin_input(self, event) -> None:
        user_id = event.sender_id
        if user_id not in ADMIN_IDS:
            return
        text = (event.raw_text or "").strip()
        if not text:
            return

        action = self.awaiting_input.get(user_id)
        if action is None:
            await event.respond(
                self.admin_text(), buttons=self.admin_buttons(), parse_mode="html",
            )
            return

        back = [[Button.inline("⬅️ В админ-панель", b"admin:menu")]]
        chat_id = await self.get_active_chat_id()

        try:
            if action == "nft":
                parts = text.split(maxsplit=1)
                link = parts[0]
                name = parts[1].strip() if len(parts) > 1 else "NFT"
                if not (link.startswith("http://") or link.startswith("https://")):
                    await event.respond(
                        "❌ Ссылка должна начинаться с http:// или https://. Попробуйте снова."
                    )
                    return
                await self.db.update_settings(chat_id, prize_type="nft", nft_link=link, nft_name=name)
                self.awaiting_input.pop(user_id, None)
                await event.respond(
                    "✅ <b>Приз переключён на NFT</b>\n\n"
                    f"📦 Название: <b>{esc(name)}</b>\n🔗 {esc(link)}",
                    parse_mode="html", buttons=back, link_preview=False,
                )

            elif action == "cost":
                try:
                    value = int(text)
                except ValueError:
                    await event.respond("❌ Нужно число. Попробуйте снова.")
                    return
                if not (0 <= value <= MAX_MESSAGE_COST):
                    await event.respond(f"❌ Диапазон 0–{MAX_MESSAGE_COST}.")
                    return
                await self.db.update_settings(chat_id, message_cost=value)
                state = self.states.get(chat_id)
                if state and state.active:
                    state.message_cost = value
                    await self.set_paid_price(chat_id, value)
                    try:
                        await self.client.edit_message(
                            chat_id, state.start_message_id,
                            self.event_start_text(state), parse_mode="html",
                        )
                    except Exception:
                        logger.warning("Could not edit start message", exc_info=True)
                self.awaiting_input.pop(user_id, None)
                await event.respond(
                    f"✅ Стоимость 1 сообщения: <b>{value} ⭐</b>",
                    parse_mode="html", buttons=back,
                )

            elif action == "next_time":
                try:
                    local_dt = datetime.strptime(text, "%d.%m.%Y %H:%M")
                    target = local_dt.replace(tzinfo=UTC)
                except ValueError:
                    await event.respond("❌ Формат: <code>03.10.2026 20:00</code>", parse_mode="html")
                    return
                if target <= now_utc():
                    await event.respond("❌ Время должно быть в будущем.")
                    return
                await self.db.update_settings(chat_id, next_event_time=target)
                self.awaiting_input.pop(user_id, None)
                await event.respond(
                    f"✅ Следующий запуск: <b>{display_dt(dt_to_str(target))}</b>",
                    parse_mode="html", buttons=back,
                )

            elif action == "user_lookup":
                try:
                    lookup_id = int(text)
                except ValueError:
                    await event.respond("❌ ID — это число.")
                    return
                row = await self.db.get_user(lookup_id)
                self.awaiting_input.pop(user_id, None)
                if not row:
                    await event.respond(
                        f"Пользователь <code>{lookup_id}</code> не участвовал.",
                        parse_mode="html", buttons=back,
                    )
                    return
                username = f"@{esc(row['username'])}" if row["username"] else "нет username"
                await event.respond(
                    "<b>👤 ПОЛЬЗОВАТЕЛЬ</b>\n"
                    f"🆔 ID: <code>{row['user_id']}</code>\n"
                    f"👤 Имя: {esc(row['first_name'])}\n"
                    f"🔗 Username: {username}\n"
                    f"🏆 Побед: <b>{row['wins']}</b>\n"
                    f"🎮 Участий: <b>{row['participations']}</b>\n"
                    f"💥 Перебивов: <b>{row['steals']}</b>\n"
                    f"📅 Последняя победа: <b>{display_dt(row['last_win'])}</b>",
                    parse_mode="html", buttons=back,
                )
        except Exception:
            logger.exception("Admin input failed")

    async def show_stats(self, event) -> None:
        stats = await self.db.get_statistics()
        lines = [
            "<b>📊 СТАТИСТИКА</b>", "",
            f"👥 Участников: <b>{stats['users']}</b>",
            f"🏆 Всего побед: <b>{stats['wins']}</b>",
            f"💥 Всего перебивов: <b>{stats['steals']}</b>",
            f"🎮 Проведено ивентов: <b>{stats['events']}</b>",
            "", "<b>👑 ТОП ПОБЕДИТЕЛЕЙ:</b>",
        ]
        medals = ["🥇", "🥈", "🥉"]
        for i, row in enumerate(stats["top"]):
            name = f"@{esc(row['username'])}" if row["username"] else esc(row["first_name"] or row["user_id"])
            lines.append(f"{medals[i]} {name} — <b>{row['wins']}</b> побед")
        await self._edit(
            event, "\n".join(lines),
            [[Button.inline("⬅️ Назад", b"admin:menu")]],
        )

    async def show_winners(self, event) -> None:
        rows = await self.db.get_recent_winners(10)
        lines = ["<b>🏆 ПОСЛЕДНИЕ ПОБЕДИТЕЛИ</b>", ""]
        if not rows:
            lines.append("Пока побед нет.")
        else:
            for i, row in enumerate(rows, 1):
                name = f"@{esc(row['username'])}" if row["username"] else esc(row["first_name"] or row["user_id"])
                lines.extend([
                    f"<b>{i}.</b> 👑 {name} — {esc(row['reward'] or '—')}",
                    f"    {display_dt(row['won_at'])}",
                    f"   ⏱ {display_time(str_to_dt(row['won_at']))}",
                ])
        await self._edit(
            event, "\n".join(lines),
            [[Button.inline("⬅️ Назад", b"admin:menu")]],
        )

    async def show_current(self, event) -> None:
        chat_id = await self.get_active_chat_id()
        state = self.states.get(chat_id)
        if not state or not state.active:
            text = "<b>👑 ТЕКУЩИЙ ИВЕНТ</b>\n⏹ Активного ивента сейчас нет."
        else:
            async with state.lock:
                prize_line = f"🎁 Приз: {state.current_prize_text()}"
                extra = []
                if state.prize_type == "gift" and state.base_gift_key != "diamond":
                    extra.append(f"<i>Стартовый: {gift_display(state.base_gift_key)}</i>")
                if state.message_cost > 0:
                    extra.append(f"⭐ Цена 1 сообщения: <b>{state.message_cost}</b>")
                if state.current_user_id is None:
                    text = (
                        "<b>👑 ТЕКУЩИЙ ИВЕНТ</b>\n Ивент активен.\n👤 Лидер ещё не выбран.\n"
                        f"{prize_line}\n" + ("\n".join(extra) if extra else "")
                    )
                else:
                    elapsed = int((now_utc() - state.crown_started_at).total_seconds()) if state.crown_started_at else 0
                    remaining = max(0, state.duration - elapsed)
                    mention = user_mention(state.current_user_id, state.current_username, state.current_first_name or "Пользователь")
                    text = (
                        "<b>👑 ТЕКУЩИЙ ИВЕНТ</b>\n Статус: <b>активен</b>\n"
                        f"👑 Лидер: {mention}\n"
                        f"⏱ Длительность: <b>{format_minutes(state.duration)}</b>\n"
                        f"⏳ Осталось: <b>{format_duration(remaining)}</b>\n"
                        f"{prize_line}\n" + ("\n".join(extra) if extra else "")
                        + f"\n Event ID: <code>{state.event_id}</code>"
                    )
        await self._edit(event, text, [[Button.inline("⬅️ Назад", b"admin:menu")]])

    async def show_duration(self, event) -> None:
        chat_id = await self.get_active_chat_id()
        settings = await self.db.get_settings(chat_id)
        duration = int(settings["duration"])
        text = (
            "<b>⏱ ДЛИТЕЛЬНОСТЬ УДЕРЖАНИЯ</b>\n\n"
            f"Текущее значение: <b>{format_minutes(duration)}</b>"
        )
        buttons = [
            [Button.inline("1 мин", b"admin:duration:60"),
             Button.inline("3 мин", b"admin:duration:180")],
            [Button.inline("5 мин", b"admin:duration:300"),
             Button.inline("10 мин", b"admin:duration:600")],
            [Button.inline("⬅️ Назад", b"admin:menu")],
        ]
        await self._edit(event, text, buttons)

    async def show_prize_menu(self, event) -> None:
        chat_id = await self.get_active_chat_id()
        s = await self.db.get_settings(chat_id)
        prize_type = s["prize_type"] or "gift"
        prize_key = s["prize_key"] or DEFAULT_GIFT_KEY
        nft_link = s["nft_link"]
        nft_name = s["nft_name"]

        if prize_type == "nft" and nft_link:
            current_line = f"Текущий приз: <b>🖼 NFT — {esc(nft_name or 'NFT')}</b>\n🔗 {esc(nft_link)}"
        else:
            current_line = f"Текущий приз: <b>{gift_display(prize_key)}</b>"

        text = (
            "<b>🎁 НАСТРОЙКА ПРИЗА</b>\n\n"
            f"{current_line}\n\n"
            "Нажмите на подарок — он станет призом.\n"
            "<i>Каждый час без победителя приз повышается до 💎 Алмаза.</i>\n"
            "<i>Для NFT эскалация не работает.</i>"
        )
        rows = []
        row = []
        for g in GIFTS:
            mark = "✅ " if (prize_type == "gift" and g["key"] == prize_key) else ""
            row.append(Button.inline(
                f"{mark}{g['emoji']} {g['name']}",
                f"admin:prize:gift:{g['key']}".encode(),
            ))
            if len(row) == 2:
                rows.append(row); row = []
        if row:
            rows.append(row)
        rows.append([Button.inline(
            ("✅ " if prize_type == "nft" else "") + "🖼 NFT (по ссылке)",
            b"admin:prize:nft",
        )])
        rows.append([Button.inline("⬅️ Назад", b"admin:menu")])
        await self._edit(event, text, rows)

    async def show_cost_menu(self, event) -> None:
        chat_id = await self.get_active_chat_id()
        s = await self.db.get_settings(chat_id)
        cost = int(s["message_cost"] or 0)

        chats = await self.db.list_known_chats()
        chat_title = None
        for c in chats:
            if c["chat_id"] == chat_id:
                chat_title = c["title"]
                break
        chat_label = esc(short_chat_title(chat_title, chat_id)) if chat_title else f"ID {chat_id}"

        text = (
            "<b>⭐ СТОИМОСТЬ СООБЩЕНИЯ</b>\n\n"
            f"Применяется к чату: <b>{chat_label}</b>\n"
            f"<code>{chat_id}</code>\n\n"
            f"Текущее значение: <b>{cost} ⭐</b>\n\n"
            "<i>Бот сам устанавливает эту цену в группе через MTProto.\n"
            "Сбрасывается на 0 после окончания ивента.</i>"
        )
        values = [0, 5, 10, 15, 20, 30, 50]
        rows = []
        row = []
        for v in values:
            mark = "✅ " if v == cost else ""
            row.append(Button.inline(f"{mark}{v} ⭐", f"admin:cost:set:{v}".encode()))
            if len(row) == 3:
                rows.append(row); row = []
        if row:
            rows.append(row)
        rows.append([Button.inline("✏️ Ввести вручную", b"admin:cost:manual")])
        rows.append([Button.inline("⬅️ Назад", b"admin:menu")])
        await self._edit(event, text, rows)

    async def show_schedule(self, event) -> None:
        chat_id = await self.get_active_chat_id()
        s = await self.db.get_settings(chat_id)
        interval = int(s["interval"])
        hours = interval // 3600
        minutes = (interval % 3600) // 60
        if hours and minutes:
            interval_text = f"{hours} ч. {minutes} мин."
        elif hours:
            interval_text = f"{hours} ч."
        else:
            interval_text = f"{minutes} мин."
        text = (
            "<b>⏰ НАСТРОЙКА ЗАПУСКА</b>\n"
            f"⏰ Следующий запуск:\n<b>{display_dt(s['next_event_time'])}</b>\n"
            f"🕐 Интервал: <b>{interval_text}</b>\n"
            f"⏱ Длительность: <b>{format_minutes(int(s['duration']))}</b>\n"
            f"▶️ Автозапуск: <b>{'включён' if s['auto_start'] else 'выключен'}</b>"
        )
        buttons = [
            [Button.inline("➕ Изменить время", b"admin:next")],
            [Button.inline("🔄 24 часа", b"admin:interval:86400"),
             Button.inline("🔄 12 часов", b"admin:interval:43200"),
             Button.inline("🔄 1 час", b"admin:interval:3600")],
            [Button.inline("⏱ Длительность", b"admin:duration")],
            [Button.inline("▶️ Включить", b"admin:autoon"),
             Button.inline("⏹ Выключить", b"admin:autooff")],
            [Button.inline("⬅️ Назад", b"admin:menu")],
        ]
        await self._edit(event, text, buttons)

    async def show_chat_menu(self, event) -> None:
        active = await self.get_active_chat_id()
        chats = await self.db.list_known_chats()

        lines = [
            "<b>💬 АКТИВНЫЙ ЧАТ</b>", "",
            f"Сейчас бот работает в чате:\n<code>{active}</code>",
            "", "<b>Известные чаты:</b>",
        ]
        if not chats:
            lines.append("<i>Список пуст. Добавьте бота в группу и напишите там любое сообщение.</i>")
        else:
            for c in chats:
                mark = "✅ " if c["chat_id"] == active else "▫️ "
                lines.append(f"{mark}{esc(short_chat_title(c['title'], c['chat_id']))} — <code>{c['chat_id']}</code>")
        lines.append("")
        lines.append("<i>Нажмите на чат, чтобы сделать его активным.</i>")

        rows = []
        for c in chats:
            mark = "✅ " if c["chat_id"] == active else ""
            rows.append([
                Button.inline(
                    f"{mark}{short_chat_title(c['title'], c['chat_id'], 24)}",
                    f"admin:chat:use:{c['chat_id']}".encode(),
                ),
                Button.inline("🗑", f"admin:chat:del:{c['chat_id']}".encode()),
            ])
        rows.append([Button.inline("🔄 Обновить список", b"admin:chat:refresh")])
        rows.append([Button.inline("⬅️ Назад", b"admin:menu")])
        await self._edit(event, "\n".join(lines), rows)

    async def run(self) -> None:
        await self.client.start()
        me = await self.client.get_me()
        self._bot_id = me.id
        logger.info("Bot started as @%s (id=%s)", me.username, me.id)
        await self.initialize()
        self.register_handlers()
        try:
            await self.client.run_until_disconnected()
        finally:
            await self.shutdown()


# ============================================================
# MAIN
# ============================================================
def main() -> None:
    if not SESSION_STRING:
        raise RuntimeError("SESSION_STRING пустой. Вставьте строку сессии в код.")
    bot = PerebivBot()
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
