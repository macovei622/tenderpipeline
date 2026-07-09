"""
scratch/test_real_tender.py — Повний інтеграційний тест розширеної аналітики
на реальних даних тендера UA-2025-10-06-001013-a.
"""
import asyncio
import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from prozorro.client import fetch_tender
from analytics.spending import SpendingAnalyzer
from analytics.court_parser import CourtParser
from analytics.cpm_engine import CPMEngine
from analytics.logistics import LogisticsCalculator
from analytics.auction_simulator import AuctionSimulator
from bot.handlers_analytics import guess_work_type


async def main():
    tender_id = "UA-2025-10-06-001013-a"
    print(f"🚀 Старт повного тесту для тендера: {tender_id}")
    print("=" * 60)

    # 1. Завантаження тендера
    print("\n📥 1. Завантаження метаданих тендера з Prozorro API...")
    tender = await fetch_tender(tender_id)
    if not tender:
        print("❌ Помилка: Не вдалося завантажити тендер.")
        return

    title = tender.get("title", "")
    expected_value = tender.get("value", {}).get("amount", 0)
    pe = tender.get("procuringEntity", {})
    edrpou = pe.get("identifier", {}).get("id")
    pe_name = pe.get("name", "")
    
    print(f"✅ Успішно завантажено:")
    print(f"  • Назва: {title}")
    print(f"  • Очікувана вартість: {expected_value:,.2f} грн")
    print(f"  • Замовник: {pe_name} (ЄДРПОУ: {edrpou})")

    # 2. Модуль 1: Spending.gov.ua
    print("\n💳 2. Запуск фінансового скорингу замовника (Spending.gov.ua)...")
    if edrpou:
        spending_analyzer = SpendingAnalyzer()
        spending_res = await spending_analyzer.analyze(str(edrpou))
        print(spending_res.summary_text())
    else:
        print("⚠️ Немає ЄДРПОУ для фінансового аналізу")

    # 3. Модуль 2а: Судовий реєстр
    print("\n⚖️ 3. Запуск аналізу судового реєстру (court.gov.ua)...")
    if edrpou:
        court_parser = CourtParser()
        court_res = await court_parser.analyze(str(edrpou))
        print(court_res.summary_text())
    else:
        print("⚠️ Немає ЄДРПОУ для судового аналізу")

    # 4. Модуль 2б: CPM Engine (Календарний графік робіт)
    print("\n📅 4. Побудова календарного графіка CPM робіт...")
    items = tender.get("items", [])
    if items:
        scope_items = []
        for idx, item in enumerate(items):
            desc = item.get("description", "Будівельні роботи")
            qty = item.get("quantity", 1)
            scope_items.append({
                "name": desc,
                "volume": qty,
                "type": guess_work_type(desc)
            })

        cpm_engine = CPMEngine()
        tasks = cpm_engine.parse_tasks_from_td("", scope_items)
        cpm_res = cpm_engine.compute(tasks, deadline_days=120)
        print(cpm_res.summary_text())
    else:
        print("⚠️ У тендері немає переліку робіт для CPM")

    # 5. Модуль 3: Логістичний аналіз
    print("\n🚚 5. Запуск логістичного аналізу (OSRM + Nominatim)...")
    # Визначаємо адресу об'єкта
    obj_addr = ""
    if items:
        da = items[0].get("deliveryAddress", {})
        if da.get("streetAddress"):
            obj_addr = f"{da.get('streetAddress')}, {da.get('locality', '')}"
    if not obj_addr:
        addr = pe.get("address", {})
        if addr.get("streetAddress"):
            obj_addr = f"{addr.get('streetAddress')}, {addr.get('locality', '')}"

    # Адреса нашого складу (для тесту візьмемо центральну вулицю Вінниці)
    supplier_addr = "вулиця Пирогова 50, Вінниця"
    
    if obj_addr:
        print(f"  • Адреса об'єкта (з тендера): {obj_addr}")
        print(f"  • Адреса нашого складу (тест): {supplier_addr}")
        logistics_calc = LogisticsCalculator()
        log_res = await logistics_calc.analyze(
            object_address=obj_addr,
            supplier_address=supplier_addr,
            investor_km=30.0,
            work_days=90
        )
        print(log_res.summary_text())
    else:
        print("⚠️ Не вдалося визначити адресу об'єкта для логістики")

    # 6. Модуль 6: Симулятор аукціонів
    print("\n🎲 6. Запуск тактичного симулятора аукціону Prozorro...")
    cpv = tender.get("classification", {}).get("id", "45")[:4]
    region = pe.get("address", {}).get("region", "Вінницька область")
    drop_dead_price = expected_value * 0.9  # собівартість 90% від очікуваної

    print(f"  • CPV код: {cpv}")
    print(f"  • Регіон: {region}")
    print(f"  • Собівартість: {drop_dead_price:,.2f} грн")

    simulator = AuctionSimulator()
    sim_res = await simulator.analyze(
        tender_id=tender_id,
        expected_value=expected_value,
        cpv_prefix=cpv,
        region=region,
        drop_dead_price=drop_dead_price
    )
    print(sim_res.summary_text())
    print("\n" + "=" * 60)
    print("✅ Повний тест успішно завершено!")


if __name__ == "__main__":
    asyncio.run(main())
