"""
ai/orchestrator.py — Blackboard + Workflow Engine

Централізоване сховище фактів для мультиагентного конвеєра.

Принцип:
  - Кожен агент ЧИТАЄ з Blackboard і ПИШЕ до Blackboard.
  - Всі факти мають обов'язкові поля provenance (page_ref, raw_quote, agent).
  - Workflow Engine контролює порядок і ранній вихід (early exit) при BLOCKED.
  - Не більше 5 послідовних критичних кроків без ручної валідації.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from loguru import logger


# ─── Стани конвеєра ───────────────────────────────────────────────────────────

class PipelineStatus(str, Enum):
    PENDING     = "pending"
    RUNNING     = "running"
    NEEDS_FIX   = "needs_fix"
    BLOCKED     = "blocked"          # early exit тут
    READY       = "ready"
    ERROR       = "error"


# ─── Факт із Provenance ───────────────────────────────────────────────────────

@dataclass
class Fact:
    """
    Атомарний факт, записаний агентом.
    Кожне твердження ПОВИННО мати page_ref та raw_quote.
    Без них факт вважається непідтвердженим.
    """
    agent: str                    # scanner / calculator / collector / reviewer
    fact_type: str                # trap / margin / document / verdict / history
    content: Any                  # dict або str з даними
    page_ref: Optional[int] = None          # номер сторінки
    section_name: Optional[str] = None      # назва секції ТД
    raw_quote: Optional[str] = None         # дослівна цитата з ТД
    law_reference: Optional[str] = None     # стаття закону
    confidence: float = 1.0                 # 0.0–1.0
    verified: bool = False                  # True після Self-Check Reviewer
    timestamp: float = field(default_factory=time.time)

    @property
    def has_provenance(self) -> bool:
        """Факт вважається верифікованим лише якщо є цитата і сторінка."""
        return bool(self.raw_quote and self.page_ref is not None)

    def to_dict(self) -> dict:
        return {
            "agent":        self.agent,
            "fact_type":    self.fact_type,
            "content":      self.content,
            "page_ref":     self.page_ref,
            "section_name": self.section_name,
            "raw_quote":    self.raw_quote,
            "law_reference":self.law_reference,
            "confidence":   self.confidence,
            "verified":     self.verified,
            "has_provenance":self.has_provenance,
        }


# ─── Blackboard ───────────────────────────────────────────────────────────────

class Blackboard:
    """
    Централізоване сховище стану аналізу одного тендера.
    Передається між усіма агентами конвеєра.
    """

    def __init__(self, tender_id: str):
        self.tender_id = tender_id
        self.status: PipelineStatus = PipelineStatus.PENDING
        self.facts: list[Fact] = []

        # Метрики сесії (runtime monitoring)
        self.metrics: dict[str, Any] = {
            "start_time":          time.time(),
            "steps_completed":     0,
            "total_tokens":        0,
            "total_cost_usd":      0.0,
            "early_exit":          False,
            "unverified_warnings": 0,
        }

        # Підсумкові блоки (заповнюються агентами)
        self.scan_result:       Optional[dict] = None   # Scanner
        self.calc_result:       Optional[dict] = None   # Calculator
        self.docs_result:       Optional[dict] = None   # Collector
        self.review_result:     Optional[dict] = None   # Reviewer
        self.history_result:    Optional[dict] = None   # HistoryAnalyzer
        self.ocr_coverage:      Optional[float] = None  # % розпізнаного тексту
        self.partial_coverage_pages: list[int] = []     # сторінки без OCR

    # ── Запис фактів ──────────────────────────────────────────────────────────

    def add_fact(self, fact: Fact) -> None:
        """Додати факт. Факти без provenance логуються як unprovable."""
        if not fact.has_provenance:
            self.metrics["unverified_warnings"] += 1
            logger.warning(
                f"⚠️ Факт без provenance: agent={fact.agent} "
                f"type={fact.fact_type} — потрібна цитата і page_ref"
            )
        self.facts.append(fact)

    def get_facts_by_type(self, fact_type: str) -> list[Fact]:
        return [f for f in self.facts if f.fact_type == fact_type]

    def get_verified_facts(self) -> list[Fact]:
        return [f for f in self.facts if f.verified and f.has_provenance]

    # ── Облік токенів і витрат ────────────────────────────────────────────────

    def record_agent_call(self, usage: dict) -> None:
        """Додати витрати одного виклику агента до метрик сесії."""
        self.metrics["total_tokens"]   += usage.get("total_tokens", 0)
        self.metrics["total_cost_usd"] += usage.get("cost_usd", 0.0)
        self.metrics["steps_completed"] += 1

    # ── Статус ────────────────────────────────────────────────────────────────

    def set_status(self, status: PipelineStatus) -> None:
        self.status = status
        if status == PipelineStatus.BLOCKED:
            self.metrics["early_exit"] = True
            logger.warning(f"🚫 Конвеєр BLOCKED для тендера {self.tender_id}")

    def is_blocked(self) -> bool:
        return self.status == PipelineStatus.BLOCKED

    def elapsed_sec(self) -> float:
        return round(time.time() - self.metrics["start_time"], 2)

    def summary_metrics(self) -> dict:
        return {
            **self.metrics,
            "tender_id":   self.tender_id,
            "status":      self.status.value,
            "elapsed_sec": self.elapsed_sec(),
            "facts_total": len(self.facts),
            "facts_verified": len(self.get_verified_facts()),
        }


# ─── Workflow Engine ──────────────────────────────────────────────────────────

class TenderWorkflow:
    """
    Оркестратор послідовних кроків аналізу тендера.

    Правила:
      1. Не більше MAX_CRITICAL_STEPS критичних кроків без валідації.
      2. Early exit при BLOCKED.
      3. Логування всіх кроків із часом і витратами.
    """

    MAX_CRITICAL_STEPS = 5

    def __init__(self, blackboard: Blackboard):
        self.bb = blackboard
        self._critical_step_count = 0

    async def run(
        self,
        tender_meta:    dict,
        doc_text:       str,
        doc_sections:   dict[str, str],
        company_profile: Optional[dict] = None,
        expected_discount_pct: float = 4.0,
    ) -> Blackboard:
        """
        Запускає повний конвеєр аналізу тендера.
        Повертає заповнений Blackboard.
        """
        from ai.agents.scanner    import scan_document
        from ai.agents.calculator import calculate_margin
        from ai.agents.collector  import fill_all_required_documents
        from ai.agents.reviewer   import review_package

        self.bb.set_status(PipelineStatus.RUNNING)
        logger.info(f"🚀 Старт конвеєра для тендера {self.bb.tender_id}")

        # ── Крок 1: AI-Сканер ─────────────────────────────────────────────────
        await self._run_step("scanner", self._step_scanner, doc_text, doc_sections, tender_meta, company_profile)
        if self.bb.is_blocked():
            return self.bb

        # ── Крок 2: AI-Калькулятор ────────────────────────────────────────────
        spec_text = doc_sections.get("технічне завдання", doc_text[:20_000])
        await self._run_step("calculator", self._step_calculator,
                             spec_text, tender_meta, expected_discount_pct, company_profile)
        if self.bb.is_blocked():
            return self.bb

        # ── Крок 3: AI-Збирач (якщо є профіль компанії) ─────────────────────
        if company_profile:
            required_docs = []
            if self.bb.scan_result:
                required_docs = self.bb.scan_result.get("required_documents", [])
            await self._run_step("collector", self._step_collector,
                                 company_profile, required_docs,
                                 doc_sections.get("кваліфікаційні вимоги", ""))

        # ── Крок 4: AI-Перевіряючий ───────────────────────────────────────────
        await self._run_step("reviewer", self._step_reviewer, doc_sections)
        if self.bb.is_blocked():
            return self.bb

        # ── Фінальний статус ──────────────────────────────────────────────────
        verdict = self.bb.review_result.get("verdict", "NEEDS_FIX") \
            if self.bb.review_result else "NEEDS_FIX"
        status_map = {
            "READY":      PipelineStatus.READY,
            "NEEDS_FIX":  PipelineStatus.NEEDS_FIX,
            "BLOCKED":    PipelineStatus.BLOCKED,
        }
        self.bb.set_status(status_map.get(verdict, PipelineStatus.NEEDS_FIX))

        m = self.bb.summary_metrics()
        logger.info(
            f"✅ Конвеєр завершено: статус={m['status']} | "
            f"кроків={m['steps_completed']} | "
            f"витрат=${m['total_cost_usd']:.4f} | "
            f"час={m['elapsed_sec']}с"
        )
        return self.bb

    # ── Обгортка кроку ────────────────────────────────────────────────────────

    async def _run_step(self, name: str, fn, *args, **kwargs) -> None:
        """Виконати один крок конвеєра з обліком помилок і early exit."""
        if self.bb.is_blocked():
            logger.info(f"⏭️ Пропускаємо крок {name} — конвеєр вже BLOCKED")
            return

        self._critical_step_count += 1
        if self._critical_step_count > self.MAX_CRITICAL_STEPS:
            logger.error("❌ Перевищено MAX_CRITICAL_STEPS — потрібна ручна перевірка")
            self.bb.set_status(PipelineStatus.BLOCKED)
            return

        t0 = time.time()
        try:
            await fn(*args, **kwargs)
        except Exception as exc:
            logger.exception(f"❌ Помилка на кроці {name}: {exc}")
            self.bb.set_status(PipelineStatus.ERROR)
        finally:
            logger.info(f"  Крок [{name}] — {round(time.time()-t0, 2)}с")

    # ── Реалізація кроків ─────────────────────────────────────────────────────

    async def _step_scanner(self, doc_text: str, sections: dict, tender_meta: dict, company_profile: Optional[dict] = None) -> None:
        from ai.agents.scanner import scan_document
        result = await scan_document(doc_text, tender_meta=tender_meta, company_profile=company_profile)
        if not result:
            return
        self.bb.scan_result = result
        self.bb.record_agent_call(result.get("_meta", {}))

        # Переносимо дискримінаційні вимоги у Blackboard як Fact (Юридичні заперечення)
        for req in result.get("discriminatory_requirements", []):
            self.bb.add_fact(Fact(
                agent       = "scanner",
                fact_type   = "discriminatory_requirement",
                content     = req,
                page_ref    = req.get("section"),
                section_name= req.get("section"),
                raw_quote   = req.get("quote"),
                law_reference=req.get("law_reference"),
                confidence  = 0.9,
            ))

        # Переносимо комерційні ризики договору у Blackboard як Fact (Комерційні ризики)
        for risk in result.get("contract_risks", []):
            self.bb.add_fact(Fact(
                agent       = "scanner",
                fact_type   = "contract_risk",
                content     = risk,
                page_ref    = None,
                section_name= None,
                raw_quote   = risk.get("quote"),
                law_reference=None,
                confidence  = 0.9,
            ))

        # Early exit якщо ризик CRITICAL занадто високий
        criticals = [r for r in result.get("contract_risks", []) if r.get("type") == "CRITICAL"]
        if len(criticals) >= 3:
            logger.warning(f"🚨 Знайдено {len(criticals)} критичних ризиків договору")

        # Верифікуємо цитати відразу після сканування
        self._self_check_citations(sections)

    async def _step_calculator(self, spec_text: str, meta: dict, expected_discount_pct: float = 4.0, company_profile: Optional[dict] = None) -> None:
        from ai.agents.calculator import calculate_margin
        amount = float(meta.get("value", {}).get("amount", 0) or 0)
        machinery_cost = float(meta.get("machinery_cost", 0) or 0)
        result = await calculate_margin(
            amount, spec_text, 
            expected_discount_pct=expected_discount_pct,
            machinery_cost=machinery_cost,
            company_profile=company_profile
        )
        if not result:
            return
        self.bb.calc_result = result
        self.bb.record_agent_call(result.get("_meta", {}))

        # Early exit при збитковому тендері або критичному ризику маржі
        margin = result.get("margin_pct", 100)
        risk = result.get("margin_risk", "UNKNOWN")
        if (margin is not None and margin < 0) or risk == "CRITICAL":
            logger.warning(f"💸 Збитковий або вкрай ризиковий тендер: маржа={margin}%, ризик={risk}")
            self.bb.set_status(PipelineStatus.BLOCKED)

    async def _step_collector(self, profile: dict,
                               required: list, td_reqs: str) -> None:
        from ai.agents.collector import fill_all_required_documents
        # ВИПРАВЛЕНО: правильний порядок — (required_docs, company_data, td_requirements)
        result = await fill_all_required_documents(required, profile, td_reqs)
        if not result:
            return
        self.bb.docs_result = result
        # cost вже без _meta у collector, беремо сумарно

    async def _step_reviewer(self, sections: dict) -> None:
        from ai.agents.reviewer import review_package
        if not self.bb.scan_result:
            return

        # Reviewer отримує оригінальні секції + список фактів Сканера
        td_reqs = sections.get("кваліфікаційні вимоги", "")
        # Передаємо фактично сформовані документи учасника (якщо вони є)
        pkg = self.bb.docs_result if self.bb.docs_result else {}
        req_docs = self.bb.scan_result.get("required_documents", [])

        result = await review_package(td_reqs, pkg, req_docs)
        if not result:
            return
        self.bb.review_result = result
        self.bb.record_agent_call(result.get("_meta", {}))



        # Early exit при BLOCKED вердикті
        if result.get("verdict") == "BLOCKED":
            self.bb.set_status(PipelineStatus.BLOCKED)

    def _self_check_citations(self, sections: dict) -> None:
        """
        Перехресна перевірка: шукаємо raw_quote кожного факту
        у тексті відповідної секції.
        Непідтверджені цитати позначаємо verified=False та логуємо.
        """
        all_text = "\n".join(sections.values()).lower()
        # Нормалізуємо весь текст ТД для надійного пошуку (видаляємо пробіли та пунктуацію)
        normalized_all = "".join(c for c in all_text if c.isalnum())
        unverified = 0
        # Перевіряємо обидва типи фактів: юридичні заперечення І комерційні ризики
        facts_to_check = (
            self.bb.get_facts_by_type("discriminatory_requirement") +
            self.bb.get_facts_by_type("contract_risk")
        )
        for fact in facts_to_check:
            if not fact.raw_quote:
                unverified += 1
                continue
            # Беремо перші 60 символів, очищуємо від пунктуації
            clean_quote = "".join(c for c in fact.raw_quote[:60] if c.isalnum()).lower()
            if clean_quote and clean_quote in normalized_all:
                fact.verified = True
            else:
                fact.verified = False
                unverified += 1
                logger.warning(
                    f"🔍 Цитату не знайдено у тексті ТД (після нормалізації): "
                    f"\"{fact.raw_quote[:60]}...\""
                )

        self.bb.metrics["unverified_warnings"] = unverified
        if unverified > 0:
            logger.warning(
                f"⚠️ Self-Check: {unverified} фактів без підтвердженої цитати"
            )
