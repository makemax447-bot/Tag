# -*- coding: utf-8 -*-
"""
بوت إعادة نشر حسابات ببجي - Multi-tenant Forward Bot
------------------------------------------------------
الفكرة:
  - جروب رسمي واحد فيه منشورات (فيديو + وصف) لحسابات ببجي معروضة.
  - تجار كتير بيشتركوا (5$/شهر، دفع يدوي خارج البوت).
  - كل منشور جديد فيه فيديو ووصف في الجروب الرسمي، بيتحول أوتوماتيك
    لقنوات كل التجار الـ Active.
  - الأدمن (انت) عنده لوحة تحكم جوه البوت لتفعيل/إيقاف/تجديد التجار.

الإعداد قبل التشغيل (غيّر القيم دي في الأسفل أو حطها Environment Variables):
  BOT_TOKEN        - توكن البوت من BotFather
  ADMIN_ID         - رقم التليجرام آيدي بتاعك (مش اليوزر)
  SOURCE_GROUP_ID  - آيدي الجروب الرسمي (رقم سالب زي -1001234567890)
  ADMIN_USERNAME   - يوزرك للتواصل (بدون @) - بيظهر للتاجر عشان يدفع
"""

import os
import logging
import sqlite3
from datetime import datetime, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# ----------------------- الإعدادات -----------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8357386417:AAFWkKuCdQviXOXy4kViPah1pJZKgZLFNTE")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "429325696"))  # حط آيدي التليجرام بتاعك
SOURCE_GROUP_ID = int(os.environ.get("SOURCE_GROUP_ID", "-1005471960213"))  # آيدي الجروب الرسمي
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "Bobeka11")
SUBSCRIPTION_DAYS = 30
DB_PATH = os.environ.get("DB_PATH", "traders.db")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# محادثة التسجيل - حالات
ASK_USERNAME, ASK_CHANNEL, WAIT_CONFIRM = range(3)


# ----------------------- قاعدة البيانات -----------------------
def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db_connect()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS traders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER UNIQUE NOT NULL,
            trader_username TEXT,
            channel_id TEXT,
            channel_title TEXT,
            status TEXT DEFAULT 'pending',   -- pending / active / expired
            created_at TEXT,
            expires_at TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def upsert_trader(telegram_id, trader_username, channel_id, channel_title):
    conn = db_connect()
    conn.execute(
        """
        INSERT INTO traders (telegram_id, trader_username, channel_id, channel_title, status, created_at)
        VALUES (?, ?, ?, ?, 'pending', ?)
        ON CONFLICT(telegram_id) DO UPDATE SET
            trader_username=excluded.trader_username,
            channel_id=excluded.channel_id,
            channel_title=excluded.channel_title,
            status='pending'
        """,
        (telegram_id, trader_username, channel_id, channel_title, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def get_trader(trader_db_id):
    conn = db_connect()
    row = conn.execute("SELECT * FROM traders WHERE id=?", (trader_db_id,)).fetchone()
    conn.close()
    return row


def get_traders_by_status(status):
    conn = db_connect()
    rows = conn.execute("SELECT * FROM traders WHERE status=?", (status,)).fetchall()
    conn.close()
    return rows


def set_status(trader_db_id, status, extend=False):
    conn = db_connect()
    if extend:
        expires_at = (datetime.utcnow() + timedelta(days=SUBSCRIPTION_DAYS)).isoformat()
        conn.execute(
            "UPDATE traders SET status=?, expires_at=? WHERE id=?",
            (status, expires_at, trader_db_id),
        )
    else:
        conn.execute("UPDATE traders SET status=? WHERE id=?", (status, trader_db_id))
    conn.commit()
    conn.close()


# ----------------------- تسجيل التاجر (Conversation) -----------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "أهلاً بيك 👋\n"
        "البوت ده بينقل منشورات حسابات ببجي أوتوماتيك من الجروب الرسمي لقناتك.\n\n"
        "الاشتراك: 5$ شهريًا.\n\n"
        "عشان نبدأ، ابعتلي اليوزر بتاعك (اللي عايز يظهر مع منشوراتك):"
    )
    return ASK_USERNAME


async def ask_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["trader_username"] = update.message.text.strip()
    await update.message.reply_text(
        "تمام ✅\n"
        "دلوقتي ابعتلي رابط قناتك (مثال: https://t.me/BOBEKA12)\n"
        "أو يوزرها (@my_channel) أو اعمل Forward لأي رسالة منها."
    )
    return ASK_CHANNEL


def parse_channel_input(text: str) -> str:
    """يقبل رابط t.me أو يوزر بـ@ أو من غيره، ويرجع الصيغة الصح (@username)."""
    text = text.strip()
    if "t.me/" in text:
        text = text.split("t.me/")[-1]
    text = text.strip("/ ")
    if not text.startswith("@"):
        text = "@" + text
    return text


async def ask_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    channel_id = None
    channel_title = None

    if update.message.forward_from_chat:
        channel_id = str(update.message.forward_from_chat.id)
        channel_title = update.message.forward_from_chat.title
    elif update.message.text:
        channel_id = parse_channel_input(update.message.text)

    if not channel_id:
        await update.message.reply_text("محتاج رابط القناة أو يوزرها أو رسالة محولة منها. جرب تاني:")
        return ASK_CHANNEL

    context.user_data["channel_id"] = channel_id
    context.user_data["channel_title"] = channel_title or channel_id

    await update.message.reply_text(
        "خطوات التفعيل:\n"
        "1) روح لقناتك\n"
        "2) ضيفني Admin (نفس يوزر البوت ده)\n"
        "3) فعّل صلاحية 'نشر الرسائل' بس\n\n"
        "لما تخلص ابعت كلمة 'تم'"
    )
    return WAIT_CONFIRM


async def confirm_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text.strip() not in ("تم", "تمام", "done", "Done"):
        await update.message.reply_text("ابعت 'تم' لما تخلص إضافة البوت كـ Admin.")
        return WAIT_CONFIRM

    telegram_id = update.effective_user.id
    trader_username = context.user_data.get("trader_username")
    channel_id = context.user_data.get("channel_id")
    channel_title = context.user_data.get("channel_title")

    upsert_trader(telegram_id, trader_username, channel_id, channel_title)

    await update.message.reply_text(
        "تسجيلك خلص ✅\n\n"
        f"للاشتراك (5$/شهر) كلم الأدمن مباشرة: @{ADMIN_USERNAME}\n"
        "وبعد الدفع هيتم تفعيل حسابك ويبدأ النشر أوتوماتيك."
    )

    if ADMIN_ID:
        await context.bot.send_message(
            ADMIN_ID,
            f"🆕 تاجر جديد سجّل بياناته:\n"
            f"يوزر: {trader_username}\n"
            f"قناة: {channel_title} ({channel_id})\n"
            f"استخدم /admin لمراجعته وتفعيله بعد الدفع.",
        )

    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("تم الإلغاء. ابعت /start تاني لما تحب تسجل.")
    return ConversationHandler.END


# ----------------------- لوحة تحكم الأدمن -----------------------
def is_admin(update: Update) -> bool:
    return update.effective_user and update.effective_user.id == ADMIN_ID


def build_admin_keyboard(status_filter):
    rows = get_traders_by_status(status_filter)
    buttons = []
    for r in rows:
        label = f"{r['trader_username']} | {r['channel_title']}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"view_{r['id']}")])
    return buttons, rows


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    keyboard = [
        [InlineKeyboardButton("🆕 طلبات جديدة (Pending)", callback_data="list_pending")],
        [InlineKeyboardButton("✅ مشتركين نشطين (Active)", callback_data="list_active")],
        [InlineKeyboardButton("⛔ منتهية صلاحيتهم (Expired)", callback_data="list_expired")],
    ]
    await update.message.reply_text(
        "📋 لوحة تحكم التجار", reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if update.effective_user.id != ADMIN_ID:
        await query.answer("مش مسموح لك.", show_alert=True)
        return
    await query.answer()

    data = query.data

    if data.startswith("list_"):
        status = data.split("_", 1)[1]
        buttons, rows = build_admin_keyboard(status)
        if not rows:
            await query.edit_message_text(f"مفيش تجار بحالة '{status}' دلوقتي.")
            return
        buttons.append([InlineKeyboardButton("🔙 رجوع", callback_data="back_main")])
        await query.edit_message_text(
            f"قائمة التجار ({status}):", reply_markup=InlineKeyboardMarkup(buttons)
        )

    elif data == "back_main":
        keyboard = [
            [InlineKeyboardButton("🆕 طلبات جديدة (Pending)", callback_data="list_pending")],
            [InlineKeyboardButton("✅ مشتركين نشطين (Active)", callback_data="list_active")],
            [InlineKeyboardButton("⛔ منتهية صلاحيتهم (Expired)", callback_data="list_expired")],
        ]
        await query.edit_message_text("📋 لوحة تحكم التجار", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("view_"):
        trader_id = int(data.split("_", 1)[1])
        t = get_trader(trader_id)
        if not t:
            await query.edit_message_text("التاجر ده مش موجود.")
            return
        text = (
            f"يوزر: {t['trader_username']}\n"
            f"قناة: {t['channel_title']} ({t['channel_id']})\n"
            f"الحالة: {t['status']}\n"
            f"ينتهي: {t['expires_at'] or '-'}"
        )
        buttons = [
            [
                InlineKeyboardButton("✅ تفعيل/تجديد", callback_data=f"activate_{trader_id}"),
                InlineKeyboardButton("⛔ إيقاف", callback_data=f"deactivate_{trader_id}"),
            ],
            [InlineKeyboardButton("🔙 رجوع", callback_data="back_main")],
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))

    elif data.startswith("activate_"):
        trader_id = int(data.split("_", 1)[1])
        set_status(trader_id, "active", extend=True)
        t = get_trader(trader_id)
        await context.bot.send_message(
            t["telegram_id"],
            f"تم تفعيل اشتراكك ✅ صالح لمدة {SUBSCRIPTION_DAYS} يوم. هيبدأ النشر في قناتك حالًا.",
        )
        await query.edit_message_text(f"تم تفعيل {t['trader_username']} ✅")

    elif data.startswith("deactivate_"):
        trader_id = int(data.split("_", 1)[1])
        set_status(trader_id, "expired")
        t = get_trader(trader_id)
        await context.bot.send_message(
            t["telegram_id"], "تم إيقاف اشتراكك. كلم الأدمن لو عايز تجدد."
        )
        await query.edit_message_text(f"تم إيقاف {t['trader_username']} ⛔")


# ----------------------- إعادة النشر من الجروب الرسمي -----------------------
async def relay_source_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if msg.chat_id != SOURCE_GROUP_ID:
        return
    # فلتر: لازم فيديو + وصف (كابشن) عشان يعتبر "عرض حساب"
    if not msg.video or not msg.caption:
        return

    active_traders = get_traders_by_status("active")
    for t in active_traders:
        try:
            await context.bot.copy_message(
                chat_id=t["channel_id"],
                from_chat_id=msg.chat_id,
                message_id=msg.message_id,
            )
        except Exception as e:
            logger.warning(f"فشل النشر لقناة {t['channel_id']}: {e}")


# ----------------------- فحص الاشتراكات المنتهية (يومي) -----------------------
async def check_expired(context: ContextTypes.DEFAULT_TYPE):
    conn = db_connect()
    now = datetime.utcnow().isoformat()
    rows = conn.execute(
        "SELECT * FROM traders WHERE status='active' AND expires_at < ?", (now,)
    ).fetchall()
    for t in rows:
        conn.execute("UPDATE traders SET status='expired' WHERE id=?", (t["id"],))
        try:
            await context.bot.send_message(
                t["telegram_id"],
                f"⚠️ اشتراكك خلص. كلم @{ADMIN_USERNAME} عشان تجدد وترجع تستقبل المنشورات.",
            )
        except Exception:
            pass
    conn.commit()
    conn.close()


# ----------------------- تشغيل البوت -----------------------
def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    reg_conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ASK_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_username)],
            ASK_CHANNEL: [MessageHandler(filters.TEXT | filters.FORWARDED, ask_channel)],
            WAIT_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_channel)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(reg_conv)
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CallbackQueryHandler(admin_callback))
    app.add_handler(MessageHandler(filters.Chat(SOURCE_GROUP_ID), relay_source_post))

    if app.job_queue:
        app.job_queue.run_repeating(check_expired, interval=3600, first=10)

    logger.info("Bot started.")
    app.run_polling()


if __name__ == "__main__":
    main()
