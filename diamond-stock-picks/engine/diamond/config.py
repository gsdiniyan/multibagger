"""Centralized configuration with Pydantic validation.

All settings loaded from .env with sensible defaults.
Access via `get_config()` singleton.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).parent.parent.parent
_ENV_FILE = str(PROJECT_ROOT / ".env")


class AISettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", env_file=_ENV_FILE, extra="ignore")

    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"
    ai_retry_limit: int = Field(default=3, ge=1, le=10)
    ai_rate_limit_rpm: int = Field(default=15, ge=1, le=60)
    ai_toxicity_threshold: float = Field(default=0.6, ge=0.0, le=1.0)


class CacheSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CACHE_", env_file=_ENV_FILE, extra="ignore")

    screener_ttl: int = Field(default=86400, ge=0)  # 24h
    prices_ttl: int = Field(default=14400, ge=0)  # 4h
    sentiment_ttl: int = Field(default=3600, ge=0)  # 1h


class PortfolioConstraints(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", env_file=_ENV_FILE, extra="ignore")

    max_position_weight: float = Field(default=0.10, ge=0.01, le=1.0)
    max_stocks_per_sector: int = Field(default=4, ge=1, le=20)
    max_sector_exposure: float = Field(default=0.25, ge=0.05, le=1.0)


class RiskSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", env_file=_ENV_FILE, extra="ignore")

    stop_loss_threshold: float = Field(default=0.10, ge=0.01, le=0.50)
    cooldown_days: int = Field(default=5, ge=0, le=30)
    initial_capital: float = Field(default=100000, ge=1000)


class RebalanceSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", env_file=_ENV_FILE, extra="ignore")

    rebalance_frequency_days: int = Field(default=90, ge=1, le=365)
    drift_threshold: float = Field(default=0.05, ge=0.01, le=0.50)
    min_trade_value: float = Field(default=1000, ge=100)
    annual_turnover_budget: float = Field(default=0.50, ge=0.10, le=2.0)


class ScreenerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", env_file=_ENV_FILE, extra="ignore")

    lookback_days: int = Field(default=504, ge=60, le=2520)
    risk_free_rate: float = Field(default=0.065, ge=0.0, le=0.20)


class PassiveSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PASSIVE_", env_file=_ENV_FILE, extra="ignore")

    index_ticker: str = "^NSEI"
    target_tracking_error: float = Field(default=0.002, ge=0.0, le=0.05)


class KiteSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KITE_", env_file=_ENV_FILE, extra="ignore")

    api_key: str = ""
    api_secret: str = ""
    exchange: str = "NSE"
    product_type: str = "CNC"  # CNC for delivery
    order_validity: str = "DAY"
    dry_run: bool = Field(default=True)
    circuit_breaker_pct: float = Field(default=0.05, ge=0.01, le=0.20)
    max_trade_value: float = Field(default=50000, ge=1000)
    max_trades_per_session: int = Field(default=30, ge=1, le=100)


class GodsPlanSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GODS_PLAN_", env_file=_ENV_FILE, extra="ignore")

    rebalance_days: int = Field(default=90, ge=1, le=365)
    max_position_weight: float = Field(default=0.12, ge=0.01, le=1.0)
    core_min_alpha: float = Field(default=0.15, ge=-1.0, le=5.0)
    core_min_cagr: float = Field(default=0.18, ge=0.0, le=1.0)
    core_min_hurst: float = Field(default=0.50, ge=0.0, le=1.0)
    core_min_beta: float = Field(default=0.80, ge=0.0, le=3.0)
    core_max_beta: float = Field(default=1.30, ge=0.0, le=3.0)
    drawdown_critical_pct: float = Field(default=20.0, ge=5.0, le=50.0)


class ActiveSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ACTIVE_", env_file=_ENV_FILE, extra="ignore")

    target_size: int = Field(default=15, ge=5, le=30)
    candidate_pool: int = Field(default=30, ge=10, le=100)
    growth_min_alpha: float = Field(default=0.10, ge=-1.0, le=5.0)
    defensive_max_beta: float = Field(default=0.80, ge=0.0, le=3.0)
    defensive_min_cagr: float = Field(default=0.12, ge=0.0, le=1.0)
    momentum_lookback_months: int = Field(default=12, ge=1, le=36)
    momentum_skip_months: int = Field(default=1, ge=0, le=6)
    value_max_pb: float = Field(default=3.0, ge=0.1, le=20.0)
    quality_roe_weight: float = Field(default=0.4, ge=0.0, le=1.0)
    quality_safety_weight: float = Field(default=0.3, ge=0.0, le=1.0)
    quality_growth_weight: float = Field(default=0.3, ge=0.0, le=1.0)


class BalancedSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BALANCED_", env_file=_ENV_FILE, extra="ignore")

    passive_alloc: float = Field(default=0.50, ge=0.0, le=1.0)
    alpha_alloc: float = Field(default=0.25, ge=0.0, le=1.0)
    defensive_alloc: float = Field(default=0.25, ge=0.0, le=1.0)
    alpha_count: int = Field(default=10, ge=1, le=30)
    defensive_count: int = Field(default=5, ge=1, le=20)


class UltimateSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ULTIMATE_", env_file=_ENV_FILE, extra="ignore")

    rebalance_days: int = Field(default=90, ge=1, le=365)


class Config(BaseSettings):
    """Top-level configuration aggregating all subsections."""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    ai: AISettings = Field(default_factory=AISettings)
    cache: CacheSettings = Field(default_factory=CacheSettings)
    portfolio: PortfolioConstraints = Field(default_factory=PortfolioConstraints)
    risk: RiskSettings = Field(default_factory=RiskSettings)
    rebalance: RebalanceSettings = Field(default_factory=RebalanceSettings)
    screener: ScreenerSettings = Field(default_factory=ScreenerSettings)
    passive: PassiveSettings = Field(default_factory=PassiveSettings)
    gods_plan: GodsPlanSettings = Field(default_factory=GodsPlanSettings)
    kite: KiteSettings = Field(default_factory=KiteSettings)
    active: ActiveSettings = Field(default_factory=ActiveSettings)
    balanced: BalancedSettings = Field(default_factory=BalancedSettings)
    ultimate: UltimateSettings = Field(default_factory=UltimateSettings)

    @property
    def data_dir(self) -> Path:
        return PROJECT_ROOT / "data"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def ledger_dir(self) -> Path:
        return self.data_dir / "ledgers"

    @property
    def reports_dir(self) -> Path:
        return PROJECT_ROOT / "reports"

    def ledger_path(self, strategy: str) -> Path:
        return self.ledger_dir / f"{strategy}.db"

    def ensure_dirs(self) -> None:
        """Create all required runtime directories."""
        for d in [self.data_dir, self.cache_dir, self.ledger_dir, self.reports_dir]:
            d.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_config() -> Config:
    """Singleton config instance. Cached after first call."""
    cfg = Config()
    cfg.ensure_dirs()
    return cfg
