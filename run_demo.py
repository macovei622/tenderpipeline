"""
run_demo.py — Скрипт для локальної демонстрації роботи конвеєра v2.1
Завантажує реальний тендер по Вінниці та проганяє його через повний конвеєр.
"""
import asyncio
import json
import os
import sys
from loguru import logger

# Додаємо поточну папку до шляху пошуку модулів
sys.path.append(os.path.abspath(os.path.dirname(__file__)))

from config import OPENROUTER_API_KEY
from prozorro.client import (
    fetch_tender, get_tender_documents, download_document,
    fetch_related_processes, fetch_procuring_entity_history,
    format_history_report
)
from ai.ocr_pipeline import get_ocr_pipeline
from ai.pdf_parser import split_into_sections
from ai.orchestrator import Blackboard, TenderWorkflow


async def run_analysis(tender_id: str):
    logger.remove()
    logger.add(sys.stdout, level="INFO", colorize=True)
    
    logger.info(f"🚀 Старт локального аналізу тендера: {tender_id}")
    
    # 1. Завантаження з Prozorro
    tender_data = await fetch_tender(tender_id)
    if not tender_data:
        logger.error("❌ Не вдалося отримати дані з Prozorro")
        return
        
    amount = tender_data.get("value", {}).get("amount", 0)
    title = tender_data.get("title", "")
    entity = tender_data.get("procuringEntity", {}).get("name", "")
    logger.info(f"🏢 Замовник: {entity}")
    logger.info(f"💰 Сума лоту: {amount:,.2f} грн")
    logger.info(f"📋 Предмет: {title}")

    # 2. Аналіз замовника
    edrpou = tender_data.get("procuringEntity", {}).get("identifier", {}).get("id", "")
    if edrpou:
        logger.info(f"📊 Аналізуємо замовника ЄДРПОУ: {edrpou}...")
        history = await fetch_procuring_entity_history(edrpou, max_tenders=10)
        report = format_history_report(history)
        print("\n=== ЗВІТ ПО ЗАМОВНИКУ ===")
        print(report)
        print("=========================\n")

    # 3. Документи
    documents = await get_tender_documents(tender_data)
    if not documents:
        logger.warning("⚠️ Документи відсутні")
        return

    # Сортуємо документи за важливістю назви
    def doc_priority(doc: dict) -> int:
        title = doc.get("title", "").lower()
        if any(w in title for w in ["документ", "тд", "td_"]):
            return 0
        if any(w in title for w in ["техніч", "специф", "тз", "tz_", "вимог"]):
            return 1
        if any(w in title for w in ["договір", "dogovor", "проект", "проєкт"]):
            return 2
        return 3

    documents.sort(key=doc_priority)
    docs_to_analyze = documents[:3]  # Беремо топ-3 найважливіших
    logger.info(f"📚 Вибрано {len(docs_to_analyze)} документів для глибокого аналізу")

    combined_text_parts = []
    ocr = get_ocr_pipeline(use_ocr=True)

    for idx, doc in enumerate(docs_to_analyze, 1):
        logger.info(f"📥 Завантажуємо та обробляємо ({idx}/{len(docs_to_analyze)}): {doc['title']}...")
        tmp_path = f"temp_demo_tender_{idx}.pdf"
        success = await download_document(doc["url"], tmp_path)
        if not success:
            logger.warning(f"⚠️ Не вдалося завантажити {doc['title']}")
            continue

        try:
            parsed = await ocr.parse(tmp_path)
            if parsed.all_text:
                combined_text_parts.append(
                    f"\n\n==================================================\n"
                    f"📄 НАЗВА ДОКУМЕНТА: {doc['title']}\n"
                    f"==================================================\n"
                    f"{parsed.all_text}"
                )
        except Exception as exc:
            logger.error(f"Помилка парсингу {doc['title']}: {exc}")
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    combined_text = "".join(combined_text_parts)
    if not combined_text.strip():
        logger.error("❌ Жоден документ не містить корисного тексту для аналізу")
        return

    sections = split_into_sections(combined_text)
    
    # 5. Мультиагентний аналіз
    logger.info("🤖 Запускаємо AI-Оркестратор (Scanner + Calculator + Reviewer)...")
    bb = Blackboard(tender_id)
    workflow = TenderWorkflow(bb)
    
    company_profile = {
        "name": "ТОВ БудКомпані Вінниця",
        "license": "Ліцензія на будівництво об'єктів СС2",
        "has_equipment": True,
        "has_staff": True,
        "has_experience": True
    }
    
    # Ліміт тексту та кошторис
    doc_text_limited = combined_text[:40_000]
    
    # 4.5 Спроба підключити кошторис Excel, якщо передано файл
    estimate_path = sys.argv[2] if len(sys.argv) > 2 else None
    if estimate_path:
        from ai.estimate_parser import parse_avk_excel_estimate
        logger.info(f"📂 Знайдено файл кошторису Excel: {estimate_path}. Починаємо парсинг...")
        res = parse_avk_excel_estimate(estimate_path, return_dict=True)
        
        materials = res.get("materials", [])
        machinery = res.get("machinery", [])
        
        machinery_total = sum(m["total"] for m in machinery)
        tender_data["machinery_cost"] = machinery_total
        logger.info(f"⚙️ Вартість машин та механізмів з кошторису: {machinery_total:,.2f} грн")
        
        if materials:
            estimate_text = "=== КОШТОРИС УЧАСНИКА (ДЕТАЛЬНА ВІДОМІСТЬ РЕСУРСІВ) ===\n"
            estimate_text += "\n--- МАТЕРІАЛИ ---\n"
            for m in materials[:100]:  # Ліміт 100 матеріалів для уникнення переповнення контексту
                estimate_text += f"- {m['item']} ({m['code']}): {m['quantity']} {m['unit']} × {m['unit_price']} грн (всього {m['total']} грн)\n"
                    
            doc_text_limited = estimate_text + "\n\n" + doc_text_limited
            logger.info("✅ Кошторис (тільки матеріали) успішно інтегровано у вхідний текст аналізу")
        elif machinery:
            logger.info("ℹ️ У кошторисі знайдено тільки техніку, матеріали відсутні")
        else:
            logger.warning("⚠️ Не вдалося витягти дані з кошторису")

    expected_discount = float(sys.argv[3]) if len(sys.argv) > 3 else 4.0
    logger.info(f"💰 Передбачувана знижка на аукціоні: {expected_discount}%")

    await workflow.run(
        tender_meta=tender_data,
        doc_text=doc_text_limited,
        doc_sections=sections,
        company_profile=company_profile,
        expected_discount_pct=expected_discount
    )
    
    # 6. Вивід результату
    print("\n=== ФІНАЛЬНИЙ AI ЗВІТ ===")
    from bot.handlers import _format_scan_report
    report = _format_scan_report(bb, tender_title=title)
    print(report)
    print("=========================")

    # 7. Збереження результатів у файл для глибокого аналізу
    results_dict = {
        "tender_id": tender_id,
        "title": title,
        "amount": amount,
        "scan_result": bb.scan_result,
        "calc_result": bb.calc_result,
        "review_result": bb.review_result,
        "facts": [
            {
                "agent": f.agent,
                "fact_type": f.fact_type,
                "content": f.content,
                "page_ref": f.page_ref,
                "raw_quote": f.raw_quote,
                "law_reference": f.law_reference,
                "verified": f.verified
            } for f in bb.facts
        ]
    }
    with open("demo_results.json", "w", encoding="utf-8") as f:
        json.dump(results_dict, f, ensure_ascii=False, indent=2)
    logger.info("💾 Повні результати аналізу збережено в demo_results.json")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        tid = sys.argv[1]
    else:
        tid = "UA-2026-01-12-008228-a" # наш знайдений тендер
    asyncio.run(run_analysis(tid))
