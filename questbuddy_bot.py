#!/usr/bin/env python3
"""
QuestBuddy: a tiny Telegram life-RPG bot.

It uses only Python's standard library:
  - Telegram Bot HTTP API via urllib
  - SQLite for local storage

Setup:
  1. Create a bot with Telegram's @BotFather and copy the token.
  2. Put TELEGRAM_BOT_TOKEN=your_token in a .env file, or export it.
  3. Run: python questbuddy_bot.py
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
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional


API_URL = "https://api.telegram.org/bot{token}/{method}"
DEFAULT_DB_PATH = "questbuddy.db"
DEFAULT_POLL_TIMEOUT = 30
DEFAULT_TZ_OFFSET_MINUTES = 330

QUEST_VERBS = [
    "Conquer",
    "Defeat",
    "Tame",
    "Clear",
    "Outsmart",
    "Wrangle",
    "Finish",
    "Rescue",
]

LEVEL_TITLES = [
    "Sleepy Initiate",
    "Mildly Responsible Human",
    "Apprentice of Getting Things Done",
    "Deadline Duelist",
    "Focus Ranger",
    "Certified Quest Crusher",
    "Grandmaster of Tiny Wins",
]

NUDGES = [
    "One tiny quest is enough to restart the engine.",
    "Start with the easiest one. Momentum is basically legal magic.",
    "Past you made the list. Present you gets the XP.",
    "The task cannot defeat you if you open it dramatically.",
    "Do the first two minutes. Negotiate with the universe after that.",
]

SHOP_ITEMS = {
    "break": {
        "name": "10-minute guilt-free break",
        "cost": 20,
        "message": "Break token unlocked. Walk away like a responsible legend.",
    },
    "episode": {
        "name": "One episode token",
        "cost": 75,
        "message": "Episode token unlocked. No doom-scrolling side quests.",
    },
    "snack": {
        "name": "Snack privilege",
        "cost": 35,
        "message": "Snack privilege unlocked. Hydrate too, brave adventurer.",
    },
}


@dataclass
class TelegramUser:
    chat_id: int
    user_id: int
    username: str
    first_name: str


@dataclass
class Task:
    id: int
    text: str
    xp: int
    coins: int
    created_at: str


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
    raw_offset = os.getenv("QUESTBOT_UTC_OFFSET_MINUTES", str(DEFAULT_TZ_OFFSET_MINUTES))
    try:
        offset = int(raw_offset)
    except ValueError:
        offset = DEFAULT_TZ_OFFSET_MINUTES
    return datetime.utcnow() + timedelta(minutes=offset)


def today_key() -> str:
    return now_local().date().isoformat()


def yesterday_key() -> str:
    return (now_local().date() - timedelta(days=1)).isoformat()


def timestamp() -> str:
    return now_local().replace(microsecond=0).isoformat(sep=" ")


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
            coins INTEGER NOT NULL DEFAULT 0,
            completed_count INTEGER NOT NULL DEFAULT 0,
            streak INTEGER NOT NULL DEFAULT 0,
            last_completed_date TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (chat_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            xp INTEGER NOT NULL,
            coins INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL,
            completed_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_tasks_owner_status
        ON tasks (chat_id, user_id, status);
        """
    )
    db.commit()


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


def level_for_xp(xp: int) -> int:
    return max(1, xp // 100 + 1)


def level_title(level: int) -> str:
    index = min(level - 1, len(LEVEL_TITLES) - 1)
    return LEVEL_TITLES[index]


def calculate_rewards(text: str) -> tuple[int, int]:
    clean = text.strip()
    xp = 20 + min(45, len(clean) // 8 * 5)

    duration_match = re.search(
        r"\b(\d+)\s*(min|mins|minute|minutes|hr|hrs|hour|hours)\b",
        clean,
        re.IGNORECASE,
    )
    if duration_match:
        amount = int(duration_match.group(1))
        unit = duration_match.group(2).lower()
        minutes = amount * 60 if unit.startswith(("hr", "hour")) else amount
        xp += min(40, max(0, minutes // 15 * 5))

    if re.search(r"\b(study|assignment|project|workout|exam|clean|code|write)\b", clean, re.I):
        xp += 10

    xp = min(100, max(15, xp))
    coins = max(3, xp // 5)
    return xp, coins


def quest_name(text: str) -> str:
    return f"{random.choice(QUEST_VERBS)}: {text.strip()}"


def add_task(db: sqlite3.Connection, user: TelegramUser, text: str) -> Task:
    ensure_user(db, user)
    xp, coins = calculate_rewards(text)
    current_time = timestamp()
    cursor = db.execute(
        """
        INSERT INTO tasks (chat_id, user_id, text, xp, coins, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (user.chat_id, user.user_id, text.strip(), xp, coins, current_time),
    )
    db.commit()
    return Task(
        id=int(cursor.lastrowid),
        text=text.strip(),
        xp=xp,
        coins=coins,
        created_at=current_time,
    )


def list_open_tasks(db: sqlite3.Connection, user: TelegramUser) -> list[Task]:
    ensure_user(db, user)
    rows = db.execute(
        """
        SELECT id, text, xp, coins, created_at
        FROM tasks
        WHERE chat_id = ? AND user_id = ? AND status = 'open'
        ORDER BY id
        """,
        (user.chat_id, user.user_id),
    ).fetchall()
    return [
        Task(
            id=int(row["id"]),
            text=str(row["text"]),
            xp=int(row["xp"]),
            coins=int(row["coins"]),
            created_at=str(row["created_at"]),
        )
        for row in rows
    ]


def find_open_task(db: sqlite3.Connection, user: TelegramUser, query: str) -> tuple[Optional[Task], list[Task]]:
    ensure_user(db, user)
    query = query.strip()
    if query.isdigit():
        rows = db.execute(
            """
            SELECT id, text, xp, coins, created_at
            FROM tasks
            WHERE chat_id = ? AND user_id = ? AND status = 'open' AND id = ?
            """,
            (user.chat_id, user.user_id, int(query)),
        ).fetchall()
    else:
        rows = db.execute(
            """
            SELECT id, text, xp, coins, created_at
            FROM tasks
            WHERE chat_id = ? AND user_id = ? AND status = 'open'
              AND lower(text) LIKE lower(?)
            ORDER BY id
            LIMIT 5
            """,
            (user.chat_id, user.user_id, f"%{query}%"),
        ).fetchall()

    matches = [
        Task(
            id=int(row["id"]),
            text=str(row["text"]),
            xp=int(row["xp"]),
            coins=int(row["coins"]),
            created_at=str(row["created_at"]),
        )
        for row in rows
    ]
    return (matches[0], matches) if len(matches) == 1 else (None, matches)


def complete_task(db: sqlite3.Connection, user: TelegramUser, task: Task) -> sqlite3.Row:
    ensure_user(db, user)
    row = get_user_row(db, user)
    last_completed = row["last_completed_date"]

    if last_completed == today_key():
        streak = max(1, int(row["streak"]))
    elif last_completed == yesterday_key():
        streak = int(row["streak"]) + 1
    else:
        streak = 1

    current_time = timestamp()
    with db:
        db.execute(
            """
            UPDATE tasks
            SET status = 'done', completed_at = ?
            WHERE id = ? AND chat_id = ? AND user_id = ? AND status = 'open'
            """,
            (current_time, task.id, user.chat_id, user.user_id),
        )
        db.execute(
            """
            UPDATE users
            SET xp = xp + ?,
                coins = coins + ?,
                completed_count = completed_count + 1,
                streak = ?,
                last_completed_date = ?,
                updated_at = ?
            WHERE chat_id = ? AND user_id = ?
            """,
            (
                task.xp,
                task.coins,
                streak,
                today_key(),
                current_time,
                user.chat_id,
                user.user_id,
            ),
        )
    return get_user_row(db, user)


def delete_task(db: sqlite3.Connection, user: TelegramUser, task_id: int) -> bool:
    ensure_user(db, user)
    cursor = db.execute(
        """
        DELETE FROM tasks
        WHERE chat_id = ? AND user_id = ? AND status = 'open' AND id = ?
        """,
        (user.chat_id, user.user_id, task_id),
    )
    db.commit()
    return cursor.rowcount > 0


def completed_today_count(db: sqlite3.Connection, user: TelegramUser) -> int:
    ensure_user(db, user)
    prefix = today_key()
    row = db.execute(
        """
        SELECT count(*) AS total
        FROM tasks
        WHERE chat_id = ? AND user_id = ? AND status = 'done'
          AND completed_at LIKE ?
        """,
        (user.chat_id, user.user_id, f"{prefix}%"),
    ).fetchone()
    return int(row["total"] if row else 0)


def format_task(task: Task) -> str:
    return f"{task.id}. {task.text} [{task.xp} XP, {task.coins} coins]"


def help_text() -> str:
    return (
        "QuestBuddy commands:\n"
        "/add <task> - add a new quest\n"
        "/today - show open quests\n"
        "/done <id or text> - complete a quest\n"
        "/delete <id> - remove an open quest\n"
        "/stats - show XP, coins, streak, and level\n"
        "/boss - face your current procrastination boss\n"
        "/shop - see rewards you can buy with coins\n"
        "/buy <item> - buy a reward from the shop\n"
        "/nudge - get a small push\n"
        "/help - show this menu"
    )


def start_text(first_name: str) -> str:
    name = first_name or "adventurer"
    return (
        f"Welcome, {name}. I am QuestBuddy.\n\n"
        "Send me real-life tasks and I will turn them into quests with XP, coins, "
        "levels, streaks, and tiny dramatic pressure.\n\n"
        "Try:\n"
        "/add Study DSA for 30 minutes\n"
        "/add Drink water\n"
        "/today\n\n"
        + help_text()
    )


def stats_text(db: sqlite3.Connection, user: TelegramUser) -> str:
    row = get_user_row(db, user)
    xp = int(row["xp"])
    level = level_for_xp(xp)
    next_level_xp = level * 100
    progress = next_level_xp - xp
    return (
        "Your quest stats:\n"
        f"Level: {level} - {level_title(level)}\n"
        f"XP: {xp} ({progress} XP to next level)\n"
        f"Coins: {int(row['coins'])}\n"
        f"Completed quests: {int(row['completed_count'])}\n"
        f"Current streak: {int(row['streak'])} day(s)"
    )


def today_text(db: sqlite3.Connection, user: TelegramUser) -> str:
    tasks = list_open_tasks(db, user)
    if not tasks:
        return "No open quests. Suspiciously peaceful. Add one with /add <task>."

    lines = ["Today's open quests:"]
    lines.extend(format_task(task) for task in tasks)
    lines.append("\nComplete one with /done <id>.")
    return "\n".join(lines)


def boss_text(db: sqlite3.Connection, user: TelegramUser) -> str:
    open_tasks = list_open_tasks(db, user)
    done_today = completed_today_count(db, user)
    if not open_tasks:
        return "No boss today. The realm is quiet. This is either victory or denial."

    hp = len(open_tasks) * 30
    damage = min(hp, done_today * 30)
    remaining = hp - damage
    boss_names = [
        "The Procrastination Hydra",
        "The Deadline Ogre",
        "The Scroll Hole Serpent",
        "The Maybe-Later Titan",
    ]
    return (
        f"Boss encounter: {random.choice(boss_names)}\n"
        f"Open quest heads: {len(open_tasks)}\n"
        f"HP: {remaining}/{hp}\n"
        f"Damage dealt today: {damage}\n\n"
        "Finish quests with /done <id> to keep attacking."
    )


def shop_text(db: sqlite3.Connection, user: TelegramUser) -> str:
    row = get_user_row(db, user)
    lines = [f"Reward shop. Your coins: {int(row['coins'])}"]
    for key, item in SHOP_ITEMS.items():
        lines.append(f"{key} - {item['name']} ({item['cost']} coins)")
    lines.append("\nBuy with /buy <item>, for example /buy break.")
    return "\n".join(lines)


def buy_item(db: sqlite3.Connection, user: TelegramUser, item_key: str) -> str:
    ensure_user(db, user)
    item_key = item_key.strip().lower()
    item = SHOP_ITEMS.get(item_key)
    if not item:
        return "Unknown shop item. Use /shop to see what exists in this tiny economy."

    row = get_user_row(db, user)
    coins = int(row["coins"])
    cost = int(item["cost"])
    if coins < cost:
        return f"Not enough coins. You need {cost}, but you have {coins}."

    with db:
        db.execute(
            """
            UPDATE users
            SET coins = coins - ?, updated_at = ?
            WHERE chat_id = ? AND user_id = ?
            """,
            (cost, timestamp(), user.chat_id, user.user_id),
        )
    return f"Purchased: {item['name']}.\n{item['message']}"


def ambiguous_task_text(matches: list[Task]) -> str:
    if not matches:
        return "I could not find that open quest. Use /today to see the quest IDs."

    lines = ["I found multiple matching quests. Use the ID:"]
    lines.extend(format_task(task) for task in matches)
    return "\n".join(lines)


def handle_command(db: sqlite3.Connection, user: TelegramUser, text: str) -> str:
    command, _, arg = text.partition(" ")
    command = command.split("@", 1)[0].lower()
    arg = arg.strip()

    if command in {"/start", "/help"}:
        return start_text(user.first_name) if command == "/start" else help_text()

    if command == "/add":
        if not arg:
            return "Usage: /add <task>\nExample: /add Study DSA for 30 minutes"
        task = add_task(db, user, arg)
        return (
            "New quest added:\n"
            f"{task.id}. {quest_name(task.text)}\n"
            f"Reward: {task.xp} XP + {task.coins} coins"
        )

    if command == "/today":
        return today_text(db, user)

    if command == "/done":
        if not arg:
            return "Usage: /done <id or text>\nTip: /today shows quest IDs."
        task, matches = find_open_task(db, user, arg)
        if task is None:
            return ambiguous_task_text(matches)
        before_level = level_for_xp(int(get_user_row(db, user)["xp"]))
        updated = complete_task(db, user, task)
        after_level = level_for_xp(int(updated["xp"]))
        lines = [
            "Quest complete.",
            f"{task.text}",
            f"You gained {task.xp} XP and {task.coins} coins.",
            f"Streak: {int(updated['streak'])} day(s).",
        ]
        if after_level > before_level:
            lines.append(f"Level up: Level {after_level} - {level_title(after_level)}")
        return "\n".join(lines)

    if command == "/delete":
        if not arg or not arg.isdigit():
            return "Usage: /delete <id>"
        if delete_task(db, user, int(arg)):
            return f"Quest {arg} deleted. We shall pretend it never happened."
        return "I could not delete that quest. Use /today to check the ID."

    if command == "/stats":
        return stats_text(db, user)

    if command == "/boss":
        return boss_text(db, user)

    if command == "/shop":
        return shop_text(db, user)

    if command == "/buy":
        if not arg:
            return "Usage: /buy <item>\nTry /shop first."
        return buy_item(db, user, arg)

    if command == "/nudge":
        return random.choice(NUDGES)

    return "Unknown command. Use /help to see what I understand."


def api_call(token: str, method: str, params: Optional[dict[str, Any]] = None) -> Any:
    url = API_URL.format(token=token, method=method)
    data = urllib.parse.urlencode(params or {}).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(request, timeout=DEFAULT_POLL_TIMEOUT + 10) as response:
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
    return list(api_call(token, "getUpdates", params) or [])


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
    print("QuestBuddy is running. Open your bot in Telegram and send /start.")
    print(f"Database: {db_path}")
    print("Press Ctrl+C to stop.")

    while True:
        try:
            updates = get_updates(token, offset, poll_timeout)
            for update in updates:
                offset = int(update["update_id"]) + 1
                process_update(db, token, update)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            print(f"[telegram http error] {exc.code}: {body}", file=sys.stderr)
            time.sleep(5)
        except urllib.error.URLError as exc:
            print(f"[network error] {exc.reason}", file=sys.stderr)
            time.sleep(5)
        except KeyboardInterrupt:
            print("\nQuestBuddy stopped.")
            break
        except Exception as exc:  # noqa: BLE001 - long-running bot should recover
            print(f"[error] {exc}", file=sys.stderr)
            time.sleep(5)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the QuestBuddy Telegram bot.")
    parser.add_argument("--db", default=os.getenv("QUESTBOT_DB", DEFAULT_DB_PATH))
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

    if not token:
        print(
            "Missing TELEGRAM_BOT_TOKEN.\n"
            "Create a bot with @BotFather, then put this in .env:\n"
            "TELEGRAM_BOT_TOKEN=123456:ABC-your-token",
            file=sys.stderr,
        )
        return 2

    run_bot(token, args.db, args.poll_timeout, args.process_old)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
