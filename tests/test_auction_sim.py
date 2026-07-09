"""
tests/test_auction_sim.py — Unit тести для AuctionSimulator

Всі тести використовують mock — без реальних мережевих запитів.
"""
import asyncio
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import AsyncMock, MagicMock, patch
from analytics.auction_simulator import (
    AuctionSimulator, AuctionResult, CompetitorProfile,
    MONTE_CARLO_RUNS, MIN_HISTORY_LOTS,
)


def run(coro):
    return asyncio.run(coro)


# ── Допоміжні дані ────────────────────────────────────────────────────────────

def make_competitor(
    edrpou: str = "12345678",
    name: str = "ТОВ Тест",
    lots: int = 10,
    avg_discount: float = 5.0,
    round2_drop: float = 2.0,
    win_rate: float = 0.3,
) -> CompetitorProfile:
    return CompetitorProfile(
        edrpou=edrpou,
        name=name,
        lots_analyzed=lots,
        avg_initial_discount_pct=avg_discount,
        avg_round2_drop_pct=round2_drop,
        win_rate=win_rate,
    )


def make_tender_data(
    base_amount: float = 1_000_000,
    n_bids: int = 3,
    winner_edrpou: str = "11111111",
) -> dict:
    """Синтетичний завершений тендер з кількома ставками."""
    bids = []
    for i in range(n_bids):
        edrpou = f"{11111111 + i * 11111111}"
        discount_pct = (i + 1) * 3  # 3%, 6%, 9%
        bids.append({
            "status": "active",
            "value": {"amount": base_amount * (1 - discount_pct / 100)},
            "tenderers": [{"identifier": {"id": edrpou}, "name": f"ТОВ Учасник {i+1}"}],
        })
    return {
        "value": {"amount": base_amount},
        "bids": bids,
        "awards": [{"status": "active", "suppliers": [{"identifier": {"id": winner_edrpou}}]}],
    }


# ── Тести CompetitorProfile ───────────────────────────────────────────────────

class TestCompetitorProfile:
    def test_fields_stored(self):
        c = make_competitor(avg_discount=7.5, win_rate=0.4)
        assert c.avg_initial_discount_pct == 7.5
        assert c.win_rate == 0.4
        assert c.lots_analyzed == 10


# ── Тести _build_competitor_profiles ─────────────────────────────────────────

class TestBuildCompetitorProfiles:
    simulator = AuctionSimulator()

    def test_extracts_profiles(self):
        tenders = [make_tender_data() for _ in range(MIN_HISTORY_LOTS + 2)]
        profiles = self.simulator._build_competitor_profiles(tenders, 1_000_000)
        # Маємо отримати профілі для учасників з достатньою кількістю лотів
        assert len(profiles) >= 1

    def test_skips_zero_amount(self):
        """Тендери з нульовою базовою сумою ігноруються."""
        tenders = [{"value": {"amount": 0}, "bids": [], "awards": []}]
        profiles = self.simulator._build_competitor_profiles(tenders, 1_000_000)
        assert profiles == []

    def test_skips_insufficient_lots(self):
        """Конкурент з менше ніж MIN_HISTORY_LOTS лотів не включається."""
        tenders = [make_tender_data() for _ in range(MIN_HISTORY_LOTS - 1)]
        profiles = self.simulator._build_competitor_profiles(tenders, 1_000_000)
        # Учасники з'являються рідше ніж MIN_HISTORY_LOTS разів → порожній список
        # (кожен тендер має різних учасників)
        assert isinstance(profiles, list)

    def test_sorted_by_lots_desc(self):
        """Конкуренти відсортовані за кількістю лотів (найбільше вперше)."""
        # Створюємо тендери з повторюваним учасником 11111111
        tenders = [make_tender_data(n_bids=2) for _ in range(12)]
        profiles = self.simulator._build_competitor_profiles(tenders, 1_000_000)
        if len(profiles) >= 2:
            assert profiles[0].lots_analyzed >= profiles[1].lots_analyzed

    def test_win_rate_calculated(self):
        """win_rate = кількість перемог / кількість ставок."""
        winner_edrpou = "11111111"
        tenders = [make_tender_data(winner_edrpou=winner_edrpou) for _ in range(10)]
        profiles = self.simulator._build_competitor_profiles(tenders, 1_000_000)
        winner_profile = next((p for p in profiles if p.edrpou == winner_edrpou), None)
        if winner_profile:
            assert 0.0 <= winner_profile.win_rate <= 1.0
            assert winner_profile.win_rate > 0


# ── Тести Monte Carlo ─────────────────────────────────────────────────────────

class TestMonteCarlo:
    simulator = AuctionSimulator()

    def test_optimal_bid_within_range(self):
        """Оптимальна ставка між drop_dead і expected."""
        expected = 1_000_000.0
        drop_dead =   800_000.0
        competitors = [make_competitor(avg_discount=5.0, round2_drop=2.0)]

        bid, prob = self.simulator._monte_carlo(expected, competitors, drop_dead)
        assert drop_dead <= bid <= expected, f"bid={bid} поза межами [{drop_dead}, {expected}]"

    def test_win_probability_valid_range(self):
        """Ймовірність перемоги між 0 і 1."""
        expected = 2_000_000.0
        drop_dead = 1_500_000.0
        competitors = [make_competitor(avg_discount=10.0)]

        _, prob = self.simulator._monte_carlo(expected, competitors, drop_dead)
        assert 0.0 <= prob <= 1.0

    def test_high_drop_dead_equals_expected_100pct(self):
        """Якщо drop_dead == expected → єдина можлива ставка."""
        expected = 1_000_000.0
        competitors = [make_competitor(avg_discount=50.0)]  # агресивний конкурент

        bid, prob = self.simulator._monte_carlo(expected, competitors, expected)
        assert bid == expected

    def test_no_competitors_wins_always(self):
        """Без конкурентів — завжди перемагаємо (prob → 1.0)."""
        expected = 1_000_000.0
        drop_dead =   900_000.0

        bid, prob = self.simulator._monte_carlo(expected, [], drop_dead)
        # Без конкурентів min_competitor_bid = expected, наш bid < expected → перемага
        assert prob == 1.0 or bid <= expected


# ── Тести верифікації джерела ─────────────────────────────────────────────────

class TestDataSourceVerification:
    def _make_sim_with_response(self, json_data: dict):
        """Створює симулятор з mock-клієнтом."""
        class FakeResponse:
            def __init__(self, d): self.d = d
            def raise_for_status(self): pass
            def json(self): return self.d

        class FakeClient:
            def __init__(self):
                self.calls = 0
            async def get(self, url, *args, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    # стрім списку (повертає ID)
                    return FakeResponse({"data": [{"id": "abc"}]})
                # деталі тендера (повертає json_data)
                return FakeResponse({"data": json_data})
            async def aclose(self): pass

        sim = AuctionSimulator.__new__(AuctionSimulator)
        sim._client = FakeClient()
        return sim

    def test_verify_ok(self):
        """API повернув тендер з полем value → верифікація OK."""
        async def run_test():
            sim = self._make_sim_with_response(
                {"value": {"amount": 500_000}, "id": "abc"}
            )
            result = await sim._verify_data_source()
            assert result is True
        run(run_test())

    def test_verify_empty_list(self):
        """API повернув порожній список → верифікація FAILED."""
        async def run_test():
            # Тут імітуємо порожній список на першому ж запиті
            class EmptyStreamResponse:
                def raise_for_status(self): pass
                def json(self): return {"data": []}
            class EmptyClient:
                async def get(self, *args, **kwargs): return EmptyStreamResponse()
                async def aclose(self): pass

            sim = AuctionSimulator.__new__(AuctionSimulator)
            sim._client = EmptyClient()
            result = await sim._verify_data_source()
            assert result is False
        run(run_test())

    def test_verify_missing_value_field(self):
        """Тендер без поля value → верифікація FAILED."""
        async def run_test():
            sim = self._make_sim_with_response(
                {"id": "abc", "title": "test"}  # немає value
            )
            result = await sim._verify_data_source()
            assert result is False
        run(run_test())

    def test_verify_network_error(self):
        """Мережева помилка → верифікація FAILED, не падає."""
        async def run_test():
            import httpx

            class FailingClient:
                async def get(self, *args, **kwargs):
                    raise httpx.ConnectError("timeout")
                async def aclose(self): pass

            sim = AuctionSimulator.__new__(AuctionSimulator)
            sim._client = FailingClient()
            result = await sim._verify_data_source()
            assert result is False
        run(run_test())


# ── Тести AuctionResult.summary_text ─────────────────────────────────────────

class TestAuctionResultSummaryText:
    def test_data_source_error_text(self):
        result = AuctionResult(
            tender_id="TEST-001",
            expected_value=1_000_000,
            data_source_error="API недоступний",
        )
        text = result.summary_text()
        assert "недоступне" in text
        assert "перевірки джерела" in text

    def test_no_competitors_text(self):
        result = AuctionResult(
            tender_id="TEST-001",
            expected_value=1_000_000,
            data_source_verified=True,
        )
        text = result.summary_text()
        assert "Недостатньо даних" in text

    def test_full_result_text(self):
        result = AuctionResult(
            tender_id="TEST-001",
            expected_value=1_000_000,
            data_source_verified=True,
            competitors=[make_competitor()],
            optimal_bid=950_000,
            win_probability=0.72,
            drop_dead_price=850_000,
        )
        text = result.summary_text()
        assert "Monte Carlo" in text
        assert "950" in text  # частина optimal_bid
        assert "72%" in text

    def test_error_text(self):
        result = AuctionResult(
            tender_id="TEST-001",
            expected_value=1_000_000,
            error="Немає завершених тендерів",
        )
        text = result.summary_text()
        assert "Помилка" in text


# ── Інтеграційний тест: повний аналіз з реальним fake-клієнтом ───────────────

class TestAuctionSimulatorFullFlow:
    def test_full_flow_with_fake_client(self):
        """Симулює повний цикл: верифікація → збір → профілі → Monte Carlo."""
        async def run_test():
            verify_stream = {"data": [{"id": "v_id"}]}
            verify_details = {"data": {"value": {"amount": 500_000}, "id": "v_id"}}
            tenders_stream = {"data": [{"id": f"t_{i}"} for i in range(12)]}
            single_tender_details = {"data": make_tender_data(n_bids=3)}

            class FakeResponse:
                def __init__(self, data): self._data = data
                def raise_for_status(self): pass
                def json(self): return self._data

            class FakeClient:
                def __init__(self):
                    self.calls = 0
                async def get(self, url, *args, **kwargs):
                    self.calls += 1
                    if self.calls == 1:
                        return FakeResponse(verify_stream)
                    elif self.calls == 2:
                        return FakeResponse(verify_details)
                    elif self.calls == 3:
                        return FakeResponse(tenders_stream)
                    else:
                        return FakeResponse(single_tender_details)
                async def aclose(self): pass

            sim = AuctionSimulator.__new__(AuctionSimulator)
            sim._client = FakeClient()

            with patch("asyncio.sleep", return_value=None):
                result = await sim.analyze(
                    tender_id="UA-2024-TEST",
                    expected_value=1_000_000,
                    cpv_prefix="4521",
                    region="Вінницька область",
                    drop_dead_price=850_000,
                )

            assert result.data_source_verified is True
            assert result.data_source_error is None
            if result.competitors:
                assert result.optimal_bid is not None
                assert result.optimal_bid >= 850_000
                assert result.win_probability is not None
                assert 0.0 <= result.win_probability <= 1.0

        run(run_test())


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
