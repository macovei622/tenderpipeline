# test_no_estimate.py
import asyncio
import json
import os
import sys
from loguru import logger

# Додаємо робочу директорію до шляху імпорту
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from prozorro.client import fetch_tender, get_tender_documents, download_document
from ai.ocr_pipeline import get_ocr_pipeline
from ai.pdf_parser import split_into_sections
from ai.orchestrator import Blackboard, TenderWorkflow
from bot.handlers import _format_scan_report

# Змінений профіль без кошторису, з низьким оборотом і низьким класом ліцензії СС1
USER_PROFILE = {
  "company_name": "ТОВ «Промбудконструкція-Вінниця»",
  "edrpou": "39481527",
  "legal_form": "Товариство з обмеженою відповідальністю",
  "registration_date": "2016-03-14",
  "legal_address": "м. Вінниця, вул. Хмельницьке шосе, 104",
  "director": {
    "full_name": "Ковальчук Андрій Петрович",
    "appointment_document": "Наказ №1 від 14.03.2016 про призначення директора; діє на підставі Статуту без довіреності",
    "acts_on_basis_of": "Статут ТОВ, редакція від 22.05.2021"
  },
  "personal_data_consent": {
    "provided": True,
    "document_name": "Згода на обробку персональних даних директора та працівників",
    "date_signed": "2025-10-01"
  },
  "license_types": [
    {
      "type": "Ліцензія на будівництво об'єктів, що належать до I-II категорії складності",
      "consequence_class": "СС1",
      "license_number": "АГ №571903-24",
      "issued_by": "Державна архітектурно-будівельна інспекція України",
      "date_issued": "2019-06-11",
      "valid_until": "безстрокова"
    }
  ],
  "equipment_list": [
    { "name": "Екскаватор колісний JCB 3CX", "quantity": 1, "ownership": "власність", "condition": "справний, 2019 р.в." }
  ],
  "staff_count": 4,
  "staff": [
    { "full_name": "Ковальчук Андрій Петрович", "position": "Директор, інженер-будівельник", "education": "Вища, ВНТУ, спеціальність «Промислове та цивільне будівництво», 2008", "experience_years": 17, "employment_type": "основне місце роботи" }
  ],
  "experience_years": 9,
  "completed_contracts": [
    {
      "object": "Капітальний ремонт підземного переходу по вул. Замостянська, м. Вінниця",
      "customer": "Департамент комунального господарства та благоустрою Вінницької міської ради",
      "contract_number": "ДКГ-118/2023",
      "amount": 5940000,
      "completion_date": "2023-11-20",
      "reference_letter": "Офіційний відгук від 28.11.2023 за підписом директора Департаменту, без зауважень до якості та строків"
    }
  ],
  "annual_revenue": 1500000.0  # Низький дохід (менше ніж 50% від вартості тендера 6.89 млн)
}

def doc_priority(doc: dict) -> int:
    title = doc.get("title", "").lower()
    if any(w in title for w in ["документ", "тд", "td_"]):
        return 0
    if any(w in title for w in ["техніч", "специф", "тз", "tz_", "вимог"]):
        return 1
    if any(w in title for w in ["договір", "dogovor", "проект", "проєкт"]):
        return 2
    return 3

async def main():
    tender_id = "d7c40373446c455f90ecf6b4ea9fbe50"
    logger.info(f"🚀 Запуск тесту БЕЗ кошторису для тендера {tender_id}")

    # 1. Завантаження тендера та OCR
    tender_data = await fetch_tender(tender_id)
    if not tender_data:
        logger.error("❌ Не вдалося отримати дані тендера з Prozorro API")
        return

    documents = await get_tender_documents(tender_data)
    if not documents:
        logger.error("❌ Документи відсутні")
        return

    documents.sort(key=doc_priority)
    docs_to_analyze = documents[:3]

    combined_text_parts = []
    ocr = get_ocr_pipeline(use_ocr=True)

    for idx, doc in enumerate(docs_to_analyze, 1):
        tmp_path = f"temp_synth_no_est_{idx}.pdf"
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
    
    # Не додаємо ведомість ресурсів у початок тексту, щоб імітувати відсутність кошторису
    doc_text_limited = combined_text[:40_000]
    sections = split_into_sections(combined_text)

    # 2. Маппінг профілю компанії для конвеєра
    mapped_equipment = []
    for eq in USER_PROFILE["equipment_list"]:
        mapped_equipment.append({
            "name": eq["name"],
            "qty": eq["quantity"],
            "source": eq["ownership"]
        })

    mapped_staff = []
    for s in USER_PROFILE["staff"]:
        mapped_staff.append({
            "role": s["position"],
            "name": s["full_name"],
            "education": s["education"],
            "exp": str(s["experience_years"])
        })

    mapped_contracts = []
    for c in USER_PROFILE["completed_contracts"]:
        year = c["completion_date"].split("-")[0] if "-" in c["completion_date"] else "2023"
        mapped_contracts.append({
            "client": c["customer"],
            "subject": c["object"],
            "amount": c["amount"],
            "year": year
        })

    license_str = USER_PROFILE["license_types"][0]["type"] + f" (клас СС1, №{USER_PROFILE['license_types'][0]['license_number']})"

    company_profile = {
        "name": USER_PROFILE["company_name"],
        "edrpou": USER_PROFILE["edrpou"],
        "director": USER_PROFILE["director"]["full_name"],
        "director_title": "Директор",
        "license": license_str,
        "equipment": mapped_equipment,
        "staff": mapped_staff,
        "analog_contracts": mapped_contracts,
        "has_equipment": len(mapped_equipment) > 0,
        "has_staff": len(mapped_staff) > 0,
        "has_experience": len(mapped_contracts) > 0,
        "annual_revenue": USER_PROFILE["annual_revenue"]
    }

    tender_data["machinery_cost"] = 0.0

    # 3. Запуск конвеєра
    bb = Blackboard(tender_id)
    workflow = TenderWorkflow(bb)

    logger.info("🤖 Запускаємо AI конвеєр без кошторису...")
    await workflow.run(
        tender_meta=tender_data,
        doc_text=doc_text_limited,
        doc_sections=sections,
        company_profile=company_profile,
        expected_discount_pct=4.0
    )

    # 4. Форматування звіту
    title = tender_data.get("title", "")
    report = _format_scan_report(bb, tender_title=title)

    # 5. Збереження результатів
    out_path = "/Users/a1111/.gemini/antigravity/brain/9c8a8a24-d53a-44c1-b11e-a42f2fd18fe8/test_no_estimate_report.md"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("# Звіт про тестування без кошторису (Нові функції)\n\n")
        f.write(report)

    logger.info(f"💾 Результати збережено в {out_path}")

if __name__ == "__main__":
    asyncio.run(main())
