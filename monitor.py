"""
monitor.py — Фоновий моніторинг нових тендерів

Кожні 30 хвилин перевіряє Prozorro API на нові тендери по Вінниці.
Якщо знаходить відповідні — надсилає адміну в Telegram.
"""
import asyncio
from loguru import logger
from aiogram import Bot

from prozorro.client import get_active_vinnytsia_tenders, format_tender_summary
from config import ADMIN_TELEGRAM_ID, MIN_AMOUNT, MAX_AMOUNT, TARGET_CPV_PREFIX

from db.database import is_tender_seen, mark_tender_as_seen

MONITOR_INTERVAL_SECONDS = 30 * 60  # 30 хвилин


async def start_monitor(bot: Bot):
    """
    Фоновий цикл моніторингу нових тендерів.
    Запускається разом з ботом і працює паралельно.
    """
    logger.info(f"👁 Моніторинг запущено. Перевірка кожні {MONITOR_INTERVAL_SECONDS // 60} хвилин")
    
    while True:
        try:
            await check_new_tenders(bot)
        except Exception as e:
            logger.error(f"❌ Помилка моніторингу: {e}")
        
        await asyncio.sleep(MONITOR_INTERVAL_SECONDS)


async def check_new_tenders(bot: Bot):
    """Перевіряє нові тендери і сповіщає всіх активних клієнтів та адміна."""
    logger.info("🔍 Перевіряю нові тендери по Вінниці...")
    tenders = await get_active_vinnytsia_tenders(
        min_amount=MIN_AMOUNT,
        max_amount=MAX_AMOUNT,
        cpv_prefix=TARGET_CPV_PREFIX,
    )
    
    new_tenders = []
    for t in tenders:
        if not await is_tender_seen(t["id"]):
            new_tenders.append(t)
    
    if not new_tenders:
        logger.info("✓ Нових тендерів немає")
        return
    
    logger.info(f"🆕 Знайдено {len(new_tenders)} нових тендерів!")
    
    # Завантажуємо список усіх активних клієнтів з бази
    from db.database import DbConnection
    active_clients = []
    try:
        async with DbConnection() as db:
            rows = await db.fetchall("SELECT telegram_id FROM clients WHERE is_active = 1")
            active_clients = [r["telegram_id"] for r in rows]
    except Exception as e:
        logger.error(f"Не вдалося завантажити клієнтів для моніторингу: {e}")
        
    # Додаємо адміна до списку отримувачів, якщо його там немає
    if ADMIN_TELEGRAM_ID and ADMIN_TELEGRAM_ID not in active_clients:
        active_clients.append(ADMIN_TELEGRAM_ID)
        
    # Відправляємо кожен новий тендер отримувачам
    for tender in new_tenders[:5]:  # Максимум 5 за раз
        amount_str = f"{tender['amount']:,.0f}".replace(",", " ")
        deadline_short = tender["deadline"][:10] if tender.get("deadline") else "?"
        
        message_text = (
            f"🆕 *Новий тендер по Вінниці!*\n\n"
            f"📋 {tender['title'][:100]}\n"
            f"🏢 {tender['procuring_entity']}\n"
            f"💰 {amount_str} грн\n"
            f"📅 Дедлайн: {deadline_short}\n"
            f"🔗 [Відкрити в Prozorro]({tender['url']})\n\n"
            f"_Для автоматичного аналізу надішліть: `/analyze {tender['id']}`_"
        )
        
        for client_id in active_clients:
            try:
                await bot.send_message(
                    client_id,
                    message_text,
                    parse_mode="Markdown"
                )
                await asyncio.sleep(0.1)  # Захист від flood limit
            except Exception as e:
                logger.warning(f"Не вдалося надіслати тендер {tender['id']} клієнту {client_id}: {e}")
                
        await mark_tender_as_seen(tender["id"])
        await asyncio.sleep(1)
