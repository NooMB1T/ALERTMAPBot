import asyncio
import aiohttp
import logging
from datetime import datetime, timezone, timedelta
import os

from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes
)
from telegram.error import TelegramError
from aiohttp import web

# ─── CONFIG ────────────────────────────────────────────────────────────────────
BOT_TOKEN    = os.getenv("BOT_TOKEN",    "8693341837:AAF2lK6bGR3uoLz1kfkZt8IjDQIF18YXHN8")
CHANNEL_ID   = os.getenv("CHANNEL_ID",   "@yampilnews")
ALERT_API_KEY = os.getenv("ALERT_API_KEY","b3de42c9:736017aa6745a605c155108e221d31a8")
TARGET_REGION_ID = "31004"          # Шепетівський р-н
ALERT_API_URL = "https://api.alerts.in.ua/v1/alerts/active.json"
CHECK_INTERVAL = 30
KYIV_TZ = timezone(timedelta(hours=3))
PORT = int(os.getenv("PORT", 10000))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

alert_active: bool | None = None   # глобальний стан тривоги

# ─── HELPERS ───────────────────────────────────────────────────────────────────
def now_kyiv() -> datetime:
    return datetime.now(KYIV_TZ)

def now_str() -> str:
    return now_kyiv().strftime("%H:%M %d.%m.%Y")

def greeting() -> str:
    h = now_kyiv().hour
    if 5 <= h < 12:  return "Доброго ранку"
    if 12 <= h < 17: return "Доброго дня"
    if 17 <= h < 22: return "Доброго вечора"
    return "Доброї ночі"

def user_name(update: Update) -> str:
    u = update.effective_user
    return u.first_name if u and u.first_name else "друже"

def alert_status_text() -> str:
    if alert_active is None:
        return "невідомо (ще перевіряємо)"
    if alert_active:
        return "🔴 ОГОЛОШЕНА"
    return "🟢 не оголошена"

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
    except TelegramError as e:
        log.error("Telegram error: %s", e)

# ─── /start ────────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    name = user_name(update)
    gr   = greeting()
    status = alert_status_text()

    lines = [
        f"{gr}, {name}!",
        f"Наразі тривога в СМТ Ямпіль — {status}",
    ]
    if alert_active:
        lines += ["", "⚠️ Прошу пройти до найближчого укриття!", "Бережіть себе! 🙏"]

    lines += [
        "",
        "Я — бот моніторингу повітряних тривог для СМТ Ямпіль.",
        "Ось що я вмію:",
    ]

    keyboard = [
        [InlineKeyboardButton("🚨 Статус тривоги",    callback_data="status")],
        [InlineKeyboardButton("📋 Команди",            callback_data="help")],
        [InlineKeyboardButton("📡 Канал новин",        url="https://t.me/yampilnews")],
        [InlineKeyboardButton("🗺 Найближчі укриття",  callback_data="shelters")],
        [InlineKeyboardButton("📞 Екстрені номери",    callback_data="emergency")],
        [InlineKeyboardButton("ℹ️ Про бота",           callback_data="about")],
    ]
    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ─── /status ───────────────────────────────────────────────────────────────────
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    status = alert_status_text()
    lines = [
        f"🕐 Час: {now_str()}",
        f"📍 Регіон: Шепетівський р-н (СМТ Ямпіль)",
        f"Тривога: {status}",
    ]
    if alert_active:
        lines += ["", "⚠️ Прошу пройти до найближчого укриття!", "Бережіть себе! 🙏"]
    await update.message.reply_text("\n".join(lines))

# ─── /help ─────────────────────────────────────────────────────────────────────
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "📋 Список команд:\n\n"
        "/start — головне меню\n"
        "/status — поточний статус тривоги\n"
        "/shelters — адреси найближчих укриттів\n"
        "/emergency — екстрені телефони\n"
        "/rules — правила поведінки під час тривоги\n"
        "/time — поточний час (Київ)\n"
        "/about — інформація про бота\n"
    )
    await update.message.reply_text(text)

# ─── /shelters ─────────────────────────────────────────────────────────────────
async def cmd_shelters(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "🗺 Найближчі укриття в СМТ Ямпіль:\n\n"
        "1️⃣ Підвал Ямпільської гімназії\n"
        "   📍 вул. Шкільна, 1\n\n"
        "2️⃣ Підвал Будинку культури\n"
        "   📍 вул. Центральна\n\n"
        "3️⃣ Підвальне приміщення амбулаторії\n"
        "   📍 вул. Медична\n\n"
        "4️⃣ Підвал адміністративного будинку ОТГ\n"
        "   📍 вул. Незалежності\n\n"
        "⚠️ У разі тривоги — рухайтесь до найближчого укриття!\n"
        "Уточнюйте адреси в місцевій адміністрації."
    )
    await (update.message or update.callback_query.message).reply_text(text)

# ─── /emergency ────────────────────────────────────────────────────────────────
async def cmd_emergency(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "📞 Екстрені телефони:\n\n"
        "🚒 Пожежна охорона: 101\n"
        "🚔 Поліція: 102\n"
        "🚑 Швидка допомога: 103\n"
        "🛡 ДСНС (рятувальники): 104\n"
        "☎️ Єдиний екстрений: 112\n\n"
        "🏛 Ямпільська ОТГ:\n"
        "   (уточнюйте номер на сайті ОТГ)\n\n"
        "📻 Слідкуйте за офіційними каналами!"
    )
    await (update.message or update.callback_query.message).reply_text(text)

# ─── /rules ────────────────────────────────────────────────────────────────────
async def cmd_rules(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "📜 Правила поведінки під час тривоги:\n\n"
        "1️⃣ Почувши сигнал — негайно припиніть всі справи\n"
        "2️⃣ Перейдіть до найближчого укриття або підвалу\n"
        "3️⃣ Якщо укриття немає — ляжте на підлогу біля несучої стіни\n"
        "4️⃣ Тримайтесь подалі від вікон і скла\n"
        "5️⃣ Вимкніть газ, електроприлади (якщо є час)\n"
        "6️⃣ Візьміть документи, воду, ліки, телефон\n"
        "7️⃣ Не виходьте з укриття до сигналу відбою\n"
        "8️⃣ Не поширюйте паніку — допоможіть іншим\n\n"
        "🙏 Бережіть себе і близьких!"
    )
    await update.message.reply_text(text)

# ─── /time ─────────────────────────────────────────────────────────────────────
async def cmd_time(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    now = now_kyiv()
    weekdays = ["Понеділок","Вівторок","Середа","Четвер","П'ятниця","Субота","Неділя"]
    text = (
        f"🕐 Поточний час:\n\n"
        f"⏰ {now.strftime('%H:%M:%S')}\n"
        f"📅 {weekdays[now.weekday()]}, {now.strftime('%d.%m.%Y')}\n"
        f"🌍 Часовий пояс: Київ (UTC+3)"
    )
    await update.message.reply_text(text)

# ─── /about ────────────────────────────────────────────────────────────────────
async def cmd_about(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "ℹ️ Про бота:\n\n"
        "🤖 Бот моніторингу повітряних тривог\n"
        "📍 СМТ Ямпіль, Шепетівський р-н\n"
        "   Хмельницька область\n\n"
        "⚡️ Що робить:\n"
        "• Автоматично публікує тривоги в канал\n"
        "• Перевіряє статус кожні 30 секунд\n"
        "• Надає корисну інформацію\n\n"
        "📡 Дані: alerts.in.ua\n"
        "📢 Канал: @yampilnews\n\n"
        "Слава Україні! 🇺🇦"
    )
    await (update.message or update.callback_query.message).reply_text(text)

# ─── CALLBACK BUTTONS ──────────────────────────────────────────────────────────
async def button_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "status":
        status = alert_status_text()
        lines = [f"🕐 {now_str()}", f"Тривога: {status}"]
        if alert_active:
            lines += ["", "⚠️ Прошу пройти до найближчого укриття!", "Бережіть себе! 🙏"]
        await query.message.reply_text("\n".join(lines))

    elif data == "help":
        await cmd_help(update, ctx)

    elif data == "shelters":
        await cmd_shelters(update, ctx)

    elif data == "emergency":
        await cmd_emergency(update, ctx)

    elif data == "about":
        await cmd_about(update, ctx)

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
                await post_to_channel(bot,
                    f"‼️УВАГА ПОВІТРЯНА ТРИВОГА‼️\n\n"
                    f"Станом на {now_str()}, в ОТГ селища Ямпіль, "
                    f"була оголошена повітряна тривога.\n\n"
                    f"БЕРЕЖІТЬ СЕБЕ!")
            elif not current and alert_active:
                alert_active = False
                await post_to_channel(bot,
                    f"❕ВІДБІЙ ПОВІТРЯНОЇ ТРИВОГИ❕\n\n"
                    f"Станом на {now_str()} був оголошений відбій повітряної тривоги.")
        except Exception as e:
            log.error("Alert loop error: %s", e)
        await asyncio.sleep(CHECK_INTERVAL)

# ─── HTTP KEEPALIVE ────────────────────────────────────────────────────────────
async def health_check(request):
    return web.json_response({"status": "ok", "alert_active": alert_active, "time": now_str()})

async def run_http(app_ref):
    app = web.Application()
    app.router.add_get("/health", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("HTTP сервер на порту %d", PORT)

# ─── MAIN ──────────────────────────────────────────────────────────────────────
async def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("status",    cmd_status))
    app.add_handler(CommandHandler("help",      cmd_help))
    app.add_handler(CommandHandler("shelters",  cmd_shelters))
    app.add_handler(CommandHandler("emergency", cmd_emergency))
    app.add_handler(CommandHandler("rules",     cmd_rules))
    app.add_handler(CommandHandler("time",      cmd_time))
    app.add_handler(CommandHandler("about",     cmd_about))
    app.add_handler(CallbackQueryHandler(button_handler))

    async with aiohttp.ClientSession() as session:
        bot = app.bot
        await run_http(None)
        asyncio.create_task(alert_check_loop(bot, session))

        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        log.info("Бот запущено!")
        await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
    
