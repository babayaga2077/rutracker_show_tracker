# RuTracker → Telegram tracker bot

Tracks RuTracker topics and sends you a Telegram message when the release is
updated — i.e. new episodes were added.

## How it works

Every `CHECK_INTERVAL_MINUTES` the bot opens each tracked topic page and reads
signals that are **visible without logging in**:

- the first post's **edit timestamp** — `... (ред. 06-Июл-26 19:09)` — which the
  uploader bumps whenever they add episodes, and
- the **episode range** in the title, e.g. `Серии: 1-7 из 10`.

It compares these to what it saw last time; if they changed you get a
notification with the old → new edit date and the current episode count. No
RuTracker account is needed. If you *do* supply credentials, the torrent's
`Зарегистрирован` (registered) date is folded in as an extra signal.

## Setup

1. Install Python 3.10+ and the dependencies:

   ```
   pip install -r requirements.txt
   ```

2. Create a bot with [@BotFather](https://t.me/BotFather) and copy the token.

3. Copy `.env.example` to `.env` and fill it in:

   ```
   TELEGRAM_BOT_TOKEN=...          # from BotFather
   ALLOWED_USER_IDS=               # your Telegram id (leave empty first run)
   RUTRACKER_USERNAME=            # optional
   RUTRACKER_PASSWORD=            # optional
   RUTRACKER_BASE=https://rutracker.org
   CHECK_INTERVAL_MINUTES=120
   ```

4. Run it:

   ```
   python bot.py
   ```

5. Message your bot `/start`. It replies with your Telegram user id. Put that id
   into `ALLOWED_USER_IDS` in `.env` and restart, so only you can control it.

## Commands

| Command | What it does |
|---|---|
| `/add <url or id>` | Start tracking a topic (paste the `viewtopic.php?t=...` link) |
| `/remove <url or id>` | Stop tracking it |
| `/list` | Show topics you track and their last update date |
| `/check` | Check all your topics right now |

## Notes & caveats

- **Login is required** — RuTracker hides topic details from guests. If login
  fails it's usually a wrong password, a captcha, or a blocked domain. Try a
  mirror: set `RUTRACKER_BASE` to `https://rutracker.net` or `https://rutracker.nl`.
- The bot detects *re-uploads* (the torrent file being replaced). Most series
  distributions on RuTracker are updated in place this way, so a changed
  registration date is a reliable "new episodes" signal.
- State lives in `data.json`; login cookies in `rutracker_cookies.pkl`. Both are
  gitignored. Delete `rutracker_cookies.pkl` to force a fresh login.
- Keep the process running (it polls continuously). To run it 24/7, wrap it in a
  `systemd` service, a `screen`/`tmux` session, Windows Task Scheduler, or
  Docker.

## Files

- `bot.py` – Telegram commands + polling loop
- `rutracker.py` – login, fetching, and update-date parsing
- `storage.py` – JSON store of subscriptions and last-seen state
