import os
import asyncio
import logging
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row
from aiohttp import web

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ============================================================
# إعدادات
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
TIMEZONE_NAME = os.getenv("TIMEZONE", "Asia/Riyadh")
TIMEZONE = ZoneInfo(TIMEZONE_NAME)

# كل كم دقيقة يعاد إرسال التنبيه غير المؤكد
PERSISTENT_MINUTES = 10

# المنفذ الذي يوفره Render
PORT = int(os.getenv("PORT", "10000"))

# ============================================================
# Logging
# ============================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("pet-reminder-bot")

# ============================================================
# Conversation states
# ============================================================

PET_NAME, PET_DEFAULT_MESSAGE = range(2)
REM_PET, REM_TIME, REM_MESSAGE = range(2, 5)

# ============================================================
# PostgreSQL
# ============================================================

def db_connect():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL غير موجود في Environment Variables.")
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def init_db():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS pets (
                    id BIGSERIAL PRIMARY KEY,
                    chat_id BIGINT NOT NULL,
                    name TEXT NOT NULL,
                    default_message TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS reminders (
                    id BIGSERIAL PRIMARY KEY,
                    chat_id BIGINT NOT NULL,
                    pet_id BIGINT NOT NULL REFERENCES pets(id) ON DELETE CASCADE,
                    hour SMALLINT NOT NULL CHECK (hour BETWEEN 0 AND 23),
                    minute SMALLINT NOT NULL CHECK (minute BETWEEN 0 AND 59),
                    message TEXT NOT NULL,
                    enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    last_triggered_date DATE NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS active_alerts (
                    id BIGSERIAL PRIMARY KEY,
                    reminder_id BIGINT NOT NULL UNIQUE REFERENCES reminders(id) ON DELETE CASCADE,
                    chat_id BIGINT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    last_sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_pets_chat_id
                ON pets(chat_id)
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_reminders_chat_id
                ON reminders(chat_id)
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_reminders_enabled
                ON reminders(enabled)
            """)

        conn.commit()


def get_user_pets(chat_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM pets WHERE chat_id=%s ORDER BY id",
                (chat_id,),
            )
            return cur.fetchall()


def get_pet(chat_id, pet_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM pets WHERE chat_id=%s AND id=%s",
                (chat_id, pet_id),
            )
            return cur.fetchone()


def add_pet(chat_id, name, default_message):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO pets(chat_id, name, default_message)
                VALUES (%s, %s, %s)
                RETURNING id
                """,
                (chat_id, name, default_message),
            )
            pet_id = cur.fetchone()["id"]
        conn.commit()
        return pet_id


def add_reminder(chat_id, pet_id, hour, minute, message):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO reminders(chat_id, pet_id, hour, minute, message)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
                """,
                (chat_id, pet_id, hour, minute, message),
            )
            reminder_id = cur.fetchone()["id"]
        conn.commit()
        return reminder_id


def get_reminder(reminder_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT r.*, p.name AS pet_name
                FROM reminders r
                JOIN pets p ON p.id=r.pet_id
                WHERE r.id=%s
                """,
                (reminder_id,),
            )
            return cur.fetchone()


def get_user_reminders(chat_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT r.*, p.name AS pet_name
                FROM reminders r
                JOIN pets p ON p.id=r.pet_id
                WHERE r.chat_id=%s
                ORDER BY r.hour, r.minute, r.id
                """,
                (chat_id,),
            )
            return cur.fetchall()


def get_enabled_reminders():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT r.*, p.name AS pet_name
                FROM reminders r
                JOIN pets p ON p.id=r.pet_id
                WHERE r.enabled=TRUE
                ORDER BY r.id
            """)
            return cur.fetchall()


def mark_triggered(reminder_id, local_date):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE reminders
                SET last_triggered_date=%s
                WHERE id=%s
                """,
                (local_date, reminder_id),
            )
        conn.commit()


def get_active_alert(reminder_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM active_alerts WHERE reminder_id=%s",
                (reminder_id,),
            )
            return cur.fetchone()


def create_active_alert(reminder_id, chat_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO active_alerts(reminder_id, chat_id)
                VALUES (%s, %s)
                ON CONFLICT (reminder_id) DO NOTHING
                RETURNING id
                """,
                (reminder_id, chat_id),
            )
            row = cur.fetchone()
            if row:
                alert_id = row["id"]
            else:
                cur.execute(
                    "SELECT id FROM active_alerts WHERE reminder_id=%s",
                    (reminder_id,),
                )
                alert_id = cur.fetchone()["id"]
        conn.commit()
        return alert_id


def update_alert_sent(alert_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE active_alerts
                SET last_sent_at=NOW()
                WHERE id=%s
                """,
                (alert_id,),
            )
        conn.commit()


def confirm_alert(alert_id, chat_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM active_alerts
                WHERE id=%s AND chat_id=%s
                RETURNING id
                """,
                (alert_id, chat_id),
            )
            row = cur.fetchone()
        conn.commit()
        return bool(row)


def delete_reminder(chat_id, reminder_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM reminders
                WHERE id=%s AND chat_id=%s
                RETURNING id
                """,
                (reminder_id, chat_id),
            )
            row = cur.fetchone()
        conn.commit()
        return bool(row)


def get_active_alerts():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    a.id AS alert_id,
                    a.chat_id,
                    a.reminder_id,
                    r.message,
                    p.name AS pet_name
                FROM active_alerts a
                JOIN reminders r ON r.id=a.reminder_id
                JOIN pets p ON p.id=r.pet_id
                WHERE r.enabled=TRUE
            """)
            return cur.fetchall()


# ============================================================
# Telegram helpers
# ============================================================

def alert_keyboard(alert_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ تأكيد (تم الإنجاز)",
                callback_data=f"confirm:{alert_id}",
            )
        ]
    ])


def reminder_text(pet_name, message):
    return (
        f"🐾 تذكير: {pet_name}\n\n"
        f"🔔 {message}\n\n"
        "⚠️ لم يتم تأكيد تنفيذ المهمة.\n"
        "سأستمر بتذكيرك كل 10 دقائق حتى تضغط "
        "«✅ تأكيد (تم الإنجاز)»."
    )


async def send_alert(application, alert_id, chat_id, pet_name, message):
    await application.bot.send_message(
        chat_id=chat_id,
        text=reminder_text(pet_name, message),
        reply_markup=alert_keyboard(alert_id),
    )
    update_alert_sent(alert_id)


def persistent_job_name(alert_id):
    return f"persistent_alert:{alert_id}"


def daily_job_name(reminder_id):
    return f"daily_reminder:{reminder_id}"


def remove_jobs_by_name(application, name):
    for job in application.job_queue.get_jobs_by_name(name):
        job.schedule_removal()


# ============================================================
# التذكير اليومي
# ============================================================

async def trigger_reminder(application, reminder_id):
    reminder = get_reminder(reminder_id)
    if not reminder or not reminder["enabled"]:
        return

    # لا نكرر نفس التنبيه مرتين في نفس اليوم.
    today = datetime.now(TIMEZONE).date()

    if reminder["last_triggered_date"] == today:
        return

    existing = get_active_alert(reminder_id)
    if existing:
        # يوجد تنبيه غير مؤكد بالفعل؛ لا ننشئ واحداً جديداً.
        mark_triggered(reminder_id, today)
        return

    alert_id = create_active_alert(reminder_id, reminder["chat_id"])
    mark_triggered(reminder_id, today)

    try:
        await send_alert(
            application,
            alert_id,
            reminder["chat_id"],
            reminder["pet_name"],
            reminder["message"],
        )
    except Exception:
        logger.exception("فشل إرسال التنبيه %s", reminder_id)
        return

    remove_jobs_by_name(application, persistent_job_name(alert_id))

    application.job_queue.run_repeating(
        persistent_alert_callback,
        interval=PERSISTENT_MINUTES * 60,
        first=PERSISTENT_MINUTES * 60,
        data={"alert_id": alert_id},
        name=persistent_job_name(alert_id),
    )


async def daily_reminder_callback(context: ContextTypes.DEFAULT_TYPE):
    reminder_id = context.job.data["reminder_id"]
    await trigger_reminder(context.application, reminder_id)


async def persistent_alert_callback(context: ContextTypes.DEFAULT_TYPE):
    alert_id = context.job.data["alert_id"]

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    a.id AS alert_id,
                    a.chat_id,
                    r.message,
                    r.enabled,
                    p.name AS pet_name
                FROM active_alerts a
                JOIN reminders r ON r.id=a.reminder_id
                JOIN pets p ON p.id=r.pet_id
                WHERE a.id=%s
            """, (alert_id,))
            alert = cur.fetchone()

    if not alert or not alert["enabled"]:
        context.job.schedule_removal()
        return

    try:
        await send_alert(
            context.application,
            alert["alert_id"],
            alert["chat_id"],
            alert["pet_name"],
            alert["message"],
        )
    except Exception:
        logger.exception("فشل إعادة إرسال التنبيه %s", alert_id)


# ============================================================
# استعادة التذكيرات بعد Restart / Wake-up
# ============================================================

def schedule_all_daily_jobs(application):
    for reminder in get_enabled_reminders():
        remove_jobs_by_name(application, daily_job_name(reminder["id"]))

        reminder_time = time(
            hour=reminder["hour"],
            minute=reminder["minute"],
            tzinfo=TIMEZONE,
        )

        application.job_queue.run_daily(
            daily_reminder_callback,
            time=reminder_time,
            days=(0, 1, 2, 3, 4, 5, 6),
            data={"reminder_id": reminder["id"]},
            name=daily_job_name(reminder["id"]),
        )


async def restore_active_alerts(application):
    alerts = get_active_alerts()

    for alert in alerts:
        remove_jobs_by_name(application, persistent_job_name(alert["alert_id"]))

        # نرسل فوراً عند استعادة البوت، ثم نواصل كل 10 دقائق.
        try:
            await send_alert(
                application,
                alert["alert_id"],
                alert["chat_id"],
                alert["pet_name"],
                alert["message"],
            )
        except Exception:
            logger.exception(
                "فشل استعادة التنبيه %s",
                alert["alert_id"],
            )

        application.job_queue.run_repeating(
            persistent_alert_callback,
            interval=PERSISTENT_MINUTES * 60,
            first=PERSISTENT_MINUTES * 60,
            data={"alert_id": alert["alert_id"]},
            name=persistent_job_name(alert["alert_id"]),
        )


async def catch_up_due_reminders(application):
    """
    إذا كان Render نائماً أو حصل Restart أثناء وقت التذكير،
    نتحقق عند عودة البوت: هل موعد اليوم مرّ ولم يُنفذ؟
    إذا نعم، نرسل التنبيه فوراً بدلاً من انتظار اليوم التالي.
    """
    now = datetime.now(TIMEZONE)
    today = now.date()
    current_minutes = now.hour * 60 + now.minute

    for reminder in get_enabled_reminders():
        reminder_minutes = reminder["hour"] * 60 + reminder["minute"]

        if (
            reminder["last_triggered_date"] != today
            and current_minutes >= reminder_minutes
        ):
            logger.info(
                "Catch-up reminder %s",
                reminder["id"],
            )
            await trigger_reminder(application, reminder["id"])


# ============================================================
# HTTP health server لـ Render + UptimeRobot
# ============================================================

async def health_handler(request):
    return web.json_response({
        "status": "ok",
        "service": "pet-reminder-bot",
        "time": datetime.now(TIMEZONE).isoformat(),
    })


async def start_health_server(application):
    app = web.Application()
    app.router.add_get("/", health_handler)
    app.router.add_get("/health", health_handler)

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(
        runner,
        host="0.0.0.0",
        port=PORT,
    )

    await site.start()

    logger.info("Health server listening on port %s", PORT)

    # الاحتفاظ بالـ runner حتى إغلاق التطبيق.
    application.bot_data["health_runner"] = runner


async def post_init(application):
    init_db()
    await start_health_server(application)

    schedule_all_daily_jobs(application)

    await restore_active_alerts(application)
    await catch_up_due_reminders(application)

    logger.info("Bot initialized successfully.")


async def post_shutdown(application):
    runner = application.bot_data.get("health_runner")
    if runner:
        await runner.cleanup()


# ============================================================
# Commands
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🐾 أهلاً بك في بوت مواعيد الحيوانات!\n\n"
        "الأوامر:\n"
        "/add_pet — إضافة حيوان\n"
        "/add_reminder — إضافة تذكير\n"
        "/pets — عرض الحيوانات\n"
        "/reminders — عرض التذكيرات\n"
        "/delete_reminder ID — حذف تذكير\n"
        "/cancel — إلغاء العملية\n\n"
        "😈 إذا جاء موعد التذكير ولم تؤكده، "
        "سأعيد إرسال التنبيه كل 10 دقائق حتى تؤكده."
    )


# ============================================================
# Add Pet conversation
# ============================================================

async def add_pet_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🐾 اكتب اسم الحيوان:\n\n"
        "مثال: لولو"
    )
    return PET_NAME


async def add_pet_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()

    if not name:
        await update.message.reply_text("❌ اكتب اسماً صحيحاً.")
        return PET_NAME

    context.user_data["pet_name"] = name

    await update.message.reply_text(
        "💬 اكتب الرسالة الافتراضية لهذا الحيوان.\n\n"
        "مثال:\n"
        "أعطِ لولو أكلها\n\n"
        "أو اكتب «لا يوجد»."
    )
    return PET_DEFAULT_MESSAGE


async def add_pet_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message.text.strip()
    if message.lower() == "لا يوجد":
        message = ""

    name = context.user_data.pop("pet_name", None)

    if not name:
        await update.message.reply_text("❌ حدث خطأ، أعد المحاولة.")
        return ConversationHandler.END

    pet_id = add_pet(
        update.effective_chat.id,
        name,
        message,
    )

    await update.message.reply_text(
        f"✅ تمت إضافة الحيوان!\n\n"
        f"🐾 {name}\n"
        f"🆔 ID: {pet_id}\n\n"
        "الآن استخدم /add_reminder لإضافة موعد."
    )
    return ConversationHandler.END


# ============================================================
# Add Reminder conversation
# ============================================================

async def add_reminder_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pets = get_user_pets(update.effective_chat.id)

    if not pets:
        await update.message.reply_text(
            "❌ لا يوجد لديك حيوان.\n"
            "أضف واحداً أولاً باستخدام /add_pet"
        )
        return ConversationHandler.END

    text = "🐾 اكتب ID الحيوان:\n\n"
    for pet in pets:
        text += f"🆔 {pet['id']} — {pet['name']}\n"

    await update.message.reply_text(text)
    return REM_PET


async def add_reminder_pet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        pet_id = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ اكتب رقم ID فقط.")
        return REM_PET

    pet = get_pet(update.effective_chat.id, pet_id)

    if not pet:
        await update.message.reply_text("❌ لم أجد هذا الحيوان.")
        return REM_PET

    context.user_data["rem_pet_id"] = pet_id

    await update.message.reply_text(
        f"🐾 الحيوان: {pet['name']}\n\n"
        "⏰ اكتب الوقت بصيغة HH:MM\n"
        "مثال: 08:30"
    )
    return REM_TIME


async def add_reminder_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()

    try:
        parts = raw.split(":")
        if len(parts) != 2:
            raise ValueError

        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError:
        await update.message.reply_text(
            "❌ الوقت غير صحيح.\n"
            "استخدم HH:MM مثل 08:30"
        )
        return REM_TIME

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        await update.message.reply_text("❌ الساعة 00-23 والدقائق 00-59.")
        return REM_TIME

    context.user_data["rem_hour"] = hour
    context.user_data["rem_minute"] = minute

    pet = get_pet(
        update.effective_chat.id,
        context.user_data["rem_pet_id"],
    )

    default = pet["default_message"] if pet else ""

    if default:
        await update.message.reply_text(
            "💬 اكتب رسالة التذكير.\n\n"
            f"الرسالة الافتراضية:\n{default}\n\n"
            "يمكنك كتابة رسالة مختلفة."
        )
    else:
        await update.message.reply_text(
            "💬 اكتب رسالة التذكير.\n\n"
            "مثال: أعطِ لولو أكلها."
        )

    return REM_MESSAGE


async def add_reminder_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message.text.strip()

    if not message:
        await update.message.reply_text("❌ الرسالة لا يمكن أن تكون فارغة.")
        return REM_MESSAGE

    chat_id = update.effective_chat.id
    pet_id = context.user_data.get("rem_pet_id")
    hour = context.user_data.get("rem_hour")
    minute = context.user_data.get("rem_minute")

    pet = get_pet(chat_id, pet_id)
    if not pet:
        await update.message.reply_text("❌ الحيوان غير موجود.")
        return ConversationHandler.END

    reminder_id = add_reminder(
        chat_id,
        pet_id,
        hour,
        minute,
        message,
    )

    # جدولة الموعد فوراً بدون الحاجة لإعادة تشغيل البوت.
    reminder_time = time(
        hour=hour,
        minute=minute,
        tzinfo=TIMEZONE,
    )

    context.application.job_queue.run_daily(
        daily_reminder_callback,
        time=reminder_time,
        days=(0, 1, 2, 3, 4, 5, 6),
        data={"reminder_id": reminder_id},
        name=daily_job_name(reminder_id),
    )

    for key in ("rem_pet_id", "rem_hour", "rem_minute"):
        context.user_data.pop(key, None)

    await update.message.reply_text(
        "✅ تم إنشاء التذكير!\n\n"
        f"🐾 الحيوان: {pet['name']}\n"
        f"⏰ الوقت: {hour:02d}:{minute:02d}\n"
        "🔁 التكرار: يومياً\n"
        "😈 الإعادة: كل 10 دقائق حتى التأكيد\n"
        f"🆔 ID التذكير: {reminder_id}"
    )

    return ConversationHandler.END


# ============================================================
# List commands
# ============================================================

async def pets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = get_user_pets(update.effective_chat.id)

    if not rows:
        await update.message.reply_text("🐾 لا توجد حيوانات.")
        return

    text = "🐾 حيواناتك:\n\n"
    for pet in rows:
        text += f"🆔 {pet['id']} — {pet['name']}\n"
        if pet["default_message"]:
            text += f"💬 {pet['default_message']}\n"
        text += "\n"

    await update.message.reply_text(text)


async def reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = get_user_reminders(update.effective_chat.id)

    if not rows:
        await update.message.reply_text("⏰ لا توجد تذكيرات.")
        return

    text = "⏰ تذكيراتك:\n\n"

    for r in rows:
        active = get_active_alert(r["id"])
        text += (
            f"🆔 {r['id']}\n"
            f"🐾 {r['pet_name']}\n"
            f"⏰ {r['hour']:02d}:{r['minute']:02d}\n"
            f"💬 {r['message']}\n"
            f"🟢 {'فعال' if r['enabled'] else 'متوقف'}\n"
        )
        if active:
            text += "🚨 يوجد تنبيه معلّق\n"
        text += "\n"

    await update.message.reply_text(text)


async def delete_reminder_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "استخدم:\n/delete_reminder ID\n\n"
            "مثال:\n/delete_reminder 3"
        )
        return

    try:
        reminder_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ ID غير صحيح.")
        return

    # نزيل Job اليومي
    remove_jobs_by_name(
        context.application,
        daily_job_name(reminder_id),
    )

    # إذا كان هناك تنبيه نشط، نزيل Job الخاص به أيضاً.
    active = get_active_alert(reminder_id)
    if active:
        remove_jobs_by_name(
            context.application,
            persistent_job_name(active["id"]),
        )

    ok = delete_reminder(
        update.effective_chat.id,
        reminder_id,
    )

    await update.message.reply_text(
        "🗑️ تم حذف التذكير."
        if ok
        else "❌ لم أجد هذا التذكير."
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ تم إلغاء العملية.")
    return ConversationHandler.END


# ============================================================
# Confirm button
# ============================================================

async def confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        alert_id = int(query.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await query.answer("❌ زر غير صالح.", show_alert=True)
        return

    chat_id = update.effective_chat.id

    ok = confirm_alert(alert_id, chat_id)

    if not ok:
        await query.answer(
            "هذا التنبيه تم تأكيده مسبقاً.",
            show_alert=True,
        )
        return

    remove_jobs_by_name(
        context.application,
        persistent_job_name(alert_id),
    )

    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    await query.message.reply_text(
        "✅ تم الإنجاز!\n"
        "لن أكرر هذا التنبيه اليوم. 🐾"
    )


# ============================================================
# Main
# ============================================================

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN غير موجود.")

    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL غير موجود.")

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    add_pet_conversation = ConversationHandler(
        entry_points=[CommandHandler("add_pet", add_pet_start)],
        states={
            PET_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_pet_name)
            ],
            PET_DEFAULT_MESSAGE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_pet_message)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    add_reminder_conversation = ConversationHandler(
        entry_points=[CommandHandler("add_reminder", add_reminder_start)],
        states={
            REM_PET: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_reminder_pet)
            ],
            REM_TIME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_reminder_time)
            ],
            REM_MESSAGE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_reminder_message)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(add_pet_conversation)
    application.add_handler(add_reminder_conversation)
    application.add_handler(CommandHandler("pets", pets))
    application.add_handler(CommandHandler("reminders", reminders))
    application.add_handler(CommandHandler("delete_reminder", delete_reminder_command))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(
        CallbackQueryHandler(confirm_callback, pattern=r"^confirm:\d+$")
    )

    logger.info("Starting bot...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
