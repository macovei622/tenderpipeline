"""
ai/agents/reviewer.py — Агент 4: AI-Перевіряючий (self-critique loop)

Модель: Claude 3.7 Sonnet (найкращий для пошуку протиріч)
Fallback: DeepSeek R1

Задача: КРИТИЧНИЙ аудит всього пакету документів перед поданням.
Роль навмисно антагоністична — знайти все що може стати причиною дискваліфікації.

Захист від помилок:
- Протилежна роль до Збирача (він будує, Перевіряючий руйнує → знаходить слабкі місця)
- Перевірка текстових збігів між документами різних клієнтів (anti-collusion)
- Чеклист відповідності кожного документа вимогам ТД
- Фінальний вердикт: READY / NEEDS_FIX / BLOCKED
"""
from typing import Optional
from loguru import logger

from ai.client import call_model, parse_json_response, build_messages


REVIEWER_SYSTEM = """Ти — прокурор-аудитор. Твоя задача — знайти ВСЕ, що може призвести до дискваліфікації учасника тендера.

РОЛЬ: Ти навмисно шукаєш слабкі місця. Будь безжальним. Не пропусти нічого.

ЧЕКЛИСТ АУДИТУ:
1. ВІДПОВІДНІСТЬ ВИМОГАМ ТД
   - Кожен документ з чеклиста ТД є в пакеті?
   - Всі документи відповідають точним вимогам ТД (форма, зміст, строки дії)?

2. ВНУТРІШНЯ КОНСИСТЕНТНІСТЬ ТА СТРОКИ ДІЇ
   - ЄДРПОУ однаковий у всіх документах?
   - Назва компанії однакова скрізь (з урахуванням скорочень)?
   - Директор, підписи — відповідність між документами?
   - Строки дії критичних довідок (МВС про відсутність судимості директора, ДПС про відсутність податкового боргу):
     * Знайди дату видачі довідки МВС та довідки ДПС у пакеті документів.
     * Порівняй дату видачі з датою розкриття тендера. Якщо довідка прострочена або закінчується до/в день торгів (стандартний строк дії 10-30 днів), обов'язково встанови вердикт NEEDS_FIX та познач це як WARNING з рекомендацією терміново оновити довідку.

3. ФІНАНСОВА ГАРАНТІЯ
   - Надана у вимаганій формі (банківська гарантія / депозит)?
   - Сума відповідає вимогам ТД?
   - Строк дії не менший за дедлайн тендера?

4. ОЗНАКИ КООРДИНАЦІЇ (КРИТИЧНО!)
   - Чи є в документах ідентичні формулювання, що вказують на копіювання від іншого учасника?
   - Чи є в документах посилання на інші компанії що теж беруть участь у цьому тендері?

5. ТЕХНІЧНІ ВИМОГИ
   - Файли в правильних форматах (PDF / docx)?
   - Розмір файлів у межах допустимого?
   - Підписи/печатки де вимагаються?

ВЕРДИКТ:
- READY: можна подавати (всі документи наявні, критичних помилок немає)
- NEEDS_FIX: є виправні помилки, відсутні або неповні документи з чеклиста, які учасник може додати або доопрацювати (наприклад, відсутня довідка про персонал, копія договору, відгук тощо). Позначай такі проблеми як severity: WARNING, а вердикт встановлюй NEEDS_FIX.
- BLOCKED: є критична непереборна проблема, яка робить участь принципово неможливою (наприклад, компанія в списку санкцій, повна відсутність обов'язкової ліцензії СС2/СС3 без можливості її отримати під цей тендер, тощо). Відсутність довідок чи копій документів, які можна підготувати/додати, НЕ є підставою для BLOCKED."""

REVIEWER_USER_TEMPLATE = """ВИМОГИ ТЕНДЕРНОЇ ДОКУМЕНТАЦІЇ:
{td_requirements}

ПАКЕТ ДОКУМЕНТІВ ДЛЯ АУДИТУ:
{documents_package}

ЧЕКЛИСТ ДОКУМЕНТІВ (від Сканера):
{required_docs_checklist}

Виконай повний аудит і відповідай JSON:
{{
  "verdict": "READY" | "NEEDS_FIX" | "BLOCKED",
  "issues": [
    {{
      "severity": "CRITICAL" | "WARNING" | "INFO",
      "document": "назва документа де знайдено проблему",
      "issue": "опис проблеми",
      "fix": "як виправити"
    }}
  ],
  "collusion_risk": false,
  "collusion_details": null,
  "checklist_status": {{
    "назва документа": "PRESENT" | "MISSING" | "INCOMPLETE"
  }},
  "ready_to_submit": false,
  "summary": "фінальний вердикт одним реченням"
}}"""


async def review_package(
    td_requirements: str,
    documents_package: dict[str, str],
    required_docs_checklist: list[str],
) -> Optional[dict]:
    """
    Критичний аудит пакету документів перед поданням.
    
    Args:
        td_requirements:      Вимоги з тендерної документації (текст)
        documents_package:    Словник {назва: текст документа}
        required_docs_checklist: Список документів з Сканера
    
    Returns:
        dict з вердиктом і списком проблем або None при помилці
    """
    # Форматуємо пакет документів для промпту
    docs_text = ""
    for doc_name, doc_content in documents_package.items():
        docs_text += f"\n\n=== {doc_name.upper()} ===\n{doc_content[:3000]}"
        if len(doc_content) > 3000:
            docs_text += "\n[...документ обрізано для аналізу...]"
    
    checklist_text = "\n".join(f"- {d}" for d in (required_docs_checklist or []))
    
    messages = build_messages(
        system_prompt=REVIEWER_SYSTEM,
        user_content=REVIEWER_USER_TEMPLATE.format(
            td_requirements=td_requirements[:10_000],
            documents_package=docs_text[:50_000],
            required_docs_checklist=checklist_text,
        )
    )
    
    logger.info("🔎 AI-Перевіряючий: аудит пакету документів...")
    result = await call_model("reviewer", messages, json_mode=True)
    
    if not result:
        return None
    
    content, usage = result
    parsed = parse_json_response(content)
    
    if parsed:
        verdict = parsed.get("verdict", "?")
        issues = parsed.get("issues", [])
        criticals = [i for i in issues if i.get("severity") == "CRITICAL"]
        
        logger.info(
            f"✅ Перевіряючий: вердикт={verdict} | "
            f"проблем={len(issues)} (критичних={len(criticals)}) | "
            f"${usage['cost_usd']:.4f}"
        )
        
        parsed["_meta"] = {
            "model": usage["model"],
            "cost_usd": usage["cost_usd"],
        }
    
    return parsed


def format_review_report(result: dict) -> str:
    """Форматує результат Перевіряючого для Telegram."""
    verdict = result.get("verdict", "?")
    verdict_icons = {
        "READY": "✅",
        "NEEDS_FIX": "🟡",
        "BLOCKED": "🔴",
    }
    
    issues = result.get("issues", [])
    criticals = [i for i in issues if i.get("severity") == "CRITICAL"]
    warnings = [i for i in issues if i.get("severity") == "WARNING"]
    
    lines = [
        f"🔎 *AI-Перевіряючий: Аудит пакету*",
        "",
        f"{verdict_icons.get(verdict, '⚪')} *ВЕРДИКТ: {verdict}*",
        "",
    ]
    
    if result.get("collusion_risk"):
        lines += [
            "🚨 *УВАГА: ОЗНАКИ КООРДИНАЦІЇ!*",
            f"_{result.get('collusion_details', '')}_",
            "",
        ]
    
    if criticals:
        lines.append(f"🔴 *Критичні проблеми ({len(criticals)}):*")
        for issue in criticals:
            lines.append(f"\n• *{issue.get('document', '?')}*")
            lines.append(f"  {issue.get('issue', '')}")
            if issue.get("fix"):
                lines.append(f"  ✏️ _{issue['fix']}_")
        lines.append("")
    
    if warnings:
        lines.append(f"⚠️ *Попередження ({len(warnings)}):*")
        for issue in warnings[:3]:
            lines.append(f"• {issue.get('issue', '')}")
        lines.append("")
    
    # Статус чеклиста
    checklist = result.get("checklist_status", {})
    if checklist:
        missing = [k for k, v in checklist.items() if v == "MISSING"]
        incomplete = [k for k, v in checklist.items() if v == "INCOMPLETE"]
        if missing:
            lines.append(f"❌ *Відсутні документи:*")
            for doc in missing:
                lines.append(f"• {doc}")
            lines.append("")
        if incomplete:
            lines.append(f"⚠️ *Неповні документи:*")
            for doc in incomplete:
                lines.append(f"• {doc}")
            lines.append("")
    
    if result.get("summary"):
        lines.append(f"💬 *Підсумок:* {result['summary']}")
    
    meta = result.get("_meta", {})
    lines.append(f"\n_Модель: {meta.get('model', '?')} | ${meta.get('cost_usd', 0):.3f}_")
    
    return "\n".join(lines)
