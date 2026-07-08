# ai/agents/__init__.py
from ai.agents.scanner import scan_document, format_report as format_scan
from ai.agents.calculator import calculate_margin, format_calculator_report
from ai.agents.collector import fill_document, fill_all_required_documents
from ai.agents.reviewer import review_package, format_review_report

__all__ = [
    "scan_document", "format_scan",
    "calculate_margin", "format_calculator_report",
    "fill_document", "fill_all_required_documents",
    "review_package", "format_review_report",
]
