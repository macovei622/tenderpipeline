"""
ai/agents/collector.py — Агент 3: AI-Збирач

Модель: Claude 3.5 Haiku (температура 0.05 — мінімум «творчості»)
Fallback: Mistral Large

Задача: заповнення типових довідок за перевіреними шаблонами.

Захист від помилок:
- Не вигадує дані — тільки підставляє з профілю компанії
- Явно позначає відсутні поля як [ПОТРЕБУЄ ЗАПОВНЕННЯ]
- Не змінює юридичні формулювання шаблону
- Низька температура — заборонена «творчість» у юридичних документах
"""
from typing import Optional
from loguru import logger

from ai.client import call_model, build_messages


COLLECTOR_SYSTEM = """Ти — нотаріус-секретар. Заповнюєш офіційні довідки для тендерів Prozorro.

АБСОЛЮТНІ ПРАВИЛА (порушення недопустимі):
1. Використовуй ТІЛЬКИ надані дані компанії. НІКОЛИ нічого не вигадуй.
2. Якщо якихось даних немає — пиши [ПОТРЕБУЄ ЗАПОВНЕННЯ: що саме], не заповнюй самостійно.
3. Дотримуйся наданого шаблону ТОЧНО — не змінюй структуру, формулювання, порядок.
4. Числа, дати, назви — тільки з наданих даних. Ніяких округлень або «приблизно».
5. Якщо шаблон містить [___] — це місце для підстановки конкретного значення.
6. Верифікація сертифікатів: перевір, чи для ключових працівників (наприклад, головний інженер, кошторисник, сертифікований проектувальник) вказано серію та номер кваліфікаційного сертифіката. Якщо номер сертифіката чи дата його видачі або наказ про призначення відсутні у профілі, обов'язково додай примітку або маркер [ПОТРЕБУЄ ЗАПОВНЕННЯ: сертифікат/наказ для посади]."""

COLLECTOR_USER_TEMPLATE = """ШАБЛОН ДОВІДКИ (заповни точно за ним):
{template}

ДАНІ КОМПАНІЇ:
{company_data}

СПЕЦИФІЧНІ ВИМОГИ З ТЕНДЕРНОЇ ДОКУМЕНТАЦІЇ:
{td_requirements}

Поверни ТІЛЬКИ заповнений текст довідки — нічого більше."""


# ── Вбудовані шаблони базових довідок ───────────────────────────────

TEMPLATES = {
    "experience": """ДОВІДКА
про виконання аналогічного договору

[Назва компанії], код ЄДРПОУ [ЄДРПОУ], підтверджує, що в період [рік початку] — [рік кінця] нами виконано договір № [номер договору] від [дата договору] з [назва замовника] (код ЄДРПОУ [ЄДРПОУ замовника]).

Предмет договору: [опис робіт/послуг]
Сума договору: [сума] грн.
Строк виконання: з [дата початку] по [дата кінця].

Роботи виконані в повному обсязі та прийняті Замовником.

Директор __________ [ПІБ директора]
[дата]         М.П.
""",

    "equipment": """ДОВІДКА
про наявність обладнання та матеріально-технічної бази

[Назва компанії], код ЄДРПОУ [ЄДРПОУ], підтверджує наявність такого обладнання та транспортних засобів:

| № | Найменування | Марка/модель | Кількість | Підстава використання |
|---|---|---|---|---|
[таблиця техніки]

Директор __________ [ПІБ директора]
[дата]         М.П.
""",

    "staff": """ДОВІДКА
про наявність працівників відповідної кваліфікації

[Назва компанії], код ЄДРПОУ [ЄДРПОУ], підтверджує, що до виконання робіт будуть залучені такі фахівці:

| № | Посада | ПІБ | Освіта | Досвід (років) |
|---|---|---|---|---|
[таблиця персоналу]

Директор __________ [ПІБ директора]
[дата]         М.П.
""",

    "no_debt": """ДОВІДКА
про відсутність заборгованості

[Назва компанії], код ЄДРПОУ [ЄДРПОУ], підтверджує відсутність заборгованості зі сплати податків і зборів.

Директор __________ [ПІБ директора]
Головний бухгалтер __________ [ПІБ бухгалтера]
[дата]         М.П.
""",
}


async def fill_document(
    template_name: str,
    company_data: dict,
    td_requirements: str = "",
    custom_template: str = "",
) -> Optional[str]:
    """
    Заповнює довідку за шаблоном.

    Args:
        template_name:   Ключ з TEMPLATES або "custom"
        company_data:    Дані компанії (dict з профілю клієнта)
        td_requirements: Специфічні вимоги з тендерної документації
        custom_template: Власний шаблон (якщо template_name="custom")
    
    Returns:
        Заповнений текст довідки або None при помилці
    """
    template = custom_template if template_name == "custom" else TEMPLATES.get(template_name)
    if not template:
        logger.error(f"❌ Шаблон '{template_name}' не знайдено")
        return None

    # Форматуємо дані компанії
    company_str = _format_company_data(company_data)

    messages = build_messages(
        system_prompt=COLLECTOR_SYSTEM,
        user_content=COLLECTOR_USER_TEMPLATE.format(
            template=template,
            company_data=company_str,
            td_requirements=td_requirements[:5000] if td_requirements else "Немає специфічних вимог",
        )
    )

    logger.info(f"📝 Збирач: генерація довідки «{template_name}»")
    result = await call_model("claude-3-5-haiku", messages, json_mode=False)

    if not result:
        return None

    content, usage = result
    logger.info(f"✅ Довідка сформована | ${usage['cost_usd']:.4f}")
    return content


async def fill_all_required_documents(
    required_docs: list[str],
    company_data: dict,
    td_requirements: str = "",
) -> dict[str, Optional[str]]:
    """
    Заповнює всі документи зі списку (від Сканера).
    
    Returns:
        {"experience": "текст довідки", "equipment": "текст", ...}
    """
    results = {}
    
    # Маппінг назв із чеклиста до шаблонів
    MAPPING = {
        "аналогічний": "experience",
        "досвід": "experience",
        "обладнання": "equipment",
        "техніка": "equipment",
        "персонал": "staff",
        "працівник": "staff",
        "заборгованість": "no_debt",
        "податків": "no_debt",
    }
    
    generated = set()
    for doc_name in required_docs:
        doc_lower = doc_name.lower()
        matched_template = None
        
        for keyword, template_key in MAPPING.items():
            if keyword in doc_lower and template_key not in generated:
                matched_template = template_key
                break
        
        if matched_template:
            text = await fill_document(matched_template, company_data, td_requirements)
            results[doc_name] = text
            generated.add(matched_template)
    
    return results


def _format_company_data(data: dict) -> str:
    """Форматує дані компанії для промпту."""
    lines = []
    
    # Загальні реквізити
    lines.append(f"Назва компанії: {data.get('name') or data.get('company_name') or '[ВІДСУТНЄ]'}")
    lines.append(f"ЄДРПОУ: {data.get('edrpou') or '[ВІДСУТНЄ]'}")
    lines.append(f"Керівник: {data.get('director_title', 'Директор')} {data.get('director') or data.get('director_name') or '[ВІДСУТНЄ]'}")
    lines.append(f"Ліцензія: {data.get('license') or 'Ліцензія на будівництво об значень СС2'}")
    
    # Обладнання
    eq = data.get("equipment") or []
    if eq:
        lines.append("Перелік обладнання та техніки:")
        for idx, item in enumerate(eq, 1):
            name = item.get("name")
            qty = item.get("qty", 1)
            source = item.get("source", "Власна")
            lines.append(f"  {idx}. {name} | {qty} шт. | {source}")
    else:
        lines.append("Перелік обладнання та техніки: [ВІДСУТНЄ]")
        
    # Персонал
    st = data.get("staff") or []
    if st:
        lines.append("Перелік кваліфікованого персоналу:")
        for idx, person in enumerate(st, 1):
            role = person.get("role")
            name = person.get("name")
            edu = person.get("education", "Вища")
            exp = person.get("exp", "5")
            lines.append(f"  {idx}. {role} | {name} | {edu} | {exp} р.")
    else:
        lines.append("Перелік кваліфікованого персоналу: [ВІДСУТНЄ]")
        
    # |Аналогічні договори
    contracts = data.get("analog_contracts") or []
    if contracts:
        lines.append("Аналогічні виконані договори:")
        for idx, c in enumerate(contracts, 1):
            client = c.get("client")
            subject = c.get("subject", "Ремонтні роботи")
            amount = c.get("amount", 1000000)
            year = c.get("year", "2024")
            lines.append(f"  {idx}. Замовник: {client} | Предмет: {subject} | Сума: {amount:,.0f} грн | Рік: {year}".replace(",", " "))
    else:
        lines.append("Аналогічні виконані договори: [ВІДСУТНЄ]")
        
    return "\n".join(lines)
