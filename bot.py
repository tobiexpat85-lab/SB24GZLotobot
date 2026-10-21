"""Anonymous Stranger Chat bot for Telegram.

Needs only a Telegram bot token (BOT_TOKEN). Uses long polling, so there is
no webhook or public URL to set up. Pairing state lives in memory; reports
and bans are stored in a small SQLite file.
"""

import asyncio
import logging
import os
import sqlite3
import time
from contextlib import closing

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.error import Forbidden, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
DB_PATH = os.environ.get("DB_PATH", "bot.db")

SEARCH_TIMEOUT = 60        # seconds to wait for a match
INACTIVITY_TIMEOUT = 600   # seconds of silence before a chat is closed
REPORTS_TO_BAN = 3         # distinct reporters needed for an automatic ban
REMATCH_BLOCK = 120        # seconds before two people can be paired again

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("stranger-chat")

# ---------------------------------------------------------------- state ----
waiting: dict[int, float] = {}            # user_id -> time they started searching
partners: dict[int, int] = {}             # user_id -> partner user_id (both directions)
activity: dict[int, float] = {}           # user_id -> last message time in a chat
last_partner: dict[int, tuple[int, float]] = {}  # user_id -> (partner, ended_at)
lock = asyncio.Lock()


# ------------------------------------------------------------- database ----
def init_db() -> None:
    with closing(sqlite3.connect(DB_PATH)) as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS reports ("
            "reporter INTEGER, reported INTEGER, ts REAL, "
            "UNIQUE(reporter, reported))"
        )
        c.execute("CREATE TABLE IF NOT EXISTS banned (user_id INTEGER PRIMARY KEY)")
        c.commit()


def is_banned(user_id: int) -> bool:
    with closing(sqlite3.connect(DB_PATH)) as c:
        row = c.execute(
            "SELECT 1 FROM banned WHERE user_id = ?", (user_id,)
        ).fetchone()
    return row is not None


def add_report(reporter: int, reported: int) -> bool:
    """Save a report. Returns True if the reported user is now banned."""
    with closing(sqlite3.connect(DB_PATH)) as c:
        c.execute(
            "INSERT OR IGNORE INTO reports VALUES (?, ?, ?)",
            (reporter, reported, time.time()),
        )
        count = c.execute(
            "SELECT COUNT(*) FROM reports WHERE reported = ?", (reported,)
        ).fetchone()[0]
        banned = count >= REPORTS_TO_BAN
        if banned:
            c.execute("INSERT OR IGNORE INTO banned VALUES (?)", (reported,))
        c.commit()
    return banned


# ------------------------------------------------------------ keyboards ----
def kb_find() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔍 Find a stranger", callback_data="find")]]
    )


def kb_searching() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ Cancel search", callback_data="stop")]]
    )


def kb_chat() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("⏭ Next", callback_data="next"),
                InlineKeyboardButton("🛑 Stop", callback_data="stop"),
            ],
            [InlineKeyboardButton("🚩 Report", callback_data="report")],
        ]
    )


# -------------------------------------------------------------- helpers ----
async def safe_send(bot, chat_id: int, text: str, markup=None) -> bool:
    try:
        await bot.send_message(chat_id, text, reply_markup=markup)
        return True
    except Forbidden:
        return False  # user blocked the bot
    except TelegramError as exc:
        log.warning("send_message to %s failed: %s", chat_id, exc)
        return False


def recently_paired(a: int, b: int) -> bool:
    now = time.time()
    for x, y in ((a, b), (b, a)):
        info = last_partner.get(x)
        if info and info[0] == y and now - info[1] < REMATCH_BLOCK:
            return True
    return False


async def end_chat(user_id: int) -> int | None:
    """Remove the user from any chat or queue. Returns the ex-partner id."""
    async with lock:
        waiting.pop(user_id, None)
        partner = partners.pop(user_id, None)
        activity.pop(user_id, None)
        if partner is not None:
            partners.pop(partner, None)
            activity.pop(partner, None)
            now = time.time()
            last_partner[user_id] = (partner, now)
            last_partner[partner] = (user_id, now)
    return partner


async def notify_left(bot, partner_id: int | None) -> None:
    if partner_id is not None:
        await safe_send(
            bot,
            partner_id,
            "👋 The stranger has left the chat.",
            kb_find(),
        )


# ---------------------------------------------------------------- logic ----
async def find(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    bot = context.bot
    if is_banned(user_id):
        await safe_send(bot, user_id, "🚫 You have been banned for repeated reports.")
        return

    match = None
    async with lock:
        if user_id in partners:
            status = "in_chat"
        elif user_id in waiting:
            status = "already_waiting"
        else:
            for cand in list(waiting):
                if cand != user_id and not recently_paired(user_id, cand):
                    match = cand
                    break
            if match is None:
                waiting[user_id] = time.time()
                status = "waiting"
            else:
                del waiting[match]
                partners[user_id] = match
                partners[match] = user_id
                activity[user_id] = activity[match] = time.time()
                status = "matched"

    if status == "in_chat":
        await safe_send(bot, user_id, "You're already in a chat. Use Next or Stop.", kb_chat())
    elif status == "already_waiting":
        await safe_send(bot, user_id, "⏳ Still searching...", kb_searching())
    elif status == "waiting":
        await safe_send(bot, user_id, "🔍 Searching for a stranger...", kb_searching())
    else:
        text = "✅ Connected! Say hi to the stranger. Your identity stays hidden."
        ok_me = await safe_send(bot, user_id, text, kb_chat())
        ok_them = await safe_send(bot, match, text, kb_chat())
        # If one side blocked the bot, close the chat and tell the other.
        if not ok_them:
            await end_chat(user_id)
            await safe_send(bot, user_id, "That stranger is unavailable. Try again.", kb_find())
        elif not ok_me:
            await end_chat(match)
            await safe_send(bot, match, "That stranger is unavailable. Try again.", kb_find())


async def do_stop(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    was_waiting = user_id in waiting
    partner = await end_chat(user_id)
    if partner is None and not was_waiting:
        await safe_send(context.bot, user_id, "You're not in a chat.", kb_find())
        return
    await notify_left(context.bot, partner)
    await safe_send(context.bot, user_id, "🛑 Chat ended. Tap below when you want another.", kb_find())


async def do_next(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    partner = await end_chat(user_id)
    await notify_left(context.bot, partner)
    await find(user_id, context)


async def do_report(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    partner = partners.get(user_id)
    if partner is None:
        await safe_send(context.bot, user_id, "There's nobody to report right now.", kb_find())
        return
    add_report(user_id, partner)
    await end_chat(user_id)
    await notify_left(context.bot, partner)
    await safe_send(
        context.bot,
        user_id,
        "🚩 Thanks, the stranger was reported and the chat has ended.",
        kb_find(),
    )


# ------------------------------------------------------------- handlers ----
WELCOME = (
    "👋 Welcome to *Stranger Chat*!\n\n"
    "I connect you with a random stranger. Neither of you sees names, "
    "usernames or profiles. I just pass messages between you.\n\n"
    "*Rules*\n"
    "• Be respectful. Harassment gets you banned.\n"
    "• Never share personal details (phone, address, socials).\n"
    "• Use /next to skip, /stop to leave, /report if someone misbehaves.\n\n"
    "By continuing you confirm you are 18 or older."
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        WELCOME, parse_mode="Markdown", reply_markup=kb_find()
    )


async def cmd_find(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await find(update.effective_user.id, context)


async def cmd_next(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await do_next(update.effective_user.id, context)


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await do_stop(update.effective_user.id, context)


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await do_report(update.effective_user.id, context)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    action = query.data
    if action == "find":
        await find(uid, context)
    elif action == "next":
        await do_next(uid, context)
    elif action == "stop":
        await do_stop(uid, context)
    elif action == "report":
        await do_report(uid, context)


async def relay(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    uid = update.effective_user.id

    if is_banned(uid):
        return

    partner = partners.get(uid)
    if partner is None:
        if uid in waiting:
            await msg.reply_text("⏳ Still searching...", reply_markup=kb_searching())
        else:
            await msg.reply_text(
                "You're not in a chat. Tap below to find a stranger.",
                reply_markup=kb_find(),
            )
        return

    # Block messages that would reveal personal details.
    if msg.contact or msg.location or msg.venue:
        await msg.reply_text("🔒 Contacts and locations can't be sent, for your safety.")
        return

    try:
        await context.bot.copy_message(
            chat_id=partner, from_chat_id=uid, message_id=msg.message_id
        )
        activity[uid] = activity[partner] = time.time()
    except Forbidden:
        await end_chat(uid)
        await msg.reply_text(
            "The stranger is no longer reachable. Chat ended.", reply_markup=kb_find()
        )
    except TelegramError as exc:
        log.warning("copy_message failed: %s", exc)
        await msg.reply_text("⚠️ That type of message couldn't be delivered.")


async def janitor(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs every few seconds: expires searches and idle chats."""
    now = time.time()
    timed_out: list[int] = []
    closed: list[tuple[int, int]] = []

    async with lock:
        for uid, started in list(waiting.items()):
            if now - started > SEARCH_TIMEOUT:
                del waiting[uid]
                timed_out.append(uid)

        for uid, ts in list(activity.items()):
            if now - ts <= INACTIVITY_TIMEOUT:
                continue
            partner = partners.pop(uid, None)
            activity.pop(uid, None)
            if partner is None:
                continue
            partners.pop(partner, None)
            activity.pop(partner, None)
            last_partner[uid] = (partner, now)
            last_partner[partner] = (uid, now)
            closed.append((uid, partner))

        for uid, (_, ended) in list(last_partner.items()):
            if now - ended > 3600:
                del last_partner[uid]

    for uid in timed_out:
        await safe_send(
            context.bot,
            uid,
            "😕 No one is available right now. Try again in a moment.",
            kb_find(),
        )
    for a, b in closed:
        text = "⌛ The chat was closed because of inactivity."
        await safe_send(context.bot, a, text, kb_find())
        await safe_send(context.bot, b, text, kb_find())


async def post_init(app: Application) -> None:
    await app.bot.set_my_commands(
        [
            BotCommand("start", "Show the welcome message"),
            BotCommand("find", "Find a stranger to chat with"),
            BotCommand("next", "Skip to a new stranger"),
            BotCommand("stop", "End the chat"),
            BotCommand("report", "Report the current stranger"),
        ]
    )


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is not set. Add it as an environment variable.")

    init_db()
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    private = filters.ChatType.PRIVATE
    app.add_handler(CommandHandler("start", cmd_start, filters=private))
    app.add_handler(CommandHandler("find", cmd_find, filters=private))
    app.add_handler(CommandHandler("next", cmd_next, filters=private))
    app.add_handler(CommandHandler("stop", cmd_stop, filters=private))
    app.add_handler(CommandHandler("report", cmd_report, filters=private))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(
        MessageHandler(
            private & ~filters.COMMAND & filters.UpdateType.MESSAGE, relay
        )
    )

    app.job_queue.run_repeating(janitor, interval=15, first=15)

    log.info("Stranger Chat bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
