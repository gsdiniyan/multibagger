"""Diamond structured exception hierarchy.

All domain exceptions inherit from DiamondError so callers can
catch broadly or narrowly as needed.
"""

from __future__ import annotations


class DiamondError(Exception):
    """Base exception for all diamond errors."""


# --- Data layer ---


class DataError(DiamondError):
    """Error fetching or processing market data."""


class PriceDownloadError(DataError):
    """Failed to download price data after retries."""

    def __init__(self, tickers: list[str], reason: str | None = None):
        self.tickers = tickers
        msg = f"Price download failed for {tickers}"
        if reason:
            msg += f": {reason}"
        super().__init__(msg)


class EmptyDataError(DataError):
    """Data source returned empty results."""

    def __init__(self, tickers: list[str], period: str = ""):
        self.tickers = tickers
        msg = f"Empty data returned for {tickers}"
        if period:
            msg += f" ({period})"
        super().__init__(msg)


class InsufficientDataError(DataError):
    """Not enough data points for analysis."""

    def __init__(self, ticker: str, got: int, need: int = 0):
        self.ticker = ticker
        self.got = got
        self.need = need
        msg = f"Insufficient data for {ticker} ({got} days"
        if need:
            msg += f", need {need}"
        msg += ")"
        super().__init__(msg)


# --- Execution layer ---


class ExecutionError(DiamondError):
    """Error during trade execution."""


class InsufficientCashError(ExecutionError):
    """Not enough cash to execute a trade."""

    def __init__(self, required: float, available: float):
        self.required = required
        self.available = available
        super().__init__(f"Insufficient cash: need {required:,.0f}, have {available:,.0f}")


class RiskGateError(ExecutionError):
    """Risk gate blocked execution."""

    def __init__(self, signals: list[str]):
        self.signals = signals
        super().__init__(f"Risk gate blocked: {', '.join(signals)}")


class BrokerError(ExecutionError):
    """Error communicating with broker (Kite)."""

    def __init__(self, operation: str, reason: str):
        self.operation = operation
        super().__init__(f"Broker error during {operation}: {reason}")


class AuthenticationError(BrokerError):
    """Not authenticated with broker."""

    def __init__(self, message: str = "Not authenticated. Run: diamond kite auth"):
        super().__init__("auth", message)


# --- Strategy layer ---


class StrategyError(DiamondError):
    """Error in strategy screening or allocation."""


class UnknownStrategyError(StrategyError):
    """Requested strategy does not exist."""

    def __init__(self, name: str, available: list[str] | None = None):
        self.name = name
        msg = f"Unknown strategy: {name}"
        if available:
            msg += f". Choose from: {available}"
        super().__init__(msg)


# --- Config layer ---


class ConfigError(DiamondError):
    """Invalid or missing configuration."""
