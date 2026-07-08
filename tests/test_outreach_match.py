import pytest
import asyncio
import random
from db.database import init_db, DbConnection, get_matching_leads

@pytest.mark.asyncio
async def test_lead_matching():
    # 1. Ініціалізація бази даних
    await init_db()
    
    unique_suffix = random.randint(100_000, 999_999)
    prozorro_id = f"UA-TEST-{unique_suffix}"
    edrpou = f"EDR-{unique_suffix}"
    
    # 2. Додаємо тестового ліда з профілем у БД
    async with DbConnection() as db:
        await db.execute("""
            INSERT INTO outreach_leads (
                prozorro_id, title, amount, procuring_entity,
                winner_name, winner_amount, disqualified_name,
                disqualified_edrpou, disqualified_amount, diff_amount,
                target_region, target_cpv, director_name
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            prozorro_id,
            "Тестовий тендер на ремонт школи",
            1_500_000.0,
            "Черкаська міська рада",
            "Переможець ТОВ",
            1_400_000.0,
            "Невдаха ПП",
            edrpou,
            1_300_000.0,
            100_000.0,
            "Черкаська",     # target_region
            "45210000-2",   # target_cpv (Будівництво)
            "Петренко Петро Петрович"
        ))
        
    try:
        # 3. Перевіряємо точний збіг регіону та CPV
        matches = await get_matching_leads("Черкаська область", "45210000-2")
        assert len(matches) >= 1
        assert any(m["disqualified_edrpou"] == edrpou for m in matches)
        
        # 4. Перевіряємо невідповідний регіон
        no_region_matches = await get_matching_leads("Київська область", "45210000-2")
        assert not any(m["disqualified_edrpou"] == edrpou for m in no_region_matches)
        
        # 5. Перевіряємо невідповідний CPV-код
        no_cpv_matches = await get_matching_leads("Черкаська область", "48300000-8")
        assert not any(m["disqualified_edrpou"] == edrpou for m in no_cpv_matches)
        
    finally:
        # Очищуємо базу даних після тесту
        async with DbConnection() as db:
            await db.execute(
                "DELETE FROM outreach_leads WHERE prozorro_id = ? AND disqualified_edrpou = ?",
                (prozorro_id, edrpou)
            )
