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
from aiohttp import web

# ─── CONFIG ────────────────────────────────────────────────────────────────────
BOT_TOKEN     = os.getenv("BOT_TOKEN",     "8693341837:AAF2lK6bGR3uoLz1kfkZt8IjDQIF18YXHN8")
CHANNEL_ID    = os.getenv("CHANNEL_ID",    "@yampilnews")
ALERT_API_KEY = os.getenv("ALERT_API_KEY", "b3de42c9:736017aa6745a605c155108e221d31a8")
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "")
ADMIN_IDS     = set(int(x.strip()) for x in ADMIN_IDS_RAW.split(",") if x.strip().isdigit())

TARGET_REGION_ID  = "31004"
ALERT_API_URL     = "https://api.alerts.in.ua/v1/alerts/active.json"
ALL_ALERTS_URL    = "https://api.alerts.in.ua/v1/alerts/active.json"
CHECK_INTERVAL    = 30
KYIV_TZ           = timezone(timedelta(hours=3))
PORT              = int(os.getenv("PORT", 10000))

MAP_URL = "https://alerts.in.ua/"
MAP_IMAGE_URL = "https://alerts.in.ua/map.png"

# Реєстрація + banned users
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

async def fetch_all_alerts(session: aiohttp.ClientSession) -> list:
    headers = {"X-API-Key": ALERT_API_KEY}
    try:
        async with session.get(ALL_ALERTS_URL, headers=headers,
                               timeout=aiohttp.ClientTimeout(total=10)) as resp:
            resp.raise_for_status()
            data = await resp.json()
        return [a for a in data.get("alerts", []) if a.get("alert_type") == "air_raid"]
    except Exception as e:
        log.error("fetch_all_alerts error: %s", e)
        return []

async def post_to_channel(bot: Bot, text: str) -> None:
    try:
        await bot.send_message(chat_id=CHANNEL_ID, text=text)
        bot_stats["messages_sent"] += 1
    except TelegramError as e:
        log.error("Telegram error: %s", e)

# ─── ACCESS CONTROL ───────────────────────────────────────────────────────────
async def check_access(update: Update) -> bool:
    """Перевіряє чи користувач має доступ."""
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

# ─── REGISTRATION FLOW ─────────────────────────────────────────────────────────
async def cmd_register(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    if is_registered(uid):
        await update.message.reply_text("✅ Ти вже зареєстрований!")
        return ConversationHandler.END
    if is_banned(uid):
        await update.message.reply_text("🚫 Реєстрація недоступна.")
        return ConversationHandler.END

    await update.message.reply_text(
        "📝 Реєстрація на бот моніторингу тривог\n\n"
        "1️⃣ Як тебе звати?"
    )
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
    
    # Реєструємо користувача
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
    lines  = [
        "🛡 Статус повітряної тривоги",
        f"📍 Шепетівський р-н (СМТ Ямпіль)",
        f"🕐 {now_str()}",
        f"Статус: {status}",
    ]
    if alert_active:
        lines += ["", "⚠️ Прошу пройти до найближчого укриття!", "Бережіть себе! 🙏"]
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Оновити", callback_data="status"),
        InlineKeyboardButton("🗺 Мапа", callback_data="map"),
    ]])
    msg = update.message or update.callback_query.message
    await msg.reply_text("\n".join(lines), reply_markup=kb)

# ─── /map ──────────────────────────────────────────────────────────────────────
async def cmd_map(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update):
        return
    
    msg = update.message or (update.callback_query.message if update.callback_query else None)
    status = alert_status_text()

    caption = (
        f"🗺 Мапа повітряних тривог України\n"
        f"🕐 {now_str()}\n"
        f"📍 СМТ Ямпіль (Шепетівський р-н): {status}\n\n"
        f"🔴 — тривога оголошена\n"
        f"🟢 — тривоги немає\n\n"
        f"Актуальна мапа онлайн: {MAP_URL}"
    )

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🌐 Відкрити мапу онлайн", url=MAP_URL),
        InlineKeyboardButton("🔄 Оновити", callback_data="map"),
    ]])

    try:
        await msg.reply_photo(photo=MAP_IMAGE_URL, caption=caption, reply_markup=kb)
    except Exception:
        await msg.reply_text(caption, reply_markup=kb)

# ─── /alerts_ua ────────────────────────────────────────────────────────────────
async def cmd_alerts_ua(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update):
        return
    
    msg = update.message
    await msg.reply_text("⏳ Завантажую список тривог...")

    session: aiohttp.ClientSession = ctx.bot_data.get("session")
    if not session:
        await msg.reply_text("❌ Помилка з'єднання з API")
        return

    alerts = await fetch_all_alerts(session)

    if not alerts:
        await msg.reply_text("🟢 Наразі активних повітряних тривог в Україні немає.")
        return

    region_names = {
        "1": "Вінницька", "2": "Волинська", "3": "Дніпропетровська",
        "4": "Донецька", "5": "Житомирська", "6": "Закарпатська",
        "7": "Запорізька", "8": "Івано-Франківська", "9": "Київська",
        "10": "Кіровоградська", "11": "Луганська", "12": "Львівська",
        "13": "Миколаївська", "14": "Одеська", "15": "Полтавська",
        "16": "Рівненська", "17": "Сумська", "18": "Тернопільська",
        "19": "Харківська", "20": "Херсонська", "21": "Хмельницька",
        "22": "Черкаська", "23": "Чернівецька", "24": "Чернігівська",
        "25": "м. Київ", "31004": "Шепетівський р-н (Хмельницька)",
    }

    lines = [f"🚨 Активні тривоги в Україні ({now_str()}):\n"]
    for a in alerts[:20]:
        uid  = str(a.get("location_uid", ""))
        name = region_names.get(uid, f"Регіон {uid}")
        lines.append(f"🔴 {name}")

    lines.append(f"\nВсього регіонів з тривогою: {len(alerts)}")
    lines.append(f"\n🗺 Мапа: {MAP_URL}")

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🗺 Відкрити мапу", url=MAP_URL)
    ]])
    await msg.reply_text("\n".join(lines), reply_markup=kb)

# ─── /shelters ─────────────────────────────────────────────────────────────────
async def cmd_shelters(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update):
        return
    
    text = (
        "🏠 Найближчі укриття в СМТ Ямпіль:\n\n"
        "1️⃣ Підвал Ямпільської гімназії\n"
        "   📍 вул. Шкільна, 1\n\n"
        "2️⃣ Підвал Будинку культури\n"
        "   📍 вул. Центральна\n\n"
        "3️⃣ Підвальне приміщення амбулаторії\n"
        "   📍 вул. Медична\n\n"
        "4️⃣ Підвал адміністративного будинку ОТГ\n"
        "   📍 вул. Незалежності\n\n"
        "⚠️ У разі тривоги — рухайтесь до найближчого укриття!\n"
        "📞 Уточнюйте адреси: 104 або місцева адміністрація"
    )
    msg = update.message or update.callback_query.message
    await msg.reply_text(text)

# ─── /emergency ────────────────────────────────────────────────────────────────
async def cmd_emergency(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update):
        return
    
    text = (
        "📞 Екстрені телефони:\n\n"
        "🚒 Пожежна охорона: 101\n"
        "🚔 Поліція: 102\n"
        "🚑 Швидка допомога: 103\n"
        "🛡 ДСНС (рятувальники): 104\n"
        "☎️ Єдиний екстрений: 112\n\n"
        "🏛 Ямпільська ОТГ:\n"
        "   Уточнюйте на сайті ОТГ\n\n"
        "🇺🇦 Гаряча лінія МО України:\n"
        "   1580 (безкоштовно)\n\n"
        "📻 Слідкуйте за офіційними джерелами!"
    )
    msg = update.message or update.callback_query.message
    await msg.reply_text(text)

# ─── /rules ────────────────────────────────────────────────────────────────────
async def cmd_rules(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update):
        return
    
    text = (
        "📜 Правила поведінки під час тривоги:\n\n"
        "1️⃣ Почувши сигнал — негайно припиніть всі справи\n"
        "2️⃣ Перейдіть до найближчого укриття або підвалу\n"
        "3️⃣ Якщо укриття немає — ляжте біля несучої стіни\n"
        "4️⃣ Тримайтесь подалі від вікон і скла\n"
        "5️⃣ Вимкніть газ, електроприлади (якщо є час)\n"
        "6️⃣ Візьміть документи, воду, ліки, телефон\n"
        "7️⃣ Не виходьте з укриття до сигналу відбою\n"
        "8️⃣ Не поширюйте паніку — допоможіть іншим\n\n"
        "🏃 Дії після вибуху поблизу:\n"
        "• Ляжте на підлогу, прикрийте голову\n"
        "• Не підходьте до вікон\n"
        "• Чекайте команди рятувальників\n\n"
        "🙏 Бережіть себе і близьких!"
    )
    msg = update.message or update.callback_query.message
    await msg.reply_text(text)

# ─── /contacts ─────────────────────────────────────────────────────────────────
async def cmd_contacts(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update):
        return
    
    text = (
        "📍 Контакти СМТ Ямпіль:\n\n"
        "🏛 Ямпільська селищна громада\n"
        "   📞 (0385) XX-XX-XX\n"
        "   🌐 https://yampil-gmada.gov.ua/\n\n"
        "📍 Адреса: м. Ямпіль, вул. Незалежності\n\n"
        "🚨 Місцева поліція:\n"
        "   📞 (0385) XX-XX-XX\n\n"
        "🏥 Медичний заклад:\n"
        "   📞 (0385) XX-XX-XX\n\n"
        "📬 Пошта:\n"
        "   🕒 Пн-Пт: 09:00-17:00\n"
        "   🕐 Сб: 09:00-14:00\n\n"
        "ℹ️ Уточнюйте актуальні номери в местевої адміністрації"
    )
    msg = update.message or update.callback_query.message
    await msg.reply_text(text)

# ─── /notifications ────────────────────────────────────────────────────────────
async def cmd_notifications(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update):
        return
    
    text = (
        "🔔 Налаштування сповіщень:\n\n"
        "✅ Ти отримуватимеш сповіщення про:\n"
        "• Оголошення повітряної тривоги\n"
        "• Відбій повітряної тривоги\n"
        "• Важливі оголошення ОТГ\n"
        "• Новини і оновлення\n\n"
        "💡 Поради:\n"
        "• Активуй звук для критичних сповіщень\n"
        "• Не вимикай уведомлення бота\n"
        "• Ділися інформацією з сусідами\n\n"
        "📢 Підпишись на канал: @yampilnews"
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("📢 Перейти на канал", url="https://t.me/yampilnews")
    ]])
    msg = update.message or update.callback_query.message
    await msg.reply_text(text, reply_markup=kb)

# ─── /about ────────────────────────────────────────────────────────────────────
async def cmd_about(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update):
        return
    
    text = (
        "ℹ️ Про бота:\n\n"
        "🤖 Бот моніторингу повітряних тривог\n"
        "📍 СМТ Ямпіль, Шепетівський р-н\n"
        "   Хмельницька область\n\n"
        "⚡️ Що вмію:\n"
        "• Слідкую за тривогами 24/7\n"
        "• Публікую в канал автоматично\n"
        "• Показую мапу тривог\n"
        "• Надаю корисну інформацію\n"
        "• Повідомляю про тривоги по всій Україні\n"
        "• Реєстрація користувачів\n\n"
        "📡 Дані: alerts.in.ua\n"
        "📢 Канал: @yampilnews\n\n"
        f"⏱ Аптайм: {uptime_str()}\n"
        f"🚨 Тривог оголошено: {bot_stats['alerts_count']}\n"
        f"📨 Повідомлень надіслано: {bot_stats['messages_sent']}\n\n"
        "Слава Україні! 🇺🇦"
    )
    msg = update.message or update.callback_query.message
    await msg.reply_text(text)

# ─── /help ─────────────────────────────────────────────────────────────────────
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update):
        return
    
    text = (
        "📋 Всі команди:\n\n"
        "🔹 Основні:\n"
        "/start — головне меню\n"
        "/status — статус тривоги зараз\n"
        "/map — мапа тривог України\n"
        "/alerts\\_ua — список тривог по Україні\n\n"
        "🔹 Інформація:\n"
        "/shelters — найближчі укриття\n"
        "/emergency — екстрені телефони\n"
        "/rules — правила під час тривоги\n"
        "/contacts — контакти ОТГ\n"
        "/notifications — про сповіщення\n"
        "/about — про бота\n\n"
        "🔹 Облік:\n"
        "/register — реєстрація\n"
        "/myid — твій ID\n\n"
        "🔐 Адміністрування:\n"
        "/admin — панель адміністратора\n"
        "/post <текст> — опублікувати в канал\n"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

# ─── /myid ─────────────────────────────────────────────────────────────────────
async def cmd_myid(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid  = update.effective_user.id
    name = user_name(update)
    status = "🔐 Адміністратор" if is_admin(update) else ("✅ Зареєстрований" if is_registered(uid) else "❌ Не зареєстрований")
    await update.message.reply_text(
        f"👤 {name}\n🆔 Твій ID: `{uid}`\n{status}",
        parse_mode="Markdown"
    )

# ─── ADMIN: /admin ─────────────────────────────────────────────────────────────
async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        await update.message.reply_text("🚫 У вас немає прав адміністратора.")
        return

    name = user_name(update)
    text = (
        f"🔐 Панель адміністратора\n"
        f"👤 {name}\n"
        f"🕐 {now_str()}\n\n"
        f"📊 Статус: {'🔴 Тривога' if alert_active else '🟢 Спокійно'}\n"
        f"⏱ Аптайм: {uptime_str()}\n"
        f"🚨 Тривог: {bot_stats['alerts_count']}\n"
        f"📨 Повідомлень: {bot_stats['messages_sent']}\n"
        f"👥 Зареєстровано користувачів: {len(registered_users)}"
    )

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Написати в канал",     callback_data="admin_post_prompt")],
        [InlineKeyboardButton("🚨 Тестова тривога",      callback_data="admin_test_alert"),
         InlineKeyboardButton("✅ Тестовий відбій",      callback_data="admin_test_clear")],
        [InlineKeyboardButton("👥 Список користувачів",  callback_data="admin_list"),
         InlineKeyboardButton("🚫 Заблокувати",          callback_data="admin_ban")],
        [InlineKeyboardButton("🔄 Розблокувати",         callback_data="admin_unban")],
    ])
    
    # Надсилаємо панель адміну в приватний чат
    await ctx.bot.send_message(
        chat_id=update.effective_user.id,
        text=text,
        reply_markup=kb
    )

# ─── ADMIN: /post ──────────────────────────────────────────────────────────────
async def cmd_post(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        await update.message.reply_text("🚫 У вас немає прав адміністратора.")
        return

    text = " ".join(ctx.args)
    if not text:
        await update.message.reply_text(
            "✍️ Використання: /post <текст повідомлення>\n\n"
            "Приклад:\n/post Увага! Сьогодні о 15:00 збори мешканців."
        )
        return

    full_text = f"📢 Оголошення:\n\n{text}\n\n🕐 {now_str()}"
    await post_to_channel(ctx.bot, full_text)
    await update.message.reply_text("✅ Повідомлення опубліковано в канал!")

# ─── CALLBACK BUTTONS ──────────────────────────────────────────────────────────
async def button_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data  = query.data

    if data == "status":       await cmd_status(update, ctx)
    elif data == "map":        await cmd_map(update, ctx)
    elif data == "shelters":   await cmd_shelters(update, ctx)
    elif data == "emergency":  await cmd_emergency(update, ctx)
    elif data == "rules":      await cmd_rules(update, ctx)
    elif data == "contacts":   await cmd_contacts(update, ctx)
    elif data == "notifications": await cmd_notifications(update, ctx)
    elif data == "about":      await cmd_about(update, ctx)

    # Адмін кнопки - тільки для адміна
    elif data == "admin_list":
        if not is_admin(update):
            await query.message.reply_text("🚫 Немає прав.")
            return
        text = f"👥 Зареєстровано користувачів: {len(registered_users)}\n\n"
        if banned_users:
            text += f"🚫 Заблокованих: {len(banned_users)}\n"
        await query.message.reply_text(text)

    elif data == "admin_test_alert":
        if not is_admin(update):
            await query.message.reply_text("🚫 Немає прав.")
            return
        text = (
            f"‼️УВАГА ПОВІТРЯНА ТРИВОГА‼️\n\n"
            f"Станом на {now_str()}, в ОТГ селища Ямпіль, "
            f"була оголошена повітряна тривога.\n\nБЕРЕЖІТЬ СЕБЕ!\n\n"
            f"🔧 [ТЕСТОВЕ ПОВІДОМЛЕННЯ]"
        )
        await post_to_channel(ctx.bot, text)
        await query.message.reply_text("✅ Тестова тривога надіслана в канал.")

    elif data == "admin_test_clear":
        if not is_admin(update):
            await query.message.reply_text("🚫 Немає прав.")
            return
        text = (
            f"❕ВІДБІЙ ПОВІТРЯНОЇ ТРИВОГИ❕\n\n"
            f"Станом на {now_str()} був оголошений відбій повітряної тривоги.\n\n"
            f"🔧 [ТЕСТОВЕ ПОВІДОМЛЕННЯ]"
        )
        await post_to_channel(ctx.bot, text)
        await query.message.reply_text("✅ Тестовий відбій надіслано в канал.")

    elif data == "admin_ban":
        if not is_admin(update):
            await query.message.reply_text("🚫 Немає прав.")
            return
        await query.message.reply_text("🚫 Введи: /ban <user_id>")

    elif data == "admin_unban":
        if not is_admin(update):
            await query.message.reply_text("🚫 Немає прав.")
            return
        await query.message.reply_text("✅ Введи: /unban <user_id>")

    elif data == "admin_post_prompt":
        if not is_admin(update):
            await query.message.reply_text("🚫 Немає прав.")
            return
        await query.message.reply_text("✍️ Введіть команду:\n/post <текст повідомлення>")

# ─── ADMIN COMMANDS ────────────────────────────────────────────────────────────
async def cmd_ban(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        await update.message.reply_text("🚫 Немає прав.")
        return
    if not ctx.args or not ctx.args[0].isdigit():
        await update.message.reply_text("Використання: /ban <user_id>")
        return
    uid = int(ctx.args[0])
    ban_user(uid)
    await update.message.reply_text(f"🚫 Користувач {uid} заблокований.")

async def cmd_unban(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        await update.message.reply_text("🚫 Немає прав.")
        return
    if not ctx.args or not ctx.args[0].isdigit():
        await update.message.reply_text("Використання: /unban <user_id>")
        return
    uid = int(ctx.args[0])
    unban_user(uid)
    await update.message.reply_text(f"✅ Користувач {uid} розблокований.")

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
                text = (
                    f"‼️УВАГА ПОВІТРЯНА ТРИВОГА‼️\n\n"
                    f"Станом на {now_str()}, в ОТГ селища Ямпіль, "
                    f"була оголошена повітряна тривога.\n\nБЕРЕЖІТЬ СЕБЕ!"
                )
                await post_to_channel(bot, text)
                try:
                    await bot.send_photo(
                        chat_id=CHANNEL_ID,
                        photo=MAP_IMAGE_URL,
                        caption=f"🗺 Мапа тривог | {now_str()}\n{MAP_URL}"
                    )
                except Exception as e:
                    log.warning("Не вдалось надіслати мапу: %s", e)

            elif not current and alert_active:
                alert_active = False
                bot_stats["allclear_count"] += 1
                await post_to_channel(bot,
                    f"❕ВІДБІЙ ПОВІТРЯНОЇ ТРИВОГИ❕\n\n"
                    f"Станом на {now_str()} був оголошений відбій повітряної тривоги.")
        except Exception as e:
            log.error("Alert loop error: %s", e)
        await asyncio.sleep(CHECK_INTERVAL)

# ─── HTTP KEEPALIVE ────────────────────────────────────────────────────────────
async def health_check(request):
    return web.json_response({"status": "ok", "alert_active": alert_active, "time": now_str()})

async def run_http():
    app = web.Application()
    app.router.add_get("/health", health_check)
    app.router.add_get("/", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("HTTP сервер на порту %d", PORT)

# ─── MAIN ──────────────────────────────────────────────────────────────────────
async def main() -> None:
    bot_stats["start_time"] = now_kyiv()

    app = Application.builder().token(BOT_TOKEN).build()

    # Conversation для реєстрації
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

    # Команди
    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("status",     cmd_status))
    app.add_handler(CommandHandler("map",        cmd_map))
    app.add_handler(CommandHandler("alerts_ua",  cmd_alerts_ua))
    app.add_handler(CommandHandler("shelters",   cmd_shelters))
    app.add_handler(CommandHandler("emergency",  cmd_emergency))
    app.add_handler(CommandHandler("rules",      cmd_rules))
    app.add_handler(CommandHandler("contacts",   cmd_contacts))
    app.add_handler(CommandHandler("notifications", cmd_notifications))
    app.add_handler(CommandHandler("about",      cmd_about))
    app.add_handler(CommandHandler("help",       cmd_help))
    app.add_handler(CommandHandler("myid",       cmd_myid))
    app.add_handler(CommandHandler("admin",      cmd_admin))
    app.add_handler(CommandHandler("post",       cmd_post))
    app.add_handler(CommandHandler("ban",        cmd_ban))
    app.add_handler(CommandHandler("unban",      cmd_unban))
    app.add_handler(CallbackQueryHandler(button_handler))

    async with aiohttp.ClientSession() as session:
        app.bot_data["session"] = session
        await run_http()
        asyncio.create_task(alert_check_loop(app.bot, session))

        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        log.info("Бот запущено! Адмінів: %d, Зареєстровано: %d", len(ADMIN_IDS), len(registered_users))
        await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
