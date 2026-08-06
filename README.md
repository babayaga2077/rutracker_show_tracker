# RuTracker → Telegram tracker bot

Tracks RuTracker topics and sends you a Telegram message when the release is
updated — i.e. new episodes were added.

## How it works

RuTracker sits behind a Cloudflare JS challenge ("Just a moment..."), so getting
a topic page takes two pieces:

| Piece | Job |
|---|---|
| `curl_cffi` | Impersonates a real Chrome TLS fingerprint. Without it every request is challenged no matter what cookies it carries. |
| **FlareSolverr** | A container running headless Chrome that solves the challenge and returns a `cf_clearance` cookie. |

FlareSolverr is slow (~10-30s per solve), so it isn't in the hot path. The bot
solves once, caches the cookie plus the matching User-Agent in
`cf_clearance.json`, and makes fast normal requests until the cookie expires —
then re-solves automatically.

Every `CHECK_INTERVAL_MINUTES` it fetches each tracked topic and compares:

- the first post's **edit timestamp** — `... (ред. 06-Июл-26 19:09)` — which the
  uploader bumps when adding episodes
- the **episode range** in the title, e.g. `Серии: 1-7 из 10`
- the **size**, which catches silent re-packs

> **Known false alarms.** The edit date bumps for *any* edit, so an uploader
> fixing a typo will trigger a notification. The accurate signal is the
> torrent's `Зарегистрирован` date, but that's only visible when logged in and
> login isn't currently wired up. See "History" below for why.

## Setup

1. Install dependencies:

   ```
   pip install -r requirements.txt
   ```

2. Start FlareSolverr:

   ```
   docker compose up -d
   curl -s http://localhost:8191/health
   ```

3. Create a bot with [@BotFather](https://t.me/BotFather) and copy the token.

4. Copy `.env.example` to `.env` and fill it in:

   ```
   TELEGRAM_BOT_TOKEN=...          # from BotFather
   ALLOWED_USER_IDS=               # your Telegram id (leave empty first run)
   RUTRACKER_BASE=https://rutracker.org
   FLARESOLVERR_URL=http://localhost:8191/v1
   CHECK_INTERVAL_MINUTES=120
   FETCH_DELAY_SECONDS=5
   ```

5. Confirm the bypass works before starting the bot — the first run is slow
   because it triggers a solve:

   ```
   python rutracker.py 6866086
   ```

   You want a Cyrillic title and an `edited` date. If you get a challenge
   error, check `docker compose logs flaresolverr`.

6. Run it:

   ```
   python bot.py
   ```

7. Message your bot `/start`, put the id it reports into `ALLOWED_USER_IDS`,
   restart.

## Commands

| Command | What it does |
|---|---|
| `/add <url or id>` | Start tracking a topic (paste the `viewtopic.php?t=...` link) |
| `/remove <url or id>` | Stop tracking it |
| `/list` | Show topics you track and their last update date |
| `/check` | Check all your topics right now |
| `/status` | Report bypass health and probe the forum — use this first when things look wrong |

## Notes & caveats

- **FlareSolverr and the bot must share a public IP.** `cf_clearance` is bound
  to the address that solved the challenge. Running both on the same box is
  fine; putting FlareSolverr behind a different VPN exit is not.
- FlareSolverr is **unauthenticated** and will fetch any URL it's given. The
  compose file binds it to `127.0.0.1` — keep it that way.
- Headless Chrome leaks memory over long uptimes; `mem_limit: 1g` and
  `restart: unless-stopped` cover it.
- Don't lower `FETCH_DELAY_SECONDS` much. Each check is a real page load, and
  hammering the forum invites a harder challenge.
- If **all** checks start failing, the bot logs `ALL n topic checks failed` —
  that's the signal Cloudflare changed something, not that nothing is new.
- State lives in `data.json`, the cached cookie in `cf_clearance.json`. Both are
  gitignored. Delete `cf_clearance.json` to force a fresh solve.

## History

Worth recording, because the failure modes repeat:

1. **Plain `requests` scraping** — died when Cloudflare went up over the forum.
   Symptom: every tracked show renamed itself to "Just a moment...".
2. **The official JSON API** (`api.rutracker.org/v1`) — clean, no challenge,
   returned `reg_time` which was a false-alarm-free update signal. The host has
   since been retired; it no longer resolves.
3. **Scraping again, through the bypass** — where we are now.

If the bypass breaks, check in this order: is FlareSolverr running, does
`rutracker.org` still resolve, has a `RUTRACKER_BASE` mirror gone down.

## Files

- `bot.py` – Telegram commands + polling loop
- `rutracker.py` – topic fetching and parsing (run it directly to self-test)
- `cloudflare.py` – the challenge bypass: curl_cffi + FlareSolverr
- `storage.py` – JSON store of subscriptions and last-seen state
- `docker-compose.yml` – FlareSolverr only; the bot itself runs under systemd
