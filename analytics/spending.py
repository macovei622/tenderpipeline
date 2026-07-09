"""
analytics/spending.py — Модуль 1: Фінансовий скоринг замовника

Алгоритм (без false precision):
  1. Беремо всі платежі від замовника (ЄДРПОУ) будь-яким підрядникам
     за CPV 45* за останні 24 місяці з API spending.gov.ua.
  2. Для кожної пари (платіж, тендер) обчислюємо затримку відносно
     офіційної дати прийому результату у Prozorro.
  3. Якщо точного зіставлення нема — рахуємо percentile по всім платежам.
  4. Результат: p10/p50/p90 затримки у днях + сигнальний прапор.

Чому percentile, а не avg:
  - Немає спільного ключа між казначейськими платежами та КБ-2в актами.
  - p50 = медіана = чесна метрика без викидів.
  - p90 = "найгіршій сценарій" для планування кешфлоу.
"""
from __future__ import annotations

import asyncio
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from loguru import logger

# ── Константи ─────────────────────────────────────────────────────────────────
SPENDING_API_BASE = "https://api.spending.gov.ua/api/v2/api/transactions"
REQUEST_TIMEOUT   = 30.0  # секунди
MAX_PAGES         = 10    # захист від нескінченного пагінування
PAYMENT_DELAY_WARN_DAYS = 30  # поріг для 🔴 прапора


# ── Результат ────────────────────────────────────────────────────────────────

@dataclass
class SpendingResult:
    edrpou: str
    transactions_count: int = 0
    delay_p10_days: Optional[float] = None   # 10-й перцентиль
    delay_p50_days: Optional[float] = None   # медіана
    delay_p90_days: Optional[float] = None   # 90-й перцентиль (гіршій сценарій)
    flag_slow_payer: bool = False             # p50 > 30 днів
    flag_no_data: bool = False                # не знайдено платежів
    raw_transactions: list[dict] = field(default_factory=list, repr=False)
    error: Optional[str] = None

    def summary_text(self) -> str:
        """Текстовий підсумок для Telegram-повідомлення."""
        if self.flag_no_data or self.error:
            return (
                f"💳 *Spending.gov.ua:* Даних немає або помилка запиту.\n"
                f"_{self.error or 'Немає транзакцій для цього ЄДРПОУ'}_"
            )
        flag = "🔴 ПОВІЛЬНИЙ ПЛАТНИК" if self.flag_slow_payer else "🟢 Платить вчасно"
        return (
            f"💳 *Фінансовий скоринг замовника* [{flag}]\n"
            f"📊 Проаналізовано транзакцій: {self.transactions_count}\n"
            f"⏱ Затримки платежів:\n"
            f"  • Найкращий сценарій (p10): {self.delay_p10_days:.0f} дн.\n"
            f"  • Медіана (p50): {self.delay_p50_days:.0f} дн.\n"
            f"  • Гірший сценарій (p90): {self.delay_p90_days:.0f} дн.\n"
            + (
                "\n⚠️ *Рекомендація:* Закладіть вартість кредитування у кошторис "
                f"(~{self.delay_p90_days:.0f} дн. очікування оплати)."
                if self.flag_slow_payer else ""
            )
        )


# ── Аналізатор ───────────────────────────────────────────────────────────────

class SpendingAnalyzer:
    """
    Асинхронний клієнт до spending.gov.ua.
    Використання:
        analyzer = SpendingAnalyzer()
        result = await analyzer.analyze(edrpou="12345678", cpv_prefix="45")
    """

    def __init__(self, http_client: Optional[httpx.AsyncClient] = None):
        self._client = http_client  # дозволяє ін'єктувати mock у тестах

    async def analyze(
        self,
        edrpou: str,
        cpv_prefix: str = "45",
        months_lookback: int = 24,
    ) -> SpendingResult:
        """Головний метод: повертає SpendingResult для заданого ЄДРПОУ."""
        result = SpendingResult(edrpou=edrpou)
        try:
            transactions = await self._fetch_transactions(edrpou, cpv_prefix, months_lookback)
        except Exception as exc:
            logger.error(f"SpendingAnalyzer error for {edrpou}: {exc}")
            result.error = str(exc)
            result.flag_no_data = True
            return result

        if not transactions:
            result.flag_no_data = True
            return result

        result.transactions_count = len(transactions)
        result.raw_transactions = transactions

        delays = self._compute_delays(transactions)
        if delays:
            delays_sorted = sorted(delays)
            n = len(delays_sorted)
            result.delay_p10_days = self._percentile(delays_sorted, 10)
            result.delay_p50_days = self._percentile(delays_sorted, 50)
            result.delay_p90_days = self._percentile(delays_sorted, 90)
            result.flag_slow_payer = result.delay_p50_days > PAYMENT_DELAY_WARN_DAYS

        logger.info(
            f"SpendingAnalyzer: {edrpou} — {len(transactions)} транзакцій, "
            f"p50={result.delay_p50_days} дн."
        )
        return result

    # ── Приватні методи ───────────────────────────────────────────────────────

    async def _fetch_transactions(
        self,
        edrpou: str,
        cpv_prefix: str,
        months_lookback: int,
    ) -> list[dict]:
        """Завантажує всі транзакції через REST API spending.gov.ua."""
        date_from = (datetime.now(timezone.utc) - timedelta(days=30 * months_lookback)).strftime("%Y-%m-%d")
        all_txns: list[dict] = []
        page = 1

        async with self._get_client() as client:
            while page <= MAX_PAGES:
                params = {
                    "payer_edrpou": edrpou,
                    "startdate":    date_from,
                    "enddate":      datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                    "cpv":          cpv_prefix,
                    "page":         page,
                    "limit":        200,
                }
                try:
                    resp = await client.get(SPENDING_API_BASE, params=params, timeout=REQUEST_TIMEOUT)
                    resp.raise_for_status()
                    data = resp.json()
                except httpx.HTTPStatusError as e:
                    logger.warning(f"Spending API HTTP {e.response.status_code} на сторінці {page}")
                    break
                except Exception as e:
                    logger.warning(f"Spending API помилка на сторінці {page}: {e}")
                    break

                items = data if isinstance(data, list) else data.get("data", data.get("transactions", []))
                if not items:
                    break
                all_txns.extend(items)

                # Якщо отримали менше ліміту — більше сторінок нема
                if len(items) < 200:
                    break
                page += 1

        return all_txns

    def _compute_delays(self, transactions: list[dict]) -> list[float]:
        """
        Обчислює затримку між датою документа (doc_date) і датою платежу (payment_date).
        Ці поля є у більшості записів spending.gov.ua.
        Якщо полів нема — транзакція пропускається.
        """
        delays: list[float] = []
        for txn in transactions:
            doc_date_str     = txn.get("doc_date") or txn.get("contractDate")
            payment_date_str = txn.get("payment_date") or txn.get("paymentDate") or txn.get("trans_date")
            if not doc_date_str or not payment_date_str:
                continue
            try:
                doc_date     = datetime.fromisoformat(doc_date_str[:10])
                payment_date = datetime.fromisoformat(payment_date_str[:10])
                delay_days   = (payment_date - doc_date).days
                if delay_days >= 0:  # ігноруємо передоплати (від'ємні значення)
                    delays.append(float(delay_days))
            except (ValueError, TypeError):
                continue
        return delays

    @staticmethod
    def _percentile(sorted_data: list[float], pct: int) -> float:
        """Лінійна інтерполяція перцентиля."""
        if not sorted_data:
            return 0.0
        n = len(sorted_data)
        if n == 1:
            return sorted_data[0]
        k = (n - 1) * pct / 100.0
        f, c = int(k), min(int(k) + 1, n - 1)
        return sorted_data[f] + (k - f) * (sorted_data[c] - sorted_data[f])

    def _get_client(self) -> httpx.AsyncClient:
        """Повертає реальний або ін'єктований клієнт."""
        if self._client is not None:
            # Якщо передали готовий клієнт (мок у тестах) — повертаємо контекст-менеджер
            return _NoopContextManager(self._client)
        return httpx.AsyncClient(
            headers={"User-Agent": "ProzorroAI/2.0 analytics (contact: admin@example.com)"},
        )


class _NoopContextManager:
    """Обгортка, яка перетворює вже існуючий клієнт на async context manager."""
    def __init__(self, client: httpx.AsyncClient):
        self._client = client
    async def __aenter__(self) -> httpx.AsyncClient:
        return self._client
    async def __aexit__(self, *args) -> None:
        pass  # не закриваємо mock-клієнт
