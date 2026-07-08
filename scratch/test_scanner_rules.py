# test_scanner_rules.py
import asyncio
import os
import sys
from loguru import logger

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai.agents.scanner import scan_document

async def test():
    # 1. Створюємо фіктивний текст ТД із класом наслідків СС2, великими вимогами до доходу та специфічним приладом
    mock_td_text = """
    ТЕХНІЧНЕ ЗАВДАННЯ ТА КВАЛІФІКАЦІЙНІ ВИМОГИ:
    1. Учасник повинен мати ліцензію на виконання робіт з класом наслідків не нижче СС2.
    2. Учасник повинен підтвердити річний дохід (обсяг річного обороту) за 2024 рік у розмірі не менше 5 000 000 грн.
    3. Учасник повинен мати у власності або користуванні ультразвуковий лазерний вимірювач міцності бетону з діючою повіркою.
    """
    
    # 2. Профіль учасника з СС1, низьким доходом і без приладу
    company_profile = {
        "name": "ТОВ ТестБуд",
        "license": "Ліцензія на будівництво об'єктів класу наслідків СС1",
        "annual_revenue": 1200000.0,  # 1.2 млн (менше ніж 5 млн)
        "equipment": [
            {"name": "Екскаватор JCB"},
            {"name": "Бетонозмішувач"}
        ]
    }
    
    tender_meta = {
        "title": "Капітальний ремонт будівлі",
        "value": {"amount": 6000000.0}
    }
    
    logger.info("🧪 Тестуємо порівняльний аналіз профілю у AI-Сканері...")
    result = await scan_document(
        text=mock_td_text,
        doc_size_chars=len(mock_td_text),
        tender_meta=tender_meta,
        company_profile=company_profile
    )
    
    print("\n=== РЕЗУЛЬТАТ AI-СКАНЕРА ===")
    import pprint
    pprint.pprint(result)

if __name__ == "__main__":
    asyncio.run(test())
