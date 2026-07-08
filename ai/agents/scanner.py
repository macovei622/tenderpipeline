"""
ai/agents/scanner.py — Агент 1: AI-Сканер

Модель: DeepSeek R1 (chain-of-thought reasoning)
Fallback: Claude 3.7 Sonnet

Задача: юридичний аналіз тендерної документації.
Шукає дискримінаційні вимоги, ловушки в договорі, прапорці ризику.

Захист від помилок:
- Температура 0.1 — мінімум «фантазії»
- JSON mode — структурована відповідь, не вільний текст
- Fallback-ланцюг при недоступності основної моделі
- Chunked аналіз для великих документів
"""
import asyncio
from typing import Optional
from loguru import logger

from ai.client import call_model, parse_json_response, build_messages
from ai.pdf_parser import split_into_sections


# ── Системний промпт Сканера ─────────────────────────────────────────

SCANNER_SYSTEM = """Ти — юридичний аналітик у сфері публічних закупівель України.
Нормативна база: Закон «Про публічні закупівлі», Постанова КМУ №1178 («Особливості»).

ЗАДАЧА: Проаналізуй тендерну документацію і знайди:
1. Дискримінаційні вимоги (обмежують коло учасників незаконно)
2. Ризики договору (штрафи, строки, умови оплати)
3. Повний чеклист документів які вимагає ТД

СТАНДАРТИЗАЦІЯ НАЗВ ДОКУМЕНТІВ (ОБОВ'ЯЗКОВО!):
Для уникнення семантичної варіативності, використовуй ТІЛЬКИ наступні стандартні назви для виявлених документів:
- 'Довідка про наявність обладнання/матеріально-технічної бази'
- 'Довідка про наявність кваліфікованого персоналу'
- 'Довідка про досвід виконання аналогічних договорів'
- 'Копії аналогічних договорів'
- 'Позитивні відгуки від замовників'
- 'Документи про повноваження особи'
- 'Гарантійний лист щодо охорони праці'
- 'Ліцензія та дозволи/декларації'
- 'Локальний кошторис (IMD)'
- 'Локальний кошторис (DOCX)'
- 'Підсумкова відомість ресурсів'
- 'Інформація про субпідрядників/співвиконавців'
- 'Декларація про відсутність підстав для відмови'
- 'Забезпечення виконання договору'

Не перефразовуй і не дописуй зайвих деталей, використовуй саме ці стандартні назви!

ТИПОВІ ПРИКЛАДИ ДИСКРИМІНАЦІЇ (це не вичерпний список! Шукай будь-які умови, що штучно звужують конкуренцію або обмежують права учасників):
- Географічна прив'язка виробника/складу без альтернативи (порушує ст. 16 ЗУ)
- Досвід ЛИШЕ з бюджетними організаціями (заборонено — ст. 16 ч. 2)
- Власність техніки (забороняє оренду/лізинг — порушення ст. 16)
- Нереальні строки виконання (менші за технологічно можливі)
- Специфічні сертифікати поза законодавством
- Заборона ГПД-договорів для персоналу

СТАНДАРТИЗАЦІЯ НАЗВ ТИПІВ ДИСКРИМІНАЦІЇ (ОБОВ'ЯЗКОВО!):
При заповненні поля "type" використовуй строго визначені назви для виявлених порушень, якщо вони відповідають одній з цих категорій:
- 'Географічне обмеження АБЗ/складу' (для гео-прив'язок)
- 'Досвід лише з бюджетними замовниками' (для обмежень щодо бюджетних організацій)
- 'Заборона оренди/лізингу техніки' (для вимог щодо виключної власності)
- 'Штучно стислі строки виконання робіт'
- 'Вимога специфічних сертифікатів/ліцензій'
- 'Заборона залучення працівників за ГПД'
- 'Обмеження досвіду за специфічним класом об\'єкта' (наприклад, вимоги щодо класу об\'єкта чи доріг СС1/СС2/СС3)

Не перефразовуй і не дописуй зайвих деталей у полі "type", використовуй саме ці стандартні назви для відповідних категорій!

РИЗИКИ ДОГОВОРУ:
- Штрафи > 20% — позначай як КРИТИЧНИЙ
- Строки оплати > 30 днів — позначай як УВАГА
- Гарантія > 0.5% від суми закупівлі — позначай як УВАГА
- Умови дострокового розірвання без відшкодування

ПОРІВНЯЛЬНИЙ АНАЛІЗ ПРОФІЛЮ УЧАСНИКА (якщо надано ДАНІ ПРОФІЛЮ):
1. Клас наслідків (СС2/СС3): порівняй клас наслідків, який вимагається у ТД, з ліцензією учасника. Якщо ТД вимагає клас наслідків вище (наприклад, вимоги СС2, а у компанії лише СС1; або вимагається СС3, а у компанії СС2), додай це як дискримінаційне обмеження 'Обмеження досвіду за специфічним класом об\'єкта' з рекомендацією оскаржити вимогу або не брати участь.
2. Фінансова спроможність: порівняй очікувану вартість тендера з річним оборотом/доходом компанії. Якщо ТД вимагає річний дохід більше ніж дохід компанії (наприклад, вимога річного доходу >= 50% чи 100% очікуваної вартості, а учасник не відповідає цьому), додай дискримінаційне обмеження 'Обмеження за фінансовою спроможністю'.
3. Специфічне МТБ (прилади з повіркою): якщо ТД вимагає специфічне вимірювальне обладнання з діючою повіркою (наприклад, 'вимірювач міцності бетону ультразвуковий лазерний', 'тепловізор' тощо), а його немає в переліку наявного обладнання учасника, додай 'Штучні вимоги до МТБ' як ризик договору з рекомендацією орендувати цей прилад або оскаржити вимогу.

ПРАВИЛА ВІДПОВІДІ:
- Тільки JSON. Нічого крім JSON.
- Цитуй дослівно тільки те, що є в тексті.
- Якщо даних немає — null, не вигадуй.
- Мова: українська."""

SCANNER_USER_TEMPLATE = """ТЕНДЕРНА ДОКУМЕНТАЦІЯ:
{text}

Відповідай строго JSON:
{{
  "risk_level": "LOW" | "MEDIUM" | "HIGH",
  "discriminatory_requirements": [
    {{
      "type": "назва типу",
      "quote": "дослівна цитата (макс 300 символів)",
      "section": "назва розділу або null",
      "law_reference": "стаття закону або null",
      "recommendation": "що зробити клієнту"
    }}
  ],
  "contract_risks": [
    {{
      "type": "CRITICAL" | "WARNING" | "INFO",
      "description": "опис ризику",
      "quote": "цитата або null"
    }}
  ],
  "required_documents": ["список документів що вимагає ТД"],
  "guarantee_amount_pct": null,
  "payment_terms_days": null,
  "summary": "висновок: варто/не варто подаватися (1-2 речення)"
}}"""


# ── Основна функція сканування ────────────────────────────────────────

async def scan_document(
    text: str,
    doc_size_chars: int = 0,
    tender_meta: Optional[dict] = None,
    company_profile: Optional[dict] = None,
) -> Optional[dict]:
    """
    Аналізує текст тендерного документа.
    
    Автоматично вибирає стратегію:
    - Маленький/середній документ (<100k символів) → один запит
    - Великий документ (>100k символів) → chunked аналіз по секціях
    
    Returns:
        dict з результатами або None при помилці
    """
    size = doc_size_chars or len(text)
    
    # Для великих документів використовуємо chunked аналіз
    if size > 100_000:
        logger.info(f"📑 Великий документ ({size:,} симв) — запускаємо секційний аналіз")
        sections = split_into_sections(text)
        return await _scan_in_chunks(sections, tender_meta, company_profile)
    
    return await _scan_single(text, tender_meta, company_profile)


async def _scan_single(
    text: str,
    tender_meta: Optional[dict] = None,
    company_profile: Optional[dict] = None,
) -> Optional[dict]:
    """Аналіз одним запитом (малі та середні документи)."""
    
    # Вибираємо модель: якщо документ займе > 50k токенів → Gemini 2.5 Pro
    # Орієнтовно 1 токен ≈ 3 символи для кирилиці
    estimated_tokens = len(text) // 3
    model_key = "gemini-2-5-pro" if estimated_tokens > 100_000 else "deepseek-r1"
    
    logger.info(f"🔍 AI-Сканер: {model_key} (~{estimated_tokens:,} токенів)")
    
    # Складаємо глобальний контекст тендера
    meta_context = ""
    if tender_meta:
        amount = tender_meta.get("value", {}).get("amount", 0) or 0
        meta_context = (
            "=== ГЛОБАЛЬНИЙ КОНТЕКСТ ТЕНДЕРА (використовуй для порівняння з вимогами цієї секції) ===\n"
            f"Назва предмета закупівлі: {tender_meta.get('title')}\n"
            f"Замовник: {tender_meta.get('procuringEntity', {}).get('name') or tender_meta.get('procuring_entity')}\n"
            f"Очікувана вартість: {amount:,.2f} UAH\n".replace(",", " ") +
            f"Опис предмета закупівлі: {tender_meta.get('description') or 'Не вказано'}\n"
            "========================================================================================\n\n"
        )
    
    profile_context = ""
    if company_profile:
        license_str = company_profile.get("license") or "Не вказано"
        revenue = company_profile.get("annual_revenue") or company_profile.get("cost_estimate", {}).get("breakdown", {}).get("total")
        revenue_str = f"{revenue:,.0f} UAH" if revenue else "Не вказано"
        
        # Перелік обладнання
        eq_names = [e.get("name") for e in company_profile.get("equipment", [])]
        eq_str = ", ".join(eq_names) if eq_names else "Не вказано"
        
        # Документи про судимість / податки
        docs = []
        if company_profile.get("has_no_conviction"):
            docs.append("Довідка про відсутність судимості (наявна)")
        if company_profile.get("has_no_debt"):
            docs.append("Довідка про відсутність заборгованості з податків (наявна)")
            
        docs_str = ", ".join(docs) if docs else "Немає даних"
        
        profile_context = (
            "\n=== ПРОФІЛЬ УЧАСНИКА (порівняй вимоги ТД з цими даними) ===\n"
            f"- Назва компанії: {company_profile.get('name')}\n"
            f"- Ліцензія/клас наслідків: {license_str}\n"
            f"- Річний оборот/дохід: {revenue_str}\n"
            f"- Наявне обладнання/МТБ: {eq_str}\n"
            f"- Наявні довідки: {docs_str}\n"
            "==========================================================\n\n"
        )
    
    full_user_content = meta_context + SCANNER_USER_TEMPLATE.format(text=text[:150_000])
    if profile_context:
        full_user_content += profile_context
    
    messages = build_messages(
        system_prompt=SCANNER_SYSTEM,
        user_content=full_user_content
    )
    
    result = await call_model(model_key, messages, json_mode=True)
    if not result:
        return None
    
    content, usage = result
    parsed = parse_json_response(content)
    
    if parsed:
        parsed["_meta"] = {
            "model": usage["model"],
            "cost_usd": usage["cost_usd"],
            "input_tokens": usage["input_tokens"],
        }
    
    return parsed


async def _scan_in_chunks(
    sections: dict[str, str],
    tender_meta: Optional[dict] = None,
    company_profile: Optional[dict] = None,
) -> Optional[dict]:
    """
    Аналізує документ по секціях (для великих ТД).
    
    Стратегія:
    - Кожна секція аналізується окремо з ін'єкцією глобального контексту
    - Результати об'єднуються
    - Фінальний рівень ризику = максимальний з усіх секцій
    """
    all_discriminatory = []
    all_contract_risks = []
    all_documents = []
    risk_levels = []
    summaries = []
    total_cost = 0.0

    for section_name, section_text in sections.items():
        if len(section_text.strip()) < 200:  # Пропускаємо мікро-секції
            continue
        
        logger.info(f"📑 Сканую секцію: «{section_name[:50]}»")
        result = await _scan_single(section_text, tender_meta, company_profile)
        
        if result:
            risk_levels.append(result.get("risk_level", "LOW"))
            all_discriminatory.extend(result.get("discriminatory_requirements", []))
            all_contract_risks.extend(result.get("contract_risks", []))
            all_documents.extend(result.get("required_documents", []))
            if result.get("summary"):
                summaries.append(result["summary"])
            total_cost += result.get("_meta", {}).get("cost_usd", 0)
        
        await asyncio.sleep(1.5)  # Rate limit пауза між запитами

    if not risk_levels:
        return None

    priority = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
    final_risk = max(risk_levels, key=lambda x: priority.get(x, 0))
    unique_docs = list(dict.fromkeys(all_documents))  # Дедублікація

    return {
        "risk_level": final_risk,
        "discriminatory_requirements": all_discriminatory,
        "contract_risks": all_contract_risks,
        "required_documents": unique_docs,
        "summary": " | ".join(summaries[:2]),
        "_meta": {
            "model": "multi-section",
            "sections_analyzed": len(sections),
            "cost_usd": round(total_cost, 4),
        }
    }


def format_report(result: dict, tender_title: str = "") -> str:
    """Форматує результат Сканера у Telegram Markdown повідомлення."""
    risk = result.get("risk_level", "?")
    icons = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢"}
    risk_icon = icons.get(risk, "⚪")

    meta = result.get("_meta", {})
    cost_line = f"_Модель: {meta.get('model', '?')} | Вартість: ${meta.get('cost_usd', 0):.3f}_"

    lines = [
        f"🔍 *AI-Сканер: Звіт*",
    ]
    if tender_title:
        lines.append(f"📋 _{tender_title[:80]}_")
    lines += ["", f"{risk_icon} *Рівень ризику: {risk}*", ""]

    discrim = result.get("discriminatory_requirements", [])
    if discrim:
        lines.append(f"⚠️ *Знайдено ловушок: {len(discrim)}*")
        for i, r in enumerate(discrim[:4], 1):
            lines.append(f"\n*{i}. {r.get('type', '?')}*")
            if r.get("quote"):
                lines.append(f'   _«{r["quote"][:200]}»_')
            if r.get("recommendation"):
                lines.append(f"   ✏️ {r['recommendation']}")
        if len(discrim) > 4:
            lines.append(f"\n_...і ще {len(discrim) - 4} пунктів_")
    else:
        lines.append("✅ *Дискримінаційних вимог не знайдено*")
    lines.append("")

    contract_risks = result.get("contract_risks", [])
    criticals = [r for r in contract_risks if r.get("type") == "CRITICAL"]
    warnings = [r for r in contract_risks if r.get("type") == "WARNING"]
    if criticals:
        lines.append("🚨 *КРИТИЧНІ ризики договору:*")
        for r in criticals:
            lines.append(f"• {r.get('description', '')}")
        lines.append("")
    if warnings:
        lines.append("⚠️ *Попередження по договору:*")
        for r in warnings[:2]:
            lines.append(f"• {r.get('description', '')}")
        lines.append("")

    docs = result.get("required_documents", [])
    if docs:
        lines.append(f"📎 *Потрібні документи ({len(docs)}):*")
        for doc in docs[:7]:
            lines.append(f"☐ {doc}")
        if len(docs) > 7:
            lines.append(f"_...і ще {len(docs) - 7}_")
        lines.append("")

    if result.get("summary"):
        lines.append(f"💬 *Висновок:* {result['summary']}")
    lines.append("")
    lines.append(cost_line)

    return "\n".join(lines)
