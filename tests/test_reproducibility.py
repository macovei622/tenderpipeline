"""
tests/test_reproducibility.py — Скрипт автоматичного тестування відтворюваності AI-Сканера
"""
import os
import sys
import asyncio
import json
from loguru import logger

# Додаємо робочу директорію до шляху імпорту
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from prozorro.client import fetch_tender, get_tender_documents, download_document
from ai.ocr_pipeline import get_ocr_pipeline
from ai.pdf_parser import split_into_sections
from ai.orchestrator import Blackboard, TenderWorkflow


def doc_priority(doc: dict) -> int:
    title = doc.get("title", "").lower()
    if any(w in title for w in ["документ", "тд", "td_"]):
        return 0
    if any(w in title for w in ["техніч", "специф", "тз", "tz_", "вимог"]):
        return 1
    if any(w in title for w in ["договір", "dogovor", "проект", "проєкт"]):
        return 2
    return 3


async def run_reproducibility_test():
    tender_id = "d7c40373446c455f90ecf6b4ea9fbe50"
    logger.info(f"🧪 Запуск тесту відтворюваності для тендера: {tender_id}")

    # 1. Завантаження та OCR (спільні для всіх прогонів, щоб виключити варіативність OCR)
    tender_data = await fetch_tender(tender_id)
    if not tender_data:
        logger.error("❌ Не вдалося отримати дані тендера")
        return

    documents = await get_tender_documents(tender_data)
    if not documents:
        logger.error("❌ Не знайдено жодного документа в тендері")
        return

    documents.sort(key=doc_priority)
    docs_to_analyze = documents[:3]

    combined_text_parts = []
    ocr = get_ocr_pipeline(use_ocr=True)

    for idx, doc in enumerate(docs_to_analyze, 1):
        tmp_path = f"temp_repro_tender_{idx}.pdf"
        success = await download_document(doc["url"], tmp_path)
        if not success:
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
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    combined_text = "".join(combined_text_parts)
    doc_text_limited = combined_text[:40_000]
    sections = split_into_sections(combined_text)

    company_profile = {
        "name": "ТОВ БудКомпані Вінниця",
        "license": "Ліцензія на будівництво об'єктів СС2",
        "has_equipment": True,
        "has_staff": True,
        "has_experience": True
    }

    # Додамо імітацію вартості машин, як у демо
    tender_data["machinery_cost"] = 38000.0

    runs_results = []
    num_runs = 3

    logger.info(f"🔄 Починаємо {num_runs} послідовних прогонів конвеєра...")
    for run_idx in range(1, num_runs + 1):
        logger.info(f"▶️ Прогон {run_idx}/{num_runs}...")
        bb = Blackboard(tender_id)
        workflow = TenderWorkflow(bb)

        await workflow.run(
            tender_meta=tender_data,
            doc_text=doc_text_limited,
            doc_sections=sections,
            company_profile=company_profile,
            expected_discount_pct=4.0
        )

        # Зберігаємо результати
        scan_res = bb.scan_result or {}
        calc_res = bb.calc_result or {}

        runs_results.append({
            "run_index": run_idx,
            "risk_level": scan_res.get("risk_level"),
            "discriminatory_requirements": [r.get("type") for r in scan_res.get("discriminatory_requirements", [])],
            "contract_risks": [r.get("description") for r in scan_res.get("contract_risks", [])],
            "required_documents": sorted(scan_res.get("required_documents", [])),
            "margin_pct": calc_res.get("margin_pct"),
            "total_cost": calc_res.get("total_cost"),
        })
        
        # Невелика пауза між запитами
        await asyncio.sleep(2)

    # 2. Аналіз розбіжностей
    logger.info("📊 Аналіз результатів та формування звіту...")
    
    # Порівняння чеклистів документів
    all_doc_lists = [run["required_documents"] for run in runs_results]
    common_docs = set(all_doc_lists[0]).intersection(*all_doc_lists[1:])
    union_docs = set().union(*all_doc_lists)
    
    # Порівняння знайдених дискримінаційних вимог
    all_disc = [run["discriminatory_requirements"] for run in runs_results]
    common_disc = set(all_disc[0]).intersection(*all_disc[1:])
    union_disc = set().union(*all_disc)

    # Формування звіту у форматі Markdown
    report = []
    report.append("# Звіт про відтворюваність AI-Сканера (DeepSeek R1)")
    report.append(f"\nТестування проведено на тендері: **{tender_id}**")
    report.append(f"Кількість послідовних запусків: **{num_runs}**")
    report.append("Параметри моделі: **temperature = 0.0**")
    report.append("\n## 📈 Метрики відтворюваності")
    
    doc_overlap_pct = (len(common_docs) / len(union_docs) * 100) if union_docs else 100.0
    report.append(f"- **Стабільність чек-листа документів (Overlap):** `{doc_overlap_pct:.1f}%` ({len(common_docs)} з {len(union_docs)} спільних пунктів)")
    
    disc_overlap_pct = (len(common_disc) / len(union_disc) * 100) if union_disc else 100.0
    report.append(f"- **Стабільність дискримінаційних вимог:** `{disc_overlap_pct:.1f}%` ({len(common_disc)} з {len(union_disc)} спільних вимог)")

    report.append("\n## 🔍 Детальна таблиця прогонів")
    report.append("| Параметр | Прогон 1 | Прогон 2 | Прогон 3 |")
    report.append("| :--- | :---: | :---: | :---: |")
    report.append(f"| Рівень ризику | {runs_results[0]['risk_level']} | {runs_results[1]['risk_level']} | {runs_results[2]['risk_level']} |")
    report.append(f"| Кіл-ть документів у чек-листі | {len(runs_results[0]['required_documents'])} | {len(runs_results[1]['required_documents'])} | {len(runs_results[2]['required_documents'])} |")
    report.append(f"| Кіл-ть дискр. вимог | {len(runs_results[0]['discriminatory_requirements'])} | {len(runs_results[1]['discriminatory_requirements'])} | {len(runs_results[2]['discriminatory_requirements'])} |")
    report.append(f"| Собівартість (грн) | {runs_results[0]['total_cost']:,.2f} | {runs_results[1]['total_cost']:,.2f} | {runs_results[2]['total_cost']:,.2f} |")
    report.append(f"| Маржа (%) | {runs_results[0]['margin_pct']}% | {runs_results[1]['margin_pct']}% | {runs_results[2]['margin_pct']}% |")

    report.append("\n## 📋 Порівняння чек-листів документів")
    report.append("Спільні для всіх прогонів документи:")
    for d in sorted(common_docs):
        report.append(f"- [x] {d}")
        
    diff_docs = union_docs - common_docs
    if diff_docs:
        report.append("\nПункти, що з'явилися не в усіх прогонах (нестабільність):")
        for d in sorted(diff_docs):
            occurrence = [idx for idx, run in enumerate(runs_results, 1) if d in run["required_documents"]]
            report.append(f"- [ ] {d} _(з'явився у прогонах: {occurrence})_")
    else:
        report.append("\n✅ Повна побітова стабільність чек-листа документів (0 розбіжностей).")

    report.append("\n## ⚖️ Порівняння юридичних заперечень")
    for d in sorted(union_disc):
        occurrence = [idx for idx, run in enumerate(runs_results, 1) if d in run["discriminatory_requirements"]]
        status = "✅ Спільна" if len(occurrence) == num_runs else "⚠️ Нестабільна"
        report.append(f"- **{d}** — {status} _(прогони: {occurrence})_")

    # Збереження звіту в артефакти
    artifact_dir = "/Users/a1111/.gemini/antigravity/brain/9c8a8a24-d53a-44c1-b11e-a42f2fd18fe8"
    os.makedirs(artifact_dir, exist_ok=True)
    report_path = os.path.join(artifact_dir, "reproducibility_report.md")
    
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(report))
        
    logger.info(f"💾 Звіт про відтворюваність збережено: {report_path}")
    print("\n" + "\n".join(report))


if __name__ == "__main__":
    asyncio.run(run_reproducibility_test())
