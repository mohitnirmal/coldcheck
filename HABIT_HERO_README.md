# HabitHero Telegram Bot

HabitHero is a small Telegram habit tracker. You add recurring habits, check
them in once per day, build streaks, earn XP, level up, and unlock tiny badges.

It is intentionally easy to deploy:

- No third-party Python packages.
- SQLite database in one local file.
- Telegram long polling, so no public webhook URL is required.

## Commands

```text
/add <habit>              Add a recurring habit
/habits                   List habits and today's progress
/today                    Same as /habits
/done <id or name>        Check in a habit for today
/delete <id>              Remove a habit from your active list
/rename <id> <name>       Rename a habit
/remind <id> <HH:MM>      Set a daily reminder for a habit
/remind <id> off          Turn off a habit reminder
/reminders                List reminder times
/stats                    Show XP, level, streaks, and badges
/week                     Show the last 7 days of check-ins
/nudge                    Get a tiny motivational push
/help                     Show the command menu
```

## Game Rules

Each habit can be checked in once per day.

- Every check-in gives 10 base XP.
- Your current streak adds a tiny bonus: up to +10 XP.
- Streak milestones add extra XP on days 3, 7, 14, 30, 60, and 100.
- Every 100 XP gives you another level.
- Badges unlock from total check-ins, best streak, and level progress.

## Reminders

Set a reminder for each habit using 24-hour local time:

```text
/remind 1 07:30
/remind 2 21:00
```

The bot checks reminders while it is running. If a habit is already checked in
for the day, no reminder is sent. If you set a reminder time that has already
passed today, the first reminder starts tomorrow.

Turn off a reminder:

```text
/remind 1 off
```

## 1. Create Your Telegram Bot

1. Open Telegram.
2. Search for `@BotFather`.
3. Send `/newbot`.
4. Choose a display name, for example `HabitHero`.
5. Choose a username ending in `bot`, for example `my_habithero_bot`.
6. Copy the token BotFather gives you.

Keep the token private.

## 2. Add Your Token

Copy `.env.example` to `.env`, then edit `.env`:

```powershell
Copy-Item .env.example .env
notepad .env
```

Use values like this:

```text
TELEGRAM_BOT_TOKEN=your-real-token-from-botfather
HABITBOT_UTC_OFFSET_MINUTES=330
HABITBOT_DB=habithero.db
```

India time is UTC+5:30, so `330` is already the right offset for you.

## 3. Run Locally

From this folder:

```powershell
python .\habit_hero_bot.py
```

Then open your bot in Telegram and send:

```text
/start
```

Try:

```text
/add Drink water
/add Walk for 10 minutes
/add Read 10 pages
/remind 1 21:00
/habits
/done 1
/stats
```

## 4. Deploy With Docker

Build the image:

```powershell
docker build -f Dockerfile.habithero -t habithero .
```

Run it:

```powershell
docker run -d --name habithero `
  -e TELEGRAM_BOT_TOKEN=your-real-token-from-botfather `
  -e HABITBOT_UTC_OFFSET_MINUTES=330 `
  -v habithero-data:/data `
  habithero
```

The Docker image stores the SQLite database at `/data/habithero.db`, backed by
the `habithero-data` volume.

## Notes

- Keep only one copy of the bot running for the same Telegram token.
- The bot uses long polling, so it works fine on a small VPS, Railway worker,
  Render background worker, or any Docker host.
- Your habit data lives in SQLite. Back up `habithero.db` if you care about the
  history.
