"""
tests/test_spending.py — Unit тести для SpendingAnalyzer

Використовує mock httpx.AsyncClient без реальних мережевих запитів.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from analytics.spending import SpendingAnalyzer, SpendingResult, PAYMENT_DELAY_WARN_DAYS


# ── Допоміжні функції ─────────────────────────────────────────────────────────

def make_transaction(doc_date: str, payment_date: str) -> dict:
    return {"doc_date": doc_date, "payment_date": payment_date, "amount": 100_000}


def run(coro):
    return asyncio.run(coro)


# ── Тести ─────────────────────────────────────────────────────────────────────

class TestSpendingAnalyzerPercentile:
    """Тестує правильність перцентильних розрахунків без HTTP."""

    def test_percentile_single(self):
        assert SpendingAnalyzer._percentile([10.0], 50) == 10.0

    def test_percentile_sorted(self):
        data = [0.0, 10.0, 20.0, 30.0, 40.0]
        assert SpendingAnalyzer._percentile(data, 0)  == 0.0
        assert SpendingAnalyzer._percentile(data, 100) == 40.0
        assert SpendingAnalyzer._percentile(data, 50) == 20.0

    def test_percentile_interpolation(self):
        data = [0.0, 10.0, 20.0]
        p75 = SpendingAnalyzer._percentile(data, 75)
        assert 10.0 < p75 < 20.0

    def test_percentile_empty(self):
        assert SpendingAnalyzer._percentile([], 50) == 0.0


class TestSpendingAnalyzerDelayCompute:
    """Тестує обчислення затримок."""

    def test_compute_basic_delay(self):
        analyzer = SpendingAnalyzer()
        txns = [
            make_transaction("2024-01-01", "2024-01-31"),  # 30 днів
            make_transaction("2024-02-01", "2024-02-15"),  # 14 днів
        ]
        delays = analyzer._compute_delays(txns)
        assert sorted(delays) == [14.0, 30.0]

    def test_skip_negative_delays(self):
        """Передоплати (від'ємна затримка) ігноруються."""
        analyzer = SpendingAnalyzer()
        txns = [make_transaction("2024-03-10", "2024-03-01")]  # payment_date < doc_date
        delays = analyzer._compute_delays(txns)
        assert delays == []

    def test_skip_missing_dates(self):
        analyzer = SpendingAnalyzer()
        txns = [{"amount": 50000}]  # немає дат
        delays = analyzer._compute_delays(txns)
        assert delays == []

    def test_zero_delay(self):
        analyzer = SpendingAnalyzer()
        txns = [make_transaction("2024-01-01", "2024-01-01")]
        delays = analyzer._compute_delays(txns)
        assert delays == [0.0]


class TestSpendingAnalyzerIntegration:
    """Інтеграційні тести з mock HTTP."""

    def _make_mock_response(self, transactions: list[dict]) -> MagicMock:
        mock_resp = AsyncMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = transactions
        mock_resp.raise_for_status = MagicMock()
        return mock_resp

    def test_slow_payer_flag(self):
        """p50 > 30 → flag_slow_payer = True."""
        transactions = [
            make_transaction("2024-01-01", f"2024-0{m}-15")
            for m in range(2, 7)
        ]
        # Затримки: 45, 74, 105, 135, 166 днів → p50 ≈ 105 > 30
        analyzer = SpendingAnalyzer()
        delays = analyzer._compute_delays(transactions)
        delays_sorted = sorted(delays)
        p50 = SpendingAnalyzer._percentile(delays_sorted, 50)
        assert p50 > PAYMENT_DELAY_WARN_DAYS, f"Очікували p50>{PAYMENT_DELAY_WARN_DAYS}, отримали {p50}"

    def test_fast_payer(self):
        """p50 < 30 → flag_slow_payer = False."""
        transactions = [
            make_transaction("2024-01-01", "2024-01-10"),
            make_transaction("2024-02-01", "2024-02-08"),
        ]
        analyzer = SpendingAnalyzer()
        delays = analyzer._compute_delays(transactions)
        delays_sorted = sorted(delays)
        p50 = SpendingAnalyzer._percentile(delays_sorted, 50)
        assert p50 < PAYMENT_DELAY_WARN_DAYS

    def test_no_data_flag(self):
        """Пустий список → flag_no_data = True."""
        async def run_test():
            import httpx
            from unittest.mock import patch

            # Замість mock клієнта — перевизначаємо _fetch_transactions
            analyzer = SpendingAnalyzer()

            async def fake_fetch(edrpou, cpv_prefix, months_lookback):
                return []

            analyzer._fetch_transactions = fake_fetch
            result = await analyzer.analyze("12345678")
            assert result.flag_no_data is True
            assert result.delay_p50_days is None

        run(run_test())

    def test_summary_text_no_data(self):
        result = SpendingResult(edrpou="12345678", flag_no_data=True)
        text = result.summary_text()
        assert "Даних немає" in text

    def test_summary_text_slow_payer(self):
        result = SpendingResult(
            edrpou="12345678",
            transactions_count=10,
            delay_p10_days=20.0,
            delay_p50_days=45.0,
            delay_p90_days=90.0,
            flag_slow_payer=True,
        )
        text = result.summary_text()
        assert "ПОВІЛЬНИЙ ПЛАТНИК" in text
        assert "45" in text


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
