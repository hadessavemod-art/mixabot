#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram bot "ПЕРЕБИВ"
Python 3.11+
python-telegram-bot 22.x
Вся логика — в одном файле.
УПРАВЛЕНИЕ ТОЛЬКО ЧЕРЕЗ КНОПКИ.
"""
import asyncio
import html
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from contextlib import closing
from typing import Optional
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ============================================================
# НАСТРОЙКИ — ИЗМЕНИТЕ ТОЛЬКО ЭТИ ЗНАЧЕНИЯ
# ============================================================
BOT_TOKEN = "8865782064:AAF_QRh0UpmS80C-u7bUcRi3IOM8jWB6zmk"
ADMIN_IDS = [
    1592503829,
    7831720836,
]
DEFAULT_CHAT_ID = -1002781123506
EVENT_DURATION = 180
DEFAULT_INTERVAL = 86400
DB_PATH = "perebiv.sqlite3"

DISPLAY_UTC_OFFSET_HOURS = 0

# Обычные Telegram-подарки в порядке возрастания ценности.
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

# ============================================================
# ЛОГИРОВАНИЕ
# ============================================================
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("perebiv")

UTC = timezone.utc


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


# ============================================================
# SQLITE
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
                CREATE INDEX IF NOT EXISTS idx_events_chat_status
                    ON events(chat_id, status);
                CREATE INDEX IF NOT EXISTS idx_winners_won_at
                    ON winners(won_at DESC);
                """
            )
            cols = [r["name"] for r in conn.execute("PRAGMA table_info(settings)").fetchall()]

            def _add_column_if_missing(name: str, ddl: str) -> None:
                if name not in cols:
                    conn.execute(f"ALTER TABLE settings ADD COLUMN {ddl}")

            _add_column_if_missing("duration", "duration INTEGER NOT NULL DEFAULT 180")
            _add_column_if_missing("prize_type", "prize_type TEXT NOT NULL DEFAULT 'gift'")
            _add_column_if_missing(
                "prize_key", f"prize_key TEXT NOT NULL DEFAULT '{DEFAULT_GIFT_KEY}'"
            )
            _add_column_if_missing("nft_link", "nft_link TEXT")
            _add_column_if_missing("nft_name", "nft_name TEXT")
            _add_column_if_missing("message_cost", "message_cost INTEGER NOT NULL DEFAULT 0")
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
            return conn.execute(
                "SELECT * FROM settings WHERE chat_id = ?", (chat_id,)
            ).fetchone()

    async def update_settings(
        self,
        chat_id: int,
        *,
        auto_start: Optional[bool] = None,
        interval: Optional[int] = None,
        next_event_time: Optional[datetime] = None,
        duration: Optional[int] = None,
        prize_type: Optional[str] = None,
        prize_key: Optional[str] = None,
        nft_link: Optional[str] = None,
        nft_name: Optional[str] = None,
        message_cost: Optional[int] = None,
    ) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._update_settings_sync,
                chat_id,
                auto_start,
                interval,
                dt_to_str(next_event_time) if next_event_time else None,
                duration,
                prize_type,
                prize_key,
                nft_link,
                nft_name,
                message_cost,
            )

    def _update_settings_sync(
        self,
        chat_id: int,
        auto_start: Optional[bool],
        interval: Optional[int],
        next_event_time: Optional[str],
        duration: Optional[int],
        prize_type: Optional[str],
        prize_key: Optional[str],
        nft_link: Optional[str],
        nft_name: Optional[str],
        message_cost: Optional[int],
    ) -> None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM settings WHERE chat_id = ?", (chat_id,)
            ).fetchone()
            if row is None:
                current = {
                    "auto_start": 1,
                    "interval": DEFAULT_INTERVAL,
                    "next_event_time": dt_to_str(now_utc() + timedelta(seconds=DEFAULT_INTERVAL)),
                    "duration": EVENT_DURATION,
                    "prize_type": "gift",
                    "prize_key": DEFAULT_GIFT_KEY,
                    "nft_link": None,
                    "nft_name": None,
                    "message_cost": 0,
                }
            else:
                current = {
                    "auto_start": row["auto_start"],
                    "interval": row["interval"],
                    "next_event_time": row["next_event_time"],
                    "duration": row["duration"],
                    "prize_type": row["prize_type"],
                    "prize_key": row["prize_key"],
                    "nft_link": row["nft_link"],
                    "nft_name": row["nft_name"],
                    "message_cost": row["message_cost"],
                }

            updates = {
                "auto_start": auto_start,
                "interval": interval,
                "next_event_time": next_event_time,
                "duration": duration,
                "prize_type": prize_type,
                "prize_key": prize_key,
                "nft_link": nft_link,
                "nft_name": nft_name,
                "message_cost": message_cost,
            }
            for k, v in updates.items():
                if v is not None:
                    current[k] = v

            if isinstance(current.get("auto_start"), bool):
                current["auto_start"] = int(current["auto_start"])

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
                    int(current["message_cost"]),
                ),
            )
            conn.commit()

    async def upsert_user(self, user_id: int, username: Optional[str], first_name: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._upsert_user_sync, user_id, username, first_name)

    def _upsert_user_sync(self, user_id: int, username: Optional[str], first_name: str) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO users(user_id, username, first_name)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    first_name = excluded.first_name
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
            raise ValueError("Invalid user statistic column")
        with closing(self._connect()) as conn:
            conn.execute(
                f"UPDATE users SET {column} = {column} + 1 WHERE user_id = ?",
                (user_id,),
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
            events = conn.execute("SELECT COUNT(*) AS c FROM events").fetchone()["c"]
            top = conn.execute(
                """
                SELECT user_id, username, first_name, wins
                FROM users WHERE wins > 0
                ORDER BY wins DESC, last_win DESC LIMIT 3
                """
            ).fetchall()
            return {"users": users, "wins": wins, "steals": steals, "events": events, "top": top}

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
                SELECT w.*, u.first_name
                FROM winners w
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
        self,
        chat_id: int,
        event_id: int,
        started_at: datetime,
        duration: int,
        prize_type: str,
        base_gift_key: str,
        nft_link: Optional[str],
        nft_name: Optional[str],
        message_cost: int,
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
        self.lock = asyncio.Lock()
        self.active = True

    def cancel_leader_tasks(self) -> None:
        current = asyncio.current_task()
        for task in (self.timer_task, self.warning_task):
            if task and not task.done() and task is not current:
                task.cancel()
        self.timer_task = None
        self.warning_task = None

    def cancel_all_tasks(self) -> None:
        self.cancel_leader_tasks()
        current = asyncio.current_task()
        if self.escalation_task and not self.escalation_task.done() and self.escalation_task is not current:
            self.escalation_task.cancel()
        self.escalation_task = None

    def current_gift(self) -> dict:
        return GIFT_MAP.get(self.current_gift_key, GIFT_MAP[DEFAULT_GIFT_KEY])

    def current_prize_text(self) -> str:
        if self.prize_type == "nft":
            name = self.nft_name or "NFT"
            return f"🖼 <b>NFT</b> — {esc(name)}"
        g = self.current_gift()
        return f"{g['emoji']} <b>{esc(g['name'])}</b>"

    def current_prize_reward_string(self) -> str:
        if self.prize_type == "nft":
            name = self.nft_name or "NFT"
            return f"🖼 NFT — {name}"
        g = self.current_gift()
        return f"{g['emoji']} {g['name']}"


# ============================================================
# БОТ
# ============================================================
class PerebivBot:
    def __init__(self):
        self.db = Database(DB_PATH)
        self.states: dict[int, EventState] = {}
        self.global_lock = asyncio.Lock()
        self.scheduler_task: Optional[asyncio.Task] = None
        self._application: Optional[Application] = None
        # Ожидание ручного ввода от админа: user_id -> action
        # action: 'nft' | 'cost' | 'next_time' | 'user_lookup'
        self.awaiting_input: dict[int, str] = {}

    # --------------------------------------------------------
    # Тексты сообщений ивента
    # --------------------------------------------------------
    def event_start_text(self, state: EventState) -> str:
        mins = max(1, state.duration // 60)
        lines = [
            "<b> ИВЕНТ НАЧАЛСЯ!</b>",
            "",
            "<b>💬 Отправь сообщение в чат и попробуй стать ПОБЕДИТЕЛЕМ.</b>",
            "",
            f"<b> Твоя задача — продержаться {mins} мин.</b>",
            "",
        ]
        if state.prize_type == "nft":
            name = state.nft_name or "NFT"
            lines.append(f"<b>🏆 Победитель получает NFT:</b> <b>{esc(name)}</b>")
            if state.nft_link:
                lines.append(f"🔗 <a href=\"{esc(state.nft_link)}\">Ссылка на NFT</a>")
        else:
            g = state.current_gift()
            lines.append(f"<b>🏆 Победитель получает: {g['emoji']} {esc(g['name'])}</b>")
            if state.base_gift_key != "diamond":
                lines.append("<i>⏫ Каждый час без победителя приз повышается!</i>")
        if state.message_cost > 0:
            lines.append(f"<b>⭐ Стоимость 1 сообщения: {state.message_cost} звёзд</b>")
        return "\n".join(lines)

    def nft_info_text(self, state: EventState) -> str:
        name = state.nft_name or "NFT"
        lines = ["<b>🖼 ПРИЗ — NFT</b>", "", f"📦 Название: <b>{esc(name)}</b>"]
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
            name = state.nft_name or "NFT"
            prize_block = f"<b>🖼 NFT:</b> <b>{esc(name)}</b>"
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

    # --------------------------------------------------------
    # Инициализация
    # --------------------------------------------------------
    async def initialize(self, application: Application) -> None:
        await self.db.initialize()
        await self.db.ensure_settings(DEFAULT_CHAT_ID)
        await self.restore_active_events(application)
        self.scheduler_task = asyncio.create_task(self.scheduler_loop(application))
        logger.info("Bot initialized")

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
        logger.info("Bot stopped")

    async def restore_active_events(self, application: Application) -> None:
        row = await self.db.get_active_event(DEFAULT_CHAT_ID)
        if not row:
            return
        logger.warning("Found unfinished event %s; cancelling it.", row["id"])
        await self.db.cancel_event(row["id"], now_utc())

    # --------------------------------------------------------
    # Автозапуск
    # --------------------------------------------------------
    async def scheduler_loop(self, application: Application) -> None:
        while True:
            try:
                settings = await self.db.get_settings(DEFAULT_CHAT_ID)
                if settings["auto_start"]:
                    next_time = str_to_dt(settings["next_event_time"])
                    current = now_utc()
                    if next_time is None:
                        next_time = current + timedelta(seconds=settings["interval"])
                        await self.db.update_settings(DEFAULT_CHAT_ID, next_event_time=next_time)
                    elif current >= next_time:
                        interval = max(1, int(settings["interval"]))
                        next_after = next_time
                        while next_after <= current:
                            next_after += timedelta(seconds=interval)
                        await self.db.update_settings(DEFAULT_CHAT_ID, next_event_time=next_after)
                        if DEFAULT_CHAT_ID not in self.states:
                            try:
                                await self.start_event(application, DEFAULT_CHAT_ID)
                            except Exception:
                                logger.exception("Automatic event start failed")
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Scheduler loop error")
                await asyncio.sleep(5)

    # --------------------------------------------------------
    # Ивент
    # --------------------------------------------------------
    async def start_event(self, application: Application, chat_id: int) -> bool:
        async with self.global_lock:
            if chat_id in self.states and self.states[chat_id].active:
                return False
            existing = await self.db.get_active_event(chat_id)
            if existing:
                logger.warning("Active DB event exists for chat %s", chat_id)
                return False
            settings = await self.db.get_settings(chat_id)
            duration = int(settings["duration"])
            prize_type = settings["prize_type"] or "gift"
            prize_key = settings["prize_key"] or DEFAULT_GIFT_KEY
            nft_link = settings["nft_link"]
            nft_name = settings["nft_name"]
            message_cost = int(settings["message_cost"] or 0)

            if prize_type == "nft" and not nft_link:
                logger.warning("NFT prize selected but no link; falling back to gift")
                prize_type = "gift"
            if prize_key not in GIFT_MAP:
                prize_key = DEFAULT_GIFT_KEY

            started = now_utc()
            event_id = await self.db.create_event(chat_id, started)

            state = EventState(
                chat_id=chat_id,
                event_id=event_id,
                started_at=started,
                duration=duration,
                prize_type=prize_type,
                base_gift_key=prize_key,
                nft_link=nft_link,
                nft_name=nft_name,
                message_cost=message_cost,
            )
            self.states[chat_id] = state

            try:
                message = await application.bot.send_message(
                    chat_id=chat_id,
                    text=self.event_start_text(state),
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
                try:
                    await application.bot.pin_chat_message(
                        chat_id=chat_id,
                        message_id=message.message_id,
                        disable_notification=True,
                    )
                except (TelegramError, BadRequest, Forbidden):
                    logger.warning("Could not pin event message in chat %s", chat_id, exc_info=True)

                if prize_type == "nft":
                    try:
                        await application.bot.send_message(
                            chat_id=chat_id,
                            text=self.nft_info_text(state),
                            parse_mode=ParseMode.HTML,
                            disable_web_page_preview=False,
                        )
                    except TelegramError:
                        logger.exception("Failed to send NFT info message")

                if prize_type == "gift":
                    state.escalation_task = asyncio.create_task(
                        self.prize_escalation_loop(application, state)
                    )

                logger.info("Event %s started in chat %s", event_id, chat_id)
                return True
            except Exception:
                logger.exception("Could not announce event %s", event_id)
                async with state.lock:
                    state.active = False
                    state.cancel_all_tasks()
                self.states.pop(chat_id, None)
                await self.db.cancel_event(event_id, now_utc())
                return False

    async def stop_event(self, chat_id: int, reason: str = "admin") -> bool:
        async with self.global_lock:
            state = self.states.get(chat_id)
            if not state:
                row = await self.db.get_active_event(chat_id)
                if row:
                    await self.db.cancel_event(row["id"], now_utc())
                    await self.db.update_settings(chat_id, message_cost=0)
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
            logger.info("Event %s stopped (%s)", event_id, reason)
            return True

    async def handle_player_message(self, update: Update) -> None:
        message = update.effective_message
        user = update.effective_user
        if not message or not user or user.is_bot:
            return
        chat = update.effective_chat
        if not chat or chat.id not in self.states:
            return
        state = self.states.get(chat.id)
        if not state:
            return

        await self.db.upsert_user(user.id, user.username, user.first_name or "")

        async with state.lock:
            if not state.active:
                return

            if state.current_user_id is None:
                state.current_user_id = user.id
                state.current_username = user.username
                state.current_first_name = user.first_name or "Пользователь"
                state.crown_started_at = now_utc()
                await self.db.increment_participation(user.id)
                state.cancel_leader_tasks()
                state.warning_task = asyncio.create_task(self.warning_after_ten_seconds(state))
                state.timer_task = asyncio.create_task(self.crown_timer(state))
                text = self.crown_text(
                    user.id, user.username, user.first_name or "Пользователь", state.duration
                )
            elif state.current_user_id == user.id:
                return
            else:
                state.current_user_id = user.id
                state.current_username = user.username
                state.current_first_name = user.first_name or "Пользователь"
                state.crown_started_at = now_utc()
                await self.db.increment_participation(user.id)
                await self.db.increment_steal(user.id)
                state.cancel_leader_tasks()
                state.warning_task = asyncio.create_task(self.warning_after_ten_seconds(state))
                state.timer_task = asyncio.create_task(self.crown_timer(state))
                text = self.steal_text(
                    user.id, user.username, user.first_name or "Пользователь", state.duration
                )

            try:
                bot = message.get_bot()
                if state.last_announcement_message_id is not None:
                    try:
                        await bot.delete_message(
                            chat_id=chat.id,
                            message_id=state.last_announcement_message_id,
                        )
                    except TelegramError:
                        pass
                new_message = await bot.send_message(
                    chat_id=chat.id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    reply_to_message_id=message.message_id,
                )
                state.last_announcement_message_id = new_message.message_id
            except TelegramError:
                logger.exception("Failed to announce crown change")

    async def warning_after_ten_seconds(self, state: EventState) -> None:
        try:
            await asyncio.sleep(max(0, state.duration - 10))
            async with state.lock:
                if not state.active or state.current_user_id is None:
                    return
                user_id = state.current_user_id
                username = state.current_username
                first_name = state.current_first_name or "Пользователь"
                if self._application is not None:
                    await self._application.bot.send_message(
                        chat_id=state.chat_id,
                        text=self.warning_text(user_id, username, first_name),
                        parse_mode=ParseMode.HTML,
                    )
        except asyncio.CancelledError:
            raise
        except TelegramError:
            logger.exception("Warning send failed")
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
                try:
                    if self._application is not None:
                        await self._application.bot.send_message(
                            chat_id=state.chat_id,
                            text=self.winner_text(state, winner_id, username, first_name),
                            parse_mode=ParseMode.HTML,
                            disable_web_page_preview=True,
                        )
                except TelegramError:
                    logger.exception("Failed to announce winner")
                logger.info("User %s won event %s", winner_id, event_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Crown timer failed")

    # --------------------------------------------------------
    # Эскалация приза
    # --------------------------------------------------------
    @staticmethod
    def escalated_gift_key(base_key: str, hours: int) -> str:
        base_i = GIFT_INDEX.get(base_key, 0)
        new_i = min(base_i + hours, MAX_GIFT_INDEX)
        return GIFTS[new_i]["key"]

    async def prize_escalation_loop(self, application: Application, state: EventState) -> None:
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
                        await application.bot.send_message(
                            chat_id=state.chat_id,
                            text=self.escalation_text(gift, hours),
                            parse_mode=ParseMode.HTML,
                        )
                    except TelegramError:
                        logger.exception("Failed to announce prize escalation")
                next_boundary = state.started_at + timedelta(hours=hours + 1)
                wait = (next_boundary - now_utc()).total_seconds()
                await asyncio.sleep(max(5.0, wait))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Prize escalation task failed")

    # ========================================================
    # АДМИН-ПАНЕЛЬ (только кнопки)
    # ========================================================
    @staticmethod
    def admin_keyboard() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("📊 Статистика", callback_data="admin:stats"),
                    InlineKeyboardButton("🏆 Победители", callback_data="admin:winners"),
                ],
                [
                    InlineKeyboardButton("👑 Текущий ивент", callback_data="admin:current"),
                    InlineKeyboardButton("⏰ Запуск", callback_data="admin:schedule"),
                ],
                [
                    InlineKeyboardButton("🎁 Приз", callback_data="admin:prize"),
                    InlineKeyboardButton("⭐ Цена сообщения", callback_data="admin:cost"),
                ],
                [
                    InlineKeyboardButton("⏱ Длительность", callback_data="admin:duration"),
                ],
                [
                    InlineKeyboardButton("▶️ Запустить", callback_data="admin:start"),
                    InlineKeyboardButton("⏹ Остановить", callback_data="admin:stop"),
                ],
                [
                    InlineKeyboardButton("🔄 Перезапустить", callback_data="admin:restart"),
                    InlineKeyboardButton("💬 Чат", callback_data="admin:chat"),
                ],
                [
                    InlineKeyboardButton("🔎 Пользователь по ID", callback_data="admin:user"),
                ],
            ]
        )

    @staticmethod
    def admin_text() -> str:
        return (
            "<b>╔════════════════════╗</b>\n"
            "<b>⚙️ АДМИН-ПАНЕЛЬ</b>\n"
            "<b>╚════════════════════╝</b>\n"
            "Управление ивентом <b>«ПЕРЕБИВ»</b>.\n"
            "Всё делается кнопками ниже."
        )

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/start — единственная команда, чтобы открыть панель."""
        user = update.effective_user
        if not user:
            return
        self.awaiting_input.pop(user.id, None)
        if is_admin(user.id):
            await update.effective_message.reply_text(
                self.admin_text(),
                parse_mode=ParseMode.HTML,
                reply_markup=self.admin_keyboard(),
            )
        else:
            await update.effective_message.reply_text("❌ ПОШЕЛ НАХYЙ ОТ СЮДА.")

    # --------------------------------------------------------
    # Роутер callback-кнопок
    # --------------------------------------------------------
    async def callback_router(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not query:
            return
        user = query.from_user
        if not is_admin(user.id):
            await query.answer("❌ Нет доступа", show_alert=True)
            return
        await query.answer()

        # Любая новая кнопка сбрасывает прошлое ожидание ввода
        self.awaiting_input.pop(user.id, None)

        data = query.data or ""
        try:
            if data == "admin:menu":
                await query.edit_message_text(
                    self.admin_text(),
                    parse_mode=ParseMode.HTML,
                    reply_markup=self.admin_keyboard(),
                )
            elif data == "admin:stats":
                await self.show_stats(query)
            elif data == "admin:winners":
                await self.show_winners(query)
            elif data == "admin:current":
                await self.show_current(query)
            elif data == "admin:schedule":
                await self.show_schedule(query)
            elif data == "admin:duration":
                await self.show_duration(query)
            elif data.startswith("admin:duration:"):
                seconds = int(data.rsplit(":", 1)[1])
                await self.db.update_settings(DEFAULT_CHAT_ID, duration=seconds)
                await query.answer(f"⏱ Длительность: {format_minutes(seconds)}", show_alert=True)
                await self.show_duration(query)

            # ---- Приз ----
            elif data == "admin:prize":
                await self.show_prize_menu(query)
            elif data.startswith("admin:prize:gift:"):
                key = data.rsplit(":", 1)[1]
                if key in GIFT_MAP:
                    await self.db.update_settings(DEFAULT_CHAT_ID, prize_type="gift", prize_key=key)
                    await query.answer(
                        f"🎁 Приз: {GIFT_MAP[key]['emoji']} {GIFT_MAP[key]['name']}",
                        show_alert=True,
                    )
                await self.show_prize_menu(query)
            elif data == "admin:prize:nft":
                self.awaiting_input[user.id] = "nft"
                await query.edit_message_text(
                    "<b>🖼 НАСТРОЙКА NFT-ПРИЗА</b>\n\n"
                    "Пришлите <b>одним сообщением</b> в этот чат (не в группу!):\n\n"
                    "<code>ССЫЛКА [Название]</code>\n\n"
                    "Пример:\n"
                    "<code>https://t.me/nft/SomeNFT Платиновый Кубок</code>\n\n"
                    "Название необязательно — по умолчанию будет «NFT».",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("⬅️ Отмена", callback_data="admin:prize")]]
                    ),
                )

            # ---- Стоимость ----
            elif data == "admin:cost":
                await self.show_cost_menu(query)
            elif data.startswith("admin:cost:set:"):
                value = int(data.rsplit(":", 1)[1])
                value = max(0, min(MAX_MESSAGE_COST, value))
                await self.db.update_settings(DEFAULT_CHAT_ID, message_cost=value)
                await query.answer(f"⭐ Стоимость 1 сообщения: {value}", show_alert=True)
                await self.show_cost_menu(query)
            elif data == "admin:cost:manual":
                self.awaiting_input[user.id] = "cost"
                await query.edit_message_text(
                    "<b>⭐ СТОИМОСТЬ СООБЩЕНИЯ</b>\n\n"
                    f"Пришлите в этот чат <b>одно число</b> от 0 до {MAX_MESSAGE_COST}.\n\n"
                    "Пример: <code>12</code>",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("⬅️ Отмена", callback_data="admin:cost")]]
                    ),
                )

            # ---- Кнопки ивента ----
            elif data == "admin:start":
                started = await self.start_event(self._application, DEFAULT_CHAT_ID)
                await query.answer(
                    "▶️ Ивент запущен" if started else "⚠️ Ивент уже активен",
                    show_alert=True,
                )
                await self.refresh_menu(query)
            elif data == "admin:stop":
                stopped = await self.stop_event(DEFAULT_CHAT_ID, "admin")
                await query.answer(
                    "⏹ Ивент остановлен" if stopped else "⚠️ Активного ивента нет",
                    show_alert=True,
                )
                await self.refresh_menu(query)
            elif data == "admin:restart":
                await self.stop_event(DEFAULT_CHAT_ID, "restart")
                started = await self.start_event(self._application, DEFAULT_CHAT_ID)
                await query.answer(
                    "🔄 Ивент перезапущен" if started else "⚠️ Не удалось запустить",
                    show_alert=True,
                )
                await self.refresh_menu(query)
            elif data == "admin:chat":
                await self.show_chat_settings(query)

            # ---- Пользователь по ID ----
            elif data == "admin:user":
                self.awaiting_input[user.id] = "user_lookup"
                await query.edit_message_text(
                    "<b>🔎 ПОЛЬЗОВАТЕЛЬ ПО ID</b>\n\n"
                    "Пришлите в этот чат <b>Telegram ID</b> (число).\n\n"
                    "Пример: <code>123456789</code>",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("⬅️ Отмена", callback_data="admin:menu")]]
                    ),
                )

            # ---- Расписание ----
            elif data == "admin:autoon":
                await self.db.update_settings(DEFAULT_CHAT_ID, auto_start=True)
                await query.answer("▶️ Автозапуск включён", show_alert=True)
                await self.show_schedule(query)
            elif data == "admin:autooff":
                await self.db.update_settings(DEFAULT_CHAT_ID, auto_start=False)
                await query.answer("⏹ Автозапуск выключен", show_alert=True)
                await self.show_schedule(query)
            elif data.startswith("admin:interval:"):
                seconds = int(data.rsplit(":", 1)[1])
                await self.db.update_settings(
                    DEFAULT_CHAT_ID,
                    interval=seconds,
                    next_event_time=now_utc() + timedelta(seconds=seconds),
                )
                await query.answer(f"🔄 Интервал: {seconds // 3600} ч.", show_alert=True)
                await self.show_schedule(query)
            elif data == "admin:next":
                self.awaiting_input[user.id] = "next_time"
                await query.edit_message_text(
                    "<b>⏰ ИЗМЕНЕНИЕ ВРЕМЕНИ</b>\n\n"
                    "Пришлите в этот чат <b>дату и время</b> в формате:\n"
                    "<code>ДД.ММ.ГГГГ ЧЧ:ММ</code>\n\n"
                    "Пример:\n<code>03.10.2026 20:00</code>\n\n"
                    "Время трактуется относительно UTC+0.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("⬅️ Отмена", callback_data="admin:schedule")]]
                    ),
                )
            else:
                logger.warning("Unknown callback: %s", data)
        except Exception:
            logger.exception("Admin callback failed")
            try:
                await query.answer("⚠️ Произошла ошибка", show_alert=True)
            except TelegramError:
                pass

    async def refresh_menu(self, query) -> None:
        try:
            await query.edit_message_text(
                self.admin_text(),
                parse_mode=ParseMode.HTML,
                reply_markup=self.admin_keyboard(),
            )
        except BadRequest:
            pass

    # --------------------------------------------------------
    # Обработка ввода от админа (личка)
    # --------------------------------------------------------
    async def handle_admin_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        message = update.effective_message
        chat = update.effective_chat
        if not user or not message or not chat:
            return
        if chat.type != "private" or not is_admin(user.id):
            return
        text = (message.text or "").strip()
        if not text:
            return

        action = self.awaiting_input.get(user.id)

        if action is None:
            # Нет ожидания — показываем панель
            await message.reply_text(
                self.admin_text(),
                parse_mode=ParseMode.HTML,
                reply_markup=self.admin_keyboard(),
            )
            return

        back_to_menu = InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ В админ-панель", callback_data="admin:menu")]]
        )

        try:
            if action == "nft":
                parts = text.split(maxsplit=1)
                link = parts[0]
                name = parts[1].strip() if len(parts) > 1 else "NFT"
                if not (link.startswith("http://") or link.startswith("https://")):
                    await message.reply_text(
                        "❌ Ссылка должна начинаться с <code>http://</code> или <code>https://</code>.\n"
                        "Попробуйте ещё раз или нажмите /start для выхода.",
                        parse_mode=ParseMode.HTML,
                    )
                    return
                await self.db.update_settings(
                    DEFAULT_CHAT_ID,
                    prize_type="nft",
                    nft_link=link,
                    nft_name=name,
                )
                self.awaiting_input.pop(user.id, None)
                await message.reply_text(
                    "✅ <b>Приз переключён на NFT</b>\n\n"
                    f"📦 Название: <b>{esc(name)}</b>\n"
                    f"🔗 {esc(link)}\n\n"
                    "Запускайте ивент — бот пришлёт это в чат.",
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                    reply_markup=back_to_menu,
                )

            elif action == "cost":
                try:
                    value = int(text)
                except ValueError:
                    await message.reply_text("❌ Нужно целое число. Попробуйте снова или /start.")
                    return
                if value < 0 or value > MAX_MESSAGE_COST:
                    await message.reply_text(
                        f"❌ Диапазон: 0–{MAX_MESSAGE_COST}. Попробуйте снова или /start."
                    )
                    return
                await self.db.update_settings(DEFAULT_CHAT_ID, message_cost=value)
                self.awaiting_input.pop(user.id, None)
                await message.reply_text(
                    f"✅ Стоимость 1 сообщения: <b>{value} ⭐</b>\n\n"
                    "<i>Сбросится на 0 после завершения ивента.</i>",
                    parse_mode=ParseMode.HTML,
                    reply_markup=back_to_menu,
                )

            elif action == "next_time":
                try:
                    local_dt = datetime.strptime(text, "%d.%m.%Y %H:%M")
                    target = local_dt.replace(tzinfo=UTC)
                except ValueError:
                    await message.reply_text(
                        "❌ Неверный формат. Пример: <code>03.10.2026 20:00</code>\n"
                        "Попробуйте снова или /start.",
                        parse_mode=ParseMode.HTML,
                    )
                    return
                if target <= now_utc():
                    await message.reply_text("❌ Время должно быть в будущем. Попробуйте снова.")
                    return
                await self.db.update_settings(DEFAULT_CHAT_ID, next_event_time=target)
                self.awaiting_input.pop(user.id, None)
                await message.reply_text(
                    f"✅ Следующий запуск: <b>{display_dt(dt_to_str(target))}</b>",
                    parse_mode=ParseMode.HTML,
                    reply_markup=back_to_menu,
                )

            elif action == "user_lookup":
                try:
                    user_id = int(text)
                except ValueError:
                    await message.reply_text("❌ Telegram ID должен быть числом. Попробуйте снова.")
                    return
                row = await self.db.get_user(user_id)
                self.awaiting_input.pop(user.id, None)
                if not row:
                    await message.reply_text(
                        f"Пользователь <code>{user_id}</code> ещё не участвовал.",
                        parse_mode=ParseMode.HTML,
                        reply_markup=back_to_menu,
                    )
                    return
                username = f"@{esc(row['username'])}" if row["username"] else "нет username"
                await message.reply_text(
                    "<b>👤 ПОЛЬЗОВАТЕЛЬ</b>\n"
                    f"🆔 ID: <code>{row['user_id']}</code>\n"
                    f"👤 Имя: {esc(row['first_name'])}\n"
                    f"🔗 Username: {username}\n"
                    f"🏆 Побед: <b>{row['wins']}</b>\n"
                    f"🎮 Участий: <b>{row['participations']}</b>\n"
                    f"💥 Перебивов: <b>{row['steals']}</b>\n"
                    f"📅 Последняя победа: <b>{display_dt(row['last_win'])}</b>",
                    parse_mode=ParseMode.HTML,
                    reply_markup=back_to_menu,
                )
        except Exception:
            logger.exception("Admin input handler failed")

    # --------------------------------------------------------
    # Экраны панели
    # --------------------------------------------------------
    async def show_stats(self, query) -> None:
        stats = await self.db.get_statistics()
        lines = [
            "<b>📊 СТАТИСТИКА</b>",
            "",
            f"👥 Участников: <b>{stats['users']}</b>",
            f"🏆 Всего побед: <b>{stats['wins']}</b>",
            f"💥 Всего перебивов: <b>{stats['steals']}</b>",
            f"🎮 Проведено ивентов: <b>{stats['events']}</b>",
            "",
            "<b>👑 ТОП ПОБЕДИТЕЛЕЙ:</b>",
        ]
        medals = ["🥇", "🥈", "🥉"]
        for i, row in enumerate(stats["top"]):
            name = f"@{esc(row['username'])}" if row["username"] else esc(row["first_name"] or row["user_id"])
            lines.append(f"{medals[i]} {name} — <b>{row['wins']}</b> побед")
        await query.edit_message_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Назад", callback_data="admin:menu")]]
            ),
        )

    async def show_winners(self, query) -> None:
        rows = await self.db.get_recent_winners(10)
        lines = ["<b>🏆 ПОСЛЕДНИЕ ПОБЕДИТЕЛИ</b>", ""]
        if not rows:
            lines.append("Пока побед нет.")
        else:
            for i, row in enumerate(rows, 1):
                name = f"@{esc(row['username'])}" if row["username"] else esc(row["first_name"] or row["user_id"])
                reward = row["reward"] or "—"
                lines.extend(
                    [
                        f"<b>{i}.</b> 👑 {name} — {esc(reward)}",
                        f"    {display_dt(row['won_at'])}",
                        f"   ⏱ {display_time(str_to_dt(row['won_at']))}",
                    ]
                )
        await query.edit_message_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Назад", callback_data="admin:menu")]]
            ),
        )

    async def show_current(self, query) -> None:
        state = self.states.get(DEFAULT_CHAT_ID)
        if not state or not state.active:
            text = "<b>👑 ТЕКУЩИЙ ИВЕНТ</b>\n⏹ Активного ивента сейчас нет."
        else:
            async with state.lock:
                prize_line = f"🎁 Приз: {state.current_prize_text()}"
                extra_lines = []
                if state.prize_type == "gift" and state.base_gift_key != "diamond":
                    extra_lines.append(f"<i>Стартовый: {gift_display(state.base_gift_key)}</i>")
                if state.message_cost > 0:
                    extra_lines.append(f"⭐ Цена 1 сообщения: <b>{state.message_cost}</b>")
                if state.current_user_id is None:
                    text = (
                        "<b>👑 ТЕКУЩИЙ ИВЕНТ</b>\n"
                        " Ивент активен.\n"
                        "👤 Лидер ещё не выбран.\n"
                        f"{prize_line}\n"
                        + ("\n".join(extra_lines) if extra_lines else "")
                    )
                else:
                    elapsed = 0
                    if state.crown_started_at:
                        elapsed = int((now_utc() - state.crown_started_at).total_seconds())
                    remaining = max(0, state.duration - elapsed)
                    mention = user_mention(
                        state.current_user_id, state.current_username,
                        state.current_first_name or "Пользователь",
                    )
                    text = (
                        "<b>👑 ТЕКУЩИЙ ИВЕНТ</b>\n"
                        " Статус: <b>активен</b>\n"
                        f"👑 Лидер: {mention}\n"
                        f"⏱ Длительность: <b>{format_minutes(state.duration)}</b>\n"
                        f"⏳ Осталось: <b>{format_duration(remaining)}</b>\n"
                        f"{prize_line}\n"
                        + ("\n".join(extra_lines) if extra_lines else "")
                        + f"\n Event ID: <code>{state.event_id}</code>"
                    )
        await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Назад", callback_data="admin:menu")]]
            ),
        )

    async def show_duration(self, query) -> None:
        settings = await self.db.get_settings(DEFAULT_CHAT_ID)
        duration = int(settings["duration"])
        text = (
            "<b>⏱ ДЛИТЕЛЬНОСТЬ УДЕРЖАНИЯ</b>\n\n"
            "Сколько должен продержаться лидер, чтобы победить?\n\n"
            f"Текущее значение: <b>{format_minutes(duration)}</b>"
        )
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("1 мин", callback_data="admin:duration:60"),
                    InlineKeyboardButton("3 мин", callback_data="admin:duration:180"),
                ],
                [
                    InlineKeyboardButton("5 мин", callback_data="admin:duration:300"),
                    InlineKeyboardButton("10 мин", callback_data="admin:duration:600"),
                ],
                [InlineKeyboardButton("⬅️ Назад", callback_data="admin:menu")],
            ]
        )
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)

    async def show_prize_menu(self, query) -> None:
        settings = await self.db.get_settings(DEFAULT_CHAT_ID)
        prize_type = settings["prize_type"] or "gift"
        prize_key = settings["prize_key"] or DEFAULT_GIFT_KEY
        nft_link = settings["nft_link"]
        nft_name = settings["nft_name"]

        if prize_type == "nft" and nft_link:
            current_line = (
                f"Текущий приз: <b>🖼 NFT — {esc(nft_name or 'NFT')}</b>\n"
                f"🔗 {esc(nft_link)}"
            )
        else:
            current_line = f"Текущий приз: <b>{gift_display(prize_key)}</b>"

        text = (
            "<b>🎁 НАСТРОЙКА ПРИЗА</b>\n\n"
            f"{current_line}\n\n"
            "Нажмите на подарок — он станет призом следующего ивента.\n"
            "<i>Каждый час без победителя приз автоматически повышается до 💎 Алмаза.</i>\n"
            "<i>Для NFT эскалация не работает.</i>"
        )

        rows: list[list[InlineKeyboardButton]] = []
        row: list[InlineKeyboardButton] = []
        for g in GIFTS:
            mark = "✅ " if (prize_type == "gift" and g["key"] == prize_key) else ""
            row.append(
                InlineKeyboardButton(
                    f"{mark}{g['emoji']} {g['name']}",
                    callback_data=f"admin:prize:gift:{g['key']}",
                )
            )
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append(
            [
                InlineKeyboardButton(
                    ("✅ " if prize_type == "nft" else "") + "🖼 NFT (по ссылке)",
                    callback_data="admin:prize:nft",
                )
            ]
        )
        rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="admin:menu")])
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))

    async def show_cost_menu(self, query) -> None:
        settings = await self.db.get_settings(DEFAULT_CHAT_ID)
        cost = int(settings["message_cost"] or 0)
        text = (
            "<b>⭐ СТОИМОСТЬ СООБЩЕНИЯ</b>\n\n"
            "Сколько звёзд должен заплатить участник за 1 сообщение в чате\n"
            "во время ивента?\n\n"
            f"Текущее значение: <b>{cost} ⭐</b>\n\n"
            "<i>После завершения ивента стоимость автоматически сбрасывается на 0.</i>\n"
            "<i>Бот только анонсирует цену; приём звёзд вы ведёте вручную.</i>"
        )
        values = [0, 5, 10, 15, 20, 30, 50]
        rows: list[list[InlineKeyboardButton]] = []
        row: list[InlineKeyboardButton] = []
        for v in values:
            mark = "✅ " if v == cost else ""
            row.append(
                InlineKeyboardButton(f"{mark}{v} ⭐", callback_data=f"admin:cost:set:{v}")
            )
            if len(row) == 3:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append(
            [InlineKeyboardButton("✏️ Ввести вручную", callback_data="admin:cost:manual")]
        )
        rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="admin:menu")])
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))

    async def show_schedule(self, query) -> None:
        settings = await self.db.get_settings(DEFAULT_CHAT_ID)
        interval = int(settings["interval"])
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
            f"⏰ Следующий запуск:\n<b>{display_dt(settings['next_event_time'])}</b>\n"
            f"🕐 Интервал: <b>{interval_text}</b>\n"
            f"⏱ Длительность: <b>{format_minutes(int(settings['duration']))}</b>\n"
            f"▶️ Автозапуск: <b>{'включён' if settings['auto_start'] else 'выключен'}</b>"
        )
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("➕ Изменить время", callback_data="admin:next")],
                [
                    InlineKeyboardButton("🔄 24 часа", callback_data="admin:interval:86400"),
                    InlineKeyboardButton("🔄 12 часов", callback_data="admin:interval:43200"),
                    InlineKeyboardButton("🔄 1 час", callback_data="admin:interval:3600"),
                ],
                [InlineKeyboardButton("⏱ Длительность", callback_data="admin:duration")],
                [
                    InlineKeyboardButton("▶️ Включить", callback_data="admin:autoon"),
                    InlineKeyboardButton("⏹ Выключить", callback_data="admin:autooff"),
                ],
                [InlineKeyboardButton("⬅️ Назад", callback_data="admin:menu")],
            ]
        )
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)

    async def show_chat_settings(self, query) -> None:
        text = (
            "<b>💬 НАСТРОЙКА ЧАТА</b>\n"
            f" Текущий CHAT_ID:\n<code>{DEFAULT_CHAT_ID}</code>\n"
            "Чтобы изменить чат, поменяйте <code>DEFAULT_CHAT_ID</code>\n"
            "в начале <code>main.py</code> и перезапустите бота.\n"
            "Бот должен быть добавлен в нужную группу."
        )
        await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Назад", callback_data="admin:menu")]]
            ),
        )


# ============================================================
# TELEGRAM HANDLERS
# ============================================================
bot_controller = PerebivBot()


async def post_init(application: Application) -> None:
    bot_controller._application = application
    await bot_controller.initialize(application)


async def post_shutdown(application: Application) -> None:
    await bot_controller.shutdown()


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await bot_controller.start_command(update, context)


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await bot_controller.callback_router(update, context)


async def group_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await bot_controller.handle_player_message(update)
    except Exception:
        logger.exception("Group message handler failed")


async def admin_private_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await bot_controller.handle_admin_input(update, context)
    except Exception:
        logger.exception("Admin private handler failed")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error
    if isinstance(error, (BadRequest, Forbidden)):
        logger.warning("Telegram API error: %s", error)
    else:
        logger.exception("Unhandled Telegram error", exc_info=error)


# ============================================================
# MAIN
# ============================================================
def main() -> None:
    if not BOT_TOKEN or BOT_TOKEN == "ВСТАВЬ_ТОКЕН_СЮДА":
        raise RuntimeError("Укажите настоящий BOT_TOKEN в начале файла.")
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # Единственная команда — /start (открыть панель)
    application.add_handler(CommandHandler("start", start_handler))
    application.add_handler(CallbackQueryHandler(callback_handler))

    # Ввод от админа в личке (когда бот его о чём-то попросил)
    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.User(ADMIN_IDS) & filters.TEXT & ~filters.COMMAND,
            admin_private_handler,
        )
    )

    # Сообщения в группах — игровая логика
    application.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS & ~filters.COMMAND,
            group_message_handler,
        )
    )

    application.add_error_handler(error_handler)
    logger.info("Starting Telegram bot...")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == "__main__":
    main()
