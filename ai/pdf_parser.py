"""
ai/pdf_parser.py — Парсер PDF-документів тендерів

Використовує PyMuPDF (fitz) — найкращий вибір для:
- Швидкості (в 10-20 разів швидший за pdfminer)
- Підтримки кирилиці (Ukrainian PDF encoding)
- Збереження структури тексту (абзаци, розділи)

Для сканів (зображень у PDF) — fallback на Gemini Vision.
"""
import io
import os
import re
from typing import Optional
from pathlib import Path
from loguru import logger

try:
    import fitz  # PyMuPDF
    PYMUPDF_AVAILABLE = True
except ImportError:
    PYMUPDF_AVAILABLE = False
    logger.warning("PyMuPDF не встановлено. Встанови: pip install PyMuPDF")


def extract_text_from_pdf(file_path: str) -> Optional[str]:
    """
    Витягує текст з PDF-файлу.
    
    Логіка:
    1. Спочатку пробуємо витягти текстовий шар (для нормальних PDF)
    2. Якщо текст порожній (сканований документ) — повертаємо None з позначкою
    
    Returns:
        str: витягнутий текст
        None: якщо PDF порожній або помилка
    """
    if not PYMUPDF_AVAILABLE:
        logger.error("PyMuPDF не встановлено!")
        return None
    
    if not os.path.exists(file_path):
        logger.error(f"Файл не знайдено: {file_path}")
        return None
    
    try:
        doc = fitz.open(file_path)
        pages_text = []
        scanned_pages = 0
        
        for page_num in range(len(doc)):
            page = doc[page_num]
            text = page.get_text("text")
            
            # Перевіряємо чи це скан (мало тексту при великій сторінці)
            if len(text.strip()) < 50:
                scanned_pages += 1
                pages_text.append(f"\n[СТОРІНКА {page_num + 1}: можливо скан, текст не розпізнано]\n")
            else:
                pages_text.append(f"\n--- СТОРІНКА {page_num + 1} ---\n{text}")
        
        doc.close()
        
        full_text = "\n".join(pages_text)
        total_pages = len(pages_text)
        
        logger.info(
            f"📄 PDF розпізнано: {total_pages} стор., "
            f"{scanned_pages} сканованих, "
            f"{len(full_text):,} символів"
        )
        
        if scanned_pages > 0:
            logger.warning(
                f"⚠️ {scanned_pages}/{total_pages} сторінок — скани. "
                f"Потрібна ручна перевірка цих сторінок."
            )
        
        # Якщо весь документ — скани, неможливо аналізувати
        if scanned_pages == total_pages:
            logger.error("❌ Весь документ є сканом — AI-аналіз неможливий без OCR")
            return None
        
        return full_text
    
    except Exception as e:
        logger.error(f"❌ Помилка читання PDF: {e}")
        return None


def split_into_sections(text: str) -> dict[str, str]:
    """
    Ділить текст ТД на логічні секції для паралельного аналізу.
    
    Шукає типові заголовки в тендерній документації Prozorro.
    Якщо не знаходить — повертає весь текст як одну секцію.
    
    Returns:
        {"Кваліфікаційні критерії": "...", "Проект договору": "...", ...}
    """
    # Типові заголовки в українських тендерних документах
    SECTION_MARKERS = [
        # Кваліфікація
        r"кваліфікаційн[іих]+ критер",
        r"вимоги до учасник",
        r"технічн[іих]+ вимог",
        # Договір
        r"проект договор",
        r"істотн[іих]+ умов",
        # ТЗ
        r"технічн[ае]+ завдання",
        r"технічн[а]? специфікац",
        r"дефектний акт",
        # Ціна
        r"розрахунок ціни",
        r"кошторисн",
    ]
    
    sections = {}
    current_section = "Загальна частина"
    current_text = []
    
    for line in text.split("\n"):
        line_lower = line.lower().strip()
        matched_section = None
        
        for marker in SECTION_MARKERS:
            if re.search(marker, line_lower):
                matched_section = line.strip()[:80]  # Назва розділу
                break
        
        if matched_section:
            # Зберігаємо попередню секцію
            if current_text:
                sections[current_section] = "\n".join(current_text)
            current_section = matched_section
            current_text = [line]
        else:
            current_text.append(line)
    
    # Зберігаємо останню секцію
    if current_text:
        sections[current_section] = "\n".join(current_text)
    
    # Якщо не вдалося розділити — повертаємо весь текст
    if len(sections) <= 1:
        logger.info("📑 Розділення на секції не вдалося — аналізуємо весь текст")
        return {"Повний текст ТД": text}
    
    logger.info(f"📑 Документ розділено на {len(sections)} секцій: {list(sections.keys())}")
    return sections


def extract_pdf_metadata(file_path: str) -> dict:
    """
    Отримує метадані PDF (автор, дата, кількість сторінок).
    """
    if not PYMUPDF_AVAILABLE:
        return {}
    try:
        doc = fitz.open(file_path)
        meta = doc.metadata
        pages = len(doc)
        doc.close()
        return {
            "pages": pages,
            "title": meta.get("title", ""),
            "author": meta.get("author", ""),
            "creation_date": meta.get("creationDate", ""),
        }
    except Exception:
        return {}
