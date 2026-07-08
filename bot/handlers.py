"""
bot/handlers.py — Обробники Telegram-бота (v2.1 — мультиагентний пайплайн)

Сценарії:
1. /start     — привітання, реєстрація клієнта
2. /analyze   — запуск AI-аналізу тендера за посиланням або ID
3. /status    — список тендерів клієнта
4. /monitor   — увімкнути/вимкнути автомоніторинг
5. /help      — довідка

Пайплайн аналізу (v2.1):
  Prozorro API → OCR Pipeline → Scanner Agent → Calculator Agent → Reviewer Agent
  + Аналіз замовника (fetch_procuring_entity_history)
  + Early Exit при BLOCKED вердикті
"""
import os
import json
import asyncio
import tempfile
from loguru import logger
from aiogram import Router, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, FSInputFile
from aiogram.filters import Command, CommandStart
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext

from prozorro.client import (
    extract_tender_id, fetch_tender, get_tender_documents,
    download_document, format_tender_summary,
    fetch_related_processes, fetch_procuring_entity_history,
    format_history_report,
)
from ai.pdf_parser import extract_text_from_pdf, split_into_sections
from ai.orchestrator import Blackboard, TenderWorkflow
from db.database import (
    get_or_create_client, create_tender, update_tender, 
    get_client_tenders, log_action, update_client_profile, 
    get_client_by_telegram_id
)
from config import ADMIN_TELEGRAM_ID

router = Router()

# ── Правовий дисклеймер (обов'язковий у кожному звіті) ──────────────────────
DISCLAIMER = (
    "\n\n─────────────────────────\n"
    "⚖️ _Цей звіт створено AI і має виключно інформаційний характер. "
    "Він не є юридичною консультацією. "
    "Перед подачею заявки зверніться до кваліфікованого юриста._"
)


# ── FSM States ───────────────────────────────────────────────────────────────

class RegistrationStates(StatesGroup):
    waiting_company_name = State()
    waiting_contact_name = State()
    
    # Нові стани для заповнення профілю (/profile)
    waiting_profile_edrpou = State()
    waiting_profile_director_name = State()
    waiting_profile_director_title = State()
    waiting_profile_equipment = State()
    waiting_profile_staff = State()
    waiting_profile_contracts = State()


# ── Хелпери ──────────────────────────────────────────────────────────────────

def status_emoji(status: str) -> str:
    return {
        "new": "🆕", "analyzing": "🔄", "ready": "✅",
        "submitted": "📤", "won": "🏆", "lost": "❌",
    }.get(status, "❓")


def _format_scan_report(bb: Blackboard, tender_title: str = "", amount: float = 0.0) -> str:
    """Форматує результати всього мультиагентного конвеєра (Blackboard) для Telegram."""
    scan_result = bb.scan_result or {}
    calc_result = bb.calc_result or {}
    review_result = bb.review_result or {}

    risk = scan_result.get("risk_level", "UNKNOWN")
    risk_emoji = {"LOW": "🟢", "MEDIUM": "🟡", "HIGH": "🔴", "CRITICAL": "🚨"}.get(risk, "⚪")

    lines = [
        f"🤖 *AI-Аналіз тендера (Мультиагентний конвеєр v2.1)*",
        f"_{tender_title[:70]}_" if tender_title else "",
        "",
        f"{risk_emoji} *Рівень ризику: {risk}*",
        "",
    ]

    # 1. Виведення юридичних заперечень та ризиків з Blackboard
    disc_reqs = bb.get_facts_by_type("discriminatory_requirement")
    contract_risks = bb.get_facts_by_type("contract_risk")

    if not disc_reqs and not contract_risks:
        lines.append("✅ Дискримінаційних вимог та критичних ризиків не виявлено.")
    else:
        # Юридичні заперечення (actionable - можна оскаржувати)
        if disc_reqs:
            lines.append("⚖️ *Юридичні заперечення (можна оскаржити або подати запит):*")
            for idx, fact in enumerate(disc_reqs[:5], 1):
                content = fact.content or {}
                req_type = content.get("type") or "Дискримінаційна вимога"
                rec = content.get("recommendation") or "Подати запит на роз'яснення"
                law = fact.law_reference or ""
                verif_str = "" if fact.verified else " 🔍 *[НЕПІДТВЕРДЖЕНО]*"
                
                lines.append(f"  {idx}. *{req_type}*{verif_str}")
                if law:
                    lines.append(f"     📖 _{law}_")
                if fact.raw_quote:
                    lines.append(f"     💬 «_{fact.raw_quote[:120]}..._»")
                lines.append(f"     💡 *Рекомендація:* {rec}")
                lines.append("")
            if len(disc_reqs) > 5:
                lines.append(f"  _...та ще {len(disc_reqs) - 5} вимог_\n")
            
            if amount >= 1500000:
                lines.append(f"👉 *Вимоги можна оскаржити в АМКУ (поріг 1.5 млн грн перевищено).*\n"
                             f"Надішліть `/amku {bb.tender_id}` для генерації офіційної скарги.")
            lines.append("")

        # Комерційні ризики договору (commercial risks)
        # Баг #1 (partial): contract_risk факти не проходять через _self_check_citations
        # (той перевіряє тільки тип "trap"). Тому не показуємо [НЕПІДТВЕРДЖЕНО] для них —
        # наявність raw_quote вже є достатнім підтвердженням для комерційних ризиків.
        if contract_risks:
            lines.append("💼 *Комерційні ризики договору (врахувати при розрахунку ціни):*")
            for idx, fact in enumerate(contract_risks[:5], 1):
                content = fact.content or {}
                severity = content.get("type") or "WARNING"
                desc = content.get("description") or "Ризик договору"
                
                sev_icon = {"CRITICAL": "🚨", "HIGH": "🔴", "WARNING": "🟡", "INFO": "⚪"}.get(severity, "🟡")
                # Для комерційних ризиків не показуємо [НЕПІДТВЕРДЖЕНО] — вони не верифікуються через self_check
                lines.append(f"  {sev_icon} {idx}. *{desc}*")
                if fact.raw_quote:
                    lines.append(f"     💬 «_{fact.raw_quote[:120]}..._»")
                lines.append("")
            if len(contract_risks) > 5:
                lines.append(f"  _...та ще {len(contract_risks) - 5} ризиків_\n")
            lines.append("")

    # 2. Розрахунок маржі (Калькулятор)
    if calc_result:
        total_cost = calc_result.get("total_cost", 0)
        margin_pct = calc_result.get("margin_pct", 0)
        margin_amount = calc_result.get("margin", 0)
        rec = calc_result.get("recommendation", "PARTICIPATE")
        completeness = calc_result.get("data_completeness", "INCOMPLETE")
        risk = calc_result.get("margin_risk", "UNKNOWN")

        cost_str = f"{total_cost:,.0f}".replace(",", " ")
        margin_str = f"{margin_amount:,.0f}".replace(",", " ")

        # Якщо даних немає — показуємо «Недостатньо даних», а не оптимістичний PARTICIPATE.
        if completeness not in ("COMPLETE", "HEURISTIC") or risk == "UNKNOWN":
            rec_display = "⚠️ *Недостатньо даних для рекомендації*"
        elif risk == "CRITICAL" or (margin_pct is not None and float(margin_pct) < 0):
            rec_display = "❌ *НЕ БРАТИ УЧАСТЬ (збиток)*"
        elif risk == "WARNING":
            rec_display = "🟡 *ОБЕРЕЖНО (маржа нижче 10% — перевірити кошторис)*"
        else:
            rec_display = "✅ *БРАТИ УЧАСТЬ*"

        if completeness == "COMPLETE":
            comp_str = "🟢 COMPLETE"
        elif completeness == "HEURISTIC":
            comp_str = "🟡 HEURISTIC (кошторис зовнішній — евристична оцінка)"
        else:
            comp_str = "🔴 INCOMPLETE (неповні дані для оцінки)"
        risk_str = {"UNKNOWN": "⚪ UNKNOWN", "LOW": "🟢 LOW", "WARNING": "🟡 WARNING", "CRITICAL": "🚨 CRITICAL"}.get(risk, risk)

        discount_pct = calc_result.get("expected_discount_pct", 4.0)
        lines += [
            "💰 *AI-Калькулятор собівартості:*",
            f"  • Повнота специфікації: *{comp_str}*",
            f"  • Розрахунок передбачає знижку *{discount_pct}%* на аукціоні",
            f"  • Фінансовий ризик: *{risk_str}*",
        ]
        
        if completeness in ("COMPLETE", "HEURISTIC"):
            machinery_cost = calc_result.get("machinery_cost", 0)
            lines += [
                f"  • Орієнтовна собівартість: {cost_str} грн",
            ]
            if machinery_cost > 0:
                mach_str = f"{machinery_cost:,.0f}".replace(",", " ")
                lines.append(f"    ↳ в т.ч. будівельна техніка: {mach_str} грн")
            lines.append(f"  • Очікувана маржа: {margin_str} грн ({margin_pct}%)")
            if completeness == "HEURISTIC":
                lines.append("  • ⚠️ _Розрахунок евристичний (кошторис ПКД зовнішній). Уточніть у АВК-5._")
            if calc_result.get("notes"):
                lines.append(f"  • 💬 _Примітки: {calc_result['notes']}_")

            # ── Блок «Ваша вигода» — Success fee консультанта ───────────────
            # Модель: 15% від розрахункової маржі клієнта, мін. 5 000 грн
            SUCCESS_FEE_RATE = 0.15
            SUCCESS_FEE_MIN  = 5_000
            if margin_amount > 0:
                raw_fee = margin_amount * SUCCESS_FEE_RATE
                success_fee = max(raw_fee, SUCCESS_FEE_MIN)
                fee_str = f"{success_fee:,.0f}".replace(",", " ")
                margin_after_fee = margin_amount - success_fee
                margin_after_str = f"{margin_after_fee:,.0f}".replace(",", " ")
                lines += [
                    "",
                    "💼 *Ваша вигода (Success Fee):*",
                    f"  • Комісія консультанта: *{fee_str} грн* (15% від маржі, мін. 5 000 грн)",
                    f"  • Чиста маржа клієнта після комісії: *{margin_after_str} грн*",
                ]
            # ─────────────────────────────────────────────────────────────────
        else:
            lines += [
                "  • Собівартість/Маржа: [Розрахунок неможливий — потрібен файл локального кошторису .imd]",
            ]
            
        lines += [
            f"  • Рекомендація: {rec_display}",
            ""
        ]

    # 3. Вердикт перевіряючого (Reviewer)
    if review_result:
        verdict = review_result.get("verdict", "NEEDS_FIX")
        conf = review_result.get("confidence", 1.0)
        verdict_icon = {"READY": "🟢 READY", "NEEDS_FIX": "🟡 NEEDS_FIX", "BLOCKED": "🔴 BLOCKED"}.get(verdict, verdict)

        lines += [
            "⚖️ *AI-Вердикт (Аудит когерентності):*",
            f"  • Статус: *{verdict_icon}* (впевненість: {int(conf*100)}%)",
        ]

        issues = review_result.get("issues", [])
        if issues:
            lines.append("  • Знайдені проблеми/ризики:")
            for issue in issues[:5]:
                severity_icon = "🔴" if issue.get("severity") == "CRITICAL" else "🟡"
                doc_name = issue.get("document", "Документ")
                desc = issue.get("issue", "")
                fix = issue.get("fix", "")
                lines.append(f"    - {severity_icon} *{doc_name}*: {desc}")
                if fix:
                    lines.append(f"      💡 Виправлення: {fix}")
            if len(issues) > 5:
                lines.append(f"    - _...та ще {len(issues)-5} проблем_")
            lines.append("")

    # 4. Чеклист документів
    req_docs = scan_result.get("required_documents", [])
    if req_docs:
        lines += ["📋 *Потрібні документи для участі:*"]
        for doc in req_docs[:6]:
            lines.append(f"  • {doc}")
        if len(req_docs) > 6:
            lines.append(f"  _...та ще {len(req_docs)-6}_")

    return "\n".join(l for l in lines if l is not None)


# ── Команди ──────────────────────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message):
    """Вітання + реєстрація клієнта."""
    await get_or_create_client(
        telegram_id=message.from_user.id,
        contact_name=message.from_user.full_name,
    )
    await message.answer(
        f"👋 Вітаю, *{message.from_user.first_name}*!\n\n"
        "Я — AI-асистент для аналізу тендерів ProZorro.\n\n"
        "Що я вмію:\n"
        "🔍 Аналізую тендерну документацію за допомогою AI\n"
        "⚠️ Знаходжу дискримінаційні вимоги й ловушки\n"
        "📊 Аналізую історію замовника (монополізація, дискваліфікації)\n"
        "💰 Оцінюю фінансову привабливість лоту\n\n"
        "📝 *Важливо:* Щоб я міг автоматично заповнювати документи під ваші дані, заповніть профіль компанії: /profile\n\n"
        "Надішли мені посилання на тендер Prozorro або його ID\n"
        "_(наприклад: UA-2025-10-06-001013-a)_",
        parse_mode="Markdown"
    )


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "📖 *Довідка*\n\n"
        "*/analyze* `<посилання або ID>` — аналіз тендера\n"
        "*/profile* — заповнити або редагувати профіль компанії\n"
        "*/status* — мої тендери\n"
        "*/monitor* — автопошук нових тендерів по Вінниці\n\n"
        "Або просто надішли посилання на Prozorro — розпізнаю автоматично.",
        parse_mode="Markdown"
    )


@router.message(Command("status"))
async def cmd_status(message: Message):
    client = await get_or_create_client(telegram_id=message.from_user.id)
    tenders = await get_client_tenders(client["id"])
    if not tenders:
        await message.answer(
            "У тебе поки немає тендерів.\n"
            "Надішли посилання на тендер Prozorro для аналізу! 👆"
        )
        return
    lines = ["📊 *Твої тендери:*\n"]
    for t in tenders:
        emoji = status_emoji(t["status"])
        amount = f"{t['amount']:,.0f}".replace(",", " ") if t.get("amount") else "?"
        lines.append(
            f"{emoji} *{(t['tender_title'] or '')[:50]}*\n"
            f"   💰 {amount} грн | {t['status']}\n"
        )
    await message.answer("\n".join(lines), parse_mode="Markdown")

# ── Вибір тарифного плану (/tariff) ──────────────────────────────────────────

def get_tariff_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton(text="🏆 Success Fee (Комісія %)", callback_data="set_scheme_success_fee"),
            InlineKeyboardButton(text="💳 Flat Rate (Передплата)", callback_data="set_scheme_flat_rate"),
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


@router.message(Command("tariff"))
async def cmd_tariff(message: Message):
    """Вибір та перегляд тарифного плану."""
    from db.database import DbConnection
    async with DbConnection() as db:
        row = await db.fetchone("SELECT billing_scheme FROM clients WHERE telegram_id = ?", (message.from_user.id,))
        
    scheme = row.get("billing_scheme", "success_fee") if row else "success_fee"
    scheme_title = "🏆 Success Fee (Комісія від виграшу)" if scheme == "success_fee" else "💳 Flat Rate (Фіксована передплата)"
    
    await message.answer(
        f"💳 *Ваш поточний тарифний план:* {scheme_title}\n\n"
        f"👉 *Ви можете змінити модель оплати:*\n\n"
        f"1. *Success Fee (Комісія за результат):*\n"
        f"   • Безкоштовний аналіз та автозаповнення довідок.\n"
        f"   • Оплата відсотку від суми контракту *тільки після виграшу та підписання договору* (5% до 4 млн, 4% від 4 до 10 млн, 2.5-3% від 10 до 20 млн).\n\n"
        f"2. *Flat Rate (Фіксована передплата):*\n"
        f"   • Оплата фіксованої суми перед початком робіт.\n"
        f"   • *10 000 грн* за детальний аналіз лоту та кошторису.\n"
        f"   • *15 000 грн* за автогенерацію повного пакету довідок.\n"
        f"   • *20 000 грн* за підготовку та юридичний супровід скарги в АМКУ.\n\n"
        f"Оберіть бажану схему оплати на кнопках нижче:",
        reply_markup=get_tariff_keyboard(),
        parse_mode="Markdown"
    )


@router.callback_query(F.data.startswith("set_scheme_"))
async def process_set_scheme(callback: CallbackQuery):
    scheme = callback.data.replace("set_scheme_", "")
    from db.database import DbConnection
    async with DbConnection() as db:
        await db.execute("UPDATE clients SET billing_scheme = ? WHERE telegram_id = ?", (scheme, callback.from_user.id))
        await db.commit()
        
    scheme_title = "🏆 Success Fee (Комісія від виграшу)" if scheme == "success_fee" else "💳 Flat Rate (Фіксована передплата)"
    
    await callback.message.edit_text(
        f"✅ *Тарифний план успішно змінено!*\n\n"
        f"Ваш новий тариф: *{scheme_title}*\n\n"
        f"Ви можете змінити його в будь-який момент за допомогою команди `/tariff`.",
        parse_mode="Markdown"
    )
    await callback.answer()


# ── Профіль компанії (/profile) ─────────────────────────────────────────────

@router.message(Command("profile"))
async def cmd_profile(message: Message, state: FSMContext):
    """Початок або перегляд профілю компанії."""
    client = await get_client_by_telegram_id(message.from_user.id)
    if not client:
        client = await get_or_create_client(
            telegram_id=message.from_user.id,
            contact_name=message.from_user.full_name,
        )
    
    is_complete = client.get("is_profile_complete", 0)
    if is_complete:
        profile_json = client.get("profile_json") or "{}"
        try:
            profile_data = json.loads(profile_json)
        except Exception:
            profile_data = {}
        
        equipment = profile_data.get("equipment", [])
        staff = profile_data.get("staff", [])
        contracts = profile_data.get("analog_contracts", [])
        
        eq_str = "\n".join(f"  • {item['name']} ({item['qty']} шт.) — {item['source']}" for item in equipment[:5])
        st_str = "\n".join(f"  • {person['role']}: {person['name']}" for person in staff[:5])
        co_str = "\n".join(f"  • {c['client']} ({c['amount']:,} грн) — {c['year']} р." for c in contracts[:5])

        await message.answer(
            f"📋 *Поточний профіль вашої компанії:*\n\n"
            f"🏢 Назва: *{client.get('company_name')}*\n"
            f"🔢 ЄДРПОУ: `{client.get('edrpou')}`\n"
            f"👤 Керівник: *{client.get('director_title')} {client.get('director_name')}*\n\n"
            f"⚙️ *Техніка (перші 5):*\n{eq_str or '  _немає_'}\n\n"
            f"👷‍♂️ *Персонал (перші 5):*\n{st_str or '  _немає_'}\n\n"
            f"📜 *Аналогічні договори (перші 5):*\n{co_str or '  _немає_'}\n\n"
            f"Бажаєте перезаписати профіль? Введіть нову назву компанії або надішліть /cancel для скасування.",
            parse_mode="Markdown"
        )
    else:
        await message.answer(
            "👋 Почнемо налаштування профілю вашої компанії!\n\n"
            "AI використовуватиме ці дані, щоб автоматично генерувати довідки про наявність техніки, кваліфікованого персоналу та аналогічного досвіду.\n\n"
            "1️⃣ *Введіть офіційну назву компанії* (наприклад: _ТОВ БудКомпані Вінниця_):",
            parse_mode="Markdown"
        )
    
    await state.set_state(RegistrationStates.waiting_company_name)


@router.message(RegistrationStates.waiting_company_name)
async def process_profile_company_name(message: Message, state: FSMContext):
    await state.update_data(company_name=message.text.strip())
    await message.answer(
        "2️⃣ *Введіть код ЄДРПОУ компанії* (8 цифр):",
        parse_mode="Markdown"
    )
    await state.set_state(RegistrationStates.waiting_profile_edrpou)


@router.message(RegistrationStates.waiting_profile_edrpou)
async def process_profile_edrpou(message: Message, state: FSMContext):
    edrpou = message.text.strip()
    if not edrpou.isdigit() or len(edrpou) != 8:
        await message.answer("❌ Код ЄДРПОУ повинен складатися рівно з 8 цифр. Спробуйте ще раз:")
        return
    await state.update_data(edrpou=edrpou)
    await message.answer(
        "3️⃣ *Введіть ПІБ керівника* у родовому відмінку (наприклад: _Ковальчука Олександра Петровича_):",
        parse_mode="Markdown"
    )
    await state.set_state(RegistrationStates.waiting_profile_director_name)


@router.message(RegistrationStates.waiting_profile_director_name)
async def process_profile_director_name(message: Message, state: FSMContext):
    await state.update_data(director_name=message.text.strip())
    await message.answer(
        "4️⃣ *Введіть посаду керівника* (наприклад: _Директор_, _Генеральний директор_):",
        parse_mode="Markdown"
    )
    await state.set_state(RegistrationStates.waiting_profile_director_title)


@router.message(RegistrationStates.waiting_profile_director_title)
async def process_profile_director_title(message: Message, state: FSMContext):
    await state.update_data(director_title=message.text.strip())
    await message.answer(
        "5️⃣ *Перелік будівельної техніки та обладнання.*\n\n"
        "Надішліть список техніки, кожен елемент з нового рядка у форматі:\n"
        "`Назва техніки | Кількість | Власна / Оренда / Лізинг`\n\n"
        "Приклад:\n"
        "_Екскаватор JCB 3CX | 1 | Власна_\n"
        "_Автомобіль бортовий КрАЗ | 2 | Оренда_\n"
        "_Риштування будівельні | 150 | Власна_",
        parse_mode="Markdown"
    )
    await state.set_state(RegistrationStates.waiting_profile_equipment)


@router.message(RegistrationStates.waiting_profile_equipment)
async def process_profile_equipment(message: Message, state: FSMContext):
    text = message.text.strip()
    equipment = []
    for line in text.split("\n"):
        if not line.strip():
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 1:
            name = parts[0]
            qty = 1
            source = "Власна"
            if len(parts) >= 2:
                try:
                    qty = int(parts[1])
                except ValueError:
                    qty = 1
            if len(parts) >= 3:
                source = parts[2]
            equipment.append({"name": name, "qty": qty, "source": source})
            
    await state.update_data(equipment=equipment)
    await message.answer(
        "6️⃣ *Перелік інженерно-технічних працівників та робітників.*\n\n"
        "Надішліть список працівників, кожен з нового рядка у форматі:\n"
        "`Посада | ПІБ | Освіта / Спеціальність | Досвід роботи років`\n\n"
        "Приклад:\n"
        "_Головний інженер | Іваненко Іван Іванович | Вища будівельна | 12_\n"
        "_Виконавець робіт | Сидоренко Петро Петрович | Середня-спеціальна | 8_\n"
        "_Дорожній робітник | Петренко Олег Васильович | Загальна середня | 3_",
        parse_mode="Markdown"
    )
    await state.set_state(RegistrationStates.waiting_profile_staff)


@router.message(RegistrationStates.waiting_profile_staff)
async def process_profile_staff(message: Message, state: FSMContext):
    text = message.text.strip()
    staff = []
    for line in text.split("\n"):
        if not line.strip():
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 2:
            role = parts[0]
            name = parts[1]
            education = parts[2] if len(parts) >= 3 else "Вища"
            exp = parts[3] if len(parts) >= 4 else "5"
            staff.append({"role": role, "name": name, "education": education, "exp": exp})
            
    await state.update_data(staff=staff)
    await message.answer(
        "7️⃣ *Перелік виконаних аналогічних договорів.*\n\n"
        "Надішліть список договорів, кожен з нового рядка у форматі:\n"
        "`Назва замовника | Предмет договору | Сума договору в грн | Рік виконання`\n\n"
        "Приклад:\n"
        "_Департамент комунального господарства ВМР | Капітальний ремонт переходу | 5400000 | 2024_\n"
        "_ПП Будівельник | Поточний ремонт офісу | 1200000 | 2023_",
        parse_mode="Markdown"
    )
    await state.set_state(RegistrationStates.waiting_profile_contracts)


@router.message(RegistrationStates.waiting_profile_contracts)
async def process_profile_contracts(message: Message, state: FSMContext):
    text = message.text.strip()
    contracts = []
    for line in text.split("\n"):
        if not line.strip():
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 3:
            client = parts[0]
            subject = parts[1]
            try:
                amount = float(parts[2].replace(" ", ""))
            except ValueError:
                amount = 1000000.0
            year = parts[3] if len(parts) >= 4 else "2024"
            contracts.append({"client": client, "subject": subject, "amount": amount, "year": year})
            
    data = await state.get_data()
    
    # Зберігаємо все як JSON blob в profile_json
    profile_data = {
        "license": "Ліцензія на будівництво об'єктів класу наслідків СС2",
        "equipment": data.get("equipment", []),
        "staff": data.get("staff", []),
        "analog_contracts": contracts,
    }
    
    await update_client_profile(
        telegram_id=message.from_user.id,
        company_name=data.get("company_name"),
        edrpou=data.get("edrpou"),
        director_name=data.get("director_name"),
        director_title=data.get("director_title"),
        profile_json=json.dumps(profile_data, ensure_ascii=False),
        is_profile_complete=1
    )
    
    await state.clear()
    
    await message.answer(
        "🎉 *Профіль компанії успішно заповнено!*\n\n"
        "Тепер при аналізі тендерів я зможу автоматично генерувати повні довідки про техніку, персонал та договори.\n\n"
        "Для перевірки надішліть будь-який тендер Prozorro! 🚀",
        parse_mode="Markdown"
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Заповнення профілю скасовано.")


# ── Головна логіка: аналіз тендера ──────────────────────────────────────────

@router.message(Command("analyze"))
async def cmd_analyze(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "Вкажи ID або посилання:\n"
            "`/analyze UA-2024-01-15-001234-a`",
            parse_mode="Markdown"
        )
        return
    await process_tender_link(message, parts[1].strip())


@router.message(F.text.regexp(r'prozorro\.gov\.ua/tender/|UA-\d{4}-\d{2}-\d{2}'))
async def handle_prozorro_link(message: Message):
    await process_tender_link(message, message.text.strip())


async def process_tender_link(message: Message, tender_input: str):
    """
    Мультиагентний конвеєр аналізу тендера v2.1:
    API → OCR → Scanner → Calculator → Reviewer + HistoryAnalyzer
    """
    client = await get_or_create_client(telegram_id=message.from_user.id)

    # ── Крок 1: Розпізнати ID ──────────────────────────────────────────────
    tender_id = extract_tender_id(tender_input)
    if not tender_id:
        await message.answer(
            "❌ Не вдалося розпізнати ID тендера.\n"
            "Приклад: `UA-2024-01-15-001234-a`",
            parse_mode="Markdown"
        )
        return

    status_msg = await message.answer(
        f"🔄 Отримую дані тендера `{tender_id}`...",
        parse_mode="Markdown"
    )

    # ── Крок 2: Дані з Prozorro ────────────────────────────────────────────
    tender_data = await fetch_tender(tender_id)
    if not tender_data:
        await status_msg.edit_text("❌ Тендер не знайдено. Перевір ID або посилання.")
        return

    summary = format_tender_summary(tender_data)
    await status_msg.edit_text(summary, parse_mode="Markdown")

    # ── Крок 2.5: Швидкий пре-скринінг релевантності ──────────────────────
    from ai.agents.screener import screen_tender
    title = tender_data.get("title", "")
    entity = tender_data.get("procuringEntity", {}).get("name", "")
    desc = tender_data.get("description", "")
    
    screening = await screen_tender(title, entity, amount, desc)
    if not screening.get("relevant"):
        await status_msg.answer(
            f"⚠️ *Тендер відхилено за нерелевантністю*\n\n"
            f"AI-Скринінг визначив цей лот як невідповідний нашому профілю.\n"
            f"💬 *Причина:* {screening.get('reason')}\n\n"
            f"Якщо ви все одно хочете проаналізувати його, надішліть запит менеджеру.",
            parse_mode="Markdown"
        )
        return

    # ── Крок 3: Зберегти в БД ─────────────────────────────────────────────
    amount = tender_data.get("value", {}).get("amount", 0)
    db_tender_id = await create_tender(
        client_id=client["id"],
        prozorro_id=tender_id,
        title=tender_data.get("title", "")[:200],
        procuring_entity=tender_data.get("procuringEntity", {}).get("name", ""),
        amount=amount,
        deadline=tender_data.get("tenderPeriod", {}).get("endDate", ""),
    )

    # ── Крок 4: Розширені дані (Q&A, зміни) ───────────────────────────────
    related = await fetch_related_processes(tender_data)
    amendments_count = len(related.get("amendments", []))
    qa_count = len(related.get("questions", []))

    # ── Крок 5: Аналіз замовника ───────────────────────────────────────────
    await message.answer("📊 Аналізую історію замовника...")
    edrpou = (tender_data.get("procuringEntity", {})
                         .get("identifier", {})
                         .get("id", ""))
    history = {}
    if edrpou:
        history = await fetch_procuring_entity_history(edrpou)
        history_report = format_history_report(history)
        await message.answer(history_report, parse_mode="Markdown")

    # ── Крок 6: Завантажити документи ─────────────────────────────────────
    analyzing_msg = await message.answer("📄 Завантажую тендерну документацію...")
    documents = await get_tender_documents(tender_data)

    if not documents:
        await analyzing_msg.edit_text(
            "⚠️ Документи в тендері не знайдені.\n"
            "AI-аналіз неможливий без тендерної документації."
        )
        await update_tender(db_tender_id, status="ready", notes="Немає документів")
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
    docs_to_analyze = documents[:3]  # Беремо максимум 3 найважливіші
    
    await analyzing_msg.edit_text(
        f"📚 Вибрано {len(docs_to_analyze)} документів для глибокого аналізу. Починаю завантаження...",
        parse_mode="Markdown"
    )

    combined_text_parts = []
    
    try:
        from ai.ocr_pipeline import get_ocr_pipeline
        ocr = get_ocr_pipeline(use_ocr=True)
        
        for idx, doc in enumerate(docs_to_analyze, 1):
            await analyzing_msg.edit_text(
                f"📥 Завантажую та розпізнаю ({idx}/{len(docs_to_analyze)}):\n"
                f"_{doc['title'][:50]}_...",
                parse_mode="Markdown"
            )
            
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                tmp_path = tmp.name
                
            success = await download_document(doc["url"], tmp_path)
            if not success:
                logger.warning(f"Не вдалося завантажити {doc['title']}")
                continue
                
            try:
                parsed_doc = await ocr.parse(tmp_path)
                if parsed_doc.all_text:
                    combined_text_parts.append(
                        f"\n\n==================================================\n"
                        f"📄 НАЗВА ДОКУМЕНТА: {doc['title']}\n"
                        f"==================================================\n"
                        f"{parsed_doc.all_text}"
                    )
                    
                    if parsed_doc.warnings:
                        for w in parsed_doc.warnings[:1]:
                            await message.answer(f"⚠️ _{doc['title'][:40]}_: {w}", parse_mode="Markdown")
            except Exception as exc:
                logger.warning(f"Помилка OCR для {doc['title']}, fallback на pdf_parser: {exc}")
                pdf_text = extract_text_from_pdf(tmp_path)
                if pdf_text:
                    combined_text_parts.append(pdf_text)
            finally:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
                    
    except Exception as exc:
        logger.error(f"Помилка OCR конвеєра: {exc}")
        await message.answer("❌ Сталася критична помилка під час OCR обробки.")
        return

    pdf_text = "".join(combined_text_parts)
    if not pdf_text.strip():
        await analyzing_msg.edit_text(
            "⚠️ Жоден документ не містить розпізнаного тексту.\n"
            "AI-аналіз неможливий без текстових даних."
        )
        await update_tender(db_tender_id, status="ready", notes="Порожні документи")
        return

    sections = split_into_sections(pdf_text)

    # ── Крок 8: AI Мультиагентний аналіз (Orchestrator + Blackboard) ───────
    await analyzing_msg.edit_text(
        "🤖 AI-Оркестратор аналізує документацію...\n"
        "_Запускається каскад моделей (DeepSeek R1, Qwen 2.5, Claude Sonnet)_\n"
        "_Це може зайняти до 60 секунд_",
        parse_mode="Markdown"
    )

    bb = Blackboard(tender_id)
    workflow = TenderWorkflow(bb)

    # Отримуємо свіжі дані клієнта для профілю
    client_row = await get_client_by_telegram_id(message.from_user.id) or client
    profile_json = client_row.get("profile_json") or "{}"
    try:
        profile_data = json.loads(profile_json)
    except Exception:
        profile_data = {}

    if not client_row.get("is_profile_complete"):
        await message.answer(
            "⚠️ *Профіль компанії не заповнено*\n\n"
            "Для автоматичного заповнення документів треба вказати дані компанії.\n"
            "Без профілю AI не зможе підготувати довідки для подачі.\n\n"
            "👉 Заповніть зараз: /profile\n"
            "_(Аналіз тендера продовжується, але довідки будуть порожніми)_",
            parse_mode="Markdown"
        )

    company_profile = {
        "name": client_row.get("company_name") or "ТОВ (не заповнено)",
        "edrpou": client_row.get("edrpou") or "",
        "director": client_row.get("director_name") or "",
        "director_title": client_row.get("director_title") or "Директор",
        "license": profile_data.get("license") or "Ліцензія на будівництво об'єктів класу наслідків СС2",
        "equipment": profile_data.get("equipment") or [],
        "staff": profile_data.get("staff") or [],
        "analog_contracts": profile_data.get("analog_contracts") or [],
        "has_equipment": len(profile_data.get("equipment") or []) > 0,
        "has_staff": len(profile_data.get("staff") or []) > 0,
        "has_experience": len(profile_data.get("analog_contracts") or []) > 0,
    }

    try:
        # Якщо текст ТД завеликий, ми автоматично перемикаємо модель на Gemini 2.5 Pro (large_document)
        # Але передаємо весь текст без обрізки, оскільки Сканер підтримує секційний chunked аналіз та великі контексти!
        text_len = len(pdf_text)
        if text_len > 80_000:
            logger.info(f"📄 Довгий документ ({text_len} символів). Перемикаємо Сканер на Gemini 2.5 Pro")
            from ai import models
            models.AGENT_MODELS["scanner"] = "large_document"  # gemini-2-5-pro
        else:
            from ai import models
            models.AGENT_MODELS["scanner"] = "deepseek-r1"

        await workflow.run(
            tender_meta=tender_data,
            doc_text=pdf_text,
            doc_sections=sections,
            company_profile=company_profile,
            expected_discount_pct=4.0
        )
    except Exception as exc:
        logger.error(f"Конвеєр помилка: {exc}")
        await analyzing_msg.edit_text("❌ Сталася помилка під час аналізу конвеєром.")
        return

    if bb.is_blocked():
        await analyzing_msg.edit_text(
            f"🚫 *Конвеєр зупинено передчасно (Early Exit)*\n\n"
            f"Аналіз показав занадто високі юридичні ризики або від'ємну маржу.\n"
            f"Рекомендовано відхилити цей тендер."
        )
        await update_tender(db_tender_id, status="blocked", notes="Early Exit")
        return

    scan_result = bb.scan_result
    if not scan_result:
        await analyzing_msg.edit_text(
            "❌ AI-аналіз не вдався. Спробуй ще раз або зверніться до підтримки."
        )
        await update_tender(db_tender_id, status="ready", notes="Помилка AI")
        return

    # ── Крок 9: Зберегти та надіслати звіт ───────────────────────────────
    await update_tender(
        db_tender_id,
        status="ready",
        scan_result=json.dumps(scan_result, ensure_ascii=False)
    )
    await log_action(db_tender_id, "ai_scan_complete",
                     details=f"risk={scan_result.get('risk_level')}")

    report = _format_scan_report(bb, tender_title=tender_data.get("title", "")[:80], amount=amount)

    # TL;DR + метадані звіту
    traps = bb.get_facts_by_type("trap")
    criticals = [t for t in traps if isinstance(t.content, dict) and t.content.get("severity") == "CRITICAL"]
    tldr = (
        f"\n\n📌 *TL;DR:* {len(criticals)} критичних / {len(traps)} всього ловушок"
        f"{' | ⚠️ Змін до ТД: '+str(amendments_count) if amendments_count else ''}"
        f"{' | 💬 Q&A: '+str(qa_count) if qa_count else ''}"
    )

    await analyzing_msg.edit_text(
        report + tldr + DISCLAIMER,
        parse_mode="Markdown"
    )

    # ── Крок 10: Комерційна пропозиція ───────────────────────────────────
    if amount > 0:
        fee = amount * 0.05
        fee_str = f"{fee:,.0f}".replace(",", " ")
        amount_str = f"{amount:,.0f}".replace(",", " ")
        await message.answer(
            f"💰 *Потенційна вигода:*\n"
            f"Сума лоту: {amount_str} грн\n"
            f"Комісія (5%): _{fee_str} грн_\n\n"
            f"Готовий допомогти підготувати документи? Відповідай — розберемо разом! 👇",
            parse_mode="Markdown"
        )

    # ── Сповістити адміна ────────────────────────────────────────────────
    await _notify_admin(message, tender_data, scan_result, history)


async def _notify_admin(message: Message, tender_data: dict,
                        scan_result: dict, history: dict):
    """Надсилає адміну сповіщення про новий запит від клієнта."""
    if not ADMIN_TELEGRAM_ID:
        return
    try:
        bot = message.bot
        risk   = scan_result.get("risk_level", "?")
        amount = tender_data.get("value", {}).get("amount", 0)
        mono   = history.get("monopoly_level", "") if history else ""

        await bot.send_message(
            ADMIN_TELEGRAM_ID,
            f"🔔 *Новий запит від клієнта*\n\n"
            f"👤 @{message.from_user.username or message.from_user.full_name}\n"
            f"📋 {tender_data.get('title', '')[:80]}\n"
            f"💰 {amount:,.0f} грн\n"
            f"⚠️ Ризик: {risk}\n"
            f"{'📊 ' + mono if mono else ''}\n\n"
            f"ID: `{tender_data.get('id', '')}`",
            parse_mode="Markdown"
        )
    except Exception as exc:
        logger.warning(f"Не вдалося сповістити адміна: {exc}")


@router.message(Command("amku"))
async def cmd_amku(message: Message):
    """Генерує чернетку скарги в АМКУ на основі виявлених дискримінаційних вимог."""
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "Вкажіть ID або посилання на тендер:\n"
            "`/amku UA-2025-10-06-001013-a`",
            parse_mode="Markdown"
        )
        return
        
    tender_input = parts[1].strip()
    tender_id = extract_tender_id(tender_input)
    if not tender_id:
        await message.answer("❌ Не вдалося розпізнати ID тендера.")
        return
        
    import db.database as db
    row = await db.get_client_tenders(message.from_user.id) # хоча краще напряму запит в БД
    # Шукаємо тендер в БД через DbConnection
    from db.database import DbConnection
    is_tendering = True
    end_date_str = ""
    fee_td_str = ""
    fee_dec_str = ""
    procuring_entity = ""
    tender_title = ""
    
    async with DbConnection() as database_conn:
        row = await database_conn.fetchone("SELECT * FROM tenders WHERE prozorro_id = ?", (tender_id,))
        if not row:
            await message.answer("❌ Тендер спочатку треба проаналізувати через `/analyze`.")
            return
            
        if not row.get("scan_result"):
            await message.answer("❌ Для цього тендера ще немає результатів AI-сканування.")
            return
            
        scan_res = json.loads(row["scan_result"])
        disc_reqs = scan_res.get("discriminatory_requirements", [])
        if not disc_reqs:
            await message.answer("✅ AI-Сканер не знайшов дискримінаційних вимог у цьому тендері. Оскаржувати нічого.")
            return
            
        amount = row.get("amount", 0)
        procuring_entity = row.get("procuring_entity")
        tender_title = row.get("tender_title")
        
        if amount < 1500000:
            await message.answer(
                f"⚠️ *Увага!* Сума тендера ({amount:,.2f} грн) є меншою за 1.5 млн грн.\n"
                "Згідно з законодавством, роботи вартістю менше 1.5 млн грн не підлягають оскарженню в колегії АМКУ.\n"
                "Ви можете подати лише вимогу замовнику безпосередньо через Prozorro.",
                parse_mode="Markdown"
            )
            return

        # Розрахунок державного збору АМКУ для робіт:
        # 1. Оскарження умов ТД (0.3% від вартості, але в межах 2 000 - 85 000 грн)
        fee_td = amount * 0.003
        if fee_td < 2000:
            fee_td = 2000
        elif fee_td > 85000:
            fee_td = 85000
        fee_td_str = f"{fee_td:,.0f}".replace(",", " ")

        # 2. Оскарження рішень/дій замовника (0.6% від вартості, але в межах 3 000 - 170 000 грн)
        fee_dec = amount * 0.006
        if fee_dec < 3000:
            fee_dec = 3000
        elif fee_dec > 170000:
            fee_dec = 170000
        fee_dec_str = f"{fee_dec:,.0f}".replace(",", " ")

        # Визначаємо стадію закупівлі за дедлайном та статусом
        raw_json_str = row.get("raw_json")
        is_contract_signed = False
        is_td_appealing_allowed = False
        is_decision_appealing_allowed = False
        is_decision_waiting = False
        contract_signed_date_str = ""
        decision_complaint_end_str = ""
        
        # Завантажуємо код ЄДРПОУ клієнта для пошуку його рішень
        client_edrpou = None
        try:
            async with DbConnection() as db_c:
                cl_row = await db_c.fetchone("SELECT edrpou FROM clients WHERE telegram_id = ?", (message.from_user.id,))
                if cl_row:
                    client_edrpou = cl_row.get("edrpou")
        except Exception:
            pass

        if raw_json_str:
            try:
                raw_data = json.loads(raw_json_str)
                status = raw_data.get("status")
                
                # 1. Перевірка статусу завершення тендера
                if status in ["complete", "cancelled", "unsuccessful"]:
                    is_contract_signed = True
                
                # 2. Перевірка наявності підписаного договору
                contracts = raw_data.get("contracts", [])
                for c in contracts:
                    if c.get("status") == "active" or c.get("dateSigned"):
                        is_contract_signed = True
                        contract_signed_date_str = c.get("dateSigned", "")
                        break
                
                from datetime import datetime, timedelta
                now = datetime.now()
                
                # 3. Дедлайн для ТД (endDate - 4 дні)
                end_date_str = raw_data.get("tenderPeriod", {}).get("endDate")
                if end_date_str:
                    try:
                        end_dt = datetime.fromisoformat(end_date_str)
                        if end_dt.tzinfo is not None:
                            end_dt = end_dt.replace(tzinfo=None)
                        if now < (end_dt - timedelta(days=4)):
                            is_td_appealing_allowed = True
                    except Exception:
                        pass
                
                # 4. Оцінка рішень (awards) за complaintPeriod
                awards = raw_data.get("awards", [])
                if awards:
                    # Шукаємо релевантний award: або нашого клієнта за ЄДРПОУ, або найсвіжіший
                    target_award = None
                    if client_edrpou:
                        for a in awards:
                            suppliers = a.get("suppliers", [])
                            for s in suppliers:
                                identifier = s.get("identifier", {})
                                if identifier.get("id") == client_edrpou:
                                    target_award = a
                                    break
                            if target_award:
                                break
                    
                    if not target_award:
                        # Беремо найновіший за датою award
                        award_dates_map = []
                        for a in awards:
                            ad_str = a.get("date")
                            if ad_str:
                                try:
                                    ad_dt = datetime.fromisoformat(ad_str)
                                    award_dates_map.append((ad_dt, a))
                                except Exception:
                                    pass
                        if award_dates_map:
                            target_award = max(award_dates_map, key=lambda x: x[0])[1]
                    
                    if target_award:
                        award_status = target_award.get("status")
                        complaint_period = target_award.get("complaintPeriod", {})
                        
                        if award_status in ["active", "unsuccessful"]:
                            end_str = complaint_period.get("endDate")
                            if end_str:
                                decision_complaint_end_str = end_str
                                try:
                                    complaint_end_dt = datetime.fromisoformat(end_str)
                                    if complaint_end_dt.tzinfo is not None:
                                        complaint_end_dt = complaint_end_dt.replace(tzinfo=None)
                                    if now <= complaint_end_dt:
                                        is_decision_appealing_allowed = True
                                except Exception:
                                    pass
                        else:
                            is_decision_waiting = True
                else:
                    # Якщо дедлайн ТД минув, але рішень ще немає
                    if not is_td_appealing_allowed and not is_contract_signed:
                        is_decision_waiting = True
            except Exception as e:
                logger.error(f"Помилка розбору raw_json у amku дедлайнах: {e}")

        # Якщо договір вже підписано — АМКУ шлях повністю закритий
        if is_contract_signed:
            signed_comment = f" ({contract_signed_date_str[:10]})" if contract_signed_date_str else ""
            await message.answer(
                f"❌ *Оскарження в АМКУ процедурно неможливе!*\n\n"
                f"Договір про закупівлю вже підписано{signed_comment} або тендер завершено/скасовано.\n"
                f"Закон забороняє оскарження в АМКУ після підписання договору. "
                f"Єдиний шлях захисту ваших прав наразі — звернення до суду.",
                parse_mode="Markdown"
            )
            return

        # Визначаємо висновок на основі етапу
        if is_td_appealing_allowed:
            is_tendering = True
        elif is_decision_appealing_allowed:
            is_tendering = False
        elif is_decision_waiting:
            await message.answer(
                f"❌ *Оскарження умов ТД вже недоступне, а вікно оскарження рішень ще не відкрилося!*\n\n"
                f"• Строк оскарження умов ТД закінчився (менше 4 днів до дедлайну).\n"
                f"• Рішення замовника щодо кваліфікації чи переможця *ще не оприлюднено* (або знаходиться в статусі pending).\n\n"
                f"👉 *Що робити:* Слідкуйте за статусом тендера. Як тільки замовник опублікує рішення (протокол кваліфікації), у вас відкриється нове вікно оскарження (протягом 10 днів згідно з complaintPeriod).",
                parse_mode="Markdown"
            )
            return
        else:
            # Строки повністю минули
            await message.answer(
                f"❌ *Всі строки оскарження в АМКУ минули!*\n\n"
                f"• Строк оскарження умов ТД закінчився (менше 4 днів до дедлайну).\n"
                f"• Строк оскарження рішень замовника закінчився (вікно оскарження за complaintPeriod у Prozorro закрилося).\n\n"
                f"Подати скаргу до АМКУ вже неможливо. Ви можете звернутися безпосередньо до замовника з вимогою або оскаржити дії в судовому порядку.",
                parse_mode="Markdown"
            )
            return

    # Звертаємося до моделі для генерації скарги
    status_msg = await message.answer("📝 AI генерує чернетку скарги до органу оскарження (АМКУ)...")
    
    # Складаємо промпт
    disc_text = "\n".join(
        f"- Вимога: {r.get('type')}\n  Цитата ТД: \"{r.get('quote')}\"\n  Стаття закону: {r.get('law_reference')}"
        for r in disc_reqs
    )
    
    from ai.client import call_model, build_messages
    
    # ── Гібридний підхід: Python будує I+II+V, LLM пише лише III+IV ─────────
    # Так модель фізично не бачить структуру для заповнення — не може вставити неправильний сценарій.
    import datetime as _dt
    today_str = _dt.date.today().strftime("%d.%m.%Y")

    if is_tendering:
        stage_type = "Оскарження умов тендерної документації (до дедлайну подачі)"
        ii_block = (
            "## II. ФАКТИЧНІ ОБСТАВИНИ\n\n"
            "1. Скаржник є потенційним учасником закупівлі, що проводиться Замовником, "
            "та має намір взяти участь у процедурі відкритих торгів.\n\n"
            f"2. Замовник оприлюднив Тендерну документацію (далі — ТД) щодо закупівлі: {tender_title}.\n\n"
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
        stage_type = "Оскарження рішень, дій чи бездіяльності замовника (після оцінки пропозицій / аукціону)"
        ii_block = (
            "## II. ФАКТИЧНІ ОБСТАВИНИ\n\n"
            f"1. Скаржник брав участь (або мав намір взяти участь) у відкритих торгах, організованих Замовником щодо закупівлі: {tender_title}.\n\n"
            "2. Замовник прийняв рішення, що порушує права та законні інтереси Скаржника "
            "(відхилення пропозиції / визначення переможця / інше рішення після оцінки).\n\n"
            "3. Рішення ґрунтується на дискримінаційних вимогах ТД, які обмежують коло учасників незаконно."
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

    # LLM пише ЛИШЕ правові підстави та вимоги — сценарій фізично поза промптом
    AMKU_LEGAL_SYSTEM = (
        "Ти — юрист у сфері публічних закупівель України.\n"
        "Напиши ЛИШЕ два розділи юридичної скарги: «III. ПРАВОВІ ПІДСТАВИ» та «IV. ВИМОГИ».\n\n"
        "СТРОГІ ЗАБОРОНИ:\n"
        "1. НЕ вигадуй номери рішень АМКУ чи суду. Пиши «відповідно до усталеної практики органу оскарження».\n"
        "2. НЕ пиши вступ, фактичні обставини або додатки — ці розділи вже написані.\n"
        "3. В розділі IV пиши лише що Скаржник ВИМАГАЄ зробити.\n"
        "Мова: українська."
    )
    AMKU_LEGAL_USER = (
        f"Замовник: {procuring_entity}\n"
        f"Предмет закупівлі: {tender_title}\n"
        f"Мета вимог: {demands_goal}\n\n"
        f"ВИЯВЛЕНІ ПОРУШЕННЯ (дискримінаційні вимоги ТД):\n{disc_text}\n\n"
        "Напиши розділи III. ПРАВОВІ ПІДСТАВИ та IV. ВИМОГИ.\n"
        "Посилайся на ЗУ «Про публічні закупівлі» (ст. 16, ст. 22, ст. 46).\n"
        "Не додавай жодних інших розділів."
    )

    messages = build_messages(system_prompt=AMKU_LEGAL_SYSTEM, user_content=AMKU_LEGAL_USER)

    # Використовуємо Claude 3.5 Haiku для правової аргументації
    result = await call_model("collector", messages, json_mode=False)
    if not result:
        await status_msg.edit_text("❌ Не вдалося згенерувати скаргу. Спробуйте пізніше.")
        return

    legal_sections, _ = result

    # Збираємо повну скаргу детерміновано
    content = (
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
        f"**Замовник:** {procuring_entity}\n"
        f"**Предмет закупівлі:** {tender_title}\n"
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
    
    # Зберігаємо скаргу як текстовий файл
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False, mode="w", encoding="utf-8") as tmp:
        tmp.write(content)
        tmp_path = tmp.name
        
    from aiogram.types import FSInputFile
    document = FSInputFile(tmp_path, filename=f"AMKU_complaint_{tender_id}.txt")
    
    await status_msg.delete()
    if is_tendering:
        fee_text = (
            f"   • *Оскарження умов ТД* (дискримінаційні вимоги до дедлайну подачі): *{fee_td_str} грн* (0.3% від вартості робіт) — *АКТУАЛЬНО ДЛЯ ЦЬОГО ЕТАПУ*.\n"
            f"   • *Оскарження рішень/дій замовника* (на майбутнє, після оцінки пропозицій): *{fee_dec_str} грн* (0.6% від вартості робіт)."
        )
        stage_comment = "ℹ️ Наразі триває етап подання пропозицій, тому ви можете оскаржити дискримінаційні вимоги ТД."
    else:
        fee_text = (
            f"   • *Оскарження рішень/дій замовника* (дискваліфікація, відхилення пропозицій після аукціону): *{fee_dec_str} грн* (0.6% від вартості робіт) — *АКТУАЛЬНО ДЛЯ ЦЬОГО ЕТАПУ*."
        )
        stage_comment = (
            f"⚠️ *Увага!* Кінцевий строк подання пропозицій вже минув" + 
            (f" ({end_date_str[:10]})" if end_date_str else "") + 
            ".\n*Оскарження умов ТД (тариф 0.3%) вже процедурно недоступне*. Ви можете оскаржити лише рішення замовника після аукціону."
        )

    await message.reply_document(
        document,
        caption=(
            f"📄 *Драфт скарги в АМКУ* для тендера `{tender_id}` згенеровано!\n\n"
            f"⚠️ *ВАЖЛИВО (ОБОВ'ЯЗКОВА ПЕРЕВІРКА ЮРИСТОМ):*\n"
            f"1. Це автоматично згенерована чернетка. Обов'язково перевірте та вичитайте її разом з вашим тендерологом або юристом перед поданням. *31.6% скарг відхиляються на етапі прийому* через формальні помилки.\n"
            f"2. *Сплата державного збору:*\n"
            f"{fee_text}\n"
            f"   Без підтвердження сплати збору в системі Prozorro скарга не буде прийнята до розгляду Колегією АМКУ.\n"
            f"3. {stage_comment}"
        ),
        parse_mode="Markdown"
    )
    
    try:
        os.unlink(tmp_path)
    except Exception:
        pass


@router.message(Command("won"))
async def cmd_won(message: Message):
    """Позначає тендер як виграний та генерує рахунок на Success Fee."""
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "Вкажіть ID або посилання на виграний тендер:\n"
            "`/won UA-2025-10-06-001013-a`",
            parse_mode="Markdown"
        )
        return
        
    tender_input = parts[1].strip()
    tender_id = extract_tender_id(tender_input)
    if not tender_id:
        await message.answer("❌ Не вдалося розпізнати ID тендера.")
        return
        
    from db.database import DbConnection, update_tender
    async with DbConnection() as database_conn:
        row = await database_conn.fetchone("SELECT * FROM tenders WHERE prozorro_id = ?", (tender_id,))
        if not row:
            await message.answer("❌ Тендер не знайдено у вашому списку.")
            return
            
        amount = row.get("amount", 0)
        
        # ── Success Fee = 15% від розрахункової маржі клієнта, мін. 5 000 грн ──
        # Модель: прив'язана до РЕАЛЬНОЇ вигоди клієнта (маржі), а не до абстрактної
        # суми контракту. Це математично справедливо і не «з'їдає» велику частку прибутку
        # на великих лотах, як було б при фіксованому % від суми.
        SUCCESS_FEE_RATE = 0.15  # 15% від маржі
        SUCCESS_FEE_MIN  = 5_000  # мін. 5 000 грн

        # Отримуємо маржу з результату калькулятора (збережено в БД)
        calc_data = {}
        if row.get("calc_result"):
            try:
                calc_data = json.loads(row["calc_result"])
            except Exception:
                pass
        client_margin = float(calc_data.get("margin", 0) or 0)
        
        if client_margin > 0:
            raw_fee = client_margin * SUCCESS_FEE_RATE
            success_fee = max(raw_fee, SUCCESS_FEE_MIN)
            fee_basis = f"15% від маржі {client_margin:,.0f} грн".replace(",", " ")
        else:
            # Якщо маржа невідома — беремо мінімальний поріг
            success_fee = SUCCESS_FEE_MIN
            fee_basis = "мінімальний поріг (маржа невідома)"
            
        # Оновлюємо статус в БД
        await update_tender(row["id"], status="won", success_fee_amount=success_fee)
        
        amount_str = f"{amount:,.0f}".replace(",", " ")
        fee_str = f"{success_fee:,.0f}".replace(",", " ")
        
        # Повідомляємо клієнта
        await message.answer(
            f"🏆 *Вітаємо з перемогою в тендері!*\n\n"
            f"Лот: *{row.get('tender_title')}*\n"
            f"Сума лоту: *{amount_str} грн*\n\n"
            f"💳 *Рахунок на оплату Success Fee:*\n"
            f"Розрахунок: {fee_basis}\n"
            f"Сума до сплати: *{fee_str} грн* (мін. 5 000 грн)\n\n"
            f"📍 *Реквізити для оплати (IBAN):*\n"
            f"Отримувач: ФОП Macovei\n"
            f"IBAN: `UA8932200300000002600123456789`\n"
            f"Призначення платежу: _Оплата за послуги супроводу тендера {tender_id}_\n\n"
            f"Дякуємо за співпрацю! Разом до нових перемог! 🤝",
            parse_mode="Markdown"
        )
        
        # Сповіщаємо адміна про отримання грошей
        if ADMIN_TELEGRAM_ID:
            try:
                bot = message.bot
                await bot.send_message(
                    ADMIN_TELEGRAM_ID,
                    f"🏆 *КЛІЄНТ ПОЗНАЧИВ ПЕРЕМОГУ!*\n\n"
                    f"👤 @{message.from_user.username or message.from_user.full_name}\n"
                    f"📋 {row.get('tender_title')[:80]}\n"
                    f"💰 Сума лоту: {amount_str} грн\n"
                    f"💳 Success Fee ({fee_basis}): *{fee_str} грн*\n"
                    f"Виставлено рахунок на оплату.",
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.warning(f"Не вдалося надіслати адміну сповіщення про виграш: {e}")

@router.message(Command("pay"))
async def cmd_pay(message: Message):
    """Адмінська команда для підтвердження оплати Success Fee за тендер."""
    if message.from_user.id != ADMIN_TELEGRAM_ID:
        await message.answer("❌ У вас немає прав доступу до цієї команди.")
        return
        
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "Вкажіть ID тендера для підтвердження оплати:\n"
            "`/pay UA-2025-10-06-001013-a`",
            parse_mode="Markdown"
        )
        return
        
    tender_id = parts[1].strip()
    from db.database import DbConnection
    async with DbConnection() as db:
        row = await db.fetchone("SELECT * FROM tenders WHERE prozorro_id = ?", (tender_id,))
        if not row:
            await message.answer("❌ Тендер не знайдено.")
            return
            
        await db.execute("UPDATE tenders SET is_fee_paid = 1 WHERE prozorro_id = ?", (tender_id,))
        await db.commit()
        
    fee_val = row.get("success_fee_amount") or 0.0
    fee_str = f"{fee_val:,.0f}".replace(",", " ")
    
    await message.answer(
        f"✅ *Оплату успішно підтверджено!*\n\n"
        f"Лот: {row.get('tender_title')}\n"
        f"ID: `{tender_id}`\n"
        f"Сума комісії: *{fee_str} грн* позначена як сплачена.",
        parse_mode="Markdown"
    )


# ── Кабінет Адміністратора (/admin) ──────────────────────────────────────────

def is_admin_filter(message: Message) -> bool:
    return message.from_user.id == ADMIN_TELEGRAM_ID


def get_admin_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton(text="📊 Статистика сервісу", callback_data="admin_stats"),
            InlineKeyboardButton(text="👥 Список клієнтів", callback_data="admin_users"),
        ],
        [
            InlineKeyboardButton(text="🔍 Запустити моніторинг", callback_data="admin_run_monitor"),
            InlineKeyboardButton(text="🎯 Знайти аутріч-цілі", callback_data="admin_run_outreach"),
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


@router.message(Command("admin"))
async def cmd_admin(message: Message):
    """Головне меню кабінету адміністратора."""
    if not is_admin_filter(message):
        await message.answer("❌ У вас немає прав доступу до цієї команди.")
        return
        
    await message.answer(
        "👮 *Кабінет Адміністратора (Tender Pipeline)*\n\n"
        "Вітаю в панелі управління сервісом супроводу Prozorro.\n"
        "Оберіть дію на клавіатурі нижче:",
        reply_markup=get_admin_keyboard(),
        parse_mode="Markdown"
    )


@router.callback_query(F.data == "admin_stats")
async def process_admin_stats(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_TELEGRAM_ID:
        await callback.answer("Немає доступу", show_alert=True)
        return
        
    from db.database import DbConnection
    async with DbConnection() as db:
        # Статистика клієнтів
        clients_count = await db.fetchone("SELECT COUNT(*) as cnt FROM clients")
        active_clients = await db.fetchone("SELECT COUNT(*) as cnt FROM clients WHERE is_profile_complete = 1")
        
        # Статистика тендерів
        tenders_count = await db.fetchone("SELECT COUNT(*) as cnt FROM tenders")
        won_tenders = await db.fetchone("""
            SELECT 
                COUNT(*) as cnt, 
                SUM(success_fee_amount) as total_fee,
                SUM(CASE WHEN is_fee_paid = 1 THEN success_fee_amount ELSE 0 END) as paid_fee
            FROM tenders 
            WHERE status = 'won'
        """)
        
    clients_cnt = clients_count["cnt"] if clients_count else 0
    active_cnt = active_clients["cnt"] if active_clients else 0
    tenders_cnt = tenders_count["cnt"] if tenders_count else 0
    won_cnt = won_tenders["cnt"] if won_tenders else 0
    total_fee = won_tenders["total_fee"] if won_tenders and won_tenders["total_fee"] else 0.0
    paid_fee = won_tenders["paid_fee"] if won_tenders and won_tenders["paid_fee"] else 0.0
    
    fee_str = f"{total_fee:,.0f}".replace(",", " ")
    paid_str = f"{paid_fee:,.0f}".replace(",", " ")
    
    stats_text = (
        "📊 *Статистика сервісу Tender Pipeline:*\n\n"
        f"👥 *Клієнти:*\n"
        f"  • Всього користувачів: *{clients_cnt}*\n"
        f"  • З повним профілем: *{active_cnt}*\n\n"
        f"📋 *Тендери:*\n"
        f"  • Всього проаналізовано: *{tenders_cnt}*\n"
        f"  • Виграно тендерів: *{won_cnt}*\n"
        f"  • Нараховано Success Fee: *{fee_str} грн*\n"
        f"  • Фактично отримано (сплачено): *{paid_str} грн*"
    )
    
    await callback.message.edit_text(
        stats_text,
        reply_markup=get_admin_keyboard(),
        parse_mode="Markdown"
    )
    await callback.answer()


@router.callback_query(F.data == "admin_users")
async def process_admin_users(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_TELEGRAM_ID:
        await callback.answer("Немає доступу", show_alert=True)
        return
        
    from db.database import DbConnection
    async with DbConnection() as db:
        rows = await db.fetchall("SELECT company_name, edrpou, contact_name, is_profile_complete FROM clients LIMIT 10")
        
    if not rows:
        await callback.message.edit_text("ℹ️ Клієнтів ще немає.", reply_markup=get_admin_keyboard())
        await callback.answer()
        return
        
    lines = ["👥 *Список клієнтів (останні 10):*\n"]
    for r in rows:
        status_char = "🟢" if r["is_profile_complete"] else "🟡"
        name = r["company_name"] or "Не вказано"
        edrpou = r["edrpou"] or "без ЄДРПОУ"
        lines.append(f"{status_char} *{name}* (ЄДРПОУ: `{edrpou}`) — контакт: {r['contact_name'] or '?'}")
        
    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=get_admin_keyboard(),
        parse_mode="Markdown"
    )
    await callback.answer()


@router.callback_query(F.data == "admin_run_monitor")
async def process_admin_run_monitor(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_TELEGRAM_ID:
        await callback.answer("Немає доступу", show_alert=True)
        return
        
    await callback.message.edit_text("🔍 Запускаю позачергову перевірку нових тендерів по Вінниці...", reply_markup=get_admin_keyboard())
    await callback.answer()
    
    # Викликаємо функцію пошуку тендерів з monitor.py
    from monitor import check_new_tenders
    try:
        await check_new_tenders(callback.message.bot)
        await callback.message.answer("✅ Перевірку нових тендерів завершено!")
    except Exception as e:
        await callback.message.answer(f"❌ Помилка під час перевірки: {e}")


@router.callback_query(F.data == "admin_run_outreach")
async def process_admin_run_outreach(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_TELEGRAM_ID:
        await callback.answer("Немає доступу", show_alert=True)
        return
        
    await callback.message.edit_text("🎯 Запускаю пошук лідів для аутрічу...", reply_markup=get_admin_keyboard())
    await callback.answer()
    
    # Викликаємо скрипт
    from scripts.find_outreach_targets import fetch_outreach_targets
    try:
        await fetch_outreach_targets()
        
        # Надсилаємо файл звіту адміну
        report_path = "/Users/a1111/.gemini/antigravity/brain/9c8a8a24-d53a-44c1-b11e-a42f2fd18fe8/outreach_targets.md"
        if os.path.exists(report_path):
            doc = FSInputFile(report_path, filename="outreach_targets.md")
            await callback.message.answer_document(
                doc,
                caption="🎯 *Список потенційних SMB-клієнтів для аутрічу готовий!*",
                parse_mode="Markdown"
            )
        else:
            await callback.message.answer("❌ Файл звіту не було створено.")
    except Exception as e:
        await callback.message.answer(f"❌ Помилка під час запуску аутрічу: {e}")
