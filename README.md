# RuTracker → Telegram tracker bot

Tracks RuTracker topics and sends you a Telegram message when the release is
updated — i.e. new episodes were added.

## How it works

The bot uses RuTracker's official **read-only JSON API** at
`https://api.rutracker.org/v1` — no account, no cookies, and no Cloudflare
challenge. (Scraping `viewtopic.php` no longer works: the website returns the
"Just a moment..." interstitial to non-browser clients.)

Every `CHECK_INTERVAL_MINUTES` it calls `get_tor_topic_data` **once** with every
tracked topic id (up to 100 per request) and compares:

- **`reg_time`** — when the current `.torrent` file was registered. Uploaders
  re-upload the torrent whenever they add episodes, so this bumps. This is a
  more reliable signal than the old post-edit-date heuristic.
- the **episode range** parsed out of the title, e.g. `Серии: 1-7 из 10`
- the **size** in bytes, which catches silent re-packs

If any of those changed you get a notification with the old → new registration
date, the current episode range, size and seeder count.

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
   RUTRACKER_API_BASE=https://api.rutracker.org/v1
   RUTRACKER_BASE=https://rutracker.org
   CHECK_INTERVAL_MINUTES=60
   ```

4. Sanity-check that the API is reachable from your machine:

   ```
   python rutracker.py 6866086
   ```

   You should see the topic title, episode range and `reg_time`. If it prints a
   "Just a moment" complaint, `RUTRACKER_API_BASE` is pointing at the website
   instead of the API. If the request is blocked by your ISP, switch to
   `https://api.t-ru.org/v1`.

5. Run it:

   ```
   python bot.py
   ```

6. Message your bot `/start`. It replies with your Telegram user id. Put that id
   into `ALLOWED_USER_IDS` in `.env` and restart, so only you can control it.

## Commands

| Command | What it does |
|---|---|
| `/add <url or id>` | Start tracking a topic (paste the `viewtopic.php?t=...` link) |
| `/remove <url or id>` | Stop tracking it |
| `/list` | Show topics you track and their last update date |
| `/check` | Check all your topics right now |

## Notes & caveats

- **No RuTracker account is needed.** `RUTRACKER_USERNAME` / `RUTRACKER_PASSWORD`
  are no longer read; `rutracker_cookies.pkl` is unused and can be deleted.
- The API only knows topics that have a torrent attached. A topic id that
  returns `null` is either wrong or torrent-less.
- `RUTRACKER_BASE` is now only used to build the clickable links in Telegram
  messages — pick whichever mirror opens for you.
- The first check after upgrading will fire notifications for every tracked
  topic, because the stored signature format changed. That's expected, once.
- State lives in `data.json` (gitignored).
- Keep the process running (it polls continuously). To run it 24/7, wrap it in a
  `systemd` service, a `screen`/`tmux` session, Windows Task Scheduler, or
  Docker.

## Files

- `bot.py` – Telegram commands + polling loop
- `rutracker.py` – JSON API client (run it directly to self-test)
- `storage.py` – JSON store of subscriptions and last-seen state
