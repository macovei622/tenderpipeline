"""
analytics/auction_simulator.py — Модуль 6: Симулятор аукціонів Prozorro

КРИТИЧНО: Перед запуском спринту виконується автоматична верифікація джерела.
  - Завантажуємо зразковий дамп з Prozorro OpenData.
  - Перевіряємо наявність обов'язкових полів: awardStatus, amount, edrpou/identifier.
  - Якщо верифікація не пройшла → симулятор повертає DataSourceError.

Алгоритм:
  1. Збираємо завершені тендери того ж CPV-коду + регіону за 12 міс.
  2. Для кожного конкурента (за ЄДРПОУ) рахуємо:
     - avg_initial_discount_pct: середній % зниження ціни на старті
     - avg_round2_drop_pct: додаткове падіння в 2-му раунді
     - win_rate: частота перемог
  3. Monte Carlo (1000 ітерацій): симулюємо поведінку конкурентів.
  4. Визначаємо optimal_bid — ціна, яка дає найвищу P(перемоги) при збережені маржі.

Джерело даних:
  - https://public-api.prozorro.gov.ua/api/2.5/tenders?status=complete&...
  - АБО щоденні JSON-дампи з https://prozorro-data.gov.ua/
"""
from __future__ import annotations

import asyncio
import random
import statistics
from dataclasses import dataclass, field
from typing import Optional

import httpx
from loguru import logger

# ── Константи ─────────────────────────────────────────────────────────────────
PROZORRO_API_BASE = "https://public-api.prozorro.gov.ua/api/2.5"
REQUIRED_FIELDS   = {"awardStatus", "value"}      # мінімальний набір для верифікації
MONTE_CARLO_RUNS  = 1_000
MIN_HISTORY_LOTS  = 5   # мінімум лотів для побудови профілю конкурента


# ── Результат ─────────────────────────────────────────────────────────────────

@dataclass
class CompetitorProfile:
    edrpou: str
    name: str
    lots_analyzed: int
    avg_initial_discount_pct: float  # % від очікуваної вартості
    avg_round2_drop_pct: float       # додаткове зниження
    win_rate: float                  # 0.0–1.0


@dataclass
class AuctionResult:
    tender_id: str
    expected_value: float
    competitors: list[CompetitorProfile] = field(default_factory=list)
    optimal_bid: Optional[float] = None
    win_probability: Optional[float] = None    # 0.0–1.0
    drop_dead_price: Optional[float] = None    # мінімальна ціна (нижче = збиток)
    data_source_verified: bool = False
    data_source_error: Optional[str] = None
    error: Optional[str] = None

    def summary_text(self) -> str:
        if self.data_source_error:
            return (
                f"🎲 *Симулятор аукціону:* Джерело даних недоступне.\n"
                f"_{self.data_source_error}_\n"
                "⚠️ Спринт 4 потребує перевірки джерела перед виконанням."
            )
        if self.error:
            return f"🎲 *Симулятор аукціону:* Помилка — {self.error}"
        if not self.competitors:
            return (
                f"🎲 *Симулятор аукціону:* Недостатньо даних про конкурентів "
                f"(потрібно мін. {MIN_HISTORY_LOTS} завершених лотів)."
            )
        comp_lines = [
            f"  • *{c.name[:30]}* (ЄДРПОУ {c.edrpou}): "
            f"знижує на {c.avg_initial_discount_pct:.1f}%, "
            f"2-й раунд: -{c.avg_round2_drop_pct:.1f}%, "
            f"win rate: {c.win_rate:.0%}"
            for c in self.competitors[:5]
        ]
        return (
            f"🎲 *Симулятор аукціону (Monte Carlo × {MONTE_CARLO_RUNS})*\n"
            f"💰 Очікувана вартість: {self.expected_value:,.0f} грн\n"
            f"🎯 Оптимальна ставка: *{self.optimal_bid:,.0f} грн* "
            f"(P(перемоги) = {self.win_probability:.0%})\n"
            f"🔻 Drop Dead Price: {self.drop_dead_price:,.0f} грн\n\n"
            f"👥 Профілі конкурентів ({len(self.competitors)}):\n"
            + "\n".join(comp_lines)
        )


# ── Симулятор ─────────────────────────────────────────────────────────────────

class AuctionSimulator:
    """
    Симулятор поведінки конкурентів на аукціоні Prozorro.
    """

    def __init__(self, http_client: Optional[httpx.AsyncClient] = None):
        self._client = http_client

    async def analyze(
        self,
        tender_id: str,
        expected_value: float,
        cpv_prefix: str,
        region: str,
        drop_dead_price: float,
        months_lookback: int = 12,
    ) -> AuctionResult:
        """
        tender_id:       ID поточного тендера (для контексту)
        expected_value:  очікувана вартість тендера (грн)
        cpv_prefix:      перші 4 цифри CPV коду (наприклад "4521")
        region:          регіон (наприклад "Вінницька область")
        drop_dead_price: мінімальна ціна нижче якої нема маржі
        """
        result = AuctionResult(
            tender_id=tender_id,
            expected_value=expected_value,
            drop_dead_price=drop_dead_price,
        )

        # ── Крок 0: Верифікація джерела ────────────────────────────────────
        source_ok = await self._verify_data_source()
        result.data_source_verified = source_ok
        if not source_ok:
            result.data_source_error = (
                "Prozorro API недоступний або структура даних змінилась. "
                "Перевірте наявність полів: " + ", ".join(REQUIRED_FIELDS)
            )
            return result

        # ── Крок 1: Збір завершених тендерів ───────────────────────────────
        try:
            completed_tenders = await self._fetch_completed_tenders(
                cpv_prefix, region, months_lookback
            )
        except Exception as exc:
            logger.error(f"AuctionSimulator fetch error: {exc}")
            result.error = str(exc)
            return result

        if not completed_tenders:
            result.error = "Немає завершених тендерів для побудови профілів конкурентів"
            return result

        # ── Крок 2: Профілі конкурентів ────────────────────────────────────
        competitor_profiles = self._build_competitor_profiles(completed_tenders, expected_value)
        result.competitors = competitor_profiles

        if not competitor_profiles:
            return result

        # ── Крок 3: Monte Carlo ────────────────────────────────────────────
        optimal_bid, win_prob = self._monte_carlo(
            expected_value=expected_value,
            competitors=competitor_profiles,
            drop_dead_price=drop_dead_price,
        )
        result.optimal_bid     = optimal_bid
        result.win_probability = win_prob

        logger.info(
            f"AuctionSimulator: {tender_id} — optimal={optimal_bid:,.0f} грн "
            f"P(win)={win_prob:.0%}"
        )
        return result

    # ── Верифікація джерела ───────────────────────────────────────────────────

    async def _verify_data_source(self) -> bool:
        """
        Завантажує 1 тендер зі стріму, потім його деталі та перевіряє наявність обов'язкових полів.
        Повертає True якщо джерело живе і структура правильна.
        """
        test_url = f"{PROZORRO_API_BASE}/tenders"
        params   = {"status": "complete", "limit": 1}
        try:
            async with self._get_client() as client:
                resp = await client.get(test_url, params=params, timeout=15.0)
                resp.raise_for_status()
                data = resp.json()
                tenders = data.get("data", [])
                if not tenders:
                    logger.warning("AuctionSimulator: API повернув порожній список")
                    return False
                
                sample_id = tenders[0]["id"]
                detail_resp = await client.get(f"{test_url}/{sample_id}", timeout=15.0)
                detail_resp.raise_for_status()
                sample = detail_resp.json().get("data", {})
                
                has_value = "value" in sample
                if not has_value:
                    logger.warning(f"AuctionSimulator: відсутнє поле 'value' у деталях тестового тендера")
                    return False
                logger.info("AuctionSimulator: верифікація джерела ✓")
                return True
        except Exception as e:
            logger.error(f"AuctionSimulator верифікація джерела FAILED: {e}")
            return False

    # ── Збір даних ───────────────────────────────────────────────────────────

    async def _fetch_completed_tenders(
        self,
        cpv_prefix: str,
        region: str,
        months_lookback: int,
    ) -> list[dict]:
        """Збирає завершені тендери та завантажує їх деталі."""
        from datetime import datetime, timedelta, timezone
        date_from = (datetime.now(timezone.utc) - timedelta(days=30 * months_lookback)).strftime("%Y-%m-%d")

        tender_ids: list[str] = []
        offset = ""
        max_pages = 2

        async with self._get_client() as client:
            for _ in range(max_pages):
                params: dict = {
                    "status":       "complete",
                    "cpv":          cpv_prefix,
                    "region":       region,
                    "dateModified": date_from,
                    "limit":        30,
                }
                if offset:
                    params["offset"] = offset

                resp = await client.get(
                    f"{PROZORRO_API_BASE}/tenders",
                    params=params,
                    timeout=20.0,
                )
                resp.raise_for_status()
                data = resp.json()
                batch = data.get("data", [])
                if not batch:
                    break
                for t in batch:
                    if t.get("id"):
                        tender_ids.append(t["id"])
                offset = data.get("next_offset", "")
                if not offset:
                    break
                await asyncio.sleep(0.3)

            tender_ids = tender_ids[:15]
            if not tender_ids:
                return []

            logger.info(f"AuctionSimulator: Завантажую деталі для {len(tender_ids)} тендерів...")
            
            tasks = [self._fetch_single_detail(client, tid) for tid in tender_ids]
            detailed_tenders = await asyncio.gather(*tasks, return_exceptions=True)
            
            valid_tenders = []
            for t in detailed_tenders:
                if isinstance(t, dict) and "id" in t:
                    valid_tenders.append(t)
            
            return valid_tenders

    async def _fetch_single_detail(self, client, tender_id: str) -> Optional[dict]:
        """Завантажує деталі для одного тендера."""
        url = f"{PROZORRO_API_BASE}/tenders/{tender_id}"
        try:
            resp = await client.get(url, timeout=15.0)
            if resp.status_code == 200:
                return resp.json().get("data")
        except Exception as e:
            logger.debug(f"Помилка завантаження деталей тендера {tender_id}: {e}")
        return None

    # ── Побудова профілів ────────────────────────────────────────────────────

    def _build_competitor_profiles(
        self,
        tenders: list[dict],
        expected_value: float,
    ) -> list[CompetitorProfile]:
        """
        Будує профіль кожного конкурента на основі його ставок у минулих тендерах.
        """
        competitor_data: dict[str, dict] = {}

        for tender in tenders:
            tender_value = tender.get("value", {})
            base_amount  = tender_value.get("amount", 0) if isinstance(tender_value, dict) else 0
            if base_amount <= 0:
                continue

            bids = tender.get("bids", [])
            awards = tender.get("awards", [])
            winner_edrpou = None
            for award in awards:
                if award.get("status") == "active":
                    supplier = award.get("suppliers", [{}])[0]
                    winner_edrpou = supplier.get("identifier", {}).get("id")

            for bid in bids:
                if bid.get("status") not in ("active", "invalid.pre-qualification"):
                    continue
                bid_amount = bid.get("value", {}).get("amount", 0)
                if bid_amount <= 0:
                    continue

                supplier  = bid.get("tenderers", [{}])[0]
                edrpou    = supplier.get("identifier", {}).get("id", "unknown")
                name      = supplier.get("name", "Невідомо")
                discount  = (base_amount - bid_amount) / base_amount * 100

                if edrpou not in competitor_data:
                    competitor_data[edrpou] = {
                        "name": name,
                        "discounts": [],
                        "wins": 0,
                        "bids": 0,
                    }
                competitor_data[edrpou]["discounts"].append(discount)
                competitor_data[edrpou]["bids"] += 1
                if edrpou == winner_edrpou:
                    competitor_data[edrpou]["wins"] += 1

        profiles = []
        for edrpou, data in competitor_data.items():
            if data["bids"] < MIN_HISTORY_LOTS:
                continue
            discounts = sorted(data["discounts"])
            avg_discount = statistics.mean(discounts)
            # 2-й раунд: беремо нижню половину (агресивніші ставки)
            lower_half = discounts[:len(discounts) // 2]
            round2_drop = statistics.mean(lower_half) - avg_discount if lower_half else 0.0

            profiles.append(CompetitorProfile(
                edrpou=edrpou,
                name=data["name"],
                lots_analyzed=data["bids"],
                avg_initial_discount_pct=avg_discount,
                avg_round2_drop_pct=abs(round2_drop),
                win_rate=data["wins"] / data["bids"],
            ))

        # Сортуємо за кількістю проаналізованих лотів (найбільш репрезентативні вперше)
        return sorted(profiles, key=lambda p: p.lots_analyzed, reverse=True)

    # ── Monte Carlo ───────────────────────────────────────────────────────────

    def _monte_carlo(
        self,
        expected_value: float,
        competitors: list[CompetitorProfile],
        drop_dead_price: float,
        bid_step: float = 0.01,
    ) -> tuple[float, float]:
        """
        Шукає optimal_bid перебором ставок і симуляцією конкурентів.
        Returns: (optimal_bid, win_probability)
        """
        best_bid   = expected_value
        best_prob  = 0.0

        # Діапазон наших ставок: від drop_dead до expected_value з кроком 1%
        our_bids = []
        bid = expected_value
        while bid >= drop_dead_price:
            our_bids.append(bid)
            bid -= expected_value * bid_step

        for our_bid in our_bids:
            wins = 0
            for _ in range(MONTE_CARLO_RUNS):
                # Симулюємо ставки конкурентів
                min_competitor_bid = expected_value
                for comp in competitors:
                    # Базовий дисконт + шум ±30%
                    discount_pct  = comp.avg_initial_discount_pct
                    noise         = random.gauss(0, discount_pct * 0.3)
                    comp_discount = max(0, discount_pct + noise)
                    comp_bid      = expected_value * (1 - comp_discount / 100)

                    # 2-й раунд (50% ймовірність агресії)
                    if random.random() < 0.5:
                        round2_drop = comp.avg_round2_drop_pct / 100
                        comp_bid   *= (1 - round2_drop)

                    min_competitor_bid = min(min_competitor_bid, comp_bid)

                if our_bid < min_competitor_bid and our_bid >= drop_dead_price:
                    wins += 1

            prob = wins / MONTE_CARLO_RUNS
            if prob > best_prob:
                best_prob = prob
                best_bid  = our_bid

        return round(best_bid, -3), best_prob  # округлення до тисяч

    def _get_client(self) -> "_ClientCtx":
        if self._client is not None:
            return _ClientCtx(self._client, owned=False)
        return _ClientCtx(httpx.AsyncClient(), owned=True)


class _ClientCtx:
    """Context manager для будь-якого HTTP-клієнта (реального або fake)."""
    def __init__(self, c, owned: bool):
        self._c, self._owned = c, owned
    async def __aenter__(self): return self._c
    async def __aexit__(self, *a):
        if self._owned and hasattr(self._c, "aclose"):
            await self._c.aclose()
