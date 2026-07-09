"""
analytics/court_parser.py — Модуль 2а: Парсер судових справ court.gov.ua

Ключове технічне рішення (проти false positives):
  - Аналізуємо СТАН провадження. Повідомляємо тільки АКТИВНІ справи.
  - Закриті/завершені справи показуємо окремо зі статусом "(закрита)".
  - Шукаємо статті ККУ: 191, 368 (корупція), 209 (відмивання).

Архітектура:
  - Використовує aiohttp для асинхронних HTTP запитів.
  - BeautifulSoup для парсингу HTML.
  - Playwright — ТІЛЬКИ як fallback якщо HTML-парсинг не дав результатів.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

import aiohttp
from bs4 import BeautifulSoup
from loguru import logger

# ── Константи ──────────────────────────────────────────────────────────────
COURT_SEARCH_URL = "https://court.gov.ua/sudytax/"
COURT_API_URL    = "https://court.gov.ua/api/v1/court-proceedings/search"

# Статті, які нас цікавлять
TARGET_ARTICLES  = {
    "191": "Привласнення, розтрата майна або заволодіння ним шляхом зловживання",
    "368": "Прийняття пропозиції, обіцянки або одержання неправомірної вигоди",
    "209": "Легалізація (відмивання) майна, одержаного злочинним шляхом",
    "364": "Зловживання владою або службовим становищем",
}

# Стани, які вважаються АКТИВНИМИ (провадження не завершено)
ACTIVE_STATUSES  = {
    "відкрито",
    "розглядається",
    "призначено",
    "слухається",
    "не закрито",
    "в провадженні",
}

# Стани, які вважаються ЗАКРИТИМИ
CLOSED_STATUSES  = {
    "закрито",
    "завершено",
    "припинено",
    "відмовлено",
    "виправдано",
    "закрите",
    "завершене",
}

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; ProzorroAI/2.0; +https://prozorro.gov.ua)",
    "Accept-Language": "uk-UA,uk;q=0.9",
}


# ── Моделі ─────────────────────────────────────────────────────────────────

@dataclass
class CourtCase:
    case_number: str
    court_name: str
    article: Optional[str]         # "ст. 368 ККУ"
    article_desc: Optional[str]    # короткий опис
    status_raw: str                # оригінальний текст статусу
    is_active: bool                # True = не закрита
    url: Optional[str] = None
    date: Optional[str] = None

    def status_label(self) -> str:
        return "🔴 АКТИВНА" if self.is_active else "⬜ закрита"


@dataclass
class CourtResult:
    edrpou: str
    cases_found: int = 0
    active_cases: list[CourtCase] = field(default_factory=list)
    closed_cases: list[CourtCase] = field(default_factory=list)
    flag_criminal_risk: bool = False   # True якщо є хоч одна активна справа
    error: Optional[str] = None
    flag_no_data: bool = False

    def summary_text(self) -> str:
        """Текстове резюме для Telegram."""
        if self.flag_no_data or self.error:
            return (
                f"⚖️ *Судовий реєстр:* Даних немає або помилка.\n"
                f"_{self.error or 'Немає справ за ЄДРПОУ'}_"
            )
        if self.cases_found == 0:
            return "⚖️ *Судовий реєстр:* 🟢 Кримінальних проваджень не знайдено."

        lines = [
            f"⚖️ *Судовий реєстр* {'🔴 КРИМІНАЛЬНИЙ РИЗИК' if self.flag_criminal_risk else '🟡 Є закриті справи'}",
            f"Знайдено справ: {self.cases_found} (активних: {len(self.active_cases)}, закритих: {len(self.closed_cases)})",
        ]
        for case in self.active_cases[:3]:
            lines.append(
                f"\n🔴 *{case.case_number}* ({case.date or '?'})\n"
                f"   Суд: {case.court_name[:50]}\n"
                f"   Стаття: {case.article or 'невизначено'}\n"
                f"   Статус: {case.status_raw}"
            )
        if len(self.active_cases) > 3:
            lines.append(f"   _...та ще {len(self.active_cases) - 3} активних справ_")
        return "\n".join(lines)


# ── Парсер ─────────────────────────────────────────────────────────────────

class CourtParser:
    """
    Парсер судового реєстру court.gov.ua.
    Спочатку пробує офіційний API, потім HTML-парсинг.
    """

    def __init__(self, session: Optional[aiohttp.ClientSession] = None):
        self._session = session  # для ін'єкції mock у тестах

    async def analyze(self, edrpou: str) -> CourtResult:
        """Головний метод пошуку судових справ за ЄДРПОУ."""
        result = CourtResult(edrpou=edrpou)
        try:
            cases = await self._search_cases(edrpou)
        except Exception as exc:
            logger.error(f"CourtParser error for {edrpou}: {exc}")
            result.error = str(exc)
            result.flag_no_data = True
            return result

        if not cases:
            result.flag_no_data = True
            return result

        result.cases_found = len(cases)
        for case in cases:
            if case.is_active:
                result.active_cases.append(case)
            else:
                result.closed_cases.append(case)

        result.flag_criminal_risk = len(result.active_cases) > 0
        logger.info(
            f"CourtParser: {edrpou} — {result.cases_found} справ "
            f"({len(result.active_cases)} активних)"
        )
        return result

    # ── Приватні методи ────────────────────────────────────────────────────

    async def _search_cases(self, edrpou: str) -> list[CourtCase]:
        """Пошук через JSON API court.gov.ua."""
        cases: list[CourtCase] = []

        payload = {
            "query":  edrpou,
            "type":   "criminal",
            "offset": 0,
            "limit":  50,
        }

        async with self._get_session() as session:
            try:
                async with session.post(
                    COURT_API_URL,
                    json=payload,
                    headers=HEADERS,
                    timeout=REQUEST_TIMEOUT,
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        raw_cases = data.get("data", data.get("results", []))
                        for item in raw_cases:
                            case = self._parse_case_item(item)
                            if case:
                                cases.append(case)
                        return cases
                    else:
                        logger.warning(
                            f"Court API повернув {resp.status}. "
                            "Fallback на HTML-парсинг."
                        )
            except Exception as e:
                logger.warning(f"Court API недоступний: {e}. Fallback на HTML-парсинг.")

        # ── Fallback: HTML пошук ───────────────────────────────────────────
        return await self._search_html(edrpou, session if self._session else None)

    async def _search_html(
        self,
        edrpou: str,
        session: Optional[aiohttp.ClientSession],
    ) -> list[CourtCase]:
        """HTML-парсинг публічного пошуку."""
        cases: list[CourtCase] = []
        params = {"q": edrpou, "type": "criminal"}

        async with self._get_session() as sess:
            try:
                async with sess.get(
                    COURT_SEARCH_URL,
                    params=params,
                    headers=HEADERS,
                    timeout=REQUEST_TIMEOUT,
                ) as resp:
                    if resp.status != 200:
                        return cases
                    html = await resp.text()
            except Exception as e:
                logger.error(f"CourtParser HTML fallback помилка: {e}")
                return cases

        soup = BeautifulSoup(html, "html.parser")
        rows = soup.select("table.search-results tr, .case-row, .case-item")
        for row in rows:
            case = self._parse_html_row(row)
            if case:
                cases.append(case)
        return cases

    def _parse_case_item(self, item: dict) -> Optional[CourtCase]:
        """Парсинг одного запису JSON API."""
        try:
            number    = item.get("caseNumber") or item.get("number") or ""
            court     = item.get("court", {})
            court_name= court.get("name", "") if isinstance(court, dict) else str(court)
            status    = (item.get("status") or item.get("stage") or "").lower()
            text      = (item.get("text") or item.get("description") or "").lower()
            date      = item.get("date") or item.get("registerDate") or ""
            url       = item.get("url") or ""

            article, desc = self._detect_article(text + " " + number)
            is_active = self._classify_status(status)

            return CourtCase(
                case_number=number,
                court_name=court_name,
                article=article,
                article_desc=desc,
                status_raw=status,
                is_active=is_active,
                url=url,
                date=date[:10] if date else None,
            )
        except Exception as e:
            logger.debug(f"Не вдалося розпарсити запис суду: {e}")
            return None

    def _parse_html_row(self, row) -> Optional[CourtCase]:
        """Парсинг рядка HTML-таблиці."""
        try:
            text_full = row.get_text(" ", strip=True).lower()
            if not text_full:
                return None

            number_el = row.select_one(".case-number, td:first-child")
            number    = number_el.get_text(strip=True) if number_el else "?"

            court_el  = row.select_one(".court-name, td:nth-child(2)")
            court     = court_el.get_text(strip=True) if court_el else "?"

            status_el = row.select_one(".case-status, td:last-child")
            status    = status_el.get_text(strip=True).lower() if status_el else ""

            article, desc = self._detect_article(text_full)
            if not article:
                return None  # нас цікавлять тільки кримінальні справи за цільовими статтями

            link = row.select_one("a[href]")
            url  = link["href"] if link else None
            if url and url.startswith("/"):
                url = "https://court.gov.ua" + url

            return CourtCase(
                case_number=number,
                court_name=court,
                article=article,
                article_desc=desc,
                status_raw=status,
                is_active=self._classify_status(status),
                url=url,
            )
        except Exception:
            return None

    @staticmethod
    def _detect_article(text: str) -> tuple[Optional[str], Optional[str]]:
        """Знаходить статтю ККУ в тексті. Повертає (назва, опис)."""
        for art_num, desc in TARGET_ARTICLES.items():
            pattern = rf"\b(?:ст\.?\s*|стаття\s*){art_num}\b"
            if re.search(pattern, text, re.IGNORECASE):
                return f"ст. {art_num} ККУ", desc
        return None, None

    @staticmethod
    def _classify_status(status: str) -> bool:
        """True якщо справа активна (не закрита)."""
        status_lower = status.lower()
        for closed_kw in CLOSED_STATUSES:
            if closed_kw in status_lower:
                return False
        for active_kw in ACTIVE_STATUSES:
            if active_kw in status_lower:
                return True
        # Якщо статус невизначений — перестраховуємось і вважаємо активною
        return True if status_lower else False

    def _get_session(self) -> "_SessionContextManager":
        if self._session is not None:
            return _SessionContextManager(self._session, owned=False)
        return _SessionContextManager(
            aiohttp.ClientSession(headers=HEADERS),
            owned=True,
        )


class _SessionContextManager:
    def __init__(self, session: aiohttp.ClientSession, owned: bool):
        self._session = session
        self._owned   = owned

    async def __aenter__(self) -> aiohttp.ClientSession:
        return self._session

    async def __aexit__(self, *args) -> None:
        if self._owned:
            await self._session.close()
