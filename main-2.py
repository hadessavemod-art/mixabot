#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Telegram bot "ПЕРЕБИВ"
Python 3.11+
python-telegram-bot 22.x

All project logic is intentionally contained in this single file.
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
    ApplicationHandlerStop,
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


DEFAULT_CHAT_ID = -1001234567890

EVENT_DURATION = 180
DEFAULT_INTERVAL = 86400

DB_PATH = "perebiv.sqlite3"

# Часовой пояс для отображения времени в админке.
# Автозапуск хранится в UTC, а это значение используется только
# для красивого отображения дат/времени.
DISPLAY_UTC_OFFSET_HOURS = 0

# ============================================================
# ЛОГИРОВАНИЕ
# ============================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("perebiv")

# ============================================================
# КОНСТАНТЫ / ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================

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


def user_mention(user_id: int, username: Optional[str], first_name: Optional[str]) -> str:
    if username:
        return "@" + html.escape(username)
    name = html.escape(first_name or "Пользователь")
    return f'<a href="tg://user?id={user_id}">{name}</a>'


def is_admin(user_id: Optional[int]) -> bool:
    return bool(user_id and user_id in ADMIN_IDS)


def esc(value: object) -> str:
    return html.escape(str(value if value is not None else ""))


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
                    next_event_time TEXT
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
            conn.commit()

    async def ensure_settings(self, chat_id: int) -> None:
        async with self._lock:
            await asyncio.to_thread(self._ensure_settings_sync, chat_id)

    def _ensure_settings_sync(self, chat_id: int) -> None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT chat_id FROM settings WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
            if row is None:
                next_time = dt_to_str(now_utc() + timedelta(seconds=DEFAULT_INTERVAL))
                conn.execute(
                    """
                    INSERT INTO settings(chat_id, auto_start, interval, next_event_time)
                    VALUES (?, 1, ?, ?)
                    """,
                    (chat_id, DEFAULT_INTERVAL, next_time),
                )
                conn.commit()

    async def get_settings(self, chat_id: int) -> sqlite3.Row:
        await self.ensure_settings(chat_id)
        return await asyncio.to_thread(self._get_settings_sync, chat_id)

    def _get_settings_sync(self, chat_id: int) -> sqlite3.Row:
        with closing(self._connect()) as conn:
            return conn.execute(
                "SELECT * FROM settings WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()

    async def update_settings(
        self,
        chat_id: int,
        *,
        auto_start: Optional[bool] = None,
        interval: Optional[int] = None,
        next_event_time: Optional[datetime] = None,
    ) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._update_settings_sync,
                chat_id,
                auto_start,
                interval,
                dt_to_str(next_event_time) if next_event_time else None,
            )

    def _update_settings_sync(
        self,
        chat_id: int,
        auto_start: Optional[bool],
        interval: Optional[int],
        next_event_time: Optional[str],
    ) -> None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM settings WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()

            if row is None:
                current_auto = 1
                current_interval = DEFAULT_INTERVAL
                current_next = dt_to_str(now_utc() + timedelta(seconds=DEFAULT_INTERVAL))
            else:
                current_auto = row["auto_start"]
                current_interval = row["interval"]
                current_next = row["next_event_time"]

            conn.execute(
                """
                INSERT OR REPLACE INTO settings(chat_id, auto_start, interval, next_event_time)
                VALUES (?, ?, ?, ?)
                """,
                (
                    chat_id,
                    int(current_auto if auto_start is None else auto_start),
                    current_interval if interval is None else interval,
                    current_next if next_event_time is None else next_event_time,
                ),
            )
            conn.commit()

    async def upsert_user(self, user_id: int, username: Optional[str], first_name: str) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._upsert_user_sync,
                user_id,
                username,
                first_name,
            )

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
            await asyncio.to_thread(
                self._increment_column_sync, user_id, "participations"
            )

    async def increment_steal(self, user_id: int) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._increment_column_sync, user_id, "steals"
            )

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
            return await asyncio.to_thread(
                self._create_event_sync, chat_id, dt_to_str(started_at)
            )

    def _create_event_sync(self, chat_id: int, started_at: str) -> int:
        with closing(self._connect()) as conn:
            cur = conn.execute(
                """
                INSERT INTO events(chat_id, started_at, status)
                VALUES (?, ?, 'active')
                """,
                (chat_id, started_at),
            )
            conn.commit()
            return int(cur.lastrowid)

    async def finish_event(
        self,
        event_id: int,
        winner_id: Optional[int],
        finished_at: datetime,
    ) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._finish_event_sync,
                event_id,
                winner_id,
                dt_to_str(finished_at),
            )

    def _finish_event_sync(
        self,
        event_id: int,
        winner_id: Optional[int],
        finished_at: str,
    ) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """
                UPDATE events
                SET status = 'finished', winner_id = ?, finished_at = ?
                WHERE id = ?
                """,
                (winner_id, finished_at, event_id),
            )
            conn.commit()

    async def cancel_event(self, event_id: int, finished_at: datetime) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._cancel_event_sync, event_id, dt_to_str(finished_at)
            )

    def _cancel_event_sync(self, event_id: int, finished_at: str) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """
                UPDATE events
                SET status = 'cancelled', finished_at = ?
                WHERE id = ?
                """,
                (finished_at, event_id),
            )
            conn.commit()

    async def get_active_event(self, chat_id: int) -> Optional[sqlite3.Row]:
        return await asyncio.to_thread(self._get_active_event_sync, chat_id)

    def _get_active_event_sync(self, chat_id: int) -> Optional[sqlite3.Row]:
        with closing(self._connect()) as conn:
            return conn.execute(
                """
                SELECT * FROM events
                WHERE chat_id = ? AND status = 'active'
                ORDER BY id DESC LIMIT 1
                """,
                (chat_id,),
            ).fetchone()

    async def add_winner(
        self,
        user_id: int,
        username: Optional[str],
        event_id: int,
        won_at: datetime,
        reward: str,
    ) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._add_winner_sync,
                user_id,
                username,
                event_id,
                dt_to_str(won_at),
                reward,
            )

    def _add_winner_sync(
        self,
        user_id: int,
        username: Optional[str],
        event_id: int,
        won_at: str,
        reward: str,
    ) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """
                UPDATE users
                SET wins = wins + 1, last_win = ?, username = COALESCE(?, username)
                WHERE user_id = ?
                """,
                (won_at, username, user_id),
            )
            conn.execute(
                """
                INSERT INTO winners(user_id, username, event_id, won_at, reward)
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, username, event_id, won_at, reward),
            )
            conn.commit()

    async def get_statistics(self) -> dict:
        return await asyncio.to_thread(self._get_statistics_sync)

    def _get_statistics_sync(self) -> dict:
        with closing(self._connect()) as conn:
            users = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
            wins = conn.execute(
                "SELECT COALESCE(SUM(wins), 0) AS c FROM users"
            ).fetchone()["c"]
            steals = conn.execute(
                "SELECT COALESCE(SUM(steals), 0) AS c FROM users"
            ).fetchone()["c"]
            events = conn.execute(
                "SELECT COUNT(*) AS c FROM events"
            ).fetchone()["c"]
            top = conn.execute(
                """
                SELECT user_id, username, first_name, wins
                FROM users
                WHERE wins > 0
                ORDER BY wins DESC, last_win DESC
                LIMIT 3
                """
            ).fetchall()
            return {
                "users": users,
                "wins": wins,
                "steals": steals,
                "events": events,
                "top": top,
            }

    async def get_user(self, user_id: int) -> Optional[sqlite3.Row]:
        return await asyncio.to_thread(self._get_user_sync, user_id)

    def _get_user_sync(self, user_id: int) -> Optional[sqlite3.Row]:
        with closing(self._connect()) as conn:
            return conn.execute(
                "SELECT * FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()

    async def get_recent_winners(self, limit: int = 10):
        return await asyncio.to_thread(self._get_recent_winners_sync, limit)

    def _get_recent_winners_sync(self, limit: int):
        with closing(self._connect()) as conn:
            return conn.execute(
                """
                SELECT w.*, u.first_name
                FROM winners w
                LEFT JOIN users u ON u.user_id = w.user_id
                ORDER BY w.won_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

    async def get_event_count(self, chat_id: int) -> int:
        return await asyncio.to_thread(self._get_event_count_sync, chat_id)

    def _get_event_count_sync(self, chat_id: int) -> int:
        with closing(self._connect()) as conn:
            return conn.execute(
                "SELECT COUNT(*) AS c FROM events WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()["c"]

    async def get_latest_active_event(self, chat_id: int) -> Optional[sqlite3.Row]:
        return await self.get_active_event(chat_id)


# ============================================================
# СОСТОЯНИЕ ИВЕНТА
# ============================================================

class EventState:
    def __init__(self, chat_id: int, event_id: int, started_at: datetime):
        self.chat_id = chat_id
        self.event_id = event_id
        self.started_at = started_at

        self.current_user_id: Optional[int] = None
        self.current_username: Optional[str] = None
        self.current_first_name: Optional[str] = None
        self.crown_started_at: Optional[datetime] = None

        self.timer_task: Optional[asyncio.Task] = None
        self.warning_task: Optional[asyncio.Task] = None

        self.lock = asyncio.Lock()
        self.active = True

    def cancel_tasks(self) -> None:
        current = asyncio.current_task()
        for task in (self.timer_task, self.warning_task):
            if task and not task.done() and task is not current:
                task.cancel()
        self.timer_task = None
        self.warning_task = None


# ============================================================
# БОТ
# ============================================================

class PerebivBot:
    REWARD = "🧸 Медведь от Мишки"

    def __init__(self):
        self.db = Database(DB_PATH)
        self.states: dict[int, EventState] = {}
        self.global_lock = asyncio.Lock()
        self.scheduler_task: Optional[asyncio.Task] = None

    # --------------------------------------------------------
    # Форматирование сообщений
    # --------------------------------------------------------

    @staticmethod
    def event_start_text() -> str:
        return (
            "<b>╔══════════════════╗</b>\n"
            "<b>🔥 ИВЕНТ НАЧАЛСЯ 🔥</b>\n"
            "<b>╚══════════════════╝</b>\n\n"
            "<b>👑 ПЕРЕБИВ</b>\n\n"
            "⚡ Игра началась!\n"
            "💬 Отправь сообщение в чат и попробуй стать <b>ЦАРЁМ</b>.\n\n"
            "⏱ Твоя задача — продержаться <b>3 минуты</b>.\n\n"
            "🏆 Победитель получает:\n"
            "🧸 <b>Медведь от Мишки</b>\n\n"
            "<b>УСПЕЙ ПЕРЕБИТЬ СВОЕГО СОПЕРНИКА! 🔥</b>"
        )

    @staticmethod
    def crown_text(user_id: int, username: Optional[str], first_name: str) -> str:
        mention = user_mention(user_id, username, first_name)
        return (
            "👑 <b>Новый ЦАРЬ!</b>\n\n"
            f"🔥 {mention} захватил корону!\n\n"
            "⏱ Ему необходимо продержаться:\n"
            "<b>03:00</b>\n\n"
            "💥 <b>УСПЕЙТЕ ПЕРЕБИТЬ ЕГО!</b>"
        )

    @staticmethod
    def steal_text(user_id: int, username: Optional[str], first_name: str) -> str:
        mention = user_mention(user_id, username, first_name)
        return (
            "💥 <b>ПЕРЕБИТ!</b>\n\n"
            f"👑 <b>Новый ЦАРЬ:</b> {mention}\n\n"
            "⚡ Он забрал корону!\n\n"
            "⏱ До победы:\n"
            "<b>03:00</b>\n\n"
            "🔥 <b>УСПЕЙТЕ ЕГО ПЕРЕБИТЬ!</b>"
        )

    @staticmethod
    def warning_text(user_id: int, username: Optional[str], first_name: str) -> str:
        mention = user_mention(user_id, username, first_name)
        return (
            "🚨 <b>ОСТАЛОСЬ 10 СЕКУНД!</b> 🚨\n\n"
            f"👑 {mention} почти победил!\n\n"
            "🔥 <b>УСПЕЙ ПЕРЕБИТЬ!</b>"
        )

    @staticmethod
    def winner_text(user_id: int, username: Optional[str], first_name: str) -> str:
        mention = user_mention(user_id, username, first_name)
        return (
            "<b>╔══════════════════╗</b>\n"
            "<b>🏆 ПОБЕДИТЕЛЬ!</b>\n"
            "<b>╚══════════════════╝</b>\n\n"
            f"👑 <b>ЦАРЬ:</b>\n{mention}\n\n"
            "🔥 Он продержался целых <b>3 минуты!</b>\n\n"
            "🎁 <b>НАГРАДА:</b>\n"
            "🧸 <b>Медведь от Мишки</b>\n\n"
            "Поздравляем с победой! ❤️‍🔥"
        )

    # --------------------------------------------------------
    # Инициализация / восстановление
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
                state.cancel_tasks()
        self.states.clear()
        logger.info("Bot stopped")

    async def restore_active_events(self, application: Application) -> None:
        row = await self.db.get_active_event(DEFAULT_CHAT_ID)
        if not row:
            return

        # После перезапуска мы не знаем надёжно, кто был царём,
        # если это состояние не было отдельно сохранено.
        # Чтобы не выдать ложную победу, закрываем "подвисший"
        # event и планируем следующий обычный запуск.
        logger.warning(
            "Found unfinished event %s after restart; cancelling it safely.",
            row["id"],
        )
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
                        await self.db.update_settings(
                            DEFAULT_CHAT_ID,
                            next_event_time=next_time,
                        )

                    elif current >= next_time:
                        # После каждого срабатывания сразу вычисляем
                        # следующую дату, чтобы не создать несколько событий.
                        interval = max(1, int(settings["interval"]))
                        next_after = next_time
                        while next_after <= current:
                            next_after += timedelta(seconds=interval)

                        await self.db.update_settings(
                            DEFAULT_CHAT_ID,
                            next_event_time=next_after,
                        )

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

            started = now_utc()
            event_id = await self.db.create_event(chat_id, started)
            state = EventState(chat_id, event_id, started)
            self.states[chat_id] = state

        try:
            message = await application.bot.send_message(
                chat_id=chat_id,
                text=self.event_start_text(),
                parse_mode=ParseMode.HTML,
            )
            try:
                await application.bot.pin_chat_message(
                    chat_id=chat_id,
                    message_id=message.message_id,
                    disable_notification=True,
                )
            except (TelegramError, BadRequest, Forbidden):
                logger.warning(
                    "Could not pin event message in chat %s",
                    chat_id,
                    exc_info=True,
                )

            logger.info("Event %s started in chat %s", event_id, chat_id)
            return True

        except Exception:
            logger.exception("Could not announce event %s", event_id)
            async with state.lock:
                state.active = False
                state.cancel_tasks()
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
                    return True
                return False

            async with state.lock:
                if not state.active:
                    return False
                state.active = False
                state.cancel_tasks()
                event_id = state.event_id

            self.states.pop(chat_id, None)

        await self.db.cancel_event(event_id, now_utc())
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

        # Администраторы не являются участниками автоматически.
        # Они могут участвовать только если написать сообщение во время
        # события и при этом использоваться как обычный пользователь.
        # Поэтому здесь НЕ исключаем администраторов.
        await self.db.upsert_user(user.id, user.username, user.first_name or "")

        async with state.lock:
            if not state.active:
                return

            # Первое сообщение — захват короны.
            if state.current_user_id is None:
                state.current_user_id = user.id
                state.current_username = user.username
                state.current_first_name = user.first_name or "Пользователь"
                state.crown_started_at = now_utc()

                await self.db.increment_participation(user.id)

                state.cancel_tasks()
                state.warning_task = asyncio.create_task(
                    self.warning_after_ten_seconds(state)
                )
                state.timer_task = asyncio.create_task(
                    self.crown_timer(state)
                )

                text = self.crown_text(
                    user.id,
                    user.username,
                    user.first_name or "Пользователь",
                )

            # Сообщение самого царя ничего не меняет.
            elif state.current_user_id == user.id:
                return

            # Новый пользователь перебивает царя.
            else:
                state.current_user_id = user.id
                state.current_username = user.username
                state.current_first_name = user.first_name or "Пользователь"
                state.crown_started_at = now_utc()

                await self.db.increment_participation(user.id)
                await self.db.increment_steal(user.id)

                state.cancel_tasks()
                state.warning_task = asyncio.create_task(
                    self.warning_after_ten_seconds(state)
                )
                state.timer_task = asyncio.create_task(
                    self.crown_timer(state)
                )

                text = self.steal_text(
                    user.id,
                    user.username,
                    user.first_name or "Пользователь",
                )

        try:
            await message.get_bot().send_message(
                chat_id=chat.id,
                text=text,
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            logger.exception("Failed to announce crown change")

    async def warning_after_ten_seconds(self, state: EventState) -> None:
        try:
            await asyncio.sleep(max(0, EVENT_DURATION - 10))

            async with state.lock:
                if not state.active or state.current_user_id is None:
                    return

                user_id = state.current_user_id
                username = state.current_username
                first_name = state.current_first_name or "Пользователь"

            await self._send_warning(
                state.chat_id,
                user_id,
                username,
                first_name,
            )

        except asyncio.CancelledError:
            raise
        except TelegramError:
            logger.exception("Warning send failed")
        except Exception:
            logger.exception("Warning task failed")

    async def _send_warning(
        self,
        chat_id: int,
        user_id: int,
        username: Optional[str],
        first_name: str,
    ) -> None:
        # context-free sending через Application недоступно здесь,
        # поэтому метод переопределяется через bot reference ниже.
        if hasattr(self, "_application"):
            await self._application.bot.send_message(
                chat_id=chat_id,
                text=self.warning_text(user_id, username, first_name),
                parse_mode=ParseMode.HTML,
            )

    async def crown_timer(self, state: EventState) -> None:
        try:
            await asyncio.sleep(EVENT_DURATION)

            async with state.lock:
                if not state.active:
                    return
                if state.current_user_id is None:
                    return

                winner_id = state.current_user_id
                username = state.current_username
                first_name = state.current_first_name or "Пользователь"
                event_id = state.event_id

                # Сразу меняем состояние, чтобы гонка с новым сообщением
                # после 180 секунд не создала вторую победу.
                state.active = False
                state.cancel_tasks()

            await self.db.add_winner(
                winner_id,
                username,
                event_id,
                now_utc(),
                self.REWARD,
            )
            await self.db.finish_event(event_id, winner_id, now_utc())

            self.states.pop(state.chat_id, None)

            try:
                await self._application.bot.send_message(
                    chat_id=state.chat_id,
                    text=self.winner_text(winner_id, username, first_name),
                    parse_mode=ParseMode.HTML,
                )
            except TelegramError:
                logger.exception("Failed to announce winner")

            logger.info(
                "User %s won event %s in chat %s",
                winner_id,
                event_id,
                state.chat_id,
            )

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Crown timer failed")

    # --------------------------------------------------------
    # Админка
    # --------------------------------------------------------

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
            "<b>╚════════════════════╝</b>\n\n"
            "Управление ивентом <b>«ПЕРЕБИВ»</b>.\n\n"
            "Выберите нужный раздел:"
        )

    async def admin_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not user or not is_admin(user.id):
            if update.effective_message:
                await update.effective_message.reply_text(
                    "❌ У вас нет доступа к панели управления."
                )
            return

        await update.effective_message.reply_text(
            self.admin_text(),
            parse_mode=ParseMode.HTML,
            reply_markup=self.admin_keyboard(),
        )

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not user:
            return

        if is_admin(user.id):
            await update.effective_message.reply_text(
                self.admin_text(),
                parse_mode=ParseMode.HTML,
                reply_markup=self.admin_keyboard(),
            )
        else:
            await update.effective_message.reply_text(
                "❌ У вас нет доступа к панели управления."
            )

    async def callback_router(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        query = update.callback_query
        if not query:
            return

        user = query.from_user
        if not is_admin(user.id):
            await query.answer("❌ Нет доступа", show_alert=True)
            return

        await query.answer()
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
            elif data == "admin:user":
                await query.edit_message_text(
                    "<b>🔎 ПОЛЬЗОВАТЕЛЬ ПО ID</b>\n\n"
                    "Отправьте в этот чат сообщение вида:\n"
                    "<code>/user 123456789</code>",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("⬅️ Назад", callback_data="admin:menu")]]
                    ),
                )
            elif data == "admin:autoon":
                await self.db.update_settings(DEFAULT_CHAT_ID, auto_start=True)
                await query.answer("▶️ Автозапуск включён", show_alert=True)
                await self.show_schedule(query)
            elif data == "admin:autooff":
                await self.db.update_settings(DEFAULT_CHAT_ID, auto_start=False)
                await query.answer("⏹ Автозапуск выключен", show_alert=True)
                await self.show_schedule(query)
            elif data == "admin:interval:86400":
                await self.db.update_settings(
                    DEFAULT_CHAT_ID,
                    interval=86400,
                    next_event_time=now_utc() + timedelta(seconds=86400),
                )
                await query.answer("🔄 Интервал: 24 часа", show_alert=True)
                await self.show_schedule(query)
            elif data == "admin:interval:43200":
                await self.db.update_settings(
                    DEFAULT_CHAT_ID,
                    interval=43200,
                    next_event_time=now_utc() + timedelta(seconds=43200),
                )
                await query.answer("🔄 Интервал: 12 часов", show_alert=True)
                await self.show_schedule(query)
            elif data == "admin:interval:3600":
                await self.db.update_settings(
                    DEFAULT_CHAT_ID,
                    interval=3600,
                    next_event_time=now_utc() + timedelta(seconds=3600),
                )
                await query.answer("🔄 Интервал: 1 час", show_alert=True)
                await self.show_schedule(query)
            elif data == "admin:next":
                await query.edit_message_text(
                    "<b>⏰ ИЗМЕНЕНИЕ ВРЕМЕНИ</b>\n\n"
                    "Отправьте команду:\n"
                    "<code>/next 03.10.2026 20:00</code>\n\n"
                    "Время трактуется относительно UTC+0.\n"
                    "После команды следующий запуск будет установлен точно на указанное время.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("⬅️ Назад", callback_data="admin:schedule")]]
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
            name = (
                f"@{esc(row['username'])}"
                if row["username"]
                else esc(row["first_name"] or row["user_id"])
            )
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
                name = (
                    f"@{esc(row['username'])}"
                    if row["username"]
                    else esc(row["first_name"] or row["user_id"])
                )
                lines.extend(
                    [
                        f"<b>{i}.</b> 👑 {name} — 🧸",
                        f"   📅 {display_dt(row['won_at'])}",
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
            text = (
                "<b>👑 ТЕКУЩИЙ ИВЕНТ</b>\n\n"
                "⏹ Активного ивента сейчас нет."
            )
        else:
            async with state.lock:
                if state.current_user_id is None:
                    text = (
                        "<b>👑 ТЕКУЩИЙ ИВЕНТ</b>\n\n"
                        "🔥 Ивент активен.\n"
                        "👤 Царь ещё не выбран."
                    )
                else:
                    elapsed = 0
                    if state.crown_started_at:
                        elapsed = int((now_utc() - state.crown_started_at).total_seconds())
                    remaining = max(0, EVENT_DURATION - elapsed)
                    mention = user_mention(
                        state.current_user_id,
                        state.current_username,
                        state.current_first_name or "Пользователь",
                    )
                    text = (
                        "<b>👑 ТЕКУЩИЙ ИВЕНТ</b>\n\n"
                        "🔥 Статус: <b>активен</b>\n"
                        f"👑 Царь: {mention}\n"
                        f"⏱ Осталось: <b>{format_duration(remaining)}</b>\n"
                        f"🆔 Event ID: <code>{state.event_id}</code>"
                    )

        await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Назад", callback_data="admin:menu")]]
            ),
        )

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
            "<b>⏰ НАСТРОЙКА ЗАПУСКА</b>\n\n"
            f"⏰ Следующий запуск:\n<b>{display_dt(settings['next_event_time'])}</b>\n\n"
            f"⚙️ Интервал: <b>{interval_text}</b>\n"
            f"▶️ Автозапуск: <b>{'включён' if settings['auto_start'] else 'выключен'}</b>\n\n"
            "Для произвольного времени используйте:\n"
            "<code>/next ДД.ММ.ГГГГ ЧЧ:ММ</code>"
        )

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("➕ Изменить время", callback_data="admin:next"),
                ],
                [
                    InlineKeyboardButton("🔄 24 часа", callback_data="admin:interval:86400"),
                    InlineKeyboardButton("🔄 12 часов", callback_data="admin:interval:43200"),
                    InlineKeyboardButton("🔄 1 час", callback_data="admin:interval:3600"),
                ],
                [
                    InlineKeyboardButton("▶️ Включить", callback_data="admin:autoon"),
                    InlineKeyboardButton("⏹ Выключить", callback_data="admin:autooff"),
                ],
                [
                    InlineKeyboardButton("⬅️ Назад", callback_data="admin:menu"),
                ],
            ]
        )

        await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )

    async def show_chat_settings(self, query) -> None:
        text = (
            "<b>💬 НАСТРОЙКА ЧАТА</b>\n\n"
            f"🆔 Текущий CHAT_ID:\n<code>{DEFAULT_CHAT_ID}</code>\n\n"
            "Чтобы изменить чат, поменяйте <code>DEFAULT_CHAT_ID</code>\n"
            "в начале <code>main.py</code> и перезапустите бота.\n\n"
            "Бот должен быть добавлен в нужную группу."
        )
        await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Назад", callback_data="admin:menu")]]
            ),
        )

    async def user_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not user or not is_admin(user.id):
            return

        if not context.args:
            await update.effective_message.reply_text(
                "Использование: /user 123456789"
            )
            return

        try:
            user_id = int(context.args[0])
        except ValueError:
            await update.effective_message.reply_text("❌ Telegram ID должен быть числом.")
            return

        row = await self.db.get_user(user_id)
        if not row:
            await update.effective_message.reply_text(
                f"Пользователь <code>{user_id}</code> ещё не участвовал.",
                parse_mode=ParseMode.HTML,
            )
            return

        username = f"@{esc(row['username'])}" if row["username"] else "нет username"
        text = (
            "<b>👤 ПОЛЬЗОВАТЕЛЬ</b>\n\n"
            f"🆔 ID: <code>{row['user_id']}</code>\n"
            f"👤 Имя: {esc(row['first_name'])}\n"
            f"🔗 Username: {username}\n\n"
            f"🏆 Побед: <b>{row['wins']}</b>\n"
            f"🎮 Участий: <b>{row['participations']}</b>\n"
            f"💥 Перебивов: <b>{row['steals']}</b>\n"
            f"📅 Последняя победа: <b>{display_dt(row['last_win'])}</b>"
        )
        await update.effective_message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
        )

    async def next_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not user or not is_admin(user.id):
            return

        if len(context.args) < 2:
            await update.effective_message.reply_text(
                "Использование: /next 03.10.2026 20:00"
            )
            return

        value = " ".join(context.args[:2])
        try:
            local_dt = datetime.strptime(value, "%d.%m.%Y %H:%M")
            target = local_dt.replace(tzinfo=UTC)
        except ValueError:
            await update.effective_message.reply_text(
                "❌ Неверный формат.\nПример: /next 03.10.2026 20:00"
            )
            return

        if target <= now_utc():
            await update.effective_message.reply_text(
                "❌ Время должно быть в будущем."
            )
            return

        await self.db.update_settings(
            DEFAULT_CHAT_ID,
            next_event_time=target,
        )
        await update.effective_message.reply_text(
            f"✅ Следующий запуск установлен на <b>{display_dt(dt_to_str(target))}</b>.",
            parse_mode=ParseMode.HTML,
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


async def admin_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await bot_controller.admin_command(update, context)


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await bot_controller.start_command(update, context)


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await bot_controller.callback_router(update, context)


async def user_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await bot_controller.user_command(update, context)


async def next_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await bot_controller.next_command(update, context)


async def group_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await bot_controller.handle_player_message(update)
    except Exception:
        logger.exception("Group message handler failed")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error
    if isinstance(error, (BadRequest, Forbidden)):
        logger.warning("Telegram API error: %s", error)
    else:
        logger.exception("Unhandled Telegram error", exc_info=error)


async def chatid_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показывает ID текущего чата."""
    user = update.effective_user
    message = update.effective_message
    chat = update.effective_chat

    if not user or not message or not chat:
        return

    if not is_admin(user.id):
        await message.reply_text("❌ У вас нет доступа к этой команде.")
        return

    chat_title = chat.title or "Личный чат"
    await message.reply_text(
        f"🆔 <b>CHAT ID</b>\n\n"
        f"💬 Чат: <b>{esc(chat_title)}</b>\n"
        f"🔢 ID: <code>{chat.id}</code>\n\n"
        f"Скопируйте это число и вставьте в <code>DEFAULT_CHAT_ID</code> "
        f"в начале <code>main.py</code>.",
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# MAIN
# ============================================================

def main() -> None:
    if not BOT_TOKEN or BOT_TOKEN == "ВСТАВЬ_ТОКЕН_СЮДА":
        raise RuntimeError(
            "Укажите настоящий BOT_TOKEN в начале файла main.py."
        )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # Команды
    application.add_handler(CommandHandler("start", start_handler))
    application.add_handler(CommandHandler("admin", admin_handler))
    application.add_handler(CommandHandler("user", user_handler))
    application.add_handler(CommandHandler("next", next_handler))
    application.add_handler(CommandHandler("chatid", chatid_handler))

    # Inline-кнопки
    application.add_handler(CallbackQueryHandler(callback_handler))

    # Только сообщения из групп/супергрупп.
    # Бот сам игнорирует свои сообщения, а также игнорирует обычные
    # сообщения, когда активного события нет.
    application.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS & ~filters.COMMAND,
            group_message_handler,
        )
    )

    application.add_error_handler(error_handler)

    logger.info("Starting Telegram bot...")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
