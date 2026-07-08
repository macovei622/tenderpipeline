# scripts/find_outreach_targets.py
"""
scripts/find_outreach_targets.py — Інструмент накопичення SMB-підрядників для точкового аутрічу.
Знаходить тендери по Вінниці та інших регіонах (500к - 20 млн грн, стратегічний цільовий сегмент), 
де учасник з найвигіднішою ціною був дискваліфікований.
Працює з активними, завершеними та присудженими тендерами для максимального охоплення історії.
"""
import os
import sys
import asyncio
import aiohttp
import sqlite3
from loguru import logger

# Додаємо поточну папку до шляху пошуку модулів
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import PROZORRO_API_BASE, MIN_AMOUNT, MAX_AMOUNT, TARGET_REGION
from db.database import init_db, DbConnection


async def fetch_outreach_targets():
    logger.info("⚙️ Ініціалізація бази даних...")
    await init_db()
    
    logger.info("🔍 Пошук потенційних цільових тендерів для аутрічу (проходимо 10 сторінок)...")
    
    url = f"{PROZORRO_API_BASE}/tenders"
    params = {"descending": "1", "limit": 100, "opt_fields": "id,status"}
    
    new_leads_count = 0
    offset = None
    scanned_tenders_count = 0
    
    # Стадії тендера, де вже були оцінені пропозиції
    allowed_statuses = ["active.qualification", "active.awarded", "complete", "unsuccessful"]
    
    try:
        async with aiohttp.ClientSession() as session:
            for page in range(10): # Скануємо 10 сторінок (1000 тендерів) для глибокого аналізу завершених
                if offset:
                    params["offset"] = offset
                    
                logger.info(f"Завантаження сторінки {page+1}...")
                async with session.get(url, params=params, timeout=30) as resp:
                    if resp.status != 200:
                        logger.error(f"Помилка Prozorro API на сторінці {page+1}: {resp.status}")
                        break
                        
                    data = await resp.json()
                    tenders = data.get("data", [])
                    if not tenders:
                        break
                        
                    scanned_tenders_count += len(tenders)
                    
                    # Відбираємо тендери на стадіях оцінки/завершення
                    qual_tenders = [t for t in tenders if t.get("status") in allowed_statuses]
                    logger.info(f"Сторінка {page+1}: знайдено {len(qual_tenders)} потенційних тендерів для аналізу деталізації...")
                    
                    # Завантажуємо деталі для кожного тендера
                    for t in qual_tenders:
                        detail_url = f"{PROZORRO_API_BASE}/tenders/{t['id']}"
                        async with session.get(detail_url, timeout=15) as det_resp:
                            if det_resp.status != 200:
                                continue
                            
                            det_data = (await det_resp.json()).get("data", {})
                            
                            # 1. Регіон: фільтр з config.py (якщо задано TARGET_REGION)
                            region = det_data.get("procuringEntity", {}).get("address", {}).get("region", "")
                            if TARGET_REGION and TARGET_REGION not in region:
                                continue
                                
                            # 2. Сума: від MIN_AMOUNT до MAX_AMOUNT (наш цільовий сегмент з config.py)
                            amount = det_data.get("value", {}).get("amount", 0)
                            if not (MIN_AMOUNT <= amount <= MAX_AMOUNT):
                                continue
                                
                            # 3. CPV-код: Будівництво/Ремонт (45xxxxx)
                            items = det_data.get("items", [])
                            cpv = items[0].get("classification", {}).get("id", "") if items else ""
                            if not cpv.startswith("45"):
                                continue
                                
                            awards = det_data.get("awards", [])
                            if not awards:
                                continue
                                
                            # Аналізуємо дискваліфікації та переможця
                            disqualified = []
                            winner = None
                            
                            for award in awards:
                                suppliers = award.get("suppliers") or []
                                if not suppliers:
                                    continue
                                supplier = suppliers[0]
                                supplier_name = supplier.get("name", "Невідомо")
                                supplier_edrpou = supplier.get("identifier", {}).get("id", "")
                                status = award.get("status")
                                award_amount = award.get("value", {}).get("amount", 0)
                                
                                if status == "unsuccessful":
                                    disqualified.append({
                                        "name": supplier_name,
                                        "edrpou": supplier_edrpou,
                                        "amount": award_amount,
                                    })
                                elif status == "active":
                                    winner = {
                                        "name": supplier_name,
                                        "edrpou": supplier_edrpou,
                                        "amount": award_amount,
                                    }
                            
                            # Якщо є переможець та дискваліфіковані учасники
                            if winner and disqualified:
                                for disq in disqualified:
                                    # Нас цікавлять тільки випадки, де відхилений був ДЕШЕВШИМ за переможця
                                    diff = winner["amount"] - disq["amount"]
                                    if diff > 0:
                                        # Зберігаємо лід в базу даних
                                        async with DbConnection() as db:
                                            try:
                                                res = await db.execute("""
                                                    INSERT OR IGNORE INTO outreach_leads (
                                                        prozorro_id, title, amount, procuring_entity,
                                                        winner_name, winner_amount, disqualified_name,
                                                        disqualified_edrpou, disqualified_amount, diff_amount,
                                                        target_region, target_cpv
                                                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                                """, (
                                                    det_data.get("tenderID") or t["id"],
                                                    det_data.get("title", "Будівельні роботи"),
                                                    amount,
                                                    det_data.get("procuringEntity", {}).get("name"),
                                                    winner["name"],
                                                    winner["amount"],
                                                    disq["name"],
                                                    disq["edrpou"],
                                                    disq["amount"],
                                                    diff,
                                                    region,
                                                    cpv
                                                ))
                                                # SQLite cursor rowcount > 0 означає успішну вставку
                                                if res and getattr(res, "rowcount", 0) > 0:
                                                    new_leads_count += 1
                                                    logger.info(f"✨ Знайдено НОВИЙ лід: {disq['name']} у тендері {det_data.get('tenderID')}")
                                            except Exception as e:
                                                logger.error(f"Помилка збереження ліда в БД: {e}")
                                                
                    # Перехід на наступну сторінку
                    next_page = data.get("next_page", {})
                    offset = next_page.get("offset")
                    if not offset:
                        break
                        
    except Exception as e:
        logger.error(f"Помилка при завантаженні тендерів: {e}")
        
    logger.info(f"🏁 Сканування завершено. Оброблено {scanned_tenders_count} тендерів.")
    logger.info(f"📈 Додано нових лідів до бази: {new_leads_count}")
    
    # Отримуємо всі "new" ліди з бази для побудови Markdown звіту
    async with DbConnection() as db:
        leads = await db.fetchall("SELECT * FROM outreach_leads WHERE status = 'new' ORDER BY created_at DESC")
        total_leads = await db.fetchone("SELECT COUNT(*) as cnt FROM outreach_leads")
        total_cnt = total_leads.get("cnt", 0) if total_leads else 0
        
    # Створюємо Markdown звіт
    report = []
    report.append("# Накопичений звіт про потенційні аутріч-цілі")
    report.append(f"\nЗгенеровано на основі накопичених лідів у базі даних.")
    report.append(f"- Всього унікальних лідів у базі: **{total_cnt}**")
    report.append(f"- Нових/неопрацьованих лідів (status = 'new'): **{len(leads)}**")
    report.append("\n## 📋 Список нових лідів")
    
    if not leads:
        report.append("\nℹ️ Немає нових лідів зі статусом 'new'. Додано симульовані ліди для демонстрації скриптів:")
        # Fallback на симульовані ліди
        mock_leads = [
            {
                "prozorro_id": "UA-2025-10-06-001013-a",
                "title": "Капітальний ремонт підземного пішохідного переходу по вул. Київська в м. Вінниці (МОДЕЛЬОВАНИЙ ДЕМО-КЕЙС)",
                "procuring_entity": "Департамент комунального господарства та благоустрою Вінницької міської ради",
                "amount": 6896639.00,
                "winner_name": "ТОВ \"ВІННИЦЯ БЛАГОУСТРІЙ\"",
                "winner_amount": 6621744.50,
                "disqualified_name": "ТОВ \"Промбудконструкція-Вінниця\" (Симульований дешевший учасник)",
                "disqualified_edrpou": "43105728",
                "disqualified_amount": 6500000.00,
                "diff_amount": 121744.50
            },
            {
                "prozorro_id": "UA-SYNTHETIC-REPRESENTATIVE-CASE",
                "title": "Капітальний ремонт будівлі амбулаторії ЗПСМ (МОДЕЛЬОВАНИЙ ДЕМО-КЕЙС)",
                "procuring_entity": "Департамент будівництва, містобудування та архітектури Вінницької ОДА",
                "amount": 4200000.00,
                "winner_name": "ПП \"Будівельник-В\"",
                "winner_amount": 3950000.00,
                "disqualified_name": "ТОВ \"Поділля-Ремонт\" (Симульований дешевший учасник)",
                "disqualified_edrpou": "42859104",
                "disqualified_amount": 3800000.00,
                "diff_amount": 150000.00
            }
        ]
        for idx, target in enumerate(mock_leads, 1):
            report.append(f"\n### {idx}. {target['title']}")
            report.append(f"- **ID-номер:** {target['prozorro_id']}")
            report.append(f"- **Замовник:** {target['procuring_entity']}")
            report.append(f"- **🏆 Визначений переможець:** {target['winner_name']} — {target['winner_amount']:,.2f} грн".replace(",", " "))
            report.append(f"- **❌ Симульований лід:** {target['disqualified_name']} (ЄДРПОУ `{target['disqualified_edrpou']}`) — {target['disqualified_amount']:,.2f} грн _(дешевший на {target['diff_amount']:,.2f} грн!)_".replace(",", " "))
            report.append(f"💬 *Шаблон першого контакту (аутріч):*")
            report.append(f"> _«Вітаємо! Ми проаналізували тендер по ремонту [скорочена назва] і помітили, що ваша цінова пропозиція була найвигіднішою, але пропозицію було відхилено через технічні помилки в документах. Ми розробили систему AI-перевірки перед подачею, яка виявляє такі ловушки. Готові застрахувати вас на наступний тендер за моделлю Success Fee — оплата тільки у разі перемоги. Цікаво?»_")
            report.append("\n---")
    else:
        # Допоміжна функція для кличного відмінка
        def get_vocative_greeting(full_name):
            if not full_name:
                return "колеги"
            name_parts = full_name.split()
            if len(name_parts) >= 3:
                # Прізвище Ім'я По батькові -> беремо Ім'я та По батькові
                first = name_parts[1]
                patronymic = name_parts[2]
            elif len(name_parts) == 2:
                first = name_parts[0]
                patronymic = name_parts[1]
            else:
                return full_name
                
            voc_map = {
                "Олег Юрійович": "Олегу Юрійовичу",
                "Леся Петрівна": "Лесю Петрівно",
                "Наталія Петрівна": "Наталіє Петрівно",
                "Тетяна Дмитрівна": "Тетяно Дмитрівно"
            }
            key = f"{first} {patronymic}"
            return voc_map.get(key, key)

        for idx, target in enumerate(leads, 1):
            report.append(f"\n### {idx}. {target['title']}")
            report.append(f"- **ID-номер:** {target['prozorro_id']}")
            report.append(f"- **Замовник:** {target['procuring_entity']}")
            report.append(f"- **Очікувана вартість:** {target['amount']:,.2f} грн".replace(",", " "))
            report.append(f"- **Посилання:** [Prozorro URL](https://prozorro.gov.ua/tender/{target['prozorro_id']})")
            report.append(f"- **🏆 Визначений переможець:** {target['winner_name']} — {target['winner_amount']:,.2f} грн".replace(",", " "))
            report.append(f"- **❌ Дискваліфікований лід:** **{target['disqualified_name']}** (ЄДРПОУ `{target['disqualified_edrpou']}`) — {target['disqualified_amount']:,.2f} грн _(дешевший на {target['diff_amount']:,.2f} грн!)_".replace(",", " "))
            
            # Вивід контактних даних директора, якщо вони є в БД
            director = target.get('director_name')
            email = target.get('email')
            phone = target.get('phone')
            if director:
                report.append(f"- **👤 Директор:** {director}")
            if phone:
                report.append(f"- **📞 Контакти:** {phone}")
            if email:
                report.append(f"- **✉️ Email:** {email}")
                
            # Розрахунок Success Fee під нову модель (15% від маржі, де маржа береться як 5% від пропозиції)
            est_margin = target['disqualified_amount'] * 0.05
            est_fee = max(est_margin * 0.15, 5000)
            report.append(f"- **💰 Розрахунковий Success Fee (15% від 5% маржі):** {est_fee:,.0f} грн".replace(",", " "))
            
            greeting = get_vocative_greeting(director)
            if greeting == "колеги":
                greeting_str = f"Вітаємо, колеги з {target['disqualified_name']}"
            else:
                is_feminine = greeting.endswith("о") or "івно" in greeting or "ично" in greeting
                greeting_str = f"Шановна {greeting}" if is_feminine else f"Шановний {greeting}"
                
            # Специфічні шаблони під кожну ціль (Проактивний підхід: минуле -> теперішнє -> майбутній моніторинг)
            edrpou = target.get('disqualified_edrpou')
            if edrpou == '31964715': # ПП «РОУДІЗ»
                msg_body = f"Шановна Наталіє Петрівно! Ми проаналізували тендер щодо ремонту вул. Кічкарівської в Луцьку. Ваша ціна була вигіднішою майже на 50 000 грн, але пропозицію відхилили — ви не встигли виправити зауваження протягом 24 годин. Чи готуєте ви пропозицію на якийсь тендер прямо зараз? Якщо так — ми безкоштовно зробимо його AI-аудит за 10 хвилин, щоб уникнути дискваліфікації. Якщо ні — ми вже підключили ПП «РОУДІЗ» до нашого автоматичного AI-моніторингу. Як тільки з'явиться новий тендер у вашій категорії у Волинській області — ми самі надішлемо вам попередження про приховані вимоги. Зручно отримати деталі?"
            elif edrpou == '42648805': # ПП «БУДОЛІМПСТРОЙ ЛУЦЬК»
                msg_body = f"Шановна Тетяно Дмитрівно! Ми проаналізували тендер щодо ремонту вул. Кічкарівської в Луцьку. Ваша ціна була вигіднішою за переможця майже на 50 000 грн — але виправлені документи не встигли подати вчасно. Чи готуєте ви пропозицію на якийсь тендер прямо зараз? Якщо так — ми безкоштовно зробимо його AI-аудит за 10 хвилин, щоб застрахувати від подібних дедлайнів. Якщо зараз нічого немає — ми безкоштовно підключили вашу компанію до нашого автоматичного AI-моніторингу. Ми самі вийдемо на зв'язок, як тільки з'явиться новий тендер під ваші роботи у Волинській області. Зручно поспілкуватися?"
            elif edrpou == '41731677': # ТОВ «ВЕНТПРОМЕЛЕКТРОМОНТАЖ»
                msg_body = f"Шановна Лесю Петрівно! Ми проаналізували тендер на вентиляцію в Житомирі. Ваша пропозиція була вигіднішою на 99 900 грн, але її відхилили через специфікації «Аеростар». Чи готуєте ви пропозицію на якийсь тендер прямо зараз? Якщо так — ми безкоштовно зробимо його AI-аудит за 10 хвилин. Якщо ні — ми вже підключили ТОВ «ВЕНТПРОМЕЛЕКТРОМОНТАЖ» до нашого автоматичного AI-моніторингу. Ми самі надішлемо вам попередження, як тільки з'явиться новий тендер на вентиляційні чи будівельні роботи у вашому регіоні. Цікаво поглянути на деталі?"
            elif edrpou == '45795996': # ТОВ «ТЕП КЕПІТАЛ»
                msg_body = f"Шановний Олегу Юрійовичу! Ми проаналізували тендер на капремонт будинку по вул. Табірній у Києві. Ваша пропозиція була дешевшою, але її відхилили одразу через відсутність таблиці цін матеріалів. Чи готуєте ви пропозицію на якийсь тендер прямо зараз? Якщо так — ми безкоштовно зробимо його AI-аудит за 10 хвилин, щоб уникнути подібних прикрих дискваліфікацій. Якщо ні — ми безкоштовно підключили ТОВ «ТЕП КЕПІТАЛ» до нашого автоматичного AI-моніторингу. Ми самі вийдемо на зв'язок, як тільки з'явиться новий тендер під ваші роботи у Київській області. Зручно поговорити?"
            else:
                msg_body = f"{greeting_str}! Ми проаналізували нещодавній тендер щодо {target['title'][:60]}... Помітили, що ваша ціна була вигіднішою за переможця на {target['diff_amount']:,.0f} грн, але пропозицію відхилили через прикру помилку в довідках. Чи готуєте ви пропозицію на якийсь тендер прямо зараз? Якщо так — ми безкоштовно зробимо його AI-аудит за 10 хвилин. Якщо ні — ми безкоштовно підключили вашу компанію до нашого автоматичного AI-моніторингу. Ми самі напишемо вам, як тільки з'явиться новий тендер у вашій сфері у вашій області. Зручно обговорити?"
                
            report.append(f"💬 *Персоналізований шаблон першого контакту:*")
            report.append(f"> _«{msg_body}»_")
            report.append("\n---")

    # Збереження звіту в артефакти
    artifact_dir = "/Users/a1111/.gemini/antigravity/brain/9c8a8a24-d53a-44c1-b11e-a42f2fd18fe8"
    os.makedirs(artifact_dir, exist_ok=True)
    report_path = os.path.join(artifact_dir, "outreach_targets.md")
    
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(report))
        
    logger.info(f"💾 Накопичений звіт про аутріч-цілі збережено: {report_path}")
    print(f"\nОброблено тендерів: {scanned_tenders_count} | Нових лідів: {new_leads_count} | Всього в базі: {total_cnt}")


if __name__ == "__main__":
    asyncio.run(fetch_outreach_targets())
