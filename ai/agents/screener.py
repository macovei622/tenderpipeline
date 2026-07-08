"""
ai/agents/screener.py — Агент 0: Швидкий пре-скринінг тендерів
"""
from typing import Optional
from loguru import logger
from ai.client import call_model, parse_json_response, build_messages

SCREENER_SYSTEM = """Ти — AI-диспетчер тендерного відділу.
Твоє завдання — зробити експрес-оцінку релевантності тендера для нашої компанії.

ПРОФІЛЬ НАШОЇ КОМПАНІЇ:
- Спеціалізація: Будівельні роботи, капітальний ремонт, реконструкція, дорожні роботи, благоустрій, проектування, вентиляція, тепломережі, водопровід.
- Нецільові сфери (НЕ релевантні): закупівля продуктів харчування, канцтоварів, IT-послуг, меблів, медикаментів, палива, страхування, охоронних послуг тощо.
- Цільовий діапазон сум лотів: від 2 000 000 до 25 000 000 грн.

Проаналізуй вхідні дані тендера та поверни JSON:
{
  "relevant": true | false,
  "reason": "коротке пояснення рішення українською мовою"
}
"""

SCREENER_USER_TEMPLATE = """ДАНІ ТЕНДЕРА:
- Назва: {title}
- Замовник: {entity}
- Очікувана вартість: {amount} грн
- Опис: {description}
"""

async def screen_tender(
    title: str,
    entity: str,
    amount: float,
    description: str = "",
) -> dict:
    """
    Робить швидкий скринінг тендера через дешеву модель (Gemini 2.0 Flash).
    Повертає dict з ключами 'relevant' та 'reason'.
    """
    logger.info(f"🔍 Запуск швидкого AI-скринінгу для тендера: '{title[:50]}...'")
    
    messages = build_messages(
        system_prompt=SCREENER_SYSTEM,
        user_content=SCREENER_USER_TEMPLATE.format(
            title=title,
            entity=entity,
            amount=f"{amount:,.2f}".replace(",", " "),
            description=description or "Не вказано",
        )
    )
    
    result = await call_model("screener", messages, json_mode=True)
    if not result:
        # Fallback при помилці: вважаємо тендер релевантним, щоб не втратити клієнта
        return {"relevant": True, "reason": "Помилка API скринінгу, пропускаємо далі"}
        
    content, usage = result
    parsed = parse_json_response(content)
    if not parsed:
        return {"relevant": True, "reason": "Не вдалося розпарсити відповідь скринінгу, пропускаємо"}
        
    logger.info(f"📊 Результат скринінгу: relevant={parsed.get('relevant')} | {parsed.get('reason')}")
    return parsed
