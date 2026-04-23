# QuestBuddy Telegram Bot MVP

QuestBuddy is a tiny life-RPG Telegram bot. You add real tasks, complete them,
earn XP and coins, keep a streak, and buy small rewards.

## What It Can Do

- `/add <task>` creates a quest with XP and coin rewards.
- `/today` lists your open quests.
- `/done <id or text>` completes a quest and awards XP.
- `/stats` shows your level, XP, coins, streak, and completed count.
- `/boss` turns your unfinished tasks into a procrastination boss.
- `/shop` shows tiny self-rewards.
- `/buy <item>` spends coins on a reward.
- `/delete <id>` removes a mistaken open quest.
- `/nudge` gives a small motivational push.

## 1. Create Your Telegram Bot

1. Open Telegram.
2. Search for `@BotFather`.
3. Send `/newbot`.
4. Choose a display name, for example `QuestBuddy`.
5. Choose a username ending in `bot`, for example `my_questbuddy_bot`.
6. BotFather will give you a token that looks like:

```text
123456789:ABCdefYourLongSecretToken
```

Keep this token private.

## 2. Add Your Token

Copy `.env.example` to `.env`, then edit `.env`:

```powershell
Copy-Item .env.example .env
notepad .env
```

Replace the placeholder value:

```text
TELEGRAM_BOT_TOKEN=your-real-token-from-botfather
QUESTBOT_UTC_OFFSET_MINUTES=330
QUESTBOT_DB=questbuddy.db
```

## 3. Run The Bot

From this folder:

```powershell
python .\questbuddy_bot.py
```

Then open your bot in Telegram and send:

```text
/start
```

If Windows says Python is not found, install Python from `python.org` and make
sure you enable "Add python.exe to PATH" during installation.

## 4. Try These Commands

```text
/add Study DSA for 30 minutes
/add Drink water
/add Clean desk
/today
/done 1
/stats
/boss
/shop
/buy break
```

## Notes

- Keep the terminal running while you want the bot online.
- This MVP uses long polling, so you do not need a public server or webhook.
- Run only one copy of the bot at a time for the same Telegram token.
- Your data is stored locally in `questbuddy.db`.
- Your bot token lives in `.env`; do not share it or commit it.
