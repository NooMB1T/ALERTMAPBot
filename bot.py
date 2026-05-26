import asyncio
import aiohttp
import logging
from datetime import datetime, timezone, timedelta
import os

from telegram import Bot
from telegram.error import TelegramError
from aiohttp import web

# ─── CONFIG ────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "8693341837:AAF2lK6bGR3uoLz1kfkZt8IjDQIF18YXHN8")
CHANNEL_ID = os.getenv("CHANNEL_ID", "@yampilnews")
ALERT_API_KEY = os.getenv("ALERT_API_KEY", "b3de42c9:736017aa6745a605c155108e221d31a8")

# Шепетівський р-н Хмельницької обл. — ID регіону в API alerts.in.ua
TARGET_REGION_ID = "31004"

ALERT_API_URL = "https://api.alerts.in.ua/v1/alerts/active.json"
CHECK_INTERVAL = 30  # секунд між перевірками

KYIV_TZ = timezone(timedelta(hours=3))
PORT = int(os.getenv("PORT", 8000))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ─── STATE ─────────────────────────────────────────────────────────────────────
alert_active: bool | None = None  # None = ще не знаємо (перший запуск)


def now_str() -> str:
    """Поточний час у форматі ГГ:ХХ DD.MM.YYYY (Київ UTC+3)."""
    return datetime.now(KYIV_TZ).strftime("%H:%M %d.%m.%Y")


async def fetch_alert_status(session: aiohttp.ClientSession) -> bool:
    """Повертає True якщо в Шепетівському р-ні зараз тривога."""
    headers = {"X-API-Key": ALERT_API_KEY}
    try:
        async with session.get(ALERT_API_URL, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            resp.raise_for_status()
            data = await resp.json()

        alerts = data.get("alerts", [])
        for alert in alerts:
            region_id = str(alert.get("location_uid", ""))
            alert_type = alert.get("alert_type", "")
            if region_id == TARGET_REGION_ID and alert_type == "air_raid":
                return True
        return False
    except Exception as e:
        log.error("Помилка при опитуванні API: %s", e)
        return False


async def send_message(bot: Bot, text: str) -> None:
    try:
        await bot.send_message(chat_id=CHANNEL_ID, text=text, parse_mode="HTML")
        log.info("Повідомлення надіслано: %s", text[:60])
    except TelegramError as e:
        log.error("Помилка Telegram: %s", e)


# ─── HTTP HANDLERS ─────────────────────────────────────────────────────────────
async def health_check(request: web.Request) -> web.Response:
    """Endpoint для перевірки здоров'я."""
    return web.json_response({"status": "ok", "timestamp": now_str()})


async def metrics(request: web.Request) -> web.Response:
    """Endpoint для метрик (статус тривоги)."""
    return web.json_response({
        "alert_active": alert_active,
        "region_id": TARGET_REGION_ID,
        "timestamp": now_str()
    })


# ─── ALERT CHECK LOOP ──────────────────────────────────────────────────────────
async def alert_check_loop(bot: Bot, session: aiohttp.ClientSession) -> None:
    """Основний цикл перевірки тривоги."""
    global alert_active

    log.info("Початок цикла перевірки тривоги. Інтервал: %d сек.", CHECK_INTERVAL)

    while True:
        try:
            current = await fetch_alert_status(session)

            if alert_active is None:
                # Перший запуск — просто запам'ятовуємо стан, нічого не пишемо
                alert_active = current
                log.info("Початковий стан тривоги: %s", current)

            elif current and not alert_active:
                # Тривога розпочалась
                alert_active = True
                msg = (
                    "‼️УВАГА ПОВІТРЯНА ТРИВОГА‼️\n\n"
                    f"Станом на {now_str()}, в ОТГ селища Ямпіль, "
                    "була оголошена повітряна тривога.\n\n"
                    "БЕРЕЖІТЬ СЕБЕ!"
                )
                await send_message(bot, msg)

            elif not current and alert_active:
                # Відбій
                alert_active = False
                msg = (
                    "❕ВІДБІЙ ПОВІТРЯНОЇ ТРИВОГИ❕\n\n"
                    f"Станом на {now_str()} був оголошений "
                    "відбій повітряної тривоги."
                )
                await send_message(bot, msg)

        except Exception as e:
            log.error("Помилка при перевірці тривоги: %s", e)

        await asyncio.sleep(CHECK_INTERVAL)


# ─── STARTUP ───────────────────────────────────────────────────────────────────
async def start_alert_loop(app: web.Application) -> None:
    """Запускається при старті aiohttp сервера."""
    bot = Bot(token=BOT_TOKEN)
    session = aiohttp.ClientSession()

    # Зберігаємо посилання на цикл для коректного завершення
    app["alert_task"] = asyncio.create_task(alert_check_loop(bot, session))
    log.info("Alert loop запущено")


async def stop_alert_loop(app: web.Application) -> None:
    """Зупиняється при завершенні сервера."""
    if "alert_task" in app:
        app["alert_task"].cancel()
        try:
            await app["alert_task"]
        except asyncio.CancelledError:
            log.info("Alert loop зупинено")


# ─── MAIN ──────────────────────────────────────────────────────────────────────
async def main() -> None:
    """Запускає aiohttp сервер + alert loop."""
    app = web.Application()

    # Маршрути
    app.router.add_get("/health", health_check)
    app.router.add_get("/metrics", metrics)

    # Обробники старту/зупинки
    app.startup.append(start_alert_loop)
    app.cleanup.append(stop_alert_loop)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    log.info(f"HTTP сервер запущено на http://0.0.0.0:{PORT}")
    log.info("Endpoints: GET /health, GET /metrics")

    # Чекаємо на сигнал завершення (він ніколи не приходить у нормальній роботі)
    try:
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
        
