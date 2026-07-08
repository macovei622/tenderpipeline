"""
ai/ocr_pipeline.py — Комбінований OCR-пайплайн

Три шари обробки документів:
  1. PyMuPDF — витягує текстовий шар (швидко, безкоштовно)
  2. pdfplumber — витягує таблиці зі структурою (критично для специфікацій)
  3. Tesseract OCR — розпізнає скановані сторінки

Coverage metric: якщо < 90% сторінок покриті текстом — додаємо попередження у звіт.

Детерміновані rule-based перевірки (НЕ LLM):
  - ЄДРПОУ (8 цифр)
  - CPV-коди (8 цифр + -7)
  - Дати у форматі дд.мм.рррр
  - Суми у гривнях
"""
from __future__ import annotations

import re
import tempfile
import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from loguru import logger


# ─── Результат парсингу сторінки ─────────────────────────────────────────────

@dataclass
class PageResult:
    page_num:    int
    text:        str
    tables:      list[list[list[str]]] = field(default_factory=list)
    is_scanned:  bool = False
    ocr_applied: bool = False
    char_count:  int = 0

    def __post_init__(self):
        self.char_count = len(self.text)


@dataclass
class ParsedDocument:
    pages:           list[PageResult]
    total_pages:     int
    scanned_pages:   list[int]
    coverage_pct:    float          # % сторінок з розпізнаним текстом
    all_text:        str            # Склеєний текст усіх сторінок
    all_tables:      list[dict]     # [{page, table_md}]
    metadata:        dict           # Детерміновані поля: ЄДРПОУ, CPV, дати, суми
    warnings:        list[str]


# ─── Rule-Based Extractors (детермінований код, не LLM) ──────────────────────

_RE_EDRPOU  = re.compile(r'\b\d{8}\b')
_RE_CPV     = re.compile(r'\b\d{8}-\d\b')
_RE_DATE_UA = re.compile(r'\b(\d{2})\.(\d{2})\.(\d{4})\b')
_RE_AMOUNT  = re.compile(r'\b(\d[\d\s]*[\d])[,.](\d{2})\s*(грн|UAH|гривень)\b', re.I)
_RE_PROCENT = re.compile(r'\b(\d{1,3})\s*%')


def extract_structured_fields(text: str) -> dict:
    """
    Детерміноване вилучення структурованих полів із тексту.
    Жоден LLM тут не потрібен.
    """
    edrpous = list(set(_RE_EDRPOU.findall(text)))
    cpv_codes = list(set(_RE_CPV.findall(text)))
    dates     = list(set(_RE_DATE_UA.findall(text)))
    amounts   = _RE_AMOUNT.findall(text)

    # Перевірка ЄДРПОУ: 8 цифр, не все нулі
    valid_edrpous = [e for e in edrpous if e != "00000000"]

    return {
        "edrpou_codes": valid_edrpous[:5],   # топ-5 (може бути замовник + учасник)
        "cpv_codes":    cpv_codes,
        "dates":        [f"{d[0]}.{d[1]}.{d[2]}" for d in dates[:20]],
        "amounts_uah":  [f"{a[0].replace(' ','')}.{a[1]}" for a in amounts[:10]],
        "has_construction_cpv": any(c.startswith("45") for c in cpv_codes),
    }


def table_to_markdown(table: list[list]) -> str:
    """Перетворити таблицю (list of lists) у Markdown рядок."""
    if not table or not table[0]:
        return ""
    rows = []
    for i, row in enumerate(table):
        cells = " | ".join(str(c or "").strip() for c in row)
        rows.append(f"| {cells} |")
        if i == 0:
            rows.append("|" + "|".join(" --- " for _ in row) + "|")
    return "\n".join(rows)


# ─── Основний парсер ─────────────────────────────────────────────────────────

class OCRPipeline:
    """
    Комбінований пайплайн обробки PDF.
    """

    SCANNED_THRESHOLD = 50      # < 50 символів → сторінка вважається скано.
    COVERAGE_MIN_PCT  = 90.0    # < 90% → попередження в звіті

    def __init__(self, use_ocr: bool = True):
        self.use_ocr = use_ocr
        self._tesseract_available = self._check_tesseract()
        self._easyocr_available   = self._check_easyocr()
        self._plumber_available   = self._check_pdfplumber()
        self._easyocr_reader      = None  # ледаче ініціалізування

    @staticmethod
    def _check_tesseract() -> bool:
        """Перевіряє наявність pytesseract + системного бінарника tesseract."""
        try:
            import pytesseract
            pytesseract.get_tesseract_version()  # перевіряє бінарник
            return True
        except Exception:
            logger.warning("Tesseract бінарник не знайдено — спробуємо EasyOCR")
            return False

    @staticmethod
    def _check_easyocr() -> bool:
        """EasyOCR — чисто Python OCR, не потребує системного бінарника."""
        try:
            import easyocr  # noqa
            return True
        except ImportError:
            logger.warning("easyocr не встановлено — OCR сканів недоступний")
            return False

    @staticmethod
    def _check_pdfplumber() -> bool:
        try:
            import pdfplumber  # noqa
            return True
        except ImportError:
            logger.warning("pdfplumber не встановлено — таблиці не будуть витягнуті")
            return False

    async def parse(self, pdf_path: str) -> ParsedDocument:
        """
        Основний метод. Повертає ParsedDocument із текстом, таблицями та метрикою.
        """
        import fitz  # PyMuPDF

        path = Path(pdf_path)
        if not path.exists():
            raise FileNotFoundError(f"PDF не знайдено: {pdf_path}")

        logger.info(f"📄 Парсинг документа: {path.name}")
        doc = fitz.open(str(path))
        total_pages = doc.page_count

        pages: list[PageResult] = []
        all_tables: list[dict]  = []
        scanned_pages: list[int] = []

        # ── Шар 1: PyMuPDF — текстовий шар ─────────────────────────────────
        for page_num in range(total_pages):
            page = doc[page_num]
            text = page.get_text("text") or ""
            is_scanned = len(text.strip()) < self.SCANNED_THRESHOLD
            if is_scanned:
                scanned_pages.append(page_num + 1)
            pages.append(PageResult(
                page_num   = page_num + 1,
                text       = text,
                is_scanned = is_scanned,
            ))

        doc.close()

        # ── Шар 2: pdfplumber — таблиці ─────────────────────────────────────
        if self._plumber_available:
            all_tables = self._extract_tables(pdf_path, total_pages)
            # Вставляємо текст таблиць у відповідні сторінки
            for tbl in all_tables:
                pg = tbl["page"] - 1
                if 0 <= pg < len(pages):
                    pages[pg].text += "\n\n[ТАБЛИЦЯ]\n" + tbl["table_md"]
                    pages[pg].tables.append(tbl.get("raw", []))

        # ── Шар 3: Tesseract — OCR сканованих сторінок ──────────────────────
        if self.use_ocr and self._tesseract_available and scanned_pages:
            logger.info(f"🔍 OCR для {len(scanned_pages)} скано-сторінок...")
            ocr_texts = self._ocr_scanned(pdf_path, scanned_pages)
            for pg_num, ocr_text in ocr_texts.items():
                idx = pg_num - 1
                if 0 <= idx < len(pages):
                    pages[idx].text       = ocr_text
                    pages[idx].ocr_applied = True

        # ── Coverage Metric ──────────────────────────────────────────────────
        covered = sum(1 for p in pages if len(p.text.strip()) >= self.SCANNED_THRESHOLD)
        coverage_pct = round(covered / total_pages * 100, 1) if total_pages else 0.0

        all_text = "\n\n".join(
            f"[С.{p.page_num}]\n{p.text}" for p in pages
        )

        # ── Детерміновані поля ───────────────────────────────────────────────
        metadata = extract_structured_fields(all_text)

        # ── Попередження ────────────────────────────────────────────────────
        warnings: list[str] = []
        if coverage_pct < self.COVERAGE_MIN_PCT:
            uncovered = [p.page_num for p in pages
                         if len(p.text.strip()) < self.SCANNED_THRESHOLD]
            warnings.append(
                f"⚠️ УВАГА: тільки {coverage_pct}% документа розпізнано текстово. "
                f"Сторінки без покриття: {uncovered[:20]}. "
                "Рекомендується ручна перевірка цих сторінок перед поданням заявки."
            )
        if not metadata["has_construction_cpv"]:
            warnings.append("ℹ️ CPV-коди будівництва (45xxxxxx) у документі не виявлено.")
        if not metadata["edrpou_codes"]:
            warnings.append("⚠️ ЄДРПОУ замовника у документі не виявлено — перевірте вручну.")

        result = ParsedDocument(
            pages          = pages,
            total_pages    = total_pages,
            scanned_pages  = scanned_pages,
            coverage_pct   = coverage_pct,
            all_text       = all_text,
            all_tables     = all_tables,
            metadata       = metadata,
            warnings       = warnings,
        )

        logger.info(
            f"✅ Документ розпізнано: {total_pages} сторінок | "
            f"покриття={coverage_pct}% | таблиць={len(all_tables)} | "
            f"сканів={len(scanned_pages)}"
        )
        return result

    # ── Допоміжні методи ─────────────────────────────────────────────────────

    def _extract_tables(self, pdf_path: str, total_pages: int) -> list[dict]:
        """pdfplumber: витягуємо таблиці та конвертуємо у Markdown."""
        import pdfplumber
        tables = []
        try:
            with pdfplumber.open(pdf_path) as pdf:
                for i, page in enumerate(pdf.pages):
                    raw_tables = page.extract_tables() or []
                    for raw in raw_tables:
                        md = table_to_markdown(raw)
                        if md:
                            tables.append({
                                "page":     i + 1,
                                "table_md": md,
                                "raw":      raw,
                            })
        except Exception as exc:
            logger.warning(f"pdfplumber помилка: {exc}")
        return tables

    def _ocr_scanned(self, pdf_path: str, pages_to_ocr: list[int]) -> dict[int, str]:
        """
        OCR для сканованих сторінок.
        Пріоритет: Tesseract → EasyOCR → порожній результат.
        """
        if self._tesseract_available:
            return self._ocr_tesseract(pdf_path, pages_to_ocr)
        elif self._easyocr_available:
            return self._ocr_easyocr(pdf_path, pages_to_ocr)
        else:
            logger.warning("Жоден OCR-рушій недоступний — скановані сторінки пропущено")
            return {}

    def _ocr_tesseract(self, pdf_path: str, pages_to_ocr: list[int]) -> dict[int, str]:
        """OCR через Tesseract (потребує системного бінарника)."""
        results: dict[int, str] = {}
        try:
            import pytesseract
            from pdf2image import convert_from_path
            images = convert_from_path(pdf_path, dpi=300,
                first_page=min(pages_to_ocr), last_page=max(pages_to_ocr))
            for idx, pg_num in enumerate(range(min(pages_to_ocr), max(pages_to_ocr)+1)):
                if pg_num in pages_to_ocr and idx < len(images):
                    results[pg_num] = pytesseract.image_to_string(images[idx], lang="ukr+rus+eng")
        except Exception as exc:
            logger.warning(f"Tesseract OCR помилка: {exc}")
        return results

    def _preprocess_image_cv(self, pil_img) -> Any:
        """
        Попередня обробка зображення через OpenCV:
        1. Вирівнювання кута нахилу (deskewing)
        2. Видалення синіх/фіолетових штампів та печаток
        3. Підвищення контрасту та бінаризація
        """
        import cv2
        import numpy as np
        from PIL import Image

        # Конвертуємо PIL Image в OpenCV BGR
        img_np = np.array(pil_img.convert("RGB"))
        img = cv2.cvtColor(img_np, cv2.COLOR_RGB2BGR)

        # ── 1. Вирівнювання (deskewing) ──
        try:
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            # Інвертований поріг
            thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
            # Знаходимо координати всіх текстових точок
            pts = np.column_stack(np.where(thresh > 0))
            if len(pts) > 0:
                angle = cv2.minAreaRect(pts)[-1]
                # minAreaRect повертає кути в різних діапазонах
                if angle < -45:
                    angle = -(90 + angle)
                else:
                    angle = -angle
                
                # Повертаємо лише якщо кут значущий
                if 0.5 < abs(angle) < 15:
                    (h, w) = img.shape[:2]
                    center = (w // 2, h // 2)
                    M = cv2.getRotationMatrix2D(center, angle, 1.0)
                    img = cv2.warpAffine(img, M, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
        except Exception as e:
            logger.warning(f"Помилка deskewing: {e}")

        # ── 2. Стирання синіх/фіолетових печаток (color masking) ──
        try:
            hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
            # Діапазон синього та фіолетового в HSV
            lower_blue = np.array([85, 40, 40])
            upper_blue = np.array([165, 255, 255])
            mask = cv2.inRange(hsv, lower_blue, upper_blue)
            # Зафарбовуємо маску білим кольором
            img[mask > 0] = [255, 255, 255]
        except Exception as e:
            logger.warning(f"Помилка color masking: {e}")

        # ── 3. Бінаризація для чіткості тексту ──
        try:
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            # Адаптивний поріг
            processed = cv2.adaptiveThreshold(
                gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 2
            )
            return Image.fromarray(processed)
        except Exception as e:
            logger.warning(f"Помилка binarization: {e}")
            return pil_img

    async def _ocr_vision_fallback(self, pil_img) -> str:
        """Fallback на LLM Vision (Gemini 2.0 Flash) для складних зображень."""
        import io
        import base64
        from ai.client import call_model

        try:
            buffered = io.BytesIO()
            pil_img.save(buffered, format="JPEG", quality=85)
            img_base64 = base64.b64encode(buffered.getvalue()).decode("utf-8")

            messages = [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Перепиши весь текст з цього зображення документа Prozorro. "
                                "Збережи структуру таблиць, якщо вони присутні. "
                                "Поверни тільки розпізнаний текст, без жодних коментарів."
                            )
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{img_base64}"
                            }
                        }
                    ]
                }
            ]
            
            logger.info("🔮 OCR провалився. Запускаємо LLM Vision Fallback через OpenRouter...")
            res = await call_model("screener", messages, json_mode=False, fallback_enabled=True)
            if res and res[0]:
                logger.info(f"🔮 LLM Vision успішно розпізнав {len(res[0])} символів")
                return res[0]
        except Exception as exc:
            logger.warning(f"Помилка LLM Vision Fallback: {exc}")
        return ""

    def _ocr_easyocr(self, pdf_path: str, pages_to_ocr: list[int]) -> dict[int, str]:
        """OCR через EasyOCR — чисто Python, не потребує системного бінарника."""
        results: dict[int, str] = {}
        try:
            import easyocr
            from pdf2image import convert_from_path
            if self._easyocr_reader is None:
                logger.info("⏳ Завантаження EasyOCR моделей (перший запуск ~30с)...")
                self._easyocr_reader = easyocr.Reader(["uk", "ru", "en"], gpu=False, verbose=False)
            images = convert_from_path(pdf_path, dpi=200,
                first_page=min(pages_to_ocr), last_page=max(pages_to_ocr))
            for idx, pg_num in enumerate(range(min(pages_to_ocr), max(pages_to_ocr)+1)):
                if pg_num in pages_to_ocr and idx < len(images):
                    img = images[idx]
                    # OpenCV препроцесинг
                    img_processed = self._preprocess_image_cv(img)
                    
                    detections = self._easyocr_reader.readtext(img_processed, detail=0, paragraph=True)
                    text = "\n".join(detections)
                    
                    # Перевіряємо якість OCR
                    if len(text.strip()) < 50:
                        # Запускаємо асинхронний fallback у синхронному методі через event loop
                        loop = asyncio.get_event_loop()
                        fallback_text = loop.run_until_complete(self._ocr_vision_fallback(img))
                        if fallback_text:
                            text = fallback_text

                    results[pg_num] = text
        except Exception as exc:
            logger.warning(f"EasyOCR помилка: {exc}")
        return results


# ─── Singleton ───────────────────────────────────────────────────────────────

_pipeline: Optional[OCRPipeline] = None


def get_ocr_pipeline(use_ocr: bool = True) -> OCRPipeline:
    global _pipeline
    if _pipeline is None:
        _pipeline = OCRPipeline(use_ocr=use_ocr)
    return _pipeline
