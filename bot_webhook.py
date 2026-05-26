import asyncio
import aiohttp
import logging
from datetime import datetime, timezone, timedelta
import os
import json

from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)
from telegram.error import TelegramError
from telegram.request import HttpxRequest
from aiohttp import web

# ─── CONFIG ────────────────────────────────────────────────────────────────────
BOT_TOKEN     = os.getenv("BOT_TOKEN",     "8693341837:AAF2lK6bGR3uoLz1kfkZt8IjDQIF18YXHN8")
CHANNEL_ID    = os.getenv("CHANNEL_ID",    "@yampilnews")
ALERT_API_KEY = os.getenv("ALERT_API_KEY", "b3de42c9:736017aa6745a605c155108e221d31a8")
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "")
ADMIN_IDS     = set(int(x.strip()) for x in ADMIN_IDS_RAW.split(",") if x.strip().isdigit())

TARGET_REGION_ID  = "31004"
ALERT_API_URL     = "https://api.alerts.in.ua/v1/alerts/active.json"
CHECK_INTERVAL    = 30
KYIV_TZ           = timezone(timedelta(hours=3))
PORT              = int(os.getenv("PORT", 10000))

MAP_URL = "https://alerts.in.ua/"
MAP_IMAGE_URL = "https://alerts.in.ua/map.png"

# Webhook URL (Render назначить автоматично)
RENDER_URL = os.getenv("RENDER_URL", "https://mapyampilalert.onrender.com")
WEBHOOK_URL = f"{RENDER_URL}/webhook"

# Реєстрація
REGISTERED_FILE = "/tmp/registered_users.json"
BANNED_FILE = "/tmp/banned_users.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

alert_active: bool | None = None
bot_stats = {"messages_sent": 0, "alerts_count": 0, "allclear_count": 0, "start_time": None}

# Conversation states
AWAITING_NAME = 1
AWAITING_PHONE = 2
AWAITING_ADDRESS = 3

# ─── USER MANAGEMENT ───────────────────────────────────────────────────────────
def load_users(filename):
    if os.path.exists(filename):
        with open(filename, "r") as f:
            return set(json.load(f))
    return set()

def save_users(filename, users):
    with open(filename, "w") as f:
        json.dump(list(users), f)

registered_users = load_users(REGISTERED_FILE)
banned_users = load_users(BANNED_FILE)

def is_registered(user_id: int) -> bool:
    return user_id in registered_users

def is_banned(user_id: int) -> bool:
    return user_id in banned_users

def register_user(user_id: int) -> None:
    registered_users.add(user_id)
    save_users(REGISTERED_FILE, registered_users)

def ban_user(user_id: int) -> None:
    banned_users.add(user_id)
    save_users(BANNED_FILE, banned_users)

def unban_user(user_id: int) -> None:
    banned_users.discard(user_id)
    save_users(BANNED_FILE, banned_users)

# ─── HELPERS ───────────────────────────────────────────────────────────────────
def now_kyiv() -> datetime:
    return datetime.now(KYIV_TZ)

def now_str() -> str:
    return now_kyiv().strftime("%H:%M %d.%m.%Y")

def uptime_str() -> str:
    if not bot_stats["start_time"]:
        return "невідомо"
    delta = now_kyiv() - bot_stats["start_time"]
    h, rem = divmod(int(delta.total_seconds()), 3600)
    m = rem // 60
    return f"{h}г {m}хв"

def greeting() -> str:
    h = now_kyiv().hour
    if 5  <= h < 12: return "Доброго ранку"
    if 12 <= h < 17: return "Доброго дня"
    if 17 <= h < 22: return "Доброго вечора"
    return "Доброї ночі"

def user_name(update: Update) -> str:
    u = update.effective_user
    return u.first_name if u and u.first_name else "друже"

def alert_status_text() -> str:
    if alert_active is None: return "⏳ перевіряємо..."
    return "🔴 ОГОЛОШЕНА" if alert_active else "🟢 не оголошена"

def is_admin(update: Update) -> bool:
    return update.effective_user.id in ADMIN_IDS

def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚨 Статус тривоги",   callback_data="status"),
         InlineKeyboardButton("🗺 Мапа тривог",       callback_data="map")],
        [InlineKeyboardButton("🏠 Укриття",           callback_data="shelters"),
         InlineKeyboardButton("📞 Екстрені номери",   callback_data="emergency")],
        [InlineKeyboardButton("📜 Правила",           callback_data="rules"),
         InlineKeyboardButton("🌐 Контакти",          callback_data="contacts")],
        [InlineKeyboardButton("🔔 Сповіщення",        callback_data="notifications"),
         InlineKeyboardButton("ℹ️ Про бота",          callback_data="about")],
        [InlineKeyboardButton("📢 Канал новин",       url="https://t.me/yampilnews")],
    ])

# ─── ALERT API ─────────────────────────────────────────────────────────────────
async def fetch_alert_status(session: aiohttp.ClientSession) -> bool:
    headers = {"X-API-Key": ALERT_API_KEY}
    try:
        async with session.get(ALERT_API_URL, headers=headers,
                               timeout=aiohttp.ClientTimeout(total=10)) as resp:
            resp.raise_for_status()
            data = await resp.json()
        for a in data.get("alerts", []):
            if str(a.get("location_uid","")) == TARGET_REGION_ID and a.get("alert_type") == "air_raid":
                return True
        return False
    except Exception as e:
        log.error("API error: %s", e)
        return alert_active or False

async def post_to_channel(bot: Bot, text: str) -> None:
    try:
        await bot.send_message(chat_id=CHANNEL_ID, text=text)
        bot_stats["messages_sent"] += 1
    except TelegramError as e:
        log.error("Telegram error: %s", e)

# ─── ACCESS CONTROL ───────────────────────────────────────────────────────────
async def check_access(update: Update) -> bool:
    uid = update.effective_user.id
    if is_banned(uid):
        await update.message.reply_text("🚫 Вам заборонено використовувати цього бота.")
        return False
    if not is_registered(uid) and not is_admin(uid):
        await update.message.reply_text(
            "👋 Привіт! Ти новий користувач.\n\n"
            "🔐 Спочатку потрібна реєстрація.\n\n"
            "Натисни /register щоб розпочати."
        )
        return False
    return True

# ─── REGISTRATION ──────────────────────────────────────────────────────────────
async def cmd_register(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    if is_registered(uid):
        await update.message.reply_text("✅ Ти вже зареєстрований!")
        return ConversationHandler.END
    if is_banned(uid):
        await update.message.reply_text("🚫 Реєстрація недоступна.")
        return ConversationHandler.END
    await update.message.reply_text("📝 Реєстрація на бот моніторингу тривог\n\n1️⃣ Як тебе звати?")
    return AWAITING_NAME

async def receive_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data["name"] = update.message.text
    await update.message.reply_text("2️⃣ Твій номер телефону?")
    return AWAITING_PHONE

async def receive_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data["phone"] = update.message.text
    await update.message.reply_text("3️⃣ Твоя адреса (селище/вулиця)?")
    return AWAITING_ADDRESS

async def receive_address(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data["address"] = update.message.text
    uid = update.effective_user.id
    register_user(uid)
    await update.message.reply_text(
        "✅ Дякую! Ти успішно зареєстрований.\n\n"
        "Тепер маєш доступ до всіх функцій бота.\n\n"
        "Натисни /start щоб розпочати."
    )
    return ConversationHandler.END

async def cancel_registration(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("❌ Реєстрація скасована.")
    return ConversationHandler.END

# ─── /start ────────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update):
        return
    name   = user_name(update)
    gr     = greeting()
    status = alert_status_text()
    lines = [f"{gr}, {name}! 👋", f"\n📍 Тривога в СМТ Ямпіль: {status}"]
    if alert_active:
        lines += ["", "⚠️ Прошу пройти до найближчого укриття!", "Бережіть себе! 🙏"]
    lines += ["", "Оберіть дію з меню нижче:"]
    await update.message.reply_text("\n".join(lines), reply_markup=main_keyboard())
    bot_stats["messages_sent"] += 1

# ─── /status ───────────────────────────────────────────────────────────────────
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update):
        return
    status = alert_status_text()
    lines = ["🛡 Статус повітряної тривоги", f"📍 Шепетівський р-н (СМТ Ямпіль)",
             f"🕐 {now_str()}", f"Статус: {status}"]
    if alert_active:
        lines += ["", "⚠️ Прошу пройти до найближчого укриття!", "Бережіть себе! 🙏"]
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Оновити", callback_data="status"),
                               InlineKeyboardButton("🗺 Мапа", callback_data="map")]])
    msg = update.message or update.callback_query.message
    await msg.reply_text("\n".join(lines), reply_markup=kb)

# ─── /admin ────────────────────────────────────────────────────────────────────
async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        await update.message.reply_text("🚫 У вас немає прав адміністратора.")
        return
    name = user_name(update)
    text = (f"🔐 ПАНЕЛЬ АДМІНІСТРАТОРА\n{'='*40}\n\n"
            f"👤 Адміністратор: {name}\n🕐 Час: {now_str()}\n\n"
            f"📊 СТАТУС СИСТЕМИ:\n{'─'*40}\n"
            f"Поточний статус: {'🔴 ТРИВОГА' if alert_active else '🟢 СПОКІЙНО'}\n"
            f"⏱ Аптайм: {uptime_str()}\n\n📈 СТАТИСТИКА:\n{'─'*40}\n"
            f"🚨 Тривог оголошено: {bot_stats['alerts_count']}\n"
            f"✅ Відбоїв: {bot_stats['allclear_count']}\n"
            f"📨 Повідомлень надіслано: {bot_stats['messages_sent']}\n"
            f"👥 Зареєстровано користувачів: {len(registered_users)}\n"
            f"🚫 Заблокованих: {len(banned_users)}\n\n"
            f"📍 API СТАТУС: ✅ Онлайн (alerts.in.ua)")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Оголошення в канал", callback_data="admin_post_prompt")],
        [InlineKeyboardButton("🚨 Тестова тривога", callback_data="admin_test_alert"),
         InlineKeyboardButton("✅ Тестовий відбій", callback_data="admin_test_clear")],
        [InlineKeyboardButton("👥 Користувачі", callback_data="admin_list"),
         InlineKeyboardButton("📊 Деталі", callback_data="admin_details")],
        [InlineKeyboardButton("🚫 Блокування", callback_data="admin_ban"),
         InlineKeyboardButton("🔓 Розблокування", callback_data="admin_unban")],
        [InlineKeyboardButton("🔙 Закрити", callback_data="admin_close")],
    ])
    await update.message.reply_text(text, reply_markup=kb)

# ─── /myid ─────────────────────────────────────────────────────────────────────
async def cmd_myid(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    name = user_name(update)
    status = "🔐 Адміністратор" if is_admin(update) else ("✅ Зареєстрований" if is_registered(uid) else "❌ Не зареєстрований")
    await update.message.reply_text(f"👤 {name}\n🆔 Твій ID: `{uid}`\n{status}", parse_mode="Markdown")

# ─── /help ─────────────────────────────────────────────────────────────────────
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update):
        return
    text = ("📋 Всі команди:\n\n🔹 Основні:\n/start /status /map /alerts\\_ua\n\n"
            "🔹 Інформація:\n/shelters /emergency /rules /contacts\n"
            "/notifications /about /bot\\_stats\n\n"
            "🔹 Облік:\n/register /myid\n\n"
            "🔐 Адміністрування:\n/admin /post /ban /unban")
    await update.message.reply_text(text, parse_mode="Markdown")

# ─── CALLBACK BUTTONS ──────────────────────────────────────────────────────────
async def button_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data == "status":
        status = alert_status_text()
        lines = ["🛡 Статус повітряної тривоги", f"📍 Шепетівський р-н (СМТ Ямпіль)",
                f"🕐 {now_str()}", f"Статус: {status}"]
        if alert_active:
            lines += ["", "⚠️ Прошу пройти до найближчого укриття!", "Бережіть себе! 🙏"]
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Оновити", callback_data="status"),
                                  InlineKeyboardButton("🗺 Мапа", callback_data="map")]])
        await query.message.reply_text("\n".join(lines), reply_markup=kb)
    
    elif data == "admin_list" and is_admin(update):
        text = f"👥 Зареєстровано користувачів: {len(registered_users)}\n\n"
        if banned_users:
            text += f"🚫 Заблокованих: {len(banned_users)}\n"
        await query.message.reply_text(text)
    
    elif data == "admin_test_alert" and is_admin(update):
        text = f"‼️УВАГА ПОВІТРЯНА ТРИВОГА‼️\n\nСтаном на {now_str()}, в ОТГ селища Ямпіль, була оголошена повітряна тривога.\n\nБЕРЕЖІТЬ СЕБЕ!\n\n🔧 [ТЕСТОВЕ ПОВІДОМЛЕННЯ]"
        await post_to_channel(ctx.bot, text)
        await query.message.reply_text("✅ Тестова тривога надіслана в канал.")
    
    elif data == "admin_close" and is_admin(update):
        await query.message.delete()

# ─── ALERT LOOP ────────────────────────────────────────────────────────────────
async def alert_check_loop(bot: Bot, session: aiohttp.ClientSession) -> None:
    global alert_active
    log.info("Alert loop запущено")
    while True:
        try:
            current = await fetch_alert_status(session)
            if alert_active is None:
                alert_active = current
                log.info("Початковий стан: %s", current)
            elif current and not alert_active:
                alert_active = True
                bot_stats["alerts_count"] += 1
                text = f"‼️УВАГА ПОВІТРЯНА ТРИВОГА‼️\n\nСтаном на {now_str()}, в ОТГ селища Ямпіль, була оголошена повітряна тривога.\n\nБЕРЕЖІТЬ СЕБЕ!"
                await post_to_channel(bot, text)
                try:
                    await bot.send_photo(chat_id=CHANNEL_ID, photo=MAP_IMAGE_URL,
                                        caption=f"🗺 Мапа тривог | {now_str()}\n{MAP_URL}")
                except Exception as e:
                    log.warning("Не вдалось надіслати мапу: %s", e)
            elif not current and alert_active:
                alert_active = False
                bot_stats["allclear_count"] += 1
                await post_to_channel(bot,
                    f"❕ВІДБІЙ ПОВІТРЯНОЇ ТРИВОГИ❕\n\nСтаном на {now_str()} був оголошений відбій повітряної тривоги.")
        except Exception as e:
            log.error("Alert loop error: %s", e)
        await asyncio.sleep(CHECK_INTERVAL)

# ─── HTTP WEBHOOK ──────────────────────────────────────────────────────────────
async def handle_webhook(request):
    """Webhook endpoint для Telegram."""
    try:
        data = await request.json()
        update = Update.de_json(data, None)
        await app.process_update(update)
        return web.Response(status=200)
    except Exception as e:
        log.error("Webhook error: %s", e)
        return web.Response(status=500)

async def health_check(request):
    return web.json_response({"status": "ok", "alert_active": alert_active, "time": now_str()})

# ─── SETUP ─────────────────────────────────────────────────────────────────────
app = None

async def setup_app():
    global app
    bot_stats["start_time"] = now_kyiv()
    
    app = Application.builder().token(BOT_TOKEN).build()
    
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("register", cmd_register)],
        states={
            AWAITING_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_name)],
            AWAITING_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_phone)],
            AWAITING_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_address)],
        },
        fallbacks=[CommandHandler("cancel", cancel_registration)],
    )
    
    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("myid", cmd_myid))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CallbackQueryHandler(button_handler))
    
    log.info("Telegram Application створена")
    return app

# ─── MAIN ──────────────────────────────────────────────────────────────────────
async def main():
    global app
    app = await setup_app()
    
    # Setup webhook
    await app.bot.set_webhook(url=WEBHOOK_URL, drop_pending_updates=True)
    log.info(f"Webhook встановлений на {WEBHOOK_URL}")
    
    # Start alert loop
    async with aiohttp.ClientSession() as session:
        asyncio.create_task(alert_check_loop(app.bot, session))
        
        # HTTP сервер
        web_app = web.Application()
        web_app.router.add_post("/webhook", handle_webhook)
        web_app.router.add_get("/health", health_check)
        web_app.router.add_get("/", health_check)
        
        runner = web.AppRunner(web_app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", PORT)
        await site.start()
        
        log.info(f"HTTP сервер запущено на порту {PORT}")
        log.info(f"Webhook: {WEBHOOK_URL}")
        log.info(f"Адмінів: {len(ADMIN_IDS)}, Зареєстровано: {len(registered_users)}")
        
        try:
            await asyncio.Event().wait()
        except KeyboardInterrupt:
            log.info("Завершення роботи...")

if __name__ == "__main__":
    asyncio.run(main())
