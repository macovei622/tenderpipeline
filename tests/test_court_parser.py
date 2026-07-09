"""
tests/test_court_parser.py — Unit тести для CourtParser
"""
import asyncio
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from analytics.court_parser import (
    CourtParser, CourtResult, CourtCase,
    ACTIVE_STATUSES, CLOSED_STATUSES,
)


def run(coro):
    return asyncio.run(coro)


class TestStatusClassification:
    """Тестує класифікацію активних/закритих справ."""

    def test_active_statuses(self):
        for status in ["відкрито", "розглядається", "в провадженні", "призначено"]:
            assert CourtParser._classify_status(status) is True, f"'{status}' має бути активним"

    def test_closed_statuses(self):
        for status in ["закрито", "завершено", "припинено", "виправдано"]:
            assert CourtParser._classify_status(status) is False, f"'{status}' має бути закритим"

    def test_unknown_status_empty(self):
        # Порожній статус → не активна (обережно)
        assert CourtParser._classify_status("") is False

    def test_unknown_status_nonempty(self):
        # Невідомий статус з текстом → вважаємо активним (перестрахування)
        assert CourtParser._classify_status("невідомий статус") is True


class TestArticleDetection:
    """Тестує знаходження статей ККУ в тексті."""

    def test_detect_191(self):
        text = "кримінальне провадження за ст. 191 ккУ за фактом"
        article, desc = CourtParser._detect_article(text)
        assert article == "ст. 191 ККУ"
        assert desc is not None

    def test_detect_368(self):
        text = "стаття 368 кримінального кодексу"
        article, desc = CourtParser._detect_article(text)
        assert article == "ст. 368 ККУ"

    def test_detect_209(self):
        text = "ст.209 легалізація доходів"
        article, desc = CourtParser._detect_article(text)
        assert article == "ст. 209 ККУ"

    def test_no_match(self):
        text = "цивільна справа про стягнення боргу"
        article, desc = CourtParser._detect_article(text)
        assert article is None
        assert desc is None


class TestCourtResultSummary:
    """Тестує генерацію тексту для Telegram."""

    def test_no_data_text(self):
        result = CourtResult(edrpou="12345678", flag_no_data=True)
        text = result.summary_text()
        assert "Даних немає" in text

    def test_no_cases_text(self):
        result = CourtResult(edrpou="12345678", cases_found=0)
        text = result.summary_text()
        assert "не знайдено" in text

    def test_active_case_text(self):
        case = CourtCase(
            case_number="12-3456/2024",
            court_name="Вінницький суд",
            article="ст. 191 ККУ",
            article_desc="Привласнення",
            status_raw="відкрито",
            is_active=True,
        )
        result = CourtResult(
            edrpou="12345678",
            cases_found=1,
            active_cases=[case],
            flag_criminal_risk=True,
        )
        text = result.summary_text()
        assert "КРИМІНАЛЬНИЙ РИЗИК" in text
        assert "12-3456/2024" in text
        assert "ст. 191 ККУ" in text

    def test_closed_case_no_red_flag(self):
        """Закрита справа — НЕ повинна давати КРИМІНАЛЬНИЙ РИЗИК."""
        case = CourtCase(
            case_number="99-9999/2020",
            court_name="Суд",
            article="ст. 368 ККУ",
            article_desc="Хабар",
            status_raw="закрито",
            is_active=False,
        )
        result = CourtResult(
            edrpou="12345678",
            cases_found=1,
            closed_cases=[case],
            flag_criminal_risk=False,
        )
        text = result.summary_text()
        # Має бути "Є закриті справи", але НЕ "КРИМІНАЛЬНИЙ РИЗИК"
        assert "КРИМІНАЛЬНИЙ РИЗИК" not in text

    def test_status_label(self):
        active_case = CourtCase("1", "Суд", None, None, "відкрито", True)
        closed_case = CourtCase("2", "Суд", None, None, "закрито", False)
        assert "АКТИВНА" in active_case.status_label()
        assert "закрита" in closed_case.status_label()


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
