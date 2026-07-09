"""
bot/handlers_analytics.py — Обробники Telegram-бота для розширених аналітичних модулів

Цей файл містить реалізацію команд:
1. /spending <ЄДРПОУ / Тендер> — Фінансовий скоринг замовника
2. /court <ЄДРПОУ / Тендер> — Судові справи замовника (тільки активні)
3. /cpm <Тендер> — Розрахунок критичного шляху робіт (ДБН)
4. /logistics <Тендер / Адреси> — Розрахунок транспортного плеча та прихованої маржі
5. /auction <Тендер> <Drop Dead Price> — Симуляція аукціону та розрахунок оптимальної ставки
"""
from __future__ import annotations

import re
import json
from datetime import datetime
from typing import Optional
from loguru import logger

from aiogram import Router
from aiogram.types import Message
from aiogram.filters import Command

from prozorro.client import extract_tender_id, fetch_tender
from db.database import get_or_create_client, get_client_by_telegram_id

from analytics.spending import SpendingAnalyzer
from analytics.court_parser import CourtParser
from analytics.cpm_engine import CPMEngine, CPMTask
from analytics.logistics import LogisticsCalculator
from analytics.auction_simulator import AuctionSimulator

router = Router()


# ── Допоміжні функції ─────────────────────────────────────────────────────────

def guess_work_type(name: str) -> str:
    """Визначає тип робіт за назвою для підбору технологічних затримок."""
    name_l = name.lower()
    # Спочатку специфічні роботи, що можуть перетинатися з конструкціями
    if "гідроізол" in name_l or "покрівл" in name_l or "утепл" in name_l:
        return "waterproofing"
    if "стяжк" in name_l or "підлог" in name_l:
        return "screed"
    if "штукатур" in name_l or "опорядув" in name_l:
        return "plaster_cement"
    if "гіпс" in name_l or "шпаклів" in name_l or "гіпсокард" in name_l:
        return "plaster_gypsum"
    if "плит" in name_l or "панел" in name_l or "збірн" in name_l:
        return "concrete_prefab"
    if "фарбув" in name_l or "маляр" in name_l:
        return "paint"
    if "цегл" in name_l or "мурув" in name_l or "стін" in name_l:
        return "masonry"
    # Загальні бетонні/монолітні роботи
    if "бетон" in name_l or "залізобетон" in name_l or "фундамент" in name_l or "моноліт" in name_l:
        return "concrete_monolithic"
    return "generic"


async def resolve_edrpou_from_arg(arg: str) -> Optional[str]:
    """Спроба вилучити ЄДРПОУ з аргументу: безпосередньо або через тендер."""
    arg_clean = arg.strip()
    if arg_clean.isdigit() and len(arg_clean) == 8:
        return arg_clean

    # Можливо це тендер
    tender_id = extract_tender_id(arg_clean)
    if tender_id:
        tender = await fetch_tender(tender_id)
        if tender:
            # Спробуємо витягти ЄДРПОУ замовника
            pe = tender.get("procuringEntity", {})
            edrpou = pe.get("identifier", {}).get("id")
            if edrpou:
                return str(edrpou)
    return None


# ── Команди ──────────────────────────────────────────────────────────────────

@router.message(Command("spending"))
async def cmd_spending(message: Message):
    """
    /spending <ЄДРПОУ або тендер>
    Аналіз затримок платежів замовника.
    """
    args = message.text.replace("/spending", "").strip()
    if not args:
        await message.answer(
            "ℹ️ *Команда /spending*\n\n"
            "Дозволяє перевірити фінансову дисципліну замовника (середні затримки казначейських оплат).\n\n"
            "📝 *Використання:*\n"
            "  • `/spending 12345678` (де 12345678 — ЄДРПОУ замовника)\n"
            "  • `/spending UA-2025-01-15-001234-a` (посилання або ID тендера)",
            parse_mode="Markdown"
        )
        return

    status_msg = await message.answer("🔍 Запитую дані про транзакції замовника з spending.gov.ua...")
    
    edrpou = await resolve_edrpou_from_arg(args)
    if not edrpou:
        await status_msg.edit_text("❌ Не вдалося визначити ЄДРПОУ замовника. Перевірте формат вводу.")
        return

    analyzer = SpendingAnalyzer()
    result = await analyzer.analyze(edrpou)
    await status_msg.edit_text(result.summary_text(), parse_mode="Markdown")


@router.message(Command("court"))
async def cmd_court(message: Message):
    """
    /court <ЄДРПОУ або тендер>
    Аналіз кримінальних та корупційних судових справ замовника.
    """
    args = message.text.replace("/court", "").strip()
    if not args:
        await message.answer(
            "ℹ️ *Команда /court*\n\n"
            "Дозволяє знайти активні судові справи замовника за статтями про корупцію, розкрадання або зловживання (ст. 191, 368 ККУ).\n\n"
            "📝 *Використання:*\n"
            "  • `/court 12345678` (за ЄДРПОУ)\n"
            "  • `/court UA-2025-01-15-001234-a` (за посиланням/ID тендера)",
            parse_mode="Markdown"
        )
        return

    status_msg = await message.answer("🔍 Шукаю інформацію у судовому реєстрі court.gov.ua...")

    edrpou = await resolve_edrpou_from_arg(args)
    if not edrpou:
        await status_msg.edit_text("❌ Не вдалося визначити ЄДРПОУ замовника. Перевірте формат вводу.")
        return

    parser = CourtParser()
    result = await parser.analyze(edrpou)
    await status_msg.edit_text(result.summary_text(), parse_mode="Markdown")


@router.message(Command("cpm"))
async def cmd_cpm(message: Message):
    """
    /cpm <тендер>
    Розрахунок критичного шляху технологічних процесів.
    """
    args = message.text.replace("/cpm", "").strip()
    tender_id = extract_tender_id(args) if args else None
    
    if not tender_id:
        await message.answer(
            "ℹ️ *Команда /cpm*\n\n"
            "Будує мережевий графік будівництва на основі обсягів робіт та перевіряє реалістичність строків.\n\n"
            "📝 *Використання:*\n"
            "  • `/cpm UA-2025-01-15-001234-a` (посилання або ID тендера)",
            parse_mode="Markdown"
        )
        return

    status_msg = await message.answer("🔍 Завантажую дані тендера для побудови календарного графіка...")
    tender = await fetch_tender(tender_id)
    
    if not tender:
        await status_msg.edit_text("❌ Не вдалося завантажити тендер.")
        return

    items = tender.get("items", [])
    if not items:
        await status_msg.edit_text("❌ У тендері немає переліку робіт (items) для розрахунку.")
        return

    # Готуємо вхідний список робіт
    scope_items = []
    for idx, item in enumerate(items):
        desc = item.get("description", "Будівельні роботи")
        qty = item.get("quantity", 1)
        scope_items.append({
            "name": desc,
            "volume": qty,
            "type": guess_work_type(desc)
        })

    # Спроба отримати дедлайн виконання робіт у днях
    deadline_days = 90  # за замовчуванням
    # Якщо є contractPeriod, порахуємо різницю
    cp_end = tender.get("contractPeriod", {}).get("endDate")
    if cp_end:
        try:
            end_dt = datetime.fromisoformat(cp_end[:10])
            days = (end_dt - datetime.now()).days
            if days > 0:
                deadline_days = days
        except Exception:
            pass

    engine = CPMEngine()
    tasks = engine.parse_tasks_from_td("", scope_items)
    result = engine.compute(tasks, deadline_days=deadline_days)
    
    await status_msg.edit_text(result.summary_text(), parse_mode="Markdown")


@router.message(Command("logistics"))
async def cmd_logistics(message: Message):
    """
    /logistics <тендер> або <адреса об'єкта> | <адреса складу>
    Розрахунок логістики та прихованої маржі.
    """
    args = message.text.replace("/logistics", "").strip()
    if not args:
        await message.answer(
            "ℹ️ *Команда /logistics*\n\n"
            "Розраховує реальну відстань транспортування матеріалів та приховану маржу (якщо відстань менша за інвесторські 30 км).\n\n"
            "📝 *Використання:*\n"
            "  • `/logistics UA-2025-01-15-001234-a` (автоматично бере адресу об'єкта з тендера та адресу вашого складу з профілю)\n"
            "  • `/logistics вул. Соборна 1, Вінниця | вул. Пирогова 50, Вінниця` (ручний пошук)",
            parse_mode="Markdown"
        )
        return

    status_msg = await message.answer("🔍 Визначаю координати та прокладаю оптимальний маршрут...")

    obj_addr = ""
    sup_addr = ""

    # Варіант 1: Передано посилання або ID тендера
    tender_id = extract_tender_id(args)
    if tender_id:
        tender = await fetch_tender(tender_id)
        if not tender:
            await status_msg.edit_text("❌ Не вдалося завантажити тендер.")
            return
        
        # Спробуємо знайти адресу доставки в елементах
        items = tender.get("items", [])
        if items:
            da = items[0].get("deliveryAddress", {})
            if da.get("streetAddress"):
                obj_addr = f"{da.get('streetAddress')}, {da.get('locality', '')}"
        
        if not obj_addr:
            pe = tender.get("procuringEntity", {})
            addr = pe.get("address", {})
            if addr.get("streetAddress"):
                obj_addr = f"{addr.get('streetAddress')}, {addr.get('locality', '')}"

        # Отримуємо адресу складу клієнта з профілю
        client_row = await get_client_by_telegram_id(message.from_user.id)
        if client_row and client_row.get("profile_json"):
            try:
                prof = json.loads(client_row["profile_json"])
                # Зазвичай адреса складу/офісу вказується як назва компанії або у додаткових полях
                # Для логістики спробуємо взяти місто реєстрації + назву компанії або запитати
                sup_addr = client_row.get("company_name", "")
            except Exception:
                pass

        if not obj_addr:
            await status_msg.edit_text("❌ Не вдалося витягти адресу об'єкта з тендера.")
            return

        if not sup_addr:
            await status_msg.edit_text(
                "⚠️ *Адресу вашого складу не вказано у профілі.*\n\n"
                "Будь ласка, заповніть профіль (/profile) або виконайте команду вручну, вказавши дві адреси:\n"
                "`/logistics [адреса об'єкта] | [адреса вашого складу]`",
                parse_mode="Markdown"
            )
            return
    else:
        # Варіант 2: Передано адреси вручну через "|"
        parts = [p.strip() for p in args.split("|")]
        if len(parts) == 2:
            obj_addr, sup_addr = parts[0], parts[1]
        else:
            await status_msg.edit_text("❌ Невірний формат. Використовуйте символ `|` для розділення двох адрес.")
            return

    calc = LogisticsCalculator()
    result = await calc.analyze(object_address=obj_addr, supplier_address=sup_addr)
    await status_msg.edit_text(result.summary_text(), parse_mode="Markdown")


@router.message(Command("auction"))
async def cmd_auction(message: Message):
    """
    /auction <тендер> <Drop Dead Price>
    Симуляція аукціону та розрахунок оптимальної ставки.
    """
    raw_args = message.text.replace("/auction", "").strip().split()
    if len(raw_args) < 2:
        await message.answer(
            "ℹ️ *Команда /auction*\n\n"
            "Запускає симуляцію Monte Carlo для прогнозування поведінки конкурентів на аукціоні.\n\n"
            "📝 *Використання:*\n"
            "  • `/auction UA-2025-01-15-001234-a 950000` (де 950 000 — ваша мінімально допустима ціна / собівартість)",
            parse_mode="Markdown"
        )
        return

    tender_arg = raw_args[0]
    try:
        drop_dead_price = float(raw_args[1].replace(" ", "").replace(",", ""))
    except ValueError:
        await message.answer("❌ Другий аргумент повинен бути числом (собівартістю).")
        return

    tender_id = extract_tender_id(tender_arg)
    if not tender_id:
        await message.answer("❌ Перший аргумент повинен бути посиланням на тендер або його ID.")
        return

    status_msg = await message.answer(
        "🎲 Перевіряю джерело та аналізую історію завершених аукціонів конкурентів..."
    )

    tender = await fetch_tender(tender_id)
    if not tender:
        await status_msg.edit_text("❌ Не вдалося завантажити тендер.")
        return

    expected_value = tender.get("value", {}).get("amount", 0)
    if expected_value <= 0:
        await status_msg.edit_text("❌ Очікувана вартість тендера не знайдена або дорівнює нулю.")
        return

    if drop_dead_price >= expected_value:
        await status_msg.edit_text(
            f"❌ Ваша мінімальна ціна ({drop_dead_price:,.0f} грн) не може бути більшою або дорівнювати "
            f"очікуваній вартості тендера ({expected_value:,.0f} грн)."
        )
        return

    # Визначаємо CPV-код та Регіон
    cpv = tender.get("classification", {}).get("id", "45")[:4]
    pe = tender.get("procuringEntity", {})
    region = pe.get("address", {}).get("region", "Вінницька область")

    sim = AuctionSimulator()
    result = await sim.analyze(
        tender_id=tender_id,
        expected_value=expected_value,
        cpv_prefix=cpv,
        region=region,
        drop_dead_price=drop_dead_price
    )
    
    await status_msg.edit_text(result.summary_text(), parse_mode="Markdown")
