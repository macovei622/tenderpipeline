"""
analytics/logistics.py — Модуль 3: Логістичний калькулятор

Алгоритм:
  1. Геокодування адреси об'єкта та складу підрядника через Nominatim API.
  2. Розрахунок маршруту для вантажного транспорту через OSRM API.
  3. Порівняння реальної відстані з інвесторською (30 км за замовчуванням).
  4. Розрахунок "прихованої маржі" якщо реальна відстань < інвесторська.

Rate limits:
  - Nominatim: 1 запит/сек (публічний сервер). Ставимо sleep(1).
  - OSRM: без офіційних лімітів, але ввічливо → sleep(0.5).
  Для production рекомендується локальний Docker-інстанс OSRM.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional

import httpx
from loguru import logger

# ── Константи ─────────────────────────────────────────────────────────────────
NOMINATIM_URL    = "https://nominatim.openstreetmap.org/search"
OSRM_URL         = "http://router.project-osrm.org/route/v1/driving"
INVESTOR_KM      = 30.0    # базова відстань в інвесторській кошторисі (км)
FUEL_COST_UAH_KM = 12.0    # орієнтовна вартість 1 км вантажного авто (грн)
TRIPS_PER_DAY    = 4       # кількість рейсів на день (туди+назад = 2 рейси = 4 плечі)
NOMINATIM_DELAY  = 1.1     # секунди між запитами (ToS Nominatim)
OSRM_DELAY       = 0.5

HEADERS = {
    "User-Agent": "ProzorroAIBot-UA/2.1 (contact: macovei.procurement@gmail.com; educational/commercial auditing tool)"
}


# ── Результат ─────────────────────────────────────────────────────────────────

@dataclass
class GeoPoint:
    address: str
    lat: float
    lon: float

    def osrm_str(self) -> str:
        return f"{self.lon},{self.lat}"


@dataclass
class LogisticsResult:
    object_address:    str
    supplier_address:  str
    real_distance_km:  Optional[float] = None
    investor_km:       float = INVESTOR_KM
    duration_min:      Optional[float] = None
    hidden_margin_uah: Optional[float] = None   # економія порівняно з інвесторською
    flag_margin_found: bool = False              # реальна < інвесторська
    error:             Optional[str] = None
    geocode_failed:    bool = False

    def summary_text(self) -> str:
        if self.geocode_failed or self.error or self.real_distance_km is None or self.duration_min is None:
            return (
                f"🚚 *Логістика:* Не вдалося розрахувати.\n"
                f"_{self.error or 'Геокодування або побудова маршруту не вдалась'}_"
            )
        margin_label = (
            f"🟢 Прихована маржа: ~{self.hidden_margin_uah:,.0f} грн"
            if self.flag_margin_found
            else "⬜ Логістичної маржі немає"
        )
        return (
            f"🚚 *Логістичний аналіз* [{margin_label}]\n"
            f"📍 Відстань: {self.real_distance_km:.1f} км "
            f"(інвесторська: {self.investor_km:.0f} км)\n"
            f"⏱ Час у дорозі: {self.duration_min:.0f} хв (вантажний авто)\n"
            + (
                f"\n💡 *Пояснення:* Реальне логістичне плече на "
                f"{self.investor_km - self.real_distance_km:.1f} км коротше за інвесторське.\n"
                f"Економія на перевезеннях (~{TRIPS_PER_DAY} рейси/день): "
                f"~{self.hidden_margin_uah:,.0f} грн/день — це ваша прихована маржа."
                if self.flag_margin_found else ""
            )
        )


# ── Калькулятор ───────────────────────────────────────────────────────────────

class LogisticsCalculator:
    """
    Розраховує логістичне плече та приховану маржу.
    """

    def __init__(self, http_client: Optional[httpx.AsyncClient] = None):
        self._client = http_client

    async def analyze(
        self,
        object_address: str,
        supplier_address: str,
        investor_km: float = INVESTOR_KM,
        work_days: int = 60,
    ) -> LogisticsResult:
        """
        object_address:   адреса будівельного об'єкта (з ТД)
        supplier_address: адреса складу/офісу підрядника (з профілю Prozorro)
        investor_km:      відстань у кошторисі (за замовчуванням 30 км)
        work_days:        кількість робочих днів (для розрахунку загальної маржі)
        """
        result = LogisticsResult(
            object_address=object_address,
            supplier_address=supplier_address,
            investor_km=investor_km,
        )

        async with self._get_client() as client:
            # Геокодування
            obj_point = await self._geocode(client, object_address)
            await asyncio.sleep(NOMINATIM_DELAY)
            sup_point = await self._geocode(client, supplier_address)

            if not obj_point or not sup_point:
                result.geocode_failed = True
                result.error = f"Не знайдено координати: {object_address!r} або {supplier_address!r}"
                return result

            # Маршрут OSRM
            await asyncio.sleep(OSRM_DELAY)
            route = await self._get_route(client, obj_point, sup_point)
            if not route:
                result.error = "OSRM не повернув маршрут"
                return result

        result.real_distance_km = route["distance_km"]
        result.duration_min     = route["duration_min"]

        # Розрахунок маржі
        if result.real_distance_km < investor_km:
            saved_km_per_trip  = investor_km - result.real_distance_km
            saved_per_day_uah  = saved_km_per_trip * FUEL_COST_UAH_KM * TRIPS_PER_DAY
            result.hidden_margin_uah = saved_per_day_uah * work_days
            result.flag_margin_found = True

        logger.info(
            f"Logistics: {object_address!r} ↔ {supplier_address!r} "
            f"= {result.real_distance_km:.1f} км (інвесторська {investor_km} км)"
        )
        return result

    async def _geocode(
        self,
        client: httpx.AsyncClient,
        address: str,
    ) -> Optional[GeoPoint]:
        """Геокодує адресу через Nominatim."""
        params = {
            "q":              address,
            "format":         "json",
            "addressdetails": 1,
            "limit":          1,
            "countrycodes":   "ua",
        }
        try:
            resp = await client.get(
                NOMINATIM_URL,
                params=params,
                headers=HEADERS,
                timeout=15.0,
            )
            resp.raise_for_status()
            data = resp.json()
            if not data:
                logger.warning(f"Nominatim: адресу не знайдено: {address!r}")
                return None
            best = data[0]
            return GeoPoint(address=address, lat=float(best["lat"]), lon=float(best["lon"]))
        except Exception as e:
            logger.error(f"Nominatim геокодування помилка ({address!r}): {e}")
            return None

    async def _get_route(
        self,
        client: httpx.AsyncClient,
        origin: GeoPoint,
        destination: GeoPoint,
    ) -> Optional[dict]:
        """Отримує маршрут вантажного авто через OSRM."""
        coords = f"{origin.osrm_str()};{destination.osrm_str()}"
        url = f"{OSRM_URL}/{coords}"
        params = {
            "overview": "false",
            "annotations": "false",
        }
        try:
            resp = await client.get(url, params=params, headers=HEADERS, timeout=20.0)
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != "Ok" or not data.get("routes"):
                logger.warning(f"OSRM помилка: {data.get('code')}")
                return None
            route = data["routes"][0]
            return {
                "distance_km": route["distance"] / 1000,
                "duration_min": route["duration"] / 60,
            }
        except Exception as e:
            logger.error(f"OSRM помилка: {e}")
            return None

    def _get_client(self) -> "_ClientContextManager":
        if self._client is not None:
            return _ClientContextManager(self._client, owned=False)
        return _ClientContextManager(
            httpx.AsyncClient(headers=HEADERS),
            owned=True,
        )


class _ClientContextManager:
    def __init__(self, client: httpx.AsyncClient, owned: bool):
        self._client = client
        self._owned  = owned

    async def __aenter__(self) -> httpx.AsyncClient:
        return self._client

    async def __aexit__(self, *args) -> None:
        if self._owned:
            await self._client.aclose()
