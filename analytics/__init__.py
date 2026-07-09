"""
analytics/__init__.py

Ізольований пакет аналітичних модулів ProzorroAI.
Підключається поверх існуючого конвеєра як опціональний шар.
"""
from .spending       import SpendingAnalyzer, SpendingResult
from .court_parser   import CourtParser, CourtResult
from .cpm_engine     import CPMEngine, CPMResult
from .logistics      import LogisticsCalculator, LogisticsResult
from .auction_simulator import AuctionSimulator, AuctionResult

__all__ = [
    "SpendingAnalyzer",   "SpendingResult",
    "CourtParser",        "CourtResult",
    "CPMEngine",          "CPMResult",
    "LogisticsCalculator","LogisticsResult",
    "AuctionSimulator",   "AuctionResult",
]
