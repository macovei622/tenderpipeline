"""
ai/agents/calculator.py — Агент 2: AI-Калькулятор

Модель: Qwen 2.5 72B (температура 0.0 — детермінований)
Fallback: Mistral Large

Задача: розрахунок реальної собівартості лоту і маржі.

Захист від помилок:
- Температура 0.0 — жодної варіативності в числах
- Модель не вигадує ціни — тільки ті що надані у вхідних даних
- Якщо ціна не знайдена — явно позначає [ЦІНА ВІДСУТНЯ]
- JSON schema з явними типами (числа, не рядки)
"""
from typing import Optional
from loguru import logger

from ai.client import call_model, parse_json_response, build_messages


CALCULATOR_SYSTEM = """Ти — досвідчений кошторисник (сметчик) у будівництві.
Твоя задача: розрахувати орієнтовну собівартість та маржу участі у тендері.

ПРАВИЛА (обов'язкові):
1. Використовуй ТІЛЬКИ ціни з наданого прайс-листа або кошторису.
   Якщо ціни на матеріал немає — пиши [ЦІНА ВІДСУТНЯ], НЕ вигадуй.
2. Температура = 0.0. Ніякої «округленої оцінки» — тільки точні розрахунки.
3. Всі суми — у гривнях (UAH).
4. Якщо даних недостатньо для розрахунку — вкажи це явно.

ОЧІКУВАНА ЦІНА НА АУКЦІОНІ (з урахуванням конкурентного зниження):
Ціна Аукціону = Ціна тендера × (1 − Знижка% / 100)

ФОРМУЛА СОБІВАРТОСТІ (Скринінгова кошторисна евристика v1.1):
Собівартість = Матеріали + Робота + Транспорт + Накладні витрати + Кошторисний прибуток
де:
  Матеріали = сума (кількість × ціна) по специфікації
  Робота = 25% від матеріалів (будівельні роботи)
  Транспорт = 5% від матеріалів
  Накладні витрати = 15% від вартості роботи (орієнтовно за трудовитратами згідно з ДСТУ Б Д.1.1-1)
  Кошторисний прибуток = 15% від вартості роботи

* ПРИМІТКА: Цей розрахунок є виключно експрес-оцінкою (screening heuristic) для первинного скринінгу і не замінює реальний сметний розрахунок у програмі АВК-5.

МАРЖА = Ціна Аукціону − Собівартість
МАРЖА% = (Маржа / Ціна Аукціону) × 100

ПРАВИЛА ОЦІНКИ РИЗИКУ:
- Якщо специфікація матеріалів відсутня, або прайс-лист не містить цін для більшості (>50%) матеріалів -> "data_completeness": "INCOMPLETE", "margin_risk": "UNKNOWN".
- Якщо дані повні ("data_completeness": "COMPLETE"):
  - Якщо Маржа% < 10% — "margin_risk": "WARNING"
  - Якщо Маржа% < 5% — "margin_risk": "CRITICAL"
  - Інакше — "margin_risk": "LOW"
"""

CALCULATOR_USER_TEMPLATE = """ЦІНА ТЕНДЕРА: {tender_amount} грн
ПЕРЕДБАЧУВАНА ЗНИЖКА НА АУКЦІОНІ: {expected_discount_pct}%

СПЕЦИФІКАЦІЯ МАТЕРІАЛІВ ЗА ТЗ:
{specification}

АКТУАЛЬНИЙ ПРАЙС-ЛИСТ ПОСТАЧАЛЬНИКІВ (Вінниця, {price_date}):
{price_list}

Розрахуй собівартість і маржу. Відповідай строго JSON:
{{
  "tender_amount": {tender_amount},
  "expected_discount_pct": {expected_discount_pct},
  "expected_auction_price": 0,
  "materials_breakdown": [
    {{
      "item": "назва матеріалу",
      "quantity": 0,
      "unit": "м²/т/шт/тощо",
      "unit_price": 0,
      "price_source": "назва постачальника або [ЦІНА ВІДСУТНЯ]",
      "total": 0
    }}
  ],
  "materials_total": 0,
  "labor_cost": 0,
  "transport_cost": 0,
  "overhead_cost": 0,
  "profit_cost": 0,
  "total_cost": 0,
  "margin": 0,
  "margin_pct": 0,
  "data_completeness": "COMPLETE" | "INCOMPLETE",
  "margin_risk": "UNKNOWN" | "LOW" | "WARNING" | "CRITICAL",
  "missing_prices": ["список матеріалів без ціни"],
  "notes": "коментар кошторисника або null"
}}"""


async def calculate_margin(
    tender_amount: float,
    specification_text: str,
    price_list: str = "",
    price_date: str = "актуальний",
    expected_discount_pct: float = 4.0,
    machinery_cost: float = 0.0,
    company_profile: Optional[dict] = None,
) -> Optional[dict]:
    """
    Розраховує собівартість і маржу тендера.

    Args:
        tender_amount:      Сума тендера в грн
        specification_text: Текст специфікації матеріалів з ТЗ
        price_list:         Прайс-лист постачальників (може бути порожнім)
        price_date:         Дата прайс-листа
        expected_discount_pct: Передбачуваний відсоток зниження на аукціоні
        machinery_cost:     Вартість машин та механізмів з кошторису
    
    Returns:
        dict з розрахунком або None при помилці
    """
    if not price_list:
        price_list = "[Прайс-лист не надано — калькулятор розрахує орієнтовно]"

    if company_profile and company_profile.get("cost_estimate"):
        cost_est = company_profile["cost_estimate"]
        breakdown = cost_est.get("breakdown", {})
        
        # Використовуємо реальні дані з кошторису клієнта
        materials_total = float(breakdown.get("materials_total", 0.0))
        labor_cost = float(breakdown.get("labor_total", 0.0))
        transport_cost = float(breakdown.get("transport_total", 0.0))
        
        # Спільна стаття накладні та прибуток
        overheads_and_profit = float(breakdown.get("overheads_and_profit", 0.0))
        # Розподілимо порівну для сумісності з чеклистом та звітом
        overhead_cost = round(overheads_and_profit * 0.5, 2)
        profit_cost = round(overheads_and_profit * 0.5, 2)
        
        machinery_cost_profile = float(breakdown.get("machinery_total", 0.0))
        
        expected_auction_price = round(tender_amount * (1 - expected_discount_pct / 100), 2)
        total_cost = round(materials_total + labor_cost + transport_cost + overheads_and_profit + machinery_cost_profile, 2)
        margin = round(expected_auction_price - total_cost, 2)
        margin_pct = round((margin / expected_auction_price) * 100, 2) if expected_auction_price > 0 else 0.0
        
        risk_level = "LOW"
        if margin_pct < 5.0:
            risk_level = "CRITICAL"
        elif margin_pct < 10.0:
            risk_level = "WARNING"
            
        logger.info(
            f"✅ Калькулятор (використано реальний кошторис клієнта): ціна={expected_auction_price:.0f} UAH | "
            f"собівартість={total_cost:.0f} UAH | маржа={margin_pct:.2f}% | ризик={risk_level}"
        )
        
        return {
            "tender_amount": tender_amount,
            "expected_discount_pct": expected_discount_pct,
            "expected_auction_price": expected_auction_price,
            "materials_total": materials_total,
            "labor_cost": labor_cost,
            "transport_cost": transport_cost,
            "overhead_cost": overhead_cost,
            "profit_cost": profit_cost,
            "machinery_cost": machinery_cost_profile,
            "total_cost": total_cost,
            "margin": margin,
            "margin_pct": margin_pct,
            "data_completeness": "COMPLETE",
            "margin_risk": risk_level,
            "missing_prices": [],
            "notes": cost_est.get("notes") or "Використано реальний сметний розрахунок клієнта.",
            "_meta": {
                "model": "client-estimate-override",
                "cost_usd": 0.0
            }
        }
    
    logger.info(f"📊 AI-Калькулятор: розрахунок маржі для лоту {tender_amount:,.0f} грн (знижка {expected_discount_pct}%, техніка {machinery_cost:,.0f} грн)")
    
    messages = build_messages(
        system_prompt=CALCULATOR_SYSTEM,
        user_content=CALCULATOR_USER_TEMPLATE.format(
            tender_amount=tender_amount,
            expected_discount_pct=expected_discount_pct,
            specification=specification_text[:30_000],  # Специфікація рідко буває довшою
            price_list=price_list[:10_000],
            price_date=price_date,
        )
    )
    
    result = await call_model("qwen-2.5-72b", messages, json_mode=True)
    if not result:
        return None
    
    content, usage = result
    parsed = parse_json_response(content)
    
    if parsed:
        parsed["_meta"] = {
            "model": usage["model"],
            "cost_usd": usage["cost_usd"],
        }
        
        # Детермінований перерахунок у Python для уникнення галюцинацій арифметики у LLM
        if parsed.get("data_completeness") == "COMPLETE":
            materials_total = float(parsed.get("materials_total", 0) or 0)
            
            # Перераховуємо накладні за формулою v1.1
            labor_cost = round(materials_total * 0.25, 2)
            transport_cost = round(materials_total * 0.05, 2)
            overhead_cost = round(labor_cost * 0.15, 2)
            profit_cost = round(labor_cost * 0.15, 2)
            
            # Додаємо техніку як окрему статтю
            total_cost = round(materials_total + labor_cost + transport_cost + overhead_cost + profit_cost + machinery_cost, 2)
            
            # Розрахунок очікуваної ціни аукціону та маржі
            expected_auction_price = round(tender_amount * (1 - expected_discount_pct / 100), 2)
            margin = round(expected_auction_price - total_cost, 2)
            margin_pct = round((margin / expected_auction_price) * 100, 2) if expected_auction_price > 0 else 0.0
            
            # Оновлюємо значення в JSON результаті
            parsed["materials_total"] = materials_total
            parsed["labor_cost"] = labor_cost
            parsed["transport_cost"] = transport_cost
            parsed["overhead_cost"] = overhead_cost
            parsed["profit_cost"] = profit_cost
            parsed["machinery_cost"] = machinery_cost
            parsed["total_cost"] = total_cost
            parsed["expected_auction_price"] = expected_auction_price
            parsed["margin"] = margin
            parsed["margin_pct"] = margin_pct
            
            # Встановлюємо детермінований ризик
            if margin_pct < 5.0:
                parsed["margin_risk"] = "CRITICAL"
            elif margin_pct < 10.0:
                parsed["margin_risk"] = "WARNING"
            else:
                parsed["margin_risk"] = "LOW"
                
            logger.info(
                f"✅ Калькулятор (перераховано в Python): ціна={expected_auction_price:.0f} UAH | "
                f"собівартість={total_cost:.0f} UAH (в т.ч. техніка {machinery_cost:.0f} UAH) | "
                f"маржа={margin_pct:.2f}% | ризик={parsed['margin_risk']}"
            )
        else:
            # ── Fallback: детермінований Python-розрахунок без специфікації ──
            # Якщо LLM повернув INCOMPLETE (немає прайс-листа / кошторис зовнішній),
            # рахуємо евристику прямо від суми тендера за типовою структурою будівельних витрат.
            # Структура: матеріали 50%, робота 25%, транспорт 5%, накладні 7.5%, прибуток 7.5%
            logger.info("ℹ️ Калькулятор: INCOMPLETE — виконуємо Python-евристику від суми тендера")
            expected_auction_price = round(tender_amount * (1 - expected_discount_pct / 100), 2)

            # Типова структура будівельної собівартості (скринінгова евристика v1.2)
            materials_total_h  = round(expected_auction_price * 0.50, 2)
            labor_cost_h       = round(materials_total_h * 0.25, 2)
            transport_cost_h   = round(materials_total_h * 0.05, 2)
            overhead_cost_h    = round(labor_cost_h * 0.15, 2)
            profit_cost_h      = round(labor_cost_h * 0.15, 2)
            total_cost_h = round(
                materials_total_h + labor_cost_h + transport_cost_h
                + overhead_cost_h + profit_cost_h + machinery_cost, 2
            )
            margin_h     = round(expected_auction_price - total_cost_h, 2)
            margin_pct_h = round((margin_h / expected_auction_price) * 100, 2) if expected_auction_price > 0 else 0.0

            parsed["expected_auction_price"] = expected_auction_price
            parsed["materials_total"]  = materials_total_h
            parsed["labor_cost"]       = labor_cost_h
            parsed["transport_cost"]   = transport_cost_h
            parsed["overhead_cost"]    = overhead_cost_h
            parsed["profit_cost"]      = profit_cost_h
            parsed["machinery_cost"]   = machinery_cost
            parsed["total_cost"]       = total_cost_h
            parsed["margin"]           = margin_h
            parsed["margin_pct"]       = margin_pct_h
            parsed["data_completeness"] = "HEURISTIC"   # позначаємо що це евристика

            if margin_pct_h < 5.0:
                parsed["margin_risk"] = "CRITICAL"
            elif margin_pct_h < 10.0:
                parsed["margin_risk"] = "WARNING"
            else:
                parsed["margin_risk"] = "LOW"

            logger.info(
                f"✅ Калькулятор (Python-евристика): ціна={expected_auction_price:.0f} UAH | "
                f"собівартість={total_cost_h:.0f} UAH (в т.ч. техніка {machinery_cost:.0f} UAH) | "
                f"маржа={margin_pct_h:.2f}% | ризик={parsed['margin_risk']}"
            )
            
        # Якщо в нас немає реального кошторису з профілю, додаємо варнінг щодо АВК-5
        if not (company_profile and company_profile.get("cost_estimate")):
            margin_pct = parsed.get("margin_pct", 0) or 0
            parsed["notes"] = (
                f"Маржинальність орієнтовно {margin_pct}%, але для подачі пропозиції терміново "
                "потрібен локальний кошторис від кошторисника в програмному комплексі (АВК-5, "
                "Будівельні Технології: Кошторис тощо) у форматі .ims, .bdd або .imd!"
            )
            
    return parsed


def format_calculator_report(result: dict) -> str:
    """Форматує результат Калькулятора для Telegram."""
    risk = result.get("margin_risk", "?")  # Calculator writes to margin_risk, not risk_level
    icons = {"LOW": "🟢", "WARNING": "🟡", "CRITICAL": "🔴"}

    margin = result.get("margin", 0)
    margin_pct = result.get("margin_pct", 0)
    tender_amount = result.get("tender_amount", 0)
    total_cost = result.get("total_cost", 0)

    lines = [
        f"📊 *AI-Калькулятор: Розрахунок маржі*",
        "",
        f"💰 Сума тендера: *{tender_amount:,.0f} грн*",
        f"🏗 Собівартість: *{total_cost:,.0f} грн*",
        f"  ↳ Матеріали: {result.get('materials_total', 0):,.0f} грн",
        f"  ↳ Робота: {result.get('labor_cost', 0):,.0f} грн",
        f"  ↳ Транспорт: {result.get('transport_cost', 0):,.0f} грн",
        f"  ↳ Накладні: {result.get('overhead_cost', 0):,.0f} грн",
        "",
        f"{icons.get(risk, '⚪')} *Маржа: {margin:,.0f} грн ({margin_pct:.1f}%)*",
    ]

    missing = result.get("missing_prices", [])
    if missing:
        lines += [
            "",
            f"⚠️ *Ціни відсутні ({len(missing)} позицій):*",
            *[f"• {p}" for p in missing[:5]],
        ]

    if result.get("notes"):
        lines += ["", f"💬 {result['notes']}"]

    meta = result.get("_meta", {})
    lines.append(f"\n_Модель: {meta.get('model', '?')} | ${meta.get('cost_usd', 0):.3f}_")

    return "\n".join(lines)
