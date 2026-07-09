"""
prozorro/client.py — Клієнт для Prozorro Public API

Документація: https://openprocurement.readthedocs.io/
API Base:     https://public-api.prozorro.gov.ua/api/2.5

Що вміє:
- Отримати тендер за ID або за посиланням (Prozorro URL)
- Завантажити документи тендера (PDF/docx)
- Отримати список активних тендерів по Вінниці з фільтрами
"""
import re
import asyncio
import aiohttp
from typing import Optional
from loguru import logger
from config import PROZORRO_API_BASE, PROZORRO_REQUEST_TIMEOUT, TARGET_REGION


def extract_tender_id(url_or_id: str) -> Optional[str]:
    """
    Витягує tender_id з різних форматів вводу:
    - Повне посилання: https://prozorro.gov.ua/tender/UA-2024-01-15-001234-a
    - Тільки ID:       UA-2024-01-15-001234-a
    """
    # Патерн для Prozorro ID: UA-РРРР-ММ-ДД-XXXXXX-a/b/c
    pattern = r'UA-\d{4}-\d{2}-\d{2}-\d{6}-[a-z]'
    match = re.search(pattern, url_or_id)
    if match:
        return match.group(0)
    # Якщо вставили просто числовий хеш (старий формат)
    if len(url_or_id) == 32 and url_or_id.isalnum():
        return url_or_id
    return None


async def resolve_tender_uuid(tender_id: str) -> Optional[str]:
    """
    Якщо tender_id має вигляд UA-..., робить запит до JSON API деталей
    на prozorro.gov.ua та повертає внутрішній 32-значний UUID.
    """
    if len(tender_id) == 32 and tender_id.isalnum():
        return tender_id

    # Використовуємо офіційний веб-API деталей тендера (повертає чистий JSON з UUID у полі id)
    url = f"https://prozorro.gov.ua/api/tenders/{tender_id}/details"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=15) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    uuid = data.get("id")
                    if uuid and len(uuid) == 32:
                        return uuid
    except Exception as e:
        logger.warning(f"Не вдалося вирішити UUID через details API для {tender_id}: {e}")

    # Fallback на парсинг HTML-сторінки, якщо API деталей недоступний або повернув помилку
    fallback_url = f"https://prozorro.gov.ua/tender/{tender_id}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(fallback_url, timeout=15) as resp:
                if resp.status == 200:
                    html = await resp.text()
                    match = re.search(r'/tenders/([a-f0-9]{32})', html)
                    if match:
                        return match.group(1)
                    match = re.search(r'"id":\s*"([a-f0-9]{32})"', html)
                    if match:
                        return match.group(1)
    except Exception as e:
        logger.warning(f"Не вдалося вирішити UUID через HTML-парсер для {tender_id}: {e}")
    return None


async def fetch_tender(tender_id: str) -> Optional[dict]:
    """
    Отримує повну інформацію про тендер з Prozorro API.
    Автоматично транслює публічний ID (UA-...) у внутрішній UUID.
    """
    uuid = await resolve_tender_uuid(tender_id)
    if not uuid:
        logger.warning(f"⚠️ Не вдалося знайти внутрішній UUID для тендера {tender_id}")
        return None

    url = f"{PROZORRO_API_BASE}/tenders/{uuid}"
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=PROZORRO_REQUEST_TIMEOUT)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    logger.info(f"✅ Тендер {tender_id} отримано успішно")
                    # Зберігаємо оригінальний tenderID в даних
                    tender_data = data.get("data", {})
                    if "tenderID" not in tender_data:
                        tender_data["tenderID"] = tender_id
                    return tender_data
                elif resp.status == 404:
                    logger.warning(f"⚠️ Тендер {tender_id} (UUID: {uuid}) не знайдено (404)")
                    return None
                else:
                    logger.error(f"❌ Помилка API: статус {resp.status} для {tender_id}")
                    return None
    except asyncio.TimeoutError:
        logger.error(f"⏱ Таймаут при отриманні тендера {tender_id}")
        return None
    except aiohttp.ClientError as e:
        logger.error(f"❌ Мережева помилка: {e}")
        return None


async def get_tender_documents(tender_data: dict) -> list[dict]:
    """
    Витягує список документів тендерної документації.
    
    Повертає список:
    [{"title": "...", "url": "...", "format": "application/pdf", "documentType": "tenderNotice"}]
    """
    documents = []
    
    # Документи на рівні тендера
    for doc in tender_data.get("documents", []):
        if doc.get("format") in ("application/pdf", "application/msword",
                                  "application/vnd.openxmlformats-officedocument.wordprocessingml.document"):
            documents.append({
                "title": doc.get("title", "Без назви"),
                "url": doc.get("url"),
                "format": doc.get("format"),
                "documentType": doc.get("documentType", "other"),
                "datePublished": doc.get("datePublished"),
            })
    
    logger.info(f"📄 Знайдено {len(documents)} документів у тендері")
    return documents


async def download_document(url: str, save_path: str) -> bool:
    """
    Завантажує документ за URL і зберігає в файл.
    Повертає True при успіху.
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                if resp.status == 200:
                    content = await resp.read()
                    with open(save_path, "wb") as f:
                        f.write(content)
                    logger.info(f"💾 Документ збережено: {save_path} ({len(content)//1024} KB)")
                    return True
                else:
                    logger.error(f"❌ Помилка завантаження ({resp.status}): {url}")
                    return False
    except Exception as e:
        logger.error(f"❌ Помилка завантаження документа: {e}")
        return False


async def get_active_vinnytsia_tenders(
    min_amount: float = 2_000_000,
    max_amount: float = 20_000_000,
    cpv_prefix: str = "45",
) -> list[dict]:
    """
    Отримує список активних тендерів по Вінницькій області
    з фільтрацією за сумою та CPV-кодом (будівельні роботи).
    
    API не підтримує всі фільтри напряму — частину фільтруємо локально.
    Рекомендований інтервал виклику: не частіше ніж раз на 30 хвилин.
    """
    url = f"{PROZORRO_API_BASE}/tenders"
    params = {"status": "active.tendering", "limit": 100}
    
    matching = []
    offset = None
    pages_checked = 0
    max_pages = 10  # Ліміт щоб не ходити нескінченно

    try:
        async with aiohttp.ClientSession() as session:
            while pages_checked < max_pages:
                if offset:
                    params["offset"] = offset
                
                async with session.get(url, params=params,
                                       timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status != 200:
                        break
                    
                    data = await resp.json()
                    tenders = data.get("data", [])
                    
                    if not tenders:
                        break
                    
                    for tender in tenders:
                        # Фільтр за регіоном (якщо TARGET_REGION задано)
                        region = (tender.get("procuringEntity", {})
                                       .get("address", {})
                                       .get("region", ""))
                        if TARGET_REGION and TARGET_REGION not in region:
                            continue
                        
                        # Фільтр за сумою
                        amount = tender.get("value", {}).get("amount", 0)
                        if not (min_amount <= amount <= max_amount):
                            continue
                        
                        # Фільтр за CPV-кодом (будівельні роботи = 45xxxxxxx)
                        items = tender.get("items", [])
                        cpv = items[0].get("classification", {}).get("id", "") if items else ""
                        if not cpv.startswith(cpv_prefix):
                            continue
                        
                        matching.append({
                            "id": tender.get("id"),
                            "title": tender.get("title"),
                            "amount": amount,
                            "currency": tender.get("value", {}).get("currency", "UAH"),
                            "procuring_entity": tender.get("procuringEntity", {}).get("name"),
                            "deadline": tender.get("tenderPeriod", {}).get("endDate"),
                            "cpv": cpv,
                            "region": region,
                            "url": f"https://prozorro.gov.ua/tender/{tender.get('id')}",
                        })
                    
                    # Пагінація
                    next_page = data.get("next_page", {})
                    offset = next_page.get("offset")
                    if not offset:
                        break
                    
                    pages_checked += 1
                    await asyncio.sleep(1)  # Ввічливий затримка між запитами
    
    except Exception as e:
        logger.error(f"❌ Помилка пошуку тендерів: {e}")
    
    logger.info(f"🔍 Знайдено {len(matching)} відповідних тендерів по Вінниці")
    return matching


def format_tender_summary(tender_data: dict) -> str:
    """
    Форматує короткий опис тендера для Telegram-повідомлення.
    """
    amount = tender_data.get("value", {}).get("amount", 0)
    currency = tender_data.get("value", {}).get("currency", "UAH")
    entity = tender_data.get("procuringEntity", {}).get("name", "Невідомий замовник")
    title = tender_data.get("title", "Без назви")
    status = tender_data.get("status", "")
    deadline = tender_data.get("tenderPeriod", {}).get("endDate", "")
    tender_id = tender_data.get("id", "")

    # Форматуємо суму
    amount_str = f"{amount:,.0f}".replace(",", " ")
    
    # Обрізаємо довгу назву
    if len(title) > 120:
        title = title[:117] + "..."

    return (
        f"📋 *{title}*\n\n"
        f"🏢 Замовник: {entity}\n"
        f"💰 Сума: {amount_str} {currency}\n"
        f"📅 Дедлайн: {deadline[:10] if deadline else 'не вказано'}\n"
        f"🔗 [Відкрити в ProZorro](https://prozorro.gov.ua/tender/{tender_id})"
    )


# ─── Розширені дані: Q&A, зміни, пов'язані процеси ──────────────────────────

async def fetch_related_processes(tender_data: dict) -> dict:
    """Збирає пов'язані дані: Q&A, зміни до ТД, скарги, пов'язані процеси."""
    result = {
        "questions":  tender_data.get("questions", []),
        "complaints": tender_data.get("complaints", []),
        "amendments": [],
        "related":    tender_data.get("relatedProcesses", []),
    }
    for doc in tender_data.get("documents", []):
        if doc.get("documentType") in ("changes", "corrigendum", "clarifications"):
            result["amendments"].append({
                "title":         doc.get("title"),
                "url":           doc.get("url"),
                "datePublished": doc.get("datePublished"),
                "documentType":  doc.get("documentType"),
            })
    return result


async def fetch_procuring_entity_history(
    edrpou: str,
    years_back: int = 2,
    max_tenders: int = 50,
) -> dict:
    """
    Аналізує публічну історію тендерів замовника за ЄДРПОУ.
    Повертає метрики: avg_bids, top_winners, monopoly_index, disqualified_rate, amendment_count.
    """
    import aiohttp, asyncio
    from config import PROZORRO_API_BASE
    from loguru import logger

    if not edrpou or len(edrpou) != 8:
        return {}

    url = f"{PROZORRO_API_BASE}/tenders"
    params = {"opt_fields": "id,numberOfBids,awards,procuringEntity,documents", "limit": 100}
    all_tenders: list = []
    try:
        async with aiohttp.ClientSession() as session:
            offset = None
            for _ in range(5):
                if offset:
                    params["offset"] = offset
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status != 200:
                        break
                    data = await resp.json()
                    batch = data.get("data", [])
                    if not batch:
                        break
                    for t in batch:
                        ident = t.get("procuringEntity", {}).get("identifier", {}).get("id", "")
                        if ident == edrpou:
                            all_tenders.append(t)
                    offset = data.get("next_page", {}).get("offset")
                    if not offset or len(all_tenders) >= max_tenders:
                        break
                    await asyncio.sleep(0.5)
    except Exception as exc:
        logger.warning(f"fetch_procuring_entity_history: {exc}")

    if not all_tenders:
        return {"edrpou": edrpou, "total_tenders": 0}

    total = len(all_tenders)
    avg_bids = round(sum(t.get("numberOfBids", 0) for t in all_tenders) / total, 1)
    winner_counts: dict = {}
    disq_count = 0
    amendment_count = 0

    for t in all_tenders:
        for award in t.get("awards", []):
            supplier = (award.get("suppliers") or [{}])[0]
            name = supplier.get("name", "?")
            if award.get("status") == "active":
                winner_counts[name] = winner_counts.get(name, 0) + 1
            elif award.get("status") in ("unsuccessful", "cancelled"):
                disq_count += 1
        for doc in t.get("documents", []):
            if doc.get("documentType") in ("changes", "corrigendum"):
                amendment_count += 1

    top = sorted(winner_counts.items(), key=lambda x: x[1], reverse=True)[:3]
    total_awards = sum(winner_counts.values()) or 1
    top1_share = round(top[0][1] / total_awards * 100, 1) if top else 0
    total_decided = total_awards + disq_count
    disq_rate = round(disq_count / total_decided * 100, 1) if total_decided else 0

    if top1_share >= 70:
        monopoly_level = "🔴 КРИТИЧНИЙ"
    elif top1_share >= 40:
        monopoly_level = "🟡 ПІДВИЩЕНИЙ"
    else:
        monopoly_level = "🟢 НОРМАЛЬНИЙ"

    return {
        "edrpou":            edrpou,
        "total_tenders":     total,
        "avg_bids":          avg_bids,
        "top_winners":       [{"name": n, "wins": w} for n, w in top],
        "monopoly_index":    top1_share,
        "monopoly_level":    monopoly_level,
        "disqualified_rate": disq_rate,
        "amendment_count":   amendment_count,
        "note": "Замовник часто змінює ТД — ознака «заточки»" if amendment_count >= 3 else "",
    }


def format_history_report(history: dict) -> str:
    """Форматує результат history для Telegram-звіту."""
    if not history or history.get("total_tenders", 0) == 0:
        return "ℹ️ _Даних про замовника не знайдено_"
    lines = [
        f"📊 *Аналіз замовника (ЄДРПОУ {history.get('edrpou', '?')})*", "",
        f"• Тендерів за 2 роки: {history.get('total_tenders')}",
        f"• Середня к-сть учасників: {history.get('avg_bids')}",
        f"• Дискваліфіковано: {history.get('disqualified_rate')}%",
        f"• Змін до ТД: {history.get('amendment_count')}",
        "",
        f"*Монополізація:* {history.get('monopoly_level')}",
    ]
    for i, w in enumerate(history.get("top_winners", []), 1):
        lines.append(f"  {i}. {w['name']} — {w['wins']} перемог")
    note = history.get("note", "")
    if note:
        lines += ["", f"⚠️ _{note}_"]
    return "\n".join(lines)
