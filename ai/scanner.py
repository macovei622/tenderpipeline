"""
ai/scanner.py — AI-Сканер тендерної документації

Що робить:
1. Читає текст тендерного документа (з pdf_parser)
2. Ділить на логічні секції (кваліфікація / договір / ТЗ)
3. Запускає Gemini 2.0 Flash з низькою температурою
4. Знаходить дискримінаційні вимоги і прапорці ризику
5. Повертає структурований JSON-звіт

Важлива деталь: використовуємо chunking (шматки) а не весь текст одразу.
Причина: "Lost in the Middle" ефект — LLM губить деталі в середині великих текстів.
"""
import json
import asyncio
from typing import Optional
from loguru import logger
import google.generativeai as genai
from config import GEMINI_API_KEY, GEMINI_MODEL_FAST, GEMINI_MODEL_PRO

# Ініціалізуємо Gemini один раз
genai.configure(api_key=GEMINI_API_KEY)


# ── Системні промпти (суворі, без "творчості") ─────────────────────

SCANNER_PROMPT = """Ти — юридичний аналітик у сфері публічних закупівель України.
Закон: «Про публічні закупівлі», Постанова КМУ №1178.

ЗАДАЧА: Знайди в тендерній документації вимоги, що можуть порушувати принцип недискримінації учасників.

ТИПОВІ ОЗНАКИ ПОРУШЕНЬ (шукай саме це):
1. Вимога про географічну прив'язку (завод, склад в X км від об'єкта) без альтернативи
2. Вимога досвіду ЛИШЕ з бюджетними організаціями (забороняє приватний досвід)
3. Вимога власності техніки (забороняє оренду/лізинг/послуги)
4. Нереальні строки виконання (менше технологічно можливого)
5. Вимога специфічних сертифікатів, не передбачених законодавством
6. Вимога офіційного персоналу (забороняє ГПД-договори)

ТАКОЖ ПЕРЕВІР:
- Чи є гарантія тендера? Яка сума? (повинна бути не більше 0.5% від суми закупівлі)
- Які штрафні санкції в договорі? (якщо >20% — це ризик)
- Строки оплати — якщо більше 30 днів — це збільшує ризик клієнта

ФОРМАТ ВІДПОВІДІ — СТРОГО JSON (нічого крім JSON):
{
  "risk_level": "LOW" | "MEDIUM" | "HIGH",
  "discriminatory_requirements": [
    {
      "type": "назва типу порушення",
      "quote": "дослівна цитата з документа",
      "page_hint": "де знайшов (розділ або приблизне місце)",
      "law_reference": "стаття закону або пункт КМУ №1178",
      "recommendation": "що порадити клієнту"
    }
  ],
  "required_documents_checklist": [
    "Витяг з ЄДРПОУ",
    "Довідка про відсутність заборгованості",
    "... (всі документи що вимагає ТД)"
  ],
  "contract_risks": [
    "опис ризикованого пункту договору (якщо є)"
  ],
  "guarantee_amount": 0,
  "payment_terms_days": 0,
  "summary": "Загальний висновок: варто чи не варто подаватися (1-2 речення)"
}

ПРАВИЛА:
- Відповідай ТІЛЬКИ JSON. Без коментарів до чи після.
- Цитуй ТІЛЬКИ те, що є в тексті. Не вигадуй.
- Якщо даних недостатньо — вкажи поле як null.
- Мова відповіді: українська.
"""


async def analyze_tender_document(
    text: str,
    use_pro_model: bool = False
) -> Optional[dict]:
    """
    Аналізує текст тендерного документа на дискримінаційні вимоги.
    
    Args:
        text: Повний або частковий текст ТД
        use_pro_model: True — використати потужнішу модель для складних кейсів
    
    Returns:
        dict з результатами аналізу або None при помилці
    """
    model_name = GEMINI_MODEL_PRO if use_pro_model else GEMINI_MODEL_FAST
    model = genai.GenerativeModel(model_name)
    
    # Обрізаємо текст до розумного ліміту
    # Flash: 1M токенів, але 150k символів достатньо для більшості ТД
    MAX_CHARS = 150_000
    if len(text) > MAX_CHARS:
        logger.warning(f"⚠️ Текст обрізано: {len(text)} → {MAX_CHARS} символів")
        # Беремо початок (кваліфікація) і кінець (договір) — вони найважливіші
        half = MAX_CHARS // 2
        text = text[:half] + "\n\n[...ПРОПУЩЕНО...]\n\n" + text[-half:]
    
    prompt = f"{SCANNER_PROMPT}\n\nТЕНДЕРНА ДОКУМЕНТАЦІЯ:\n{text}"
    
    try:
        logger.info(f"🤖 Запускаємо AI-Сканер ({model_name})...")
        response = model.generate_content(
            prompt,
            generation_config=genai.GenerationConfig(
                temperature=0.1,      # Майже детермінований — менше фантазій
                max_output_tokens=4096,
            )
        )
        
        raw_text = response.text.strip()
        
        # Очищаємо markdown якщо модель обгорнула в ```json
        if raw_text.startswith("```"):
            raw_text = raw_text.split("```")[1]
            if raw_text.startswith("json"):
                raw_text = raw_text[4:]
        
        result = json.loads(raw_text)
        logger.info(f"✅ AI-Сканер: рівень ризику = {result.get('risk_level')}, "
                   f"знайдено {len(result.get('discriminatory_requirements', []))} порушень")
        return result
    
    except json.JSONDecodeError as e:
        logger.error(f"❌ AI повернув невалідний JSON: {e}")
        logger.debug(f"Відповідь AI: {response.text[:500]}")
        return None
    except Exception as e:
        logger.error(f"❌ Помилка AI-Сканера: {e}")
        return None


async def analyze_in_chunks(sections: dict[str, str]) -> Optional[dict]:
    """
    Аналізує секції документа по черзі (для великих ТД 300+ сторінок).
    
    sections — словник {"Кваліфікаційні критерії": текст, "Договір": текст, ...}
    
    Логіка:
    1. Аналізуємо кожну секцію окремим запитом
    2. Об'єднуємо результати
    3. Повертаємо консолідований звіт
    """
    all_discriminatory = []
    all_checklist = []
    all_contract_risks = []
    risk_levels = []

    for section_name, section_text in sections.items():
        if not section_text.strip():
            continue
        
        logger.info(f"📑 Аналіз секції: {section_name}")
        result = await analyze_tender_document(section_text)
        
        if result:
            risk_levels.append(result.get("risk_level", "LOW"))
            all_discriminatory.extend(result.get("discriminatory_requirements", []))
            all_checklist.extend(result.get("required_documents_checklist", []))
            all_contract_risks.extend(result.get("contract_risks", []))
        
        # Пауза між запитами (rate limit)
        await asyncio.sleep(2)
    
    if not risk_levels:
        return None
    
    # Визначаємо фінальний рівень ризику (найвищий з усіх секцій)
    priority = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
    final_risk = max(risk_levels, key=lambda x: priority.get(x, 0))
    
    # Дедублікуємо чеклист
    unique_checklist = list(dict.fromkeys(all_checklist))
    
    return {
        "risk_level": final_risk,
        "discriminatory_requirements": all_discriminatory,
        "required_documents_checklist": unique_checklist,
        "contract_risks": all_contract_risks,
        "summary": f"Проаналізовано {len(sections)} секцій документа. "
                   f"Знайдено {len(all_discriminatory)} потенційних порушень."
    }


def format_scan_report(scan_result: dict, tender_title: str = "") -> str:
    """
    Форматує результат сканування у Telegram-повідомлення з Markdown.
    """
    risk = scan_result.get("risk_level", "UNKNOWN")
    risk_emoji = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢"}.get(risk, "⚪")
    
    lines = [
        f"🔍 *Результати AI-аналізу*",
        f"{'📋 ' + tender_title if tender_title else ''}",
        "",
        f"{risk_emoji} *Рівень ризику: {risk}*",
        "",
    ]
    
    # Дискримінаційні вимоги
    discrim = scan_result.get("discriminatory_requirements", [])
    if discrim:
        lines.append(f"⚠️ *Знайдені ловушки ({len(discrim)}):*")
        for i, req in enumerate(discrim[:5], 1):  # Максимум 5 в повідомленні
            lines.append(f"{i}. *{req.get('type', '?')}*")
            quote = req.get("quote", "")
            if quote:
                lines.append(f'   _«{quote[:150]}»_')
            rec = req.get("recommendation", "")
            if rec:
                lines.append(f"   ✏️ {rec}")
        if len(discrim) > 5:
            lines.append(f"   _...і ще {len(discrim) - 5} пунктів_")
        lines.append("")
    else:
        lines.append("✅ Дискримінаційних вимог не знайдено")
        lines.append("")
    
    # Ризики договору
    contract_risks = scan_result.get("contract_risks", [])
    if contract_risks:
        lines.append(f"📜 *Ризики договору:*")
        for risk_item in contract_risks[:3]:
            lines.append(f"• {risk_item[:200]}")
        lines.append("")
    
    # Чеклист документів
    checklist = scan_result.get("required_documents_checklist", [])
    if checklist:
        lines.append(f"📎 *Потрібні документи ({len(checklist)}):*")
        for doc in checklist[:8]:
            lines.append(f"☐ {doc}")
        if len(checklist) > 8:
            lines.append(f"_...і ще {len(checklist) - 8} документів_")
        lines.append("")
    
    # Підсумок
    summary = scan_result.get("summary", "")
    if summary:
        lines.append(f"💬 *Висновок:* {summary}")
    
    return "\n".join(lines)
