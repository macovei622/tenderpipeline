"""
main.py — Точка входу в застосунок

Запуск:
    python main.py

Що відбувається при старті:
1. Ініціалізується база даних
2. Запускається Telegram-бот (polling mode)
3. Планувальник перевіряє нові тендери кожні 30 хвилин
"""
import asyncio
from loguru import logger
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

from config import (
    TELEGRAM_BOT_TOKEN, OPENROUTER_API_KEY, LOG_LEVEL,
    WEBHOOK_ENABLED, WEBHOOK_URL, WEBHOOK_PORT, WEBHOOK_PATH
)
from db.database import init_db
from bot.handlers import router
from monitor import start_monitor


def validate_config():
    """Перевіряє що всі необхідні змінні середовища задані."""
    errors = []
    if not TELEGRAM_BOT_TOKEN:
        errors.append("TELEGRAM_BOT_TOKEN не задано в .env")
    if not OPENROUTER_API_KEY:
        errors.append("OPENROUTER_API_KEY не задано в .env")
    
    if errors:
        for e in errors:
            logger.error(f"❌ {e}")
        raise RuntimeError(
            "Скопіюй .env.example у .env і заповни ключі:\n" +
            "\n".join(errors)
        )


async def main():
    # Налаштування логування
    logger.remove()
    logger.add(
        "logs/tender_service.log",
        rotation="10 MB",
        retention="7 days",
        level=LOG_LEVEL,
        encoding="utf-8"
    )
    logger.add(lambda msg: print(msg, end=""), level=LOG_LEVEL, colorize=True)
    
    logger.info("🚀 Запуск Tender Service...")
    
    # Валідація конфігурації
    validate_config()
    
    # Ініціалізація БД
    await init_db()
    
    # Ініціалізація бота
    bot = Bot(
        token=TELEGRAM_BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN)
    )
    dp = Dispatcher()
    dp.include_router(router)
    
    # Реєстрація команд в меню Telegram
    from aiogram.types import BotCommand
    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="Запустити бота та привітання"),
            BotCommand(command="help", description="Отримати довідку"),
            BotCommand(command="profile", description="Заповнити профіль компанії"),
            BotCommand(command="status", description="Переглянути мої тендери"),
            BotCommand(command="admin", description="Панель адміністратора (тільки для адміна)")
        ])
        logger.info("📅 Команди меню зареєстровані в Telegram")
    except Exception as e:
        logger.warning(f"Не вдалося зареєструвати команди: {e}")
    
    logger.info("✅ Бот ініціалізовано")
    
    # Запускаємо моніторинг нових тендерів у фоні
    monitor_task = asyncio.create_task(start_monitor(bot))
    
    logger.info("🤖 Бот запущено! Очікую повідомлень...")
    
    if WEBHOOK_ENABLED:
        from aiogram.webhook.aiohttp_impl import SimpleRequestHandler
        from aiohttp import web

        # Налаштовуємо Webhook
        await bot.set_webhook(url=WEBHOOK_URL)
        logger.info(f"🕸 Webhook встановлено на {WEBHOOK_URL}")

        # Створюємо та реєструємо веб-сервер
        app = web.Application()
        SimpleRequestHandler(
            dispatcher=dp,
            bot=bot
        ).register(app, path=WEBHOOK_PATH)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host="0.0.0.0", port=WEBHOOK_PORT)
        await site.start()
        logger.info(f"🕸 Webhook сервер запущено на порту {WEBHOOK_PORT}")

        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            await bot.delete_webhook()
            await runner.cleanup()
            monitor_task.cancel()
            await bot.session.close()
            logger.info("👋 Бот зупинено")
    else:
        logger.info("🤖 Запуск в режимі Polling...")
        try:
            await dp.start_polling(bot, skip_updates=True)
        finally:
            monitor_task.cancel()
            await bot.session.close()
            logger.info("👋 Бот зупинено")


if __name__ == "__main__":
    import os
    os.makedirs("logs", exist_ok=True)
    asyncio.run(main())
