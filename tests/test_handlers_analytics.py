"""
tests/test_handlers_analytics.py — Unit тести для bot/handlers_analytics.py
"""
import sys, os
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bot.handlers_analytics import guess_work_type, resolve_edrpou_from_arg


def run(coro):
    return asyncio.run(coro)


class TestGuessWorkType:
    def test_guess_concrete(self):
        assert guess_work_type("Влаштування монолітного бетонного фундаменту") == "concrete_monolithic"
        assert guess_work_type("Монтаж залізобетонних плит") == "concrete_prefab"

    def test_guess_plaster(self):
        assert guess_work_type("Штукатурення стін цементним розчином") == "plaster_cement"
        assert guess_work_type("Шпаклювання гіпсових поверхонь") == "plaster_gypsum"

    def test_guess_screed(self):
        assert guess_work_type("Влаштування цементної стяжки") == "screed"

    def test_guess_waterproofing(self):
        assert guess_work_type("Гідроізоляція фундаменту бітумною мастикою") == "waterproofing"

    def test_guess_paint(self):
        assert guess_work_type("Фарбування металевих грат") == "paint"

    def test_guess_masonry(self):
        assert guess_work_type("Мурування стін з цегли") == "masonry"

    def test_guess_generic(self):
        assert guess_work_type("Встановлення дверей та вікон") == "generic"


class TestResolveEdrpouFromArg:
    @patch("bot.handlers_analytics.fetch_tender")
    def test_resolve_direct_edrpou(self, mock_fetch):
        """Якщо передали 8 цифр — одразу повертає їх без запитів."""
        async def run_test():
            res = await resolve_edrpou_from_arg("12345678")
            assert res == "12345678"
            mock_fetch.assert_not_called()
        run(run_test())

    @patch("bot.handlers_analytics.fetch_tender")
    def test_resolve_from_tender_link(self, mock_fetch):
        """Якщо передали лінк/ID тендера — завантажує і бере ЄДРПОУ замовника."""
        async def run_test():
            # Мок тендера
            mock_fetch.return_value = {
                "procuringEntity": {
                    "identifier": {
                        "id": 87654321
                    }
                }
            }
            res = await resolve_edrpou_from_arg("UA-2025-01-15-001234-a")
            assert res == "87654321"
            mock_fetch.assert_called_once_with("UA-2025-01-15-001234-a")
        run(run_test())

    @patch("bot.handlers_analytics.fetch_tender")
    def test_resolve_invalid(self, mock_fetch):
        """Неможливий ЄДРПОУ чи невірне посилання -> None."""
        async def run_test():
            mock_fetch.return_value = None
            res = await resolve_edrpou_from_arg("невідомо_що")
            assert res is None
        run(run_test())


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
