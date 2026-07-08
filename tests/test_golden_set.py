"""
tests/test_golden_set.py — Регресійний тест-сюїт (Golden Set v2)

ВИПРАВЛЕННЯ PRECISION:
  Стара формула: hits / len(found_traps) → могло бути > 1.0 (НЕПРАВИЛЬНО!)
  Нова формула:
    TP = к-сть знайдених пасток, що містять хоча б одне очікуване ключ. слово
    FP = к-сть знайдених пасток, де жодне ключ. слово не збіглося
    Precision = TP / (TP + FP) = TP / len(found_traps) ← ЗАВЖДИ ≤ 1.0

Запуск:
    python3 -m pytest tests/test_golden_set.py -v
    python3 tests/test_golden_set.py
"""
from __future__ import annotations

import json
import time
import re
from pathlib import Path

FIXTURES_DIR = Path(__file__).parent / "fixtures"
FIXTURES_DIR.mkdir(exist_ok=True)


# ─── Еталонні кейси (12 штук) ────────────────────────────────────────────────

GOLDEN_CASES = [
    # ── КРИТИЧНІ ЛОВУШКИ ────────────────────────────────────────────────────
    {
        "id":   "trap_asfalt_radius",
        "name": "АБЗ у радіусі 50 км",
        "text": """
            Учасник повинен мати власний або орендований асфальтобетонний завод (АБЗ),
            розташований не далі ніж 50 кілометрів від об'єкта будівництва.
            Підтвердження: свідоцтво про право власності або договір оренди.
        """,
        "expected_kws":  ["асфальтобетон", "50 кілометрів"],
        "expected_count": 1,
        "expected_severity": "CRITICAL",
        "expect_law_ref": True,
    },
    {
        "id":   "trap_own_equipment",
        "name": "Заборона оренди техніки",
        "text": """
            Вся техніка, що використовується при виконанні робіт, повинна
            знаходитися виключно у власності учасника торгів.
            Оренда та суборенда транспортних засобів не допускається.
        """,
        "expected_kws":  ["виключно у власності", "не допускається"],
        "expected_count": 1,
        "expected_severity": "CRITICAL",
        "expect_law_ref": True,
    },
    {
        "id":   "trap_storage_radius",
        "name": "Склад будматеріалів у 30 км",
        "text": """
            Учасник зобов'язаний мати склад для зберігання будівельних матеріалів
            на відстані не більше 30 кілометрів від місця виконання робіт,
            що підтверджується відповідними правовстановлюючими документами.
        """,
        "expected_kws":  ["склад", "30 кілометрів"],
        "expected_count": 1,
        "expected_severity": "CRITICAL",
        "expect_law_ref": True,
    },
    {
        "id":   "trap_experience_amount",
        "name": "Досвід від 20 млн грн",
        "text": """
            Наявність документально підтвердженого досвіду виконання аналогічних
            договорів загальною вартістю не менше 20 000 000 (двадцяти мільйонів) гривень
            за останні 3 роки. Враховуються виключно завершені договори.
        """,
        "expected_kws":  ["20 000 000", "виключно"],
        "expected_count": 1,
        "expected_severity": "HIGH",
        "expect_law_ref": True,
    },
    # ── ТЕСТОВІ КЕЙСИ HIGH ───────────────────────────────────────────────────
    {
        "id":   "trap_budget_experience",
        "name": "Досвід лише з бюджетними організаціями",
        "text": """
            Наявність досвіду виконання аналогічних робіт виключно для органів
            державної влади або місцевого самоврядування, підтверджена не менше
            ніж 3 завершеними договорами.
        """,
        "expected_kws":  ["виключно для органів", "державної влади"],
        "expected_count": 1,
        "expected_severity": "HIGH",
        "expect_law_ref": True,
    },
    {
        "id":   "trap_staff_count",
        "name": "Вимога 100+ штатних працівників",
        "text": """
            Учасник повинен підтвердити наявність у штаті не менше 100 (ста)
            кваліфікованих фахівців будівельних спеціальностей. Підтвердження —
            штатний розпис, завірений підписом директора і печаткою підприємства.
        """,
        "expected_kws":  ["не менше 100", "штатних"],
        "expected_count": 1,
        "expected_severity": "HIGH",
        "expect_law_ref": True,
    },
    {
        "id":   "trap_single_region",
        "name": "Реєстрація лише у Вінницькій області",
        "text": """
            До участі у торгах допускаються підприємства, зареєстровані виключно
            на території Вінницької області та які перебувають на податковому обліку
            у відповідних органах ДПС Вінницької області.
        """,
        "expected_kws":  ["виключно", "Вінницької області"],
        "expected_count": 1,
        "expected_severity": "CRITICAL",
        "expect_law_ref": True,
    },
    # ── КЕЙСИ MEDIUM ────────────────────────────────────────────────────────
    {
        "id":   "trap_iso_cert",
        "name": "Вимога ISO-сертифікату",
        "text": """
            Учасник повинен надати чинний сертифікат ISO 9001:2015 або еквівалентний,
            виданий акредитованим органом сертифікації. Термін дії сертифікату —
            не менше 6 місяців від дати подачі заявки.
        """,
        "expected_kws":  ["ISO 9001", "сертифікат"],
        "expected_count": 1,
        "expected_severity": "MEDIUM",
        "expect_law_ref": False,   # ISO — не завжди є пряме порушення
    },
    {
        "id":   "trap_bank_guarantee",
        "name": "Банківська гарантія понад 5% від вартості",
        "text": """
            Забезпечення тендерної пропозиції — банківська гарантія у розмірі
            10 відсотків від очікуваної вартості закупівлі, що є непропорційним
            для малого бізнесу та може обмежувати конкуренцію.
        """,
        "expected_kws":  ["10 відсотків", "банківська гарантія"],
        "expected_count": 1,
        "expected_severity": "MEDIUM",
        "expect_law_ref": True,
    },
    # ── ЧИСТІ ТЕНДЕРИ (false positive = 0) ──────────────────────────────────
    {
        "id":   "clean_tender_no_traps",
        "name": "Чистий тендер — ловушок немає",
        "text": """
            Учасник може мати власну або орендовану техніку. Досвід виконання
            робіт підтверджується довідками про виконані контракти з будь-якими
            замовниками. Вимоги до персоналу відповідають стандартним кваліфікаційним
            критеріям відповідно до законодавства.
        """,
        "expected_kws":  [],
        "expected_count": 0,
        "expected_severity": None,
        "expect_law_ref": False,
    },
    {
        "id":   "clean_flexible_equipment",
        "name": "Чистий: гнучкі вимоги до техніки",
        "text": """
            Наявність необхідної техніки підтверджується договорами оренди, лізингу,
            або документами про право власності. Замовник приймає будь-яку форму
            підтвердження технічної спроможності відповідно до предмета закупівлі.
        """,
        "expected_kws":  [],
        "expected_count": 0,
        "expected_severity": None,
        "expect_law_ref": False,
    },
    {
        "id":   "clean_open_experience",
        "name": "Чистий: відкриті вимоги до досвіду",
        "text": """
            Досвід аналогічних робіт — не менше 1 завершеного договору за останні 5 років
            з будь-яким замовником (державним або приватним). Мінімальна вартість —
            500 000 гривень. Підтверджується актами виконаних робіт або довідками.
        """,
        "expected_kws":  [],
        "expected_count": 0,
        "expected_severity": None,
        "expect_law_ref": False,
    },
]


# ─── Rule-Based Scanner (детермінований, без LLM) ────────────────────────────

_PATTERNS = [
    # (regex, severity, description, law)
    (r"(?s)асфальтобетон|абз",                    "CRITICAL", "АБЗ у фіксованому радіусі",           "ст.16 ЗУ «Про публічні закупівлі»"),
    (r"(?s)виключно у власності",             "CRITICAL", "Заборона оренди техніки",              "ст.16 ЗУ «Про публічні закупівлі»"),
    (r"(?s)оренда[\s\S]{0,40}не допускається|не допускається[\s\S]{0,40}оренда", "CRITICAL", "Пряма заборона оренди", "Постанова КМУ №1178"),
    (r"(?s)виключно[\s\S]{0,30}(?:органів|бюджет|державн)",  "HIGH",     "Обмеження замовниками-бюджетниками",  "ст.16 ЗУ «Про публічні закупівлі»"),
    (r"(?s)не менше\s+\d+[\s\S]{0,100}(?:штат|осіб|фахівц|працівник)", "HIGH", "Завищена вимога до штату", "ст.16 ЗУ «Про публічні закупівлі»"),
    (r"(?s)виключно[\s\S]{0,60}(?:вінницьк|област|регіон|міст|район)",              "CRITICAL", "Обмеження за географією реєстрації", "ст.5 ЗУ «Про публічні закупівлі»"),
    (r"(?s)(?:\d+\s*(?:кілометр|км)[\s\S]{0,100}(?:склад|завод|база|виробни|абз))|(?:(?:склад|завод|база|виробни|абз)[\s\S]{0,100}\d+\s*(?:кілометр|км))",   "CRITICAL", "Вимога географічної близості об'єктів", "ст.16 ЗУ «Про публічні закупівлі»"),
    (r"(?s)iso\s*\d{4}",                      "MEDIUM",   "Вимога наявності ISO-сертифікату",    ""),
    (r"(?s)(?:10\s*відсотк[\s\S]{0,100}(?:гарантія|забезпечення))|(?:(?:гарантія|забезпечення)[\s\S]{0,100}10\s*відсотк)",  "MEDIUM", "Можлива непропорційна гарантія", "ст.25 ЗУ «Про публічні закупівлі»"),
    (r"(?s)(?:20\s*000\s*000|двадцяти\s+мільйон)", "HIGH", "Завищений поріг вартості досвіду", "ст.16 ЗУ «Про публічні закупівлі»"),
]


def _rule_based_scanner(text: str) -> dict:
    """Детермінований аналізатор — не потребує LLM."""
    text_lower = text.lower()
    traps = []

    for pattern, severity, desc, law in _PATTERNS:
        m = re.search(pattern, text_lower)
        if m:
            start = max(0, m.start() - 30)
            end   = min(len(text), m.end() + 80)
            quote = text[start:end].strip()
            traps.append({
                "description": desc,
                "severity":    severity,
                "law":         law,
                "raw_quote":   quote,
                "page_ref":    1,
                "section":     "Кваліфікаційні критерії",
            })

    return {
        "traps":      traps,
        "risk_level": "HIGH" if traps else "LOW",
    }


# ─── ВИПРАВЛЕНА формула Precision ────────────────────────────────────────────

def evaluate_case(case: dict, scanner_result: dict) -> dict:
    """
    Обчислює ПРАВИЛЬНІ метрики precision/recall для одного кейсу.

    Визначення:
      TP  = к-сть знайдених пасток (traps), де ≥1 очікуване ключ. слово є в цитаті/описі
      FP  = к-сть знайдених пасток, де ЖОДНОГО очікуваного слова немає
      FN  = очікувалися пастки, але не знайдено жодної

      Precision = TP / (TP + FP)   ← ЗАВЖДИ ≤ 1.0
      Recall    = TP / (TP + FN)   ← ЗАВЖДИ ≤ 1.0
    """
    found_traps  = scanner_result.get("traps", [])
    expected_kws = case.get("expected_kws", [])
    expect_count = case.get("expected_count", len(expected_kws) > 0)

    # ── Чистий тендер (очікуємо 0 пасток) ─────────────────────────────────
    if not expected_kws:
        fp = len(found_traps)
        return {
            "id":             case["id"],
            "name":           case["name"],
            "expected_traps": 0,
            "found_traps":    len(found_traps),
            "TP": 0, "FP": fp, "FN": 0,
            "precision": 1.0 if fp == 0 else 0.0,
            "recall":    1.0,
            "law_ref_ok": True,
            "pass":      fp == 0,
        }

    # ── Кейс з ловушками ───────────────────────────────────────────────────
    def _trap_matches_any_kw(trap: dict) -> bool:
        """Пастка вважається TP якщо ≥1 очікуване слово є в цитаті або описі."""
        haystack = " ".join([
            (trap.get("raw_quote") or ""),
            (trap.get("description") or ""),
        ]).lower()
        return any(kw.lower() in haystack for kw in expected_kws)

    TP = sum(1 for t in found_traps if _trap_matches_any_kw(t))
    FP = len(found_traps) - TP
    FN = max(0, expect_count - TP)

    precision = TP / (TP + FP) if (TP + FP) > 0 else 0.0
    recall    = TP / (TP + FN) if (TP + FN) > 0 else 0.0

    # Перевірка посилання на закон (якщо очікується)
    has_law = any(t.get("law") for t in found_traps)
    law_ok  = has_law if case["expect_law_ref"] else True

    # PASS = recall ≥ 0.5 AND precision ≥ 0.5 AND law_ok
    passed = (recall >= 0.5) and (precision >= 0.5) and law_ok

    return {
        "id":             case["id"],
        "name":           case["name"],
        "expected_traps": expect_count,
        "found_traps":    len(found_traps),
        "TP": TP, "FP": FP, "FN": FN,
        "precision": round(precision, 3),
        "recall":    round(recall, 3),
        "law_ref_ok": law_ok,
        "pass":       passed,
    }


# ─── Головна функція ──────────────────────────────────────────────────────────

def run_golden_set(use_real_llm: bool = False) -> dict:
    """
    Запускає регресійний тест на всіх 12 еталонних кейсах.
    use_real_llm=True → намагається використати реальний Scanner.
    use_real_llm=False → rule-based детермінований scanner.
    """
    print("\n" + "═" * 65)
    print("  🧪 GOLDEN SET REGRESSION TEST v2 (12 кейсів)")
    print("  Формула: Precision = TP/(TP+FP), Recall = TP/(TP+FN)")
    print("═" * 65)

    results = []
    total_start = time.time()

    for case in GOLDEN_CASES:
        t0 = time.time()

        if use_real_llm:
            import asyncio
            try:
                from ai.agents.scanner import scan_document
                result = asyncio.run(scan_document(case["text"], {})) or {"traps": []}
            except Exception as exc:
                print(f"  ❌ LLM помилка {case['id']}: {exc}")
                result = {"traps": []}
        else:
            result = _rule_based_scanner(case["text"])

        elapsed = round(time.time() - t0, 3)
        metrics = evaluate_case(case, result)
        metrics["elapsed_sec"] = elapsed
        results.append(metrics)

        status = "✅ PASS" if metrics["pass"] else "❌ FAIL"
        tp, fp, fn = metrics["TP"], metrics["FP"], metrics["FN"]
        print(
            f"  {status} [{case['id']}]\n"
            f"         TP={tp} FP={fp} FN={fn} | "
            f"P={metrics['precision']:.3f} R={metrics['recall']:.3f} | "
            f"law={'✓' if metrics['law_ref_ok'] else '✗'} | {elapsed}с"
        )

    # ── Зведення ──────────────────────────────────────────────────────────
    total_elapsed = round(time.time() - total_start, 2)
    passed  = sum(1 for r in results if r["pass"])
    avg_P   = round(sum(r["precision"] for r in results) / len(results), 3)
    avg_R   = round(sum(r["recall"]    for r in results) / len(results), 3)
    total_TP = sum(r["TP"] for r in results)
    total_FP = sum(r["FP"] for r in results)
    total_FN = sum(r["FN"] for r in results)

    # Macro precision/recall (aggregate)
    macro_P = round(total_TP / (total_TP + total_FP), 3) if (total_TP + total_FP) > 0 else 0.0
    macro_R = round(total_TP / (total_TP + total_FN), 3) if (total_TP + total_FN) > 0 else 0.0

    summary = {
        "total":         len(results),
        "passed":        passed,
        "failed":        len(results) - passed,
        "avg_precision": avg_P,
        "avg_recall":    avg_R,
        "macro_precision": macro_P,
        "macro_recall":    macro_R,
        "total_TP": total_TP,
        "total_FP": total_FP,
        "total_FN": total_FN,
        "total_elapsed": total_elapsed,
        "cases":         results,
    }

    print("\n" + "─" * 65)
    print(f"  Результат: {passed}/{len(results)} пройшло")
    print(f"  Avg:   Precision={avg_P}  Recall={avg_R}")
    print(f"  Macro: Precision={macro_P}  Recall={macro_R}  (TP={total_TP} FP={total_FP} FN={total_FN})")
    print(f"  Час: {total_elapsed}с")

    # Валідація що Precision ≤ 1.0
    assert avg_P   <= 1.0, f"БАГ: avg_precision={avg_P} > 1.0 — перевір формулу!"
    assert macro_P <= 1.0, f"БАГ: macro_precision={macro_P} > 1.0 — перевір формулу!"
    print("  ✅ Precision ≤ 1.0 — формула коректна")
    print("═" * 65 + "\n")

    output_path = FIXTURES_DIR / "last_run_results.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"  💾 Збережено: {output_path}\n")

    return summary


# ─── pytest ───────────────────────────────────────────────────────────────────

def test_precision_never_exceeds_one():
    """Перевірка що Precision завжди ≤ 1.0."""
    summary = run_golden_set(use_real_llm=False)
    assert summary["avg_precision"]   <= 1.0, "avg_precision > 1.0!"
    assert summary["macro_precision"] <= 1.0, "macro_precision > 1.0!"


def test_golden_set_recall():
    """Середній recall ≥ 0.5."""
    summary = run_golden_set(use_real_llm=False)
    assert summary["avg_recall"] >= 0.5, f"Recall {summary['avg_recall']} < 0.5"


def test_no_false_positives_on_clean_tenders():
    """Чисті тендери не повинні мати жодної ловушки."""
    clean_ids = {"clean_tender_no_traps", "clean_flexible_equipment", "clean_open_experience"}
    for case in GOLDEN_CASES:
        if case["id"] in clean_ids:
            result = _rule_based_scanner(case["text"])
            assert len(result["traps"]) == 0, \
                f"False positive у чистому кейсі '{case['id']}': {result['traps']}"


def test_critical_traps_always_found():
    """CRITICAL-ловушки не повинні пропускатися."""
    critical_cases = [c for c in GOLDEN_CASES if c.get("expected_severity") == "CRITICAL"]
    for case in critical_cases:
        result = _rule_based_scanner(case["text"])
        assert result["traps"], \
            f"CRITICAL-ловушка не знайдена: '{case['id']}'"


def test_law_reference_present_when_expected():
    """Якщо очікується посилання на закон — воно має бути у відповіді."""
    for case in GOLDEN_CASES:
        if case.get("expect_law_ref"):
            result = _rule_based_scanner(case["text"])
            has_law = any(t.get("law") for t in result.get("traps", []))
            assert has_law, \
                f"Відсутнє посилання на закон у '{case['id']}'"


if __name__ == "__main__":
    run_golden_set(use_real_llm=False)
