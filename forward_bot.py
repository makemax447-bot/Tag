# -*- coding: utf-8 -*-
"""
بوت إعادة نشر حسابات ببجي - Multi-tenant Forward Bot
------------------------------------------------------
الفكرة:
  - البوت يشتغل تلقائي في أي جروب "عرض حسابات" يتضاف فيه كأدمن (مفيش
    جروب واحد ثابت متحدد بالـ ID - أي جروب البوت أدمن فيه بيبقى مصدر).
  - تجار كتير بيشتركوا (5$/شهر، دفع يدوي خارج البوت) وبيسجلوا يوزرهم
    وقناتهم الخاصة.
  - أي منشور جديد فيه فيديو/صورة ووصف في أي جروب من دول، بيتحول أوتوماتيك
    لقنوات كل التجار الـ Active.
  - الأدمن (انت) عنده لوحة تحكم جوه البوت لتفعيل/إيقاف/تجديد التجار.

الإعداد قبل التشغيل (غيّر القيم دي في الأسفل أو حطها Environment Variables):
  BOT_TOKEN        - توكن البوت من BotFather
  ADMIN_ID         - رقم التليجرام آيدي بتاعك (مش اليوزر)
  ADMIN_USERNAME   - يوزرك للتواصل (بدون @) - بيظهر للتاجر عشان يدفع

ملحوظة مهمة: عشان البوت يستقبل رسائل أي جروب "عرض حسابات"، لازم يتضاف
فيه كـ Admin (مش عضو عادي بس) - كده تليجرام بيبعتله كل الرسائل حتى لو
Privacy Mode شغال في إعدادات BotFather.
"""

import os
import asyncio
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
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "Bobeka11")
SUBSCRIPTION_DAYS = 30
DB_PATH = os.environ.get("DB_PATH", "traders.db")

# كلمات لو ظهرت في الوصف، المنشور مش بيتنشر (زي طلبات الشراء مش عروض البيع)
DEFAULT_EXCLUDE_KEYWORDS = [
    "مطلوب",
    "طلب حساب",
    "محتاج حساب",
    "بدور على حساب",
    "داير على حساب",
]

# ثواني الانتظار بين كل منشور والتاني عند النشر (عشان الترتيب يفضل مظبوط
# ومايحصلش تزاحم/فلود لو نزل منشورات كتير مرة واحدة على الجروب)
BROADCAST_DELAY_SECONDS = float(os.environ.get("BROADCAST_DELAY_SECONDS", "3"))

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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )
    row = conn.execute(
        "SELECT value FROM settings WHERE key='exclude_keywords'"
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('exclude_keywords', ?)",
            (",".join(DEFAULT_EXCLUDE_KEYWORDS),),
        )
    conn.commit()
    conn.close()


def get_exclude_keywords():
    conn = db_connect()
    row = conn.execute(
        "SELECT value FROM settings WHERE key='exclude_keywords'"
    ).fetchone()
    conn.close()
    if not row or not row["value"]:
        return []
    return [k.strip() for k in row["value"].split(",") if k.strip()]


def set_exclude_keywords(keywords):
    conn = db_connect()
    conn.execute(
        """
        INSERT INTO settings (key, value) VALUES ('exclude_keywords', ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (",".join(keywords),),
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


def get_trader_by_telegram_id(telegram_id):
    conn = db_connect()
    row = conn.execute("SELECT * FROM traders WHERE telegram_id=?", (telegram_id,)).fetchone()
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
    telegram_id = update.effective_user.id
    trader = get_trader_by_telegram_id(telegram_id)

    if trader:
        if trader["status"] == "active":
            remaining_text = "غير معروف"
            if trader["expires_at"]:
                try:
                    expires = datetime.fromisoformat(trader["expires_at"])
                    remaining_days = max((expires - datetime.utcnow()).days, 0)
                    remaining_text = f"{remaining_days} يوم"
                except ValueError:
                    pass
            status_text = f"✅ اشتراكك فعّال حاليًا\nمتبقي: {remaining_text}"
        elif trader["status"] == "pending":
            status_text = "⏳ طلبك قيد المراجعة من الأدمن، هيتفعّل بعد الدفع."
        else:  # expired
            status_text = f"⛔ اشتراكك منتهي.\nكلم الأدمن @{ADMIN_USERNAME} عشان تجدد."

        keyboard = [[InlineKeyboardButton("🔄 تحديث بياناتي", callback_data="update_data")]]
        await update.message.reply_text(
            f"أهلاً بيك تاني 👋\n\n"
            f"يوزرك: {trader['trader_username']}\n"
            f"قناتك: {trader['channel_title']}\n\n"
            f"{status_text}",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "أهلاً بيك 👋\n"
        "البوت ده بينقل منشورات حسابات ببجي أوتوماتيك من الجروب الرسمي لقناتك.\n\n"
        "الاشتراك: 5$ شهريًا.\n\n"
        "عشان نبدأ، ابعتلي اليوزر بتاعك (اللي عايز يظهر مع منشوراتك):"
    )
    return ASK_USERNAME


async def start_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يبدأ من زرار 'تحديث بياناتي' - بيرجع نفس خطوات التسجيل."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "تمام، هنحدّث بياناتك ✏️\n"
        "لازم التحديث يتراجع من الأدمن قبل ما يتفعّل تاني.\n\n"
        "ابعتلي اليوزر بتاعك:"
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

    forward_origin = update.message.forward_origin
    if forward_origin and getattr(forward_origin, "chat", None):
        channel_id = str(forward_origin.chat.id)
        channel_title = forward_origin.chat.title
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


async def list_keywords(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    keywords = get_exclude_keywords()
    if keywords:
        text = "🚫 الكلمات المستبعدة حاليًا (أي منشور فيها متتنشرش):\n" + "\n".join(
            f"- {k}" for k in keywords
        )
    else:
        text = "مفيش كلمات مستبعدة حاليًا."
    text += (
        "\n\nلإضافة كلمة: /addkeyword الكلمة\n"
        "لحذف كلمة: /delkeyword الكلمة"
    )
    await update.message.reply_text(text)


async def add_keyword(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    if not context.args:
        await update.message.reply_text("اكتب الكلمة بعد الأمر، مثال:\n/addkeyword مطلوب")
        return
    word = " ".join(context.args).strip()
    keywords = get_exclude_keywords()
    if word in keywords:
        await update.message.reply_text("الكلمة دي موجودة بالفعل في القائمة.")
        return
    keywords.append(word)
    set_exclude_keywords(keywords)
    await update.message.reply_text(f'تمت إضافة "{word}" لقائمة الاستبعاد ✅')


async def del_keyword(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    if not context.args:
        await update.message.reply_text("اكتب الكلمة بعد الأمر، مثال:\n/delkeyword مطلوب")
        return
    word = " ".join(context.args).strip()
    keywords = get_exclude_keywords()
    if word not in keywords:
        await update.message.reply_text("الكلمة دي مش موجودة في القائمة أصلاً.")
        return
    keywords.remove(word)
    set_exclude_keywords(keywords)
    await update.message.reply_text(f'تم حذف "{word}" من قائمة الاستبعاد ✅')


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
# ----------------------- إعادة النشر من الجروب الرسمي -----------------------
MEDIA_GROUP_WAIT = 2  # ثواني الانتظار لتجميع كل عناصر الألبوم الواحد
media_groups = {}  # media_group_id -> {"chat_id", "message_ids", "caption", "task"}
broadcast_queue = asyncio.Queue()  # طابور المنشورات المنتظرة النشر بالترتيب


async def relay_source_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat

    # أي جروب أو سوبرجروب البوت أدمن/عضو فيه بيتعامل كمصدر أوتوماتيك.
    # (القنوات مش بتدخل هنا أصلًا - دي وجهة النشر مش مصدره)
    if chat.type not in ("group", "supergroup"):
        return

    # ألبوم (صورة + فيديو مع بعض أو أكتر من عنصر) - لازم نجمعه الأول
    if msg.media_group_id:
        await collect_media_group_message(msg, context)
        return

    # رسالة مفردة: لازم (فيديو أو صورة) + وصف عشان تعتبر "عرض حساب"
    if not (msg.video or msg.photo) or not msg.caption:
        return

    if caption_is_excluded(msg.caption):
        return

    await broadcast_queue.put((msg.chat_id, [msg.message_id], msg.caption))


async def collect_media_group_message(msg, context: ContextTypes.DEFAULT_TYPE):
    gid = msg.media_group_id
    group = media_groups.setdefault(
        gid, {"chat_id": msg.chat_id, "message_ids": [], "caption": None, "task": None}
    )
    group["message_ids"].append(msg.message_id)
    if msg.caption:
        group["caption"] = msg.caption

    if group["task"] is None:
        group["task"] = asyncio.create_task(flush_media_group(gid, context))


async def flush_media_group(gid: str, context: ContextTypes.DEFAULT_TYPE):
    await asyncio.sleep(MEDIA_GROUP_WAIT)
    group = media_groups.pop(gid, None)
    if not group:
        return
    if not group["caption"]:
        # الألبوم من غير وصف على أي عنصر فيه - متعتبرش عرض حساب
        return
    if caption_is_excluded(group["caption"]):
        return
    await broadcast_queue.put((group["chat_id"], group["message_ids"], group["caption"]))


async def broadcast_worker(bot):
    """بيسحب المنشورات من الطابور وينشرها واحد ورا التاني بالترتيب،
    مع وقفة صغيرة بين كل منشور والتاني عشان الترتيب يفضل مظبوط ومايحصلش فلود."""
    while True:
        source_chat_id, message_ids, caption = await broadcast_queue.get()
        try:
            await broadcast_messages(bot, source_chat_id, message_ids, caption)
        except Exception as e:
            logger.warning(f"فشل النشر: {e}")
        finally:
            broadcast_queue.task_done()
        await asyncio.sleep(BROADCAST_DELAY_SECONDS)


def caption_is_excluded(caption: str) -> bool:
    """بيرجع True لو الوصف فيه كلمة من قائمة الاستبعاد (زي 'مطلوب')."""
    if not caption:
        return False
    keywords = get_exclude_keywords()
    return any(kw and kw in caption for kw in keywords)


def build_final_caption(original_caption: str, trader_username: str) -> str:
    username = (trader_username or "").strip().lstrip("@")
    if not username:
        return original_caption
    return f"{original_caption}\n\n📩 للتواصل: @{username}"


async def broadcast_messages(bot, source_chat_id, message_ids, original_caption: str):
    active_traders = get_traders_by_status("active")
    ids_sorted = sorted(message_ids)
    for t in active_traders:
        final_caption = build_final_caption(original_caption, t["trader_username"])
        try:
            if len(ids_sorted) == 1:
                # رسالة مفردة: بنستبدل الكابشن مباشرة أثناء النسخ
                await bot.copy_message(
                    chat_id=t["channel_id"],
                    from_chat_id=source_chat_id,
                    message_id=ids_sorted[0],
                    caption=final_caption,
                )
            else:
                # ألبوم: بننسخه من غير كابشن، وبعدين نبعت الوصف النهائي كرسالة منفصلة
                await bot.copy_messages(
                    chat_id=t["channel_id"],
                    from_chat_id=source_chat_id,
                    message_ids=ids_sorted,
                    remove_caption=True,
                )
                await bot.send_message(
                    chat_id=t["channel_id"],
                    text=final_caption,
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
async def on_startup(app: Application):
    """بتتنفذ مرة واحدة لما البوت يبدأ - بتشغّل عامل النشر بالترتيب."""
    app.create_task(broadcast_worker(app.bot))


def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).post_init(on_startup).build()

    reg_conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CallbackQueryHandler(start_update, pattern="^update_data$"),
        ],
        states={
            ASK_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_username)],
            ASK_CHANNEL: [MessageHandler(filters.TEXT | filters.FORWARDED, ask_channel)],
            WAIT_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_channel)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(reg_conv)
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("keywords", list_keywords))
    app.add_handler(CommandHandler("addkeyword", add_keyword))
    app.add_handler(CommandHandler("delkeyword", del_keyword))
    app.add_handler(
        CallbackQueryHandler(
            admin_callback, pattern="^(list_|back_main|view_|activate_|deactivate_)"
        )
    )
    app.add_handler(
        MessageHandler(filters.ChatType.GROUPS, relay_source_post)
    )

    if app.job_queue:
        app.job_queue.run_repeating(check_expired, interval=3600, first=10)

    logger.info("Bot started.")
    app.run_polling()


if __name__ == "__main__":
    main()


