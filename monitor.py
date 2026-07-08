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
        region_name = tender.get("region") or ""
        header = f"Новий тендер ({region_name})!" if region_name else "Новий тендер!"
        
        message_text = (
            f"🆕 *{header}*\n\n"
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
                
        # ── Провідний проактивний аутріч-матчинг ──
        matched_leads = []
        try:
            from db.database import DbConnection
            async with DbConnection() as db:
                rows = await db.fetchall(
                    """SELECT l.* FROM outreach_leads l
                       WHERE l.disqualified_edrpou NOT IN (SELECT DISTINCT edrpou FROM clients WHERE edrpou IS NOT NULL)"""
                )
                for r in rows:
                    lead_region = r.get("region") or ""
                    lead_cpv = r.get("cpv") or ""
                    tender_region = tender.get("region") or ""
                    tender_cpv = tender.get("cpv") or ""
                    
                    region_match = lead_region and tender_region and (lead_region[:6].lower() in tender_region.lower() or tender_region[:6].lower() in lead_region.lower())
                    cpv_match = lead_cpv and tender_cpv and lead_cpv[:3] == tender_cpv[:3]
                    
                    if region_match and cpv_match:
                        matched_leads.append(r)
        except Exception as e:
            logger.error(f"Не вдалося виконати пошук аутріч-цілей для нового тендера: {e}")

        # Якщо знайдено збіги з лідами — відправляємо адміну тригер для аутрічу
        for lead in matched_leads:
            director_name = lead.get("director_name") or "колеги"
            company_name = lead.get("disqualified_name") or "компанія"
            edrpou = lead.get("disqualified_edrpou") or ""
            phone = lead.get("phone") or "немає"
            email = lead.get("email") or "немає"
            
            # Визначаємо ім'я директора
            greeting = director_name.split()[1] if len(director_name.split()) >= 2 else director_name
            # Створюємо копіпаст-шаблон для адміна
            outreach_pitch = (
                f"«Шановний {greeting}! Щойно опубліковано новий тендер: {tender['title'][:60]}... "
                f"на суму {amount_str} грн. Ми вже прогнали його через наш AI-сканер і готові надати вам "
                f"безкоштовний звіт про приховані вимоги замовника, щоб уникнути помилок минулого разу. "
                f"Надіслати вам звіт у Telegram чи на {email if email != 'немає' else 'пошту'}?»"
            )
            
            admin_alert = (
                f"🔔 *Проактивний Аутріч-Тригер!*\n\n"
                f"Знайдено відповідний тендер для нашого ліда:\n"
                f"🏢 *{company_name}* (ЄДРПОУ `{edrpou}`)\n"
                f"👤 Директор: {director_name}\n"
                f"📞 Контакти: {phone} | {email}\n\n"
                f"📋 *Новий тендер:* {tender['title'][:100]}\n"
                f"💰 Сума: {amount_str} грн\n"
                f"🔗 [Відкрити в Prozorro]({tender['url']})\n\n"
                f"💬 *Скопіюйте та відправте клієнту:*\n"
                f"`{outreach_pitch}`"
            )
            
            try:
                if ADMIN_TELEGRAM_ID:
                    await bot.send_message(
                        ADMIN_TELEGRAM_ID,
                        admin_alert,
                        parse_mode="Markdown"
                    )
                    await asyncio.sleep(0.5)
            except Exception as e:
                logger.error(f"Не вдалося надіслати аутріч-тригер адміну: {e}")

        await mark_tender_as_seen(tender["id"])
        await asyncio.sleep(1)
