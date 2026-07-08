# scratch/extract_detailed_leads.py
import os
import sys
import asyncio
import aiohttp
import json
from loguru import logger

PROZORRO_API_BASE = "https://public-api.prozorro.gov.ua/api/2.5"
TARGET_IDS = ['UA-2026-04-03-010474-a', 'UA-2026-05-19-005955-a', 'UA-2026-05-28-000074-a', 'UA-2026-05-28-010103-a']

async def fetch_detail(session, uuid):
    url = f"{PROZORRO_API_BASE}/tenders/{uuid}"
    try:
        async with session.get(url, timeout=15) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get("data", {})
    except Exception as e:
        logger.warning(f"Failed to fetch details for {uuid}: {e}")
    return None

async def main():
    logger.info("Starting optimized live extraction for targets...")
    
    url = f"{PROZORRO_API_BASE}/tenders"
    params = {"descending": "1", "limit": 100, "opt_fields": "id,status"}
    
    offset = None
    qual_tender_uuids = []
    
    # 1. Fetch 25 pages of list view to cover recent history
    async with aiohttp.ClientSession() as session:
        for page in range(25):
            if offset:
                params["offset"] = offset
            async with session.get(url, params=params, timeout=20) as resp:
                if resp.status != 200:
                    break
                data = await resp.json()
                tenders = data.get("data", [])
                if not tenders:
                    break
                # Only check tenders that are in evaluation/finalized statuses
                for t in tenders:
                    if t.get("status") in ["active.qualification", "active.awarded", "complete", "unsuccessful"]:
                        qual_tender_uuids.append(t["id"])
                offset = data.get("next_page", {}).get("offset")
                if not offset:
                    break
                    
        logger.info(f"Found {len(qual_tender_uuids)} qualification/completed tenders. Fetching details in parallel...")
        
        # 2. Fetch details in parallel chunks
        chunk_size = 40
        found_targets = {}
        
        for i in range(0, len(qual_tender_uuids), chunk_size):
            chunk = qual_tender_uuids[i:i+chunk_size]
            tasks = [fetch_detail(session, uuid) for uuid in chunk]
            results = await asyncio.gather(*tasks)
            
            for res in results:
                if not res:
                    continue
                tid = res.get("tenderID")
                if tid in TARGET_IDS:
                    found_targets[tid] = res
                    logger.info(f"🎯 Matched Target: {tid}")
                    
            if len(found_targets) == len(TARGET_IDS):
                break
                
    # 3. Analyze targets
    logger.info(f"Extracted details for {len(found_targets)} targets.")
    
    report = []
    report.append("# 🔍 Детальний аналіз дискваліфікацій та скарг АМКУ")
    report.append(f"\nАналіз проведено на основі живих даних Prozorro API для {len(found_targets)} знайдених тендерів.")
    
    for tid in TARGET_IDS:
        t_data = found_targets.get(tid)
        if not t_data:
            report.append(f"\n## ❌ {tid} — не знайдено в поточному вікні сканування.")
            continue
            
        title = t_data.get("title", "Будівельні роботи")
        entity = t_data.get("procuringEntity", {}).get("name", "Невідомий замовник")
        amount = t_data.get("value", {}).get("amount", 0)
        
        report.append(f"\n## 📋 [{tid}](https://prozorro.gov.ua/tender/{tid})")
        report.append(f"**Предмет:** {title}")
        report.append(f"**Замовник:** {entity}")
        report.append(f"**Очікувана вартість:** {amount:,.2f} грн".replace(",", " "))
        
        # Awards & Rejections
        awards = t_data.get("awards", [])
        report.append("\n### ⚖️ Учасники та результати розгляду")
        
        for idx, a in enumerate(awards, 1):
            suppliers = a.get("suppliers") or [{}]
            sup_name = suppliers[0].get("name", "Невідомо")
            sup_edrpou = suppliers[0].get("identifier", {}).get("id", "Невідомо")
            status = a.get("status")
            award_amount = a.get("value", {}).get("amount", 0)
            
            report.append(f"\n#### {idx}. {sup_name} (ЄДРПОУ `{sup_edrpou}`)")
            report.append(f"- **Ціна:** {award_amount:,.2f} грн".replace(",", " "))
            report.append(f"- **Статус розгляду:** `{'ПЕРЕМОЖЕЦЬ' if status == 'active' else 'ВІДХИЛЕНО' if status == 'unsuccessful' else status}`")
            
            if status == "unsuccessful":
                # Rejection reasons
                title_rej = a.get("title", "")
                desc_rej = a.get("description", "")
                report.append(f"- **Причина відхилення (З Тексту Рішення):**")
                if title_rej or desc_rej:
                    report.append(f"  > **{title_rej}**")
                    if desc_rej:
                        report.append(f"  > {desc_rej}")
                else:
                    report.append("  > _Причина відхилення не вказана у текстовому полі API (завантажено у файлах рішення)._")
                
                # Check for decision documents
                docs = a.get("documents", [])
                if docs:
                    report.append("  - **Документи протоколу відхилення:**")
                    for d in docs:
                        report.append(f"    * [{d.get('title')}]({d.get('url')}) (опубліковано {d.get('datePublished', '')[:10]})")
            
            # Award complaints
            a_complaints = a.get("complaints", [])
            if a_complaints:
                report.append(f"- **Скарги на рішення щодо цього учасника ({len(a_complaints)}):**")
                for ac in a_complaints:
                    comp_title = ac.get("title", "Без назви")
                    comp_desc = ac.get("description", "Опис відсутній")
                    comp_status = ac.get("status")
                    complainant = ac.get("author", {}).get("name", "Невідомий скаржник")
                    
                    report.append(f"  * **Скаржник:** {complainant} (Статус скарги: `{comp_status}`)")
                    report.append(f"    * **Суть скарги:** {comp_title}")
                    if comp_desc:
                        report.append(f"    * **Деталі:** {comp_desc[:300]}...")
                    if ac.get("decision"):
                        report.append(f"    * **Рішення АМКУ:** {ac.get('decision')}")
                        
        # Tender-level complaints
        t_complaints = t_data.get("complaints", [])
        if t_complaints:
            report.append(f"\n### ⚠️ Скарги на умови тендерної документації ({len(t_complaints)})")
            for tc in t_complaints:
                tc_title = tc.get("title", "Без назви")
                tc_status = tc.get("status")
                complainant = tc.get("author", {}).get("name", "Невідомий скаржник")
                report.append(f"- **Скаржник:** {complainant} (Статус: `{tc_status}`)")
                report.append(f"  * **Суть скарги:** {tc_title}")
                if tc.get("description"):
                    report.append(f"  * **Деталі:** {tc.get('description')[:300]}...")
                if tc.get("decision"):
                    report.append(f"  * **Рішення АМКУ:** {tc.get('decision')}")
                    
        report.append("\n---")
        
    # Write report to file
    out_path = "/Users/a1111/.gemini/antigravity/brain/9c8a8a24-d53a-44c1-b11e-a42f2fd18fe8/detailed_leads_analysis.md"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(report))
        
    logger.info(f"Detailed report saved to {out_path}")
    print(f"\nSUCCESS: Analysis complete. Found {len(found_targets)} targets.")

if __name__ == "__main__":
    asyncio.run(main())
