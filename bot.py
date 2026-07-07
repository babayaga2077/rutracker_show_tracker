"""RuTracker -> Telegram tracker bot.

Commands:
  /start            - show your Telegram user id and help
  /add <url|id>     - start tracking a topic
  /remove <url|id>  - stop tracking a topic
  /list             - list topics you track
  /check            - check all your topics right now

Every CHECK_INTERVAL_MINUTES the bot polls each tracked topic and messages you
when the release changes (post edit date and/or episode range) = new episodes.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

import storage
from rutracker import (
    RutrackerClient,
    RutrackerError,
    build_client_from_env,
    extract_topic_id,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("bot")

load_dotenv()

ALLOWED = {
    int(x) for x in os.getenv("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x
}
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL_MINUTES", "120"))

client: RutrackerClient  # set in main()


def _authorized(update: Update) -> bool:
    uid = update.effective_user.id if update.effective_user else None
    return not ALLOWED or uid in ALLOWED


def _topic_url(topic_id: str) -> str:
    return f"{client.base}/forum/viewtopic.php?t={topic_id}"


def _fmt_topic(title: str, topic_id: str) -> str:
    return f'<a href="{_topic_url(topic_id)}">{html.escape(title or topic_id)}</a>'


# --------------------------------------------------------------------- commands
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if not _authorized(update):
        await update.message.reply_text(
            f"Not authorized. Your Telegram user id is {uid}.\n"
            "Add it to ALLOWED_USER_IDS in .env and restart the bot."
        )
        return
    await update.message.reply_text(
        "RuTracker tracker bot.\n\n"
        f"Your user id: {uid}\n\n"
        "Commands:\n"
        "/add <url|id> - track a topic\n"
        "/remove <url|id> - untrack\n"
        "/list - your tracked topics\n"
        "/check - check now"
    )


async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /add <topic url or id>")
        return
    topic_id = extract_topic_id(ctx.args[0])
    if not topic_id:
        await update.message.reply_text("Couldn't find a topic id in that. "
                                        "Send the viewtopic.php?t=... URL.")
        return
    await update.message.reply_text("Fetching topic...")
    try:
        info = await asyncio.to_thread(client.fetch_topic, topic_id)
    except RutrackerError as e:
        await update.message.reply_text(f"Error: {e}")
        return

    stamp = info.edited or info.created or "n/a"
    newly = storage.add_subscription(
        topic_id, update.effective_chat.id, info.title,
        info.signature(), stamp,
    )
    status = "Now tracking" if newly else "Already tracking"
    ep = f"\nEpisodes: {info.episodes}" if info.episodes else ""
    await update.message.reply_text(
        f"{status}: {_fmt_topic(info.title, topic_id)}\n"
        f"Last edited: {stamp}{ep}",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def cmd_remove(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /remove <topic url or id>")
        return
    topic_id = extract_topic_id(ctx.args[0])
    if not topic_id:
        await update.message.reply_text("Couldn't find a topic id in that.")
        return
    if storage.remove_subscription(topic_id, update.effective_chat.id):
        await update.message.reply_text("Stopped tracking that topic.")
    else:
        await update.message.reply_text("You weren't tracking that topic.")


async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    subs = storage.list_subscriptions(update.effective_chat.id)
    if not subs:
        await update.message.reply_text("You aren't tracking anything yet. "
                                        "Use /add <url>.")
        return
    lines = [
        f"- {_fmt_topic(s['title'], s['topic_id'])} - {s['last_updated'] or 'n/a'}"
        for s in subs
    ]
    await update.message.reply_text(
        "Tracked topics:\n" + "\n".join(lines),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def cmd_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    await update.message.reply_text("Checking your topics now...")
    changed = await _check_topics(ctx, only_chat=update.effective_chat.id)
    if not changed:
        await update.message.reply_text("No changes - nothing new uploaded.")


# ------------------------------------------------------------------ core check
async def _check_topics(ctx: ContextTypes.DEFAULT_TYPE, only_chat: int | None = None) -> int:
    """Poll tracked topics; notify chats on change. Returns #changed topics."""
    topics = storage.all_topics()
    changed = 0
    for topic_id, sub in topics.items():
        chats = sub["chats"]
        if only_chat is not None and only_chat not in chats:
            continue
        try:
            info = await asyncio.to_thread(client.fetch_topic, topic_id)
        except RutrackerError as e:
            log.warning("check %s failed: %s", topic_id, e)
            continue

        new_sig = info.signature()
        if new_sig != sub.get("last_signature"):
            changed += 1
            old = sub.get("last_updated", "n/a")
            new_stamp = info.edited or info.created or "n/a"
            storage.update_state(topic_id, new_sig, new_stamp, info.title)
            ep = f"\nEpisodes: <b>{info.episodes}</b>" if info.episodes else ""
            targets = [only_chat] if only_chat is not None else chats
            for chat_id in targets:
                try:
                    await ctx.bot.send_message(
                        chat_id=chat_id,
                        text=(
                            "\U0001F195 <b>Topic updated</b> - likely new episodes!\n"
                            f"{_fmt_topic(info.title, topic_id)}\n"
                            f"Edited: {old} -> <b>{new_stamp}</b>{ep}"
                        ),
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True,
                    )
                except Exception as e:  # noqa: BLE001
                    log.warning("notify %s failed: %s", chat_id, e)
    return changed


async def scheduled_check(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.info("Running scheduled check")
    n = await _check_topics(ctx)
    log.info("Scheduled check done, %d topic(s) changed", n)


# -------------------------------------------------------------------- bootstrap
def main() -> None:
    global client
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN not set (see .env.example)")
    client = build_client_from_env()

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("check", cmd_check))

    app.job_queue.run_repeating(
        scheduled_check,
        interval=CHECK_INTERVAL * 60,
        first=30,
    )

    log.info("Bot started. Checking every %d min. Allowed users: %s",
             CHECK_INTERVAL, ALLOWED or "ANYONE (set ALLOWED_USER_IDS!)")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
