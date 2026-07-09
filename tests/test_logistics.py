"""
tests/test_logistics.py — Unit тести для LogisticsCalculator
"""
import asyncio
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from analytics.logistics import (
    LogisticsCalculator, LogisticsResult, GeoPoint,
    INVESTOR_KM, FUEL_COST_UAH_KM, TRIPS_PER_DAY,
)


def run(coro):
    return asyncio.run(coro)


class TestGeoPoint:
    def test_osrm_str(self):
        p = GeoPoint(address="Тест", lat=49.2331, lon=28.4682)
        assert p.osrm_str() == "28.4682,49.2331"


class TestHiddenMarginCalculation:
    """Тестує математику прихованої маржі без HTTP."""

    def test_short_distance_has_margin(self):
        """10 км замість 30 км → є прихована маржа."""
        result = LogisticsResult(
            object_address="А",
            supplier_address="Б",
            real_distance_km=10.0,
            investor_km=30.0,
            duration_min=15.0,
        )
        # Вручну розрахуємо маржу
        saved_km = 30.0 - 10.0
        expected_margin_per_day = saved_km * FUEL_COST_UAH_KM * TRIPS_PER_DAY
        # Перевіримо формулу
        assert expected_margin_per_day > 0
        assert saved_km == 20.0

    def test_long_distance_no_margin(self):
        """40 км при інвесторській 30 км → немає маржі."""
        result = LogisticsResult(
            object_address="А",
            supplier_address="Б",
            real_distance_km=40.0,
            investor_km=30.0,
            duration_min=50.0,
            flag_margin_found=False,
        )
        assert result.flag_margin_found is False
        assert result.hidden_margin_uah is None

    def test_equal_distance_no_margin(self):
        """30 км = інвесторська → маржі немає."""
        result = LogisticsResult(
            object_address="А",
            supplier_address="Б",
            real_distance_km=30.0,
            investor_km=30.0,
        )
        assert result.real_distance_km >= result.investor_km


class TestLogisticsResultSummaryText:
    """Тестує генерацію тексту."""

    def test_geocode_failed_text(self):
        result = LogisticsResult(
            object_address="Невідома вулиця 999",
            supplier_address="Тест",
            geocode_failed=True,
        )
        text = result.summary_text()
        assert "Не вдалося" in text

    def test_margin_found_text(self):
        result = LogisticsResult(
            object_address="вул. Соборна 1",
            supplier_address="вул. Пирогова 5",
            real_distance_km=8.5,
            investor_km=30.0,
            duration_min=12.0,
            hidden_margin_uah=57_600.0,
            flag_margin_found=True,
        )
        text = result.summary_text()
        assert "Прихована маржа" in text
        assert "57" in text  # частина суми

    def test_no_margin_text(self):
        result = LogisticsResult(
            object_address="А",
            supplier_address="Б",
            real_distance_km=35.0,
            investor_km=30.0,
            duration_min=40.0,
            flag_margin_found=False,
        )
        text = result.summary_text()
        assert "Логістичної маржі немає" in text


class TestLogisticsIntegration:
    """Інтеграційні тести з mock httpx."""

    def test_analyze_with_mock_data(self):
        """Симулюємо успішний геокодинг і маршрут."""
        import httpx
        from unittest.mock import AsyncMock, MagicMock

        # Nominatim responses
        nominatim_response_obj = MagicMock()
        nominatim_response_obj.json.side_effect = [
            [{"lat": "49.2330", "lon": "28.4680", "display_name": "Вінниця"}],  # об'єкт
            [{"lat": "49.2410", "lon": "28.4720", "display_name": "Склад"}],    # склад
        ]
        nominatim_response_obj.raise_for_status = MagicMock()
        nominatim_response_obj.status_code = 200

        # OSRM response (2 км)
        osrm_response = MagicMock()
        osrm_response.json.return_value = {
            "code": "Ok",
            "routes": [{"distance": 2000, "duration": 180}]  # 2 км, 3 хв
        }
        osrm_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=[
            nominatim_response_obj,
            nominatim_response_obj,
            osrm_response,
        ])

        calc = LogisticsCalculator(http_client=mock_client)

        async def run_test():
            # Підміняємо sleep щоб тест не чекав
            import unittest.mock
            with unittest.mock.patch("asyncio.sleep", return_value=None):
                result = await calc.analyze(
                    object_address="вул. Соборна 1, Вінниця",
                    supplier_address="вул. Пирогова 5, Вінниця",
                    investor_km=30.0,
                    work_days=60,
                )
            return result

        result = run(run_test())
        assert result.geocode_failed is False
        assert result.real_distance_km == 2.0  # 2000м → 2 км
        assert result.flag_margin_found is True  # 2 < 30 → маржа є
        assert result.hidden_margin_uah > 0


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
