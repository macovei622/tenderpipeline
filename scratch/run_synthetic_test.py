# run_synthetic_test.py
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
from ai.client import call_model, build_messages

# Профіль, наданий користувачем
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
      "type": "Ліцензія на будівництво об'єктів, що належать до IV-V категорії складності",
      "consequence_class": "СС2",
      "license_number": "АГ №571903-24",
      "issued_by": "Державна архітектурно-будівельна інспекція України",
      "date_issued": "2019-06-11",
      "valid_until": "безстрокова"
    }
  ],
  "equipment_list": [
    { "name": "Екскаватор колісний JCB 3CX", "quantity": 1, "ownership": "власність", "condition": "справний, 2019 р.в." },
    { "name": "Самоскид МАЗ-5551", "quantity": 2, "ownership": "власність", "condition": "справний" },
    { "name": "Бетонозмішувач стаціонарний", "quantity": 1, "ownership": "оренда", "lease_document": "Договір оренди №14/25 від 05.01.2025, ФОП Дорошенко І.В., термін до 31.12.2026" },
    { "name": "Віброплита ущільнювальна", "quantity": 2, "ownership": "власність", "condition": "справна" },
    { "name": "Генератор дизельний 20 кВт", "quantity": 1, "ownership": "власність" },
    { "name": "Насос дренажний", "quantity": 2, "ownership": "оренда", "lease_document": "Договір оренди спецтехніки №08/25 від 10.02.2025" }
  ],
  "staff_count": 14,
  "staff": [
    { "full_name": "Ковальчук Андрій Петрович", "position": "Директор, інженер-будівельник", "education": "Вища, ВНТУ, спеціальність «Промислове та цивільне будівництво», 2008", "experience_years": 17, "employment_type": "основне місце роботи" },
    { "full_name": "Стеценко Олег Миколайович", "position": "Виконроб дільниці", "education": "Вища, ВНТУ, «Будівництво та цивільна інженерія», 2012", "experience_years": 13, "employment_type": "основне місце роботи" },
    { "full_name": "Гнатюк Роман Васильович", "position": "Інженер з охорони праці", "education": "Вища, посвідчення з охорони праці №0451-24 (чинне до 2027)", "experience_years": 9, "employment_type": "основне місце роботи" },
    { "full_name": "Марчук Віталій Сергійович", "position": "Майстер дільниці", "education": "Середньо-спеціальна, будівельний технікум", "experience_years": 11, "employment_type": "основне місце роботи" }
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
    },
    {
      "object": "Реконструкція мережі зливової каналізації, вул. Пирогова, м. Вінниця",
      "customer": "КП ВМР «Вінницяоблводоканал»",
      "contract_number": "ВОВ-44/2022",
      "amount": 4120000,
      "completion_date": "2022-09-05",
      "reference_letter": "Офіційний відгук від 12.09.2022, підтверджує дотримання строків"
    }
  ],
  "cost_estimate": {
    "breakdown": {
      "materials_total": 3850000.0,
      "labor_total": 1550000.0,
      "transport_total": 210000.0,
      "overheads_and_profit": 890000.0,
      "total": 6500000.0
    },
    "notes": "маржа нижче 10% — прикордонний випадок, потребує ручної перевірки"
  }
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
    logger.info(f"🧪 Запуск аналізу з синтетичним профілем для тендера: {tender_id}")

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
        tmp_path = f"temp_synth_tender_{idx}.pdf"
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
    
    # Створюємо псевдо-кошторис з профілю для тестування Калькулятора
    materials_total = USER_PROFILE.get("cost_estimate", {}).get("breakdown", {}).get("materials_total", 3850000)
    estimate_text = (
        "=== КОШТОРИС УЧАСНИКА (ДЕТАЛЬНА ВІДОМІСТЬ РЕСУРСІВ) ===\n"
        "\n--- МАТЕРІАЛИ ---\n"
        f"- Будівельні матеріали та конструкції (комплексно): 1 компл. × {materials_total} грн (всього {materials_total} грн)\n\n"
    )
    doc_text_limited = estimate_text + combined_text[:40_000]
    sections = split_into_sections(combined_text)


    # 2. Маппінг профілю компанії для конвеєра
    mapped_equipment = []
    for eq in USER_PROFILE["equipment_list"]:
        mapped_equipment.append({
            "name": eq["name"],
            "qty": eq["quantity"],
            "source": eq["ownership"] + (f" ({eq['lease_document']})" if eq.get("lease_document") else "")
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

    license_str = USER_PROFILE["license_types"][0]["type"] + f" (клас СС2, №{USER_PROFILE['license_types'][0]['license_number']})"

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
        "cost_estimate": USER_PROFILE.get("cost_estimate")
    }

    # Машини та механізми (техніка) з профілю — 0.0 грн, оскільки в equipment_list немає сум
    tender_data["machinery_cost"] = 0.0

    # 3. Запуск конвеєра
    bb = Blackboard(tender_id)
    workflow = TenderWorkflow(bb)

    logger.info("🤖 Запускаємо AI конвеєр...")
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

    # 5. Генерація драфту скарги в АМКУ (імітація cmd_amku)
    disc_reqs = bb.scan_result.get("discriminatory_requirements", []) if bb.scan_result else []
    complaint_content = "Скарг не згенеровано (немає дискримінаційних вимог)"
    
    if disc_reqs:
        disc_text = "\n".join(
            f"- Вимога: {r.get('type')}\n  Цитата ТД: \"{r.get('quote')}\"\n  Стаття закону: {r.get('law_reference')}"
            for r in disc_reqs
        )

        # Перевірка дедлайнів
        is_tendering = True
        status = tender_data.get("status")
        if status and status != "active.tendering":
            is_tendering = False

        if is_tendering:
            stage_type = "Оскарження умов тендерної документації (до дедлайну подачі)"
            scenario_instruction = (
                "⛔ ОБОВ'ЯЗКОВО ДОТРИМУЙСЯ ЦЬОГО СЦЕНАРІЮ:\n"
                "СЦЕНАРІЙ: ПОТЕНЦІЙНИЙ УЧАСНИК ДО ПОДАЧІ ПРОПОЗИЦІЇ.\n\n"
                "Скаржник є потенційним учасником, який ЩЕ НЕ ПОДАВАВ пропозицію і НЕ БУВ ВІДХИЛЕНИЙ.\n"
                "Скарга подається на дискримінаційні УМОВИ ТЕНДЕРНОЇ ДОКУМЕНТАЦІЇ, не на рішення замовника.\n\n"
                "ЗАБОРОНЕНО ПИСАТИ (ці фрази не можуть з'являтись у тексті скарги):\n"
                "× «Скаржник подав пропозицію»\n"
                "× «Замовник відхилив пропозицію»\n"
                "× «протокол оцінки»\n"
                "× «рішення замовника про відхилення»\n\n"
                "НАТОМІСТЬ ПИСАТИ ТАК:\n"
                "✓ «Скаржник є потенційним учасником закупівлі…»\n"
                "✓ «Що має намір взяти участь у процедурі…»\n"
                "✓ «Умови ТД унеможливлюють Скаржнику подати пропозицію…»"
            )
        else:
            stage_type = "Оскарження рішень, дій чи бездіяльності замовника (після оцінки пропозицій / аукціону)"
            scenario_instruction = (
                "⛔ ОБОВ'ЯЗКОВО ДОТРИМУЙСЯ ЦЬОГО СЦЕНАРІЮ:\n"
                "СЦЕНАРІЙ: УЧАСНИК, ПРОПОЗИЦІЮ ЯКОГО ВІДХИЛЕНО.\n\n"
                "Скаржник подавав пропозицію і оскаржує конкретне рішення (дію чи бездіяльність) замовника,\n"
                "прийняте ПІСЛЯ оцінки пропозицій або аукціону. Скарга подається на РІШЕННЯ ЗАМОВНИКА.\n"
                "Зазнач, що скаржник оскаржує рішення про відхилення / визначення переможця / тощо."
            )

        # ── Гібридний підхід: Python будує I+II+V, LLM пише лише III+IV ─────────
        # Модель фізично не бачить розділів I/II — не може вставити неправильний сценарій.
        import datetime as _dt
        today_str = _dt.date.today().strftime("%d.%m.%Y")
        procuring_entity_name = tender_data.get("procuringEntity", {}).get("name", "Замовник")

        if is_tendering:
            ii_block = (
                "## II. ФАКТИЧНІ ОБСТАВИНИ\n\n"
                "1. Скаржник є потенційним учасником закупівлі, що проводиться Замовником, "
                "та має намір взяти участь у процедурі відкритих торгів.\n\n"
                f"2. Замовник оприлюднив Тендерну документацію (далі — ТД) щодо закупівлі: {title}.\n\n"
                "3. Після ознайомлення з умовами ТД Скаржник виявив дискримінаційні вимоги, "
                "що унеможливлюють або суттєво обмежують його право на подання пропозиції.\n\n"
                "4. Скаржник звертається до Органу оскарження ще до закінчення строку подачі "
                "пропозицій, оскільки оскаржувані умови ТД є дискримінаційними та суперечать "
                "законодавству про публічні закупівлі."
            )
            demands_goal = "скасувати або привести у відповідність до законодавства дискримінаційні умови ТД до завершення строку подачі пропозицій"
            v_block = (
                "До скарги додаються:\n\n"
                "1. Копія Тендерної документації з виділеними дискримінаційними вимогами.\n"
                "2. Документи, що підтверджують статус та кваліфікацію Скаржника.\n"
                "3. Інші документи на підтвердження викладених обставин."
            )
        else:
            ii_block = (
                "## II. ФАКТИЧНІ ОБСТАВИНИ\n\n"
                "1. Скаржник брав участь (або мав намір взяти участь) у відкритих торгах, організованих Замовником "
                f"щодо закупівлі: {title}.\n\n"
                "2. Замовник прийняв рішення, що порушує права та законні інтереси Скаржника "
                "(відхилення пропозиції / визначення переможця / інше рішення після оцінки).\n\n"
                "3. Рішення ґрунтується на дискримінаційних вимогах ТД, які обмежують коло "
                "учасників незаконно."
            )
            demands_goal = "визнати рішення Замовника незаконним та зобов'язати його переглянути пропозицію Скаржника"
            v_block = (
                "До скарги додаються:\n\n"
                "1. Копія Тендерної документації з виділеними дискримінаційними вимогами.\n"
                "2. Копія пропозиції Скаржника.\n"
                "3. Копія рішення Замовника (витяг з протоколу).\n"
                "4. Документи, що підтверджують статус та кваліфікацію Скаржника.\n"
                "5. Інші документи на підтвердження викладених обставин."
            )

        # LLM пише ЛИШЕ правові підстави та вимоги
        AMKU_LEGAL_SYSTEM = (
            "Ти — юрист у сфері публічних закупівель України.\n"
            "Напиши ЛИШЕ два розділи юридичної скарги: «III. ПРАВОВІ ПІДСТАВИ» та «IV. ВИМОГИ».\n\n"
            "СТРОГІ ЗАБОРОНИ:\n"
            "1. НЕ вигадуй номери рішень АМКУ чи суду. Пиши «відповідно до усталеної практики "
            "органу оскарження».\n"
            "2. НЕ пиши вступ, фактичні обставини або додатки — ці розділи вже написані.\n"
            "3. В розділі IV пиши лише що Скаржник ВИМАГАЄ зробити від Органу оскарження "
            "та Замовника — без опису минулих подій.\n"
            "Мова: українська."
        )
        AMKU_LEGAL_USER = (
            f"Замовник: {procuring_entity_name}\n"
            f"Предмет закупівлі: {title}\n"
            f"Мета вимог Скаржника: {demands_goal}\n\n"
            f"ВИЯВЛЕНІ ПОРУШЕННЯ (дискримінаційні вимоги ТД):\n{disc_text}\n\n"
            "Напиши розділи III. ПРАВОВІ ПІДСТАВИ та IV. ВИМОГИ.\n"
            "Посилайся на ЗУ «Про публічні закупівлі» (ст. 16, ст. 22, ст. 46).\n"
            "Не додавай жодних інших розділів."
        )

        messages = build_messages(system_prompt=AMKU_LEGAL_SYSTEM, user_content=AMKU_LEGAL_USER)
        logger.info("⚖️ AI генерує правову аргументацію (розд. III+IV) для скарги в АМКУ...")
        result = await call_model("collector", messages, json_mode=False)
        legal_sections = "[Не вдалося згенерувати правові підстави]"
        if result:
            legal_sections, _ = result

        # Збираємо повну скаргу детерміновано
        complaint_content = (
            "# СКАРГА\n"
            "## до Постійно діючої колегії АМКУ з розгляду скарг про порушення "
            "законодавства у сфері публічних закупівель\n\n"
            "---\n\n"
            "## I. ВСТУПНА ЧАСТИНА\n\n"
            "До Постійно діючої колегії Антимонопольного комітету України з розгляду "
            "скарг про порушення законодавства у сфері публічних закупівель\n\n"
            "**Скаржник:** ________________________________\n"
            "**Адреса:** ________________________________\n"
            "**Контактні дані:** ________________________________\n\n"
            f"**Замовник:** {procuring_entity_name}\n"
            f"**Предмет закупівлі:** {title}\n"
            f"**ID Тендера:** {tender_id}\n"
            f"**Тип скарги:** {stage_type}\n\n"
            "---\n\n"
            f"{ii_block}\n\n"
            "---\n\n"
            f"{legal_sections}\n\n"
            "---\n\n"
            "## V. ДОДАТКИ\n\n"
            f"{v_block}\n\n"
            "---\n\n"
            f"**Дата подання:** {today_str}\n\n"
            "**Підпис Скаржника:** ________________________________\n\n"
            "**Примітка:** Заповніть реквізити Скаржника та додайте підписані документи перед поданням."
        )


    # 6. Збереження результатів у артефакт
    artifact_dir = "/Users/a1111/.gemini/antigravity/brain/9c8a8a24-d53a-44c1-b11e-a42f2fd18fe8"
    report_file_path = os.path.join(artifact_dir, "test_synthetic_report.md")
    
    with open(report_file_path, "w", encoding="utf-8") as f:
        f.write(f"# Звіт про тестування конвеєра на синтетичному профілі\n\n")
        f.write(f"**Тендер:** {title} ({tender_id})\n")
        f.write(f"**Бюджет:** {tender_data.get('value', {}).get('amount', 0):,.2f} грн\n")
        f.write(f"**Компанія:** {USER_PROFILE['company_name']} (ЄДРПОУ {USER_PROFILE['edrpou']})\n\n")
        f.write(f"## 1. AI-Звіт Аналізу\n\n```markdown\n{report}\n```\n\n")
        f.write(f"## 2. Драфт скарги в АМКУ\n\n```markdown\n{complaint_content}\n```\n")

    logger.info(f"💾 Результати збережено в {report_file_path}")

if __name__ == "__main__":
    asyncio.run(main())
