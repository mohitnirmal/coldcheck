#!/usr/bin/env python3
"""
HabitHero: a tiny Telegram habit tracker with streaks, XP, levels, and badges.

It uses only Python's standard library:
  - Telegram Bot HTTP API via urllib
  - SQLite for local storage

Setup:
  1. Create a bot with Telegram's @BotFather and copy the token.
  2. Put TELEGRAM_BOT_TOKEN=your_token in a .env file, or export it.
  3. Run: python habit_hero_bot.py
  4. Open your bot in Telegram and send /start
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional


API_URL = "https://api.telegram.org/bot{token}/{method}"
DEFAULT_DB_PATH = "habithero.db"
DEFAULT_POLL_TIMEOUT = 30
DEFAULT_TZ_OFFSET_MINUTES = 330
MAX_HABIT_NAME_LENGTH = 80

LEVEL_TITLES = [
    "Seedling",
    "Momentum Rookie",
    "Routine Ranger",
    "Streak Smith",
    "Discipline Mage",
    "Tiny Wins Champion",
    "Legend of Showing Up",
]

MILESTONE_BONUSES = {
    3: 5,
    7: 15,
    14: 30,
    30: 75,
    60: 120,
    100: 200,
}

NUDGES = [
    "The smallest version still counts. Do the two-minute edition.",
    "A streak is just today's vote for who you are becoming.",
    "Make it almost too easy, then collect the XP anyway.",
    "Future you has excellent taste in tiny wins.",
    "One check-in is enough to keep the chain alive.",
]


@dataclass
class TelegramUser:
    chat_id: int
    user_id: int
    username: str
    first_name: str


@dataclass
class Habit:
    id: int
    chat_id: int
    user_id: int
    name: str
    current_streak: int
    best_streak: int
    total_checkins: int
    last_done_date: Optional[str]
    reminder_time: Optional[str]
    last_reminded_date: Optional[str]
    created_at: str


@dataclass
class RewardBreakdown:
    base_xp: int
    streak_bonus: int
    milestone_bonus: int

    @property
    def total_xp(self) -> int:
        return self.base_xp + self.streak_bonus + self.milestone_bonus


@dataclass
class CompletionResult:
    habit: Habit
    streak: int
    reward: RewardBreakdown
    level_before: int
    level_after: int
    already_done: bool = False


def load_dotenv(path: str = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def now_local() -> datetime:
    raw_offset = os.getenv("HABITBOT_UTC_OFFSET_MINUTES", str(DEFAULT_TZ_OFFSET_MINUTES))
    try:
        offset = int(raw_offset)
    except ValueError:
        offset = DEFAULT_TZ_OFFSET_MINUTES
    return datetime.utcnow() + timedelta(minutes=offset)


def today_key() -> str:
    return now_local().date().isoformat()


def date_from_key(date_key: str) -> date:
    return datetime.strptime(date_key, "%Y-%m-%d").date()


def previous_date_key(date_key: str) -> str:
    return (date_from_key(date_key) - timedelta(days=1)).isoformat()


def timestamp() -> str:
    return now_local().replace(microsecond=0).isoformat(sep=" ")


def current_time_key() -> str:
    return now_local().strftime("%H:%M")


def clean_habit_name(raw_name: str) -> str:
    return re.sub(r"\s+", " ", raw_name).strip()


def parse_reminder_time(raw_time: str) -> str:
    raw_time = raw_time.strip()
    match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", raw_time)
    if not match:
        raise ValueError("Use 24-hour time like 07:30 or 21:00.")
    hour = int(match.group(1))
    minute = int(match.group(2))
    return f"{hour:02d}:{minute:02d}"


def open_db(db_path: str) -> sqlite3.Connection:
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def init_db(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT NOT NULL DEFAULT '',
            first_name TEXT NOT NULL DEFAULT '',
            xp INTEGER NOT NULL DEFAULT 0,
            total_checkins INTEGER NOT NULL DEFAULT 0,
            best_streak INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (chat_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS habits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            current_streak INTEGER NOT NULL DEFAULT 0,
            best_streak INTEGER NOT NULL DEFAULT 0,
            total_checkins INTEGER NOT NULL DEFAULT 0,
            last_done_date TEXT,
            reminder_time TEXT,
            last_reminded_date TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            deleted_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_habits_owner_active
        ON habits (chat_id, user_id, active);

        CREATE TABLE IF NOT EXISTS checkins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            habit_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            date_key TEXT NOT NULL,
            xp_awarded INTEGER NOT NULL,
            streak_after INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (habit_id, date_key),
            FOREIGN KEY (habit_id) REFERENCES habits (id)
        );

        CREATE INDEX IF NOT EXISTS idx_checkins_owner_date
        ON checkins (chat_id, user_id, date_key);
        """
    )
    migrate_db(db)
    db.commit()


def migrate_db(db: sqlite3.Connection) -> None:
    habit_columns = {
        str(row["name"])
        for row in db.execute("PRAGMA table_info(habits)").fetchall()
    }
    if "reminder_time" not in habit_columns:
        db.execute("ALTER TABLE habits ADD COLUMN reminder_time TEXT")
    if "last_reminded_date" not in habit_columns:
        db.execute("ALTER TABLE habits ADD COLUMN last_reminded_date TEXT")


def ensure_user(db: sqlite3.Connection, user: TelegramUser) -> None:
    current_time = timestamp()
    db.execute(
        """
        INSERT INTO users (
            chat_id, user_id, username, first_name, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(chat_id, user_id) DO UPDATE SET
            username = excluded.username,
            first_name = excluded.first_name,
            updated_at = excluded.updated_at
        """,
        (
            user.chat_id,
            user.user_id,
            user.username,
            user.first_name,
            current_time,
            current_time,
        ),
    )
    db.commit()


def get_user_row(db: sqlite3.Connection, user: TelegramUser) -> sqlite3.Row:
    ensure_user(db, user)
    row = db.execute(
        "SELECT * FROM users WHERE chat_id = ? AND user_id = ?",
        (user.chat_id, user.user_id),
    ).fetchone()
    if row is None:
        raise RuntimeError("User could not be loaded after creation.")
    return row


def build_habit(row: sqlite3.Row) -> Habit:
    return Habit(
        id=int(row["id"]),
        chat_id=int(row["chat_id"]),
        user_id=int(row["user_id"]),
        name=str(row["name"]),
        current_streak=int(row["current_streak"]),
        best_streak=int(row["best_streak"]),
        total_checkins=int(row["total_checkins"]),
        last_done_date=str(row["last_done_date"]) if row["last_done_date"] else None,
        reminder_time=str(row["reminder_time"]) if row["reminder_time"] else None,
        last_reminded_date=str(row["last_reminded_date"]) if row["last_reminded_date"] else None,
        created_at=str(row["created_at"]),
    )


def level_for_xp(xp: int) -> int:
    return max(1, xp // 100 + 1)


def level_title(level: int) -> str:
    index = min(level - 1, len(LEVEL_TITLES) - 1)
    return LEVEL_TITLES[index]


def xp_to_next_level(xp: int) -> int:
    return level_for_xp(xp) * 100 - xp


def reward_for_streak(streak: int) -> RewardBreakdown:
    streak = max(1, streak)
    base_xp = 10
    streak_bonus = min(10, streak)
    milestone_bonus = MILESTONE_BONUSES.get(streak, 0)
    return RewardBreakdown(base_xp, streak_bonus, milestone_bonus)


def add_habit(db: sqlite3.Connection, user: TelegramUser, raw_name: str) -> tuple[Habit, bool]:
    ensure_user(db, user)
    name = clean_habit_name(raw_name)
    if not name:
        raise ValueError("Habit name cannot be empty.")
    if len(name) > MAX_HABIT_NAME_LENGTH:
        raise ValueError(f"Keep habit names under {MAX_HABIT_NAME_LENGTH} characters.")

    existing = db.execute(
        """
        SELECT *
        FROM habits
        WHERE chat_id = ? AND user_id = ? AND active = 1
          AND lower(name) = lower(?)
        """,
        (user.chat_id, user.user_id, name),
    ).fetchone()
    if existing is not None:
        return build_habit(existing), False

    current_time = timestamp()
    cursor = db.execute(
        """
        INSERT INTO habits (chat_id, user_id, name, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (user.chat_id, user.user_id, name, current_time, current_time),
    )
    db.commit()
    row = db.execute("SELECT * FROM habits WHERE id = ?", (cursor.lastrowid,)).fetchone()
    if row is None:
        raise RuntimeError("Habit could not be loaded after creation.")
    return build_habit(row), True


def list_active_habits(db: sqlite3.Connection, user: TelegramUser) -> list[Habit]:
    ensure_user(db, user)
    rows = db.execute(
        """
        SELECT *
        FROM habits
        WHERE chat_id = ? AND user_id = ? AND active = 1
        ORDER BY id
        """,
        (user.chat_id, user.user_id),
    ).fetchall()
    return [build_habit(row) for row in rows]


def done_habit_ids_for_date(db: sqlite3.Connection, user: TelegramUser, date_key: str) -> set[int]:
    ensure_user(db, user)
    rows = db.execute(
        """
        SELECT habit_id
        FROM checkins
        WHERE chat_id = ? AND user_id = ? AND date_key = ?
        """,
        (user.chat_id, user.user_id, date_key),
    ).fetchall()
    return {int(row["habit_id"]) for row in rows}


def find_active_habit(db: sqlite3.Connection, user: TelegramUser, query: str) -> tuple[Optional[Habit], list[Habit]]:
    ensure_user(db, user)
    query = query.strip()
    if query.isdigit():
        rows = db.execute(
            """
            SELECT *
            FROM habits
            WHERE chat_id = ? AND user_id = ? AND active = 1 AND id = ?
            """,
            (user.chat_id, user.user_id, int(query)),
        ).fetchall()
    else:
        rows = db.execute(
            """
            SELECT *
            FROM habits
            WHERE chat_id = ? AND user_id = ? AND active = 1
              AND lower(name) LIKE lower(?)
            ORDER BY CASE WHEN lower(name) = lower(?) THEN 0 ELSE 1 END, id
            LIMIT 6
            """,
            (user.chat_id, user.user_id, f"%{query}%", query),
        ).fetchall()

    matches = [build_habit(row) for row in rows]
    return (matches[0], matches) if len(matches) == 1 else (None, matches)


def complete_habit(
    db: sqlite3.Connection,
    user: TelegramUser,
    habit: Habit,
    date_key: Optional[str] = None,
) -> CompletionResult:
    ensure_user(db, user)
    checkin_date = date_key or today_key()

    if habit.last_done_date == checkin_date:
        return CompletionResult(
            habit=habit,
            streak=habit.current_streak,
            reward=RewardBreakdown(0, 0, 0),
            level_before=level_for_xp(int(get_user_row(db, user)["xp"])),
            level_after=level_for_xp(int(get_user_row(db, user)["xp"])),
            already_done=True,
        )

    if habit.last_done_date == previous_date_key(checkin_date):
        new_streak = habit.current_streak + 1
    else:
        new_streak = 1

    reward = reward_for_streak(new_streak)
    user_before = get_user_row(db, user)
    level_before = level_for_xp(int(user_before["xp"]))
    current_time = timestamp()
    new_best = max(habit.best_streak, new_streak)

    with db:
        db.execute(
            """
            INSERT INTO checkins (
                habit_id, chat_id, user_id, date_key, xp_awarded, streak_after, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                habit.id,
                user.chat_id,
                user.user_id,
                checkin_date,
                reward.total_xp,
                new_streak,
                current_time,
            ),
        )
        db.execute(
            """
            UPDATE habits
            SET current_streak = ?,
                best_streak = ?,
                total_checkins = total_checkins + 1,
                last_done_date = ?,
                updated_at = ?
            WHERE id = ? AND chat_id = ? AND user_id = ? AND active = 1
            """,
            (
                new_streak,
                new_best,
                checkin_date,
                current_time,
                habit.id,
                user.chat_id,
                user.user_id,
            ),
        )
        db.execute(
            """
            UPDATE users
            SET xp = xp + ?,
                total_checkins = total_checkins + 1,
                best_streak = max(best_streak, ?),
                updated_at = ?
            WHERE chat_id = ? AND user_id = ?
            """,
            (
                reward.total_xp,
                new_streak,
                current_time,
                user.chat_id,
                user.user_id,
            ),
        )

    level_after = level_for_xp(int(get_user_row(db, user)["xp"]))
    updated_habit, _ = find_active_habit(db, user, str(habit.id))
    return CompletionResult(
        habit=updated_habit or habit,
        streak=new_streak,
        reward=reward,
        level_before=level_before,
        level_after=level_after,
    )


def delete_habit(db: sqlite3.Connection, user: TelegramUser, habit_id: int) -> bool:
    ensure_user(db, user)
    cursor = db.execute(
        """
        UPDATE habits
        SET active = 0, deleted_at = ?, updated_at = ?
        WHERE chat_id = ? AND user_id = ? AND active = 1 AND id = ?
        """,
        (timestamp(), timestamp(), user.chat_id, user.user_id, habit_id),
    )
    db.commit()
    return cursor.rowcount > 0


def rename_habit(db: sqlite3.Connection, user: TelegramUser, habit_id: int, raw_name: str) -> str:
    ensure_user(db, user)
    name = clean_habit_name(raw_name)
    if not name:
        return "Usage: /rename <id> <new habit name>"
    if len(name) > MAX_HABIT_NAME_LENGTH:
        return f"Keep habit names under {MAX_HABIT_NAME_LENGTH} characters."

    duplicate = db.execute(
        """
        SELECT id
        FROM habits
        WHERE chat_id = ? AND user_id = ? AND active = 1
          AND id <> ? AND lower(name) = lower(?)
        """,
        (user.chat_id, user.user_id, habit_id, name),
    ).fetchone()
    if duplicate is not None:
        return "You already have an active habit with that name."

    cursor = db.execute(
        """
        UPDATE habits
        SET name = ?, updated_at = ?
        WHERE chat_id = ? AND user_id = ? AND active = 1 AND id = ?
        """,
        (name, timestamp(), user.chat_id, user.user_id, habit_id),
    )
    db.commit()
    if cursor.rowcount == 0:
        return "I could not find that habit. Use /habits to check the ID."
    return f"Renamed habit {habit_id} to: {name}"


def set_habit_reminder(
    db: sqlite3.Connection,
    user: TelegramUser,
    habit_id: int,
    raw_time: str,
) -> tuple[bool, str]:
    ensure_user(db, user)
    reminder_time = parse_reminder_time(raw_time)
    last_reminded_date = today_key() if reminder_time <= current_time_key() else None
    cursor = db.execute(
        """
        UPDATE habits
        SET reminder_time = ?,
            last_reminded_date = ?,
            updated_at = ?
        WHERE chat_id = ? AND user_id = ? AND active = 1 AND id = ?
        """,
        (
            reminder_time,
            last_reminded_date,
            timestamp(),
            user.chat_id,
            user.user_id,
            habit_id,
        ),
    )
    db.commit()
    return cursor.rowcount > 0, reminder_time


def clear_habit_reminder(db: sqlite3.Connection, user: TelegramUser, habit_id: int) -> bool:
    ensure_user(db, user)
    cursor = db.execute(
        """
        UPDATE habits
        SET reminder_time = NULL,
            last_reminded_date = NULL,
            updated_at = ?
        WHERE chat_id = ? AND user_id = ? AND active = 1 AND id = ?
        """,
        (timestamp(), user.chat_id, user.user_id, habit_id),
    )
    db.commit()
    return cursor.rowcount > 0


def due_reminder_habits(
    db: sqlite3.Connection,
    current_date: str,
    current_time: str,
) -> list[Habit]:
    rows = db.execute(
        """
        SELECT *
        FROM habits
        WHERE active = 1
          AND reminder_time IS NOT NULL
          AND reminder_time <= ?
          AND (last_reminded_date IS NULL OR last_reminded_date <> ?)
          AND (last_done_date IS NULL OR last_done_date <> ?)
        ORDER BY reminder_time, id
        """,
        (current_time, current_date, current_date),
    ).fetchall()
    return [build_habit(row) for row in rows]


def mark_habit_reminded(db: sqlite3.Connection, habit: Habit, current_date: str) -> None:
    db.execute(
        """
        UPDATE habits
        SET last_reminded_date = ?, updated_at = ?
        WHERE id = ?
        """,
        (current_date, timestamp(), habit.id),
    )
    db.commit()


def reminder_message(habit: Habit) -> str:
    return (
        f"Reminder: {habit.name}\n"
        f"Keep the chain alive with /done {habit.id}.\n"
        f"Current streak: {habit.current_streak} day(s)."
    )


def today_progress(db: sqlite3.Connection, user: TelegramUser) -> tuple[int, int]:
    habits = list_active_habits(db, user)
    done_ids = done_habit_ids_for_date(db, user, today_key())
    active_ids = {habit.id for habit in habits}
    return len(done_ids & active_ids), len(habits)


def badges_for_user(row: sqlite3.Row) -> list[str]:
    xp = int(row["xp"])
    total = int(row["total_checkins"])
    best = int(row["best_streak"])
    badges = []

    if total >= 1:
        badges.append("First Spark")
    if total >= 25:
        badges.append("Twenty-Five Check-ins")
    if best >= 3:
        badges.append("Three-Day Chain")
    if best >= 7:
        badges.append("Week Warrior")
    if best >= 30:
        badges.append("Thirty-Day Myth")
    if level_for_xp(xp) >= 5:
        badges.append("Level 5 Club")

    return badges


def help_text() -> str:
    return (
        "HabitHero commands:\n"
        "/add <habit> - add a recurring habit\n"
        "/habits - list habits and today's progress\n"
        "/today - same as /habits\n"
        "/done <id or name> - check in a habit for today\n"
        "/delete <id> - remove a habit from your active list\n"
        "/rename <id> <name> - rename a habit\n"
        "/remind <id> <HH:MM> - set a daily reminder\n"
        "/remind <id> off - turn off a reminder\n"
        "/reminders - list reminder times\n"
        "/stats - show XP, level, streaks, and badges\n"
        "/week - show the last 7 days of check-ins\n"
        "/nudge - get a tiny push\n"
        "/help - show this menu"
    )


def start_text(first_name: str) -> str:
    name = first_name or "hero"
    return (
        f"Welcome, {name}. I am HabitHero.\n\n"
        "Add habits, check them in once per day, keep streaks alive, and earn XP "
        "with small streak bonuses and milestone boosts.\n\n"
        "Try:\n"
        "/add Drink water\n"
        "/add Read 10 pages\n"
        "/remind 1 21:00\n"
        "/done 1\n\n"
        + help_text()
    )


def format_habit(habit: Habit, done_today: bool) -> str:
    marker = "[x]" if done_today else "[ ]"
    reminder = f", reminder {habit.reminder_time}" if habit.reminder_time else ""
    return (
        f"{marker} {habit.id}. {habit.name} - "
        f"streak {habit.current_streak}d, best {habit.best_streak}d{reminder}"
    )


def habits_text(db: sqlite3.Connection, user: TelegramUser) -> str:
    habits = list_active_habits(db, user)
    if not habits:
        return (
            "No habits yet. Add one with /add <habit>.\n"
            "Example: /add Walk for 10 minutes"
        )

    current_date = today_key()
    done_ids = done_habit_ids_for_date(db, user, current_date)
    done_count = len({habit.id for habit in habits} & done_ids)
    lines = [f"Today: {done_count}/{len(habits)} habits checked in."]
    lines.extend(format_habit(habit, habit.id in done_ids) for habit in habits)
    lines.append("\nCheck in with /done <id>.")
    return "\n".join(lines)


def add_habit_text(db: sqlite3.Connection, user: TelegramUser, arg: str) -> str:
    if not arg:
        return "Usage: /add <habit>\nExample: /add Stretch for 5 minutes"
    try:
        habit, created = add_habit(db, user, arg)
    except ValueError as exc:
        return str(exc)

    if not created:
        return f"That habit already exists as #{habit.id}: {habit.name}"

    return (
        "Habit added.\n"
        f"{habit.id}. {habit.name}\n"
        "Check it in once per day with /done "
        f"{habit.id}.\n"
        f"Set a reminder with /remind {habit.id} 21:00."
    )


def ambiguous_habit_text(matches: list[Habit]) -> str:
    if not matches:
        return "I could not find that habit. Use /habits to see habit IDs."

    lines = ["I found multiple matching habits. Use the ID:"]
    lines.extend(f"{habit.id}. {habit.name}" for habit in matches)
    return "\n".join(lines)


def done_habit_text(db: sqlite3.Connection, user: TelegramUser, arg: str) -> str:
    if not arg:
        return "Usage: /done <id or habit name>\nTip: /habits shows habit IDs."

    habit, matches = find_active_habit(db, user, arg)
    if habit is None:
        return ambiguous_habit_text(matches)

    try:
        result = complete_habit(db, user, habit)
    except sqlite3.IntegrityError:
        return f"{habit.name} is already checked in for today."

    if result.already_done:
        return (
            f"{habit.name} is already checked in today.\n"
            f"Current streak: {habit.current_streak} day(s)."
        )

    reward = result.reward
    lines = [
        f"Checked in: {result.habit.name}",
        f"Streak: {result.streak} day(s).",
        (
            f"XP gained: {reward.total_xp} "
            f"({reward.base_xp} base + {reward.streak_bonus} streak"
            + (f" + {reward.milestone_bonus} milestone" if reward.milestone_bonus else "")
            + ")."
        ),
    ]
    if result.level_after > result.level_before:
        lines.append(f"Level up: {result.level_after} - {level_title(result.level_after)}")
    if reward.milestone_bonus:
        lines.append("Milestone bonus unlocked. The chain is getting real.")
    return "\n".join(lines)


def delete_habit_text(db: sqlite3.Connection, user: TelegramUser, arg: str) -> str:
    if not arg or not arg.isdigit():
        return "Usage: /delete <id>"
    if delete_habit(db, user, int(arg)):
        return f"Habit {arg} removed from your active list. Your past XP stays safe."
    return "I could not delete that habit. Use /habits to check the ID."


def rename_habit_text(db: sqlite3.Connection, user: TelegramUser, arg: str) -> str:
    habit_id, _, new_name = arg.partition(" ")
    if not habit_id.isdigit() or not new_name.strip():
        return "Usage: /rename <id> <new habit name>"
    return rename_habit(db, user, int(habit_id), new_name)


def remind_habit_text(db: sqlite3.Connection, user: TelegramUser, arg: str) -> str:
    habit_id, _, raw_time = arg.partition(" ")
    raw_time = raw_time.strip()
    if not habit_id.isdigit() or not raw_time:
        return (
            "Usage: /remind <id> <HH:MM>\n"
            "Example: /remind 1 21:00\n"
            "Turn it off with /remind 1 off."
        )

    habit, _ = find_active_habit(db, user, habit_id)
    if habit is None:
        return "I could not find that habit. Use /habits to check the ID."

    if raw_time.lower() in {"off", "clear", "none", "remove"}:
        if clear_habit_reminder(db, user, int(habit_id)):
            return f"Reminder turned off for: {habit.name}"
        return "I could not turn off that reminder. Use /habits to check the ID."

    try:
        updated, reminder_time = set_habit_reminder(db, user, int(habit_id), raw_time)
    except ValueError as exc:
        return str(exc)

    if not updated:
        return "I could not set that reminder. Use /habits to check the ID."
    return (
        f"Daily reminder set for {reminder_time}.\n"
        f"Habit: {habit.name}\n"
        "If it is already checked in that day, I will stay quiet."
    )


def reminders_text(db: sqlite3.Connection, user: TelegramUser) -> str:
    habits = list_active_habits(db, user)
    reminder_habits = [habit for habit in habits if habit.reminder_time]
    if not habits:
        return "No habits yet. Add one with /add <habit>."
    if not reminder_habits:
        return "No reminders set. Use /remind <id> <HH:MM>, for example /remind 1 21:00."

    lines = ["Daily reminders:"]
    for habit in reminder_habits:
        lines.append(f"{habit.id}. {habit.name} - {habit.reminder_time}")
    lines.append("\nTurn one off with /remind <id> off.")
    return "\n".join(lines)


def stats_text(db: sqlite3.Connection, user: TelegramUser) -> str:
    row = get_user_row(db, user)
    xp = int(row["xp"])
    level = level_for_xp(xp)
    done_today, total_today = today_progress(db, user)
    badges = badges_for_user(row)

    return (
        "Your HabitHero stats:\n"
        f"Level: {level} - {level_title(level)}\n"
        f"XP: {xp} ({xp_to_next_level(xp)} XP to next level)\n"
        f"Total check-ins: {int(row['total_checkins'])}\n"
        f"Best streak: {int(row['best_streak'])} day(s)\n"
        f"Today: {done_today}/{total_today} habits checked in\n"
        f"Badges: {', '.join(badges) if badges else 'None yet'}"
    )


def week_text(db: sqlite3.Connection, user: TelegramUser) -> str:
    ensure_user(db, user)
    end_day = date_from_key(today_key())
    days = [(end_day - timedelta(days=offset)).isoformat() for offset in range(6, -1, -1)]
    rows = db.execute(
        """
        SELECT date_key, count(*) AS total
        FROM checkins
        WHERE chat_id = ? AND user_id = ? AND date_key BETWEEN ? AND ?
        GROUP BY date_key
        ORDER BY date_key
        """,
        (user.chat_id, user.user_id, days[0], days[-1]),
    ).fetchall()
    counts = {str(row["date_key"]): int(row["total"]) for row in rows}
    total = sum(counts.values())

    lines = [f"Last 7 days: {total} check-in(s)."]
    for day in days:
        lines.append(f"{day}: {counts.get(day, 0)}")
    return "\n".join(lines)


def handle_command(db: sqlite3.Connection, user: TelegramUser, text: str) -> str:
    command, _, arg = text.partition(" ")
    command = command.split("@", 1)[0].lower()
    arg = arg.strip()

    if command in {"/start", "/help"}:
        return start_text(user.first_name) if command == "/start" else help_text()

    if command == "/add":
        return add_habit_text(db, user, arg)

    if command in {"/habits", "/today"}:
        return habits_text(db, user)

    if command in {"/done", "/check"}:
        return done_habit_text(db, user, arg)

    if command in {"/delete", "/remove"}:
        return delete_habit_text(db, user, arg)

    if command == "/rename":
        return rename_habit_text(db, user, arg)

    if command == "/remind":
        return remind_habit_text(db, user, arg)

    if command == "/reminders":
        return reminders_text(db, user)

    if command == "/stats":
        return stats_text(db, user)

    if command == "/week":
        return week_text(db, user)

    if command == "/nudge":
        return random.choice(NUDGES)

    return "Unknown command. Use /help to see what I understand."


def api_call(
    token: str,
    method: str,
    params: Optional[dict[str, Any]] = None,
    request_timeout: Optional[int] = None,
) -> Any:
    url = API_URL.format(token=token, method=method)
    data = urllib.parse.urlencode(params or {}).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(
        request,
        timeout=request_timeout or DEFAULT_POLL_TIMEOUT + 10,
    ) as response:
        payload = json.loads(response.read().decode("utf-8"))

    if not payload.get("ok"):
        description = payload.get("description", "unknown Telegram API error")
        raise RuntimeError(description)
    return payload.get("result")


def send_message(token: str, chat_id: int, text: str, reply_to: Optional[int] = None) -> None:
    params: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text[:4096],
        "disable_web_page_preview": "true",
    }
    if reply_to is not None:
        params["reply_to_message_id"] = reply_to
        params["allow_sending_without_reply"] = "true"
    api_call(token, "sendMessage", params)


def get_updates(token: str, offset: Optional[int], timeout: int) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "timeout": timeout,
        "allowed_updates": json.dumps(["message"]),
    }
    if offset is not None:
        params["offset"] = offset
    return list(api_call(token, "getUpdates", params, request_timeout=timeout + 10) or [])


def send_due_reminders(db: sqlite3.Connection, token: str) -> None:
    current_date = today_key()
    current_time = current_time_key()
    for habit in due_reminder_habits(db, current_date, current_time):
        try:
            send_message(token, habit.chat_id, reminder_message(habit))
        except Exception as exc:  # noqa: BLE001 - one bad send should not stop the bot
            print(f"[reminder error] habit {habit.id}: {exc}", file=sys.stderr)
            continue
        mark_habit_reminded(db, habit, current_date)


def telegram_user_from_message(message: dict[str, Any]) -> Optional[TelegramUser]:
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    chat_id = chat.get("id")
    user_id = sender.get("id")
    if chat_id is None or user_id is None:
        return None
    return TelegramUser(
        chat_id=int(chat_id),
        user_id=int(user_id),
        username=str(sender.get("username") or ""),
        first_name=str(sender.get("first_name") or ""),
    )


def process_update(db: sqlite3.Connection, token: str, update: dict[str, Any]) -> None:
    message = update.get("message") or {}
    text = str(message.get("text") or "").strip()
    if not text.startswith("/"):
        return

    user = telegram_user_from_message(message)
    if user is None:
        return

    try:
        response = handle_command(db, user, text)
    except Exception as exc:  # noqa: BLE001 - keep the bot alive for the next message
        print(f"[error] command failed: {exc}", file=sys.stderr)
        response = "Something broke while handling that command. Check the bot console logs."

    send_message(token, user.chat_id, response, message.get("message_id"))


def warm_start_offset(token: str, process_old: bool) -> Optional[int]:
    if process_old:
        return None
    try:
        updates = get_updates(token, offset=None, timeout=1)
    except Exception:
        return None
    if not updates:
        return None
    return int(updates[-1]["update_id"]) + 1


def run_bot(token: str, db_path: str, poll_timeout: int, process_old: bool) -> None:
    db = open_db(db_path)
    init_db(db)

    offset = warm_start_offset(token, process_old)
    print("HabitHero is running. Open your bot in Telegram and send /start.")
    print(f"Database: {db_path}")
    print("Press Ctrl+C to stop.")

    while True:
        try:
            send_due_reminders(db, token)
            updates = get_updates(token, offset, poll_timeout)
            for update in updates:
                offset = int(update["update_id"]) + 1
                process_update(db, token, update)
            send_due_reminders(db, token)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            print(f"[telegram http error] {exc.code}: {body}", file=sys.stderr)
            time.sleep(5)
        except urllib.error.URLError as exc:
            print(f"[network error] {exc.reason}", file=sys.stderr)
            time.sleep(5)
        except KeyboardInterrupt:
            print("\nHabitHero stopped.")
            break
        except Exception as exc:  # noqa: BLE001 - long-running bot should recover
            print(f"[error] {exc}", file=sys.stderr)
            time.sleep(5)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the HabitHero Telegram bot.")
    parser.add_argument("--db", help="SQLite database path. Default: HABITBOT_DB or habithero.db")
    parser.add_argument("--env", default=".env", help="Path to .env file. Default: .env")
    parser.add_argument("--token", help="Telegram bot token. Overrides TELEGRAM_BOT_TOKEN.")
    parser.add_argument(
        "--poll-timeout",
        type=int,
        default=DEFAULT_POLL_TIMEOUT,
        help=f"Telegram long-poll timeout in seconds. Default: {DEFAULT_POLL_TIMEOUT}.",
    )
    parser.add_argument(
        "--process-old",
        action="store_true",
        help="Process old queued Telegram updates instead of starting fresh.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    load_dotenv(args.env)
    token = args.token or os.getenv("TELEGRAM_BOT_TOKEN")
    db_path = args.db or os.getenv("HABITBOT_DB", DEFAULT_DB_PATH)

    if not token:
        print(
            "Missing TELEGRAM_BOT_TOKEN.\n"
            "Create a bot with @BotFather, then put this in .env:\n"
            "TELEGRAM_BOT_TOKEN=123456:ABC-your-token",
            file=sys.stderr,
        )
        return 2

    run_bot(token, db_path, args.poll_timeout, args.process_old)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
