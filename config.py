"""Configuration loader with validation."""

import os
import logging
from dataclasses import dataclass, field
from typing import List
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # API
    api_key: str = ""
    api_secret: str = ""

    # Mode
    trading_mode: str = "paper"  # "paper" or "live"

    # Pairs & strategy
    trading_pairs: List[str] = field(default_factory=lambda: [])  # empty = scan all markets
    strategy: str = "combined"
    timeframe: str = "15m"

    # Market scanning
    scan_all_markets: bool = True           # dynamically fetch all USDT pairs
    min_volume_usdt: float = 5_000_000.0   # ignore pairs with 24h volume below this
    top_opportunities: int = 5             # max new positions to open per cycle from ranked signals
    rebalance: bool = True                  # sell weaker holdings if a stronger signal appears

    # Risk management
    max_position_size_pct: float = 5.0
    stop_loss_pct: float = 1.5
    take_profit_pct: float = 3.0
    max_open_positions: int = 5
    max_daily_loss_pct: float = 10.0

    # SMA settings (tuned for 15m)
    sma_fast_period: int = 8
    sma_slow_period: int = 21

    # RSI settings
    rsi_period: int = 14
    rsi_oversold: float = 30.0
    rsi_overbought: float = 70.0

    # Logging
    log_level: str = "INFO"
    log_file: str = "bot.log"

    @classmethod
    def from_env(cls) -> "Config":
        cfg = cls(
            api_key=os.getenv("BINANCE_API_KEY", ""),
            api_secret=os.getenv("BINANCE_API_SECRET", ""),
            trading_mode=os.getenv("TRADING_MODE", "paper").lower(),
            trading_pairs=[
                p.strip()
                for p in os.getenv("TRADING_PAIRS", "").split(",")
                if p.strip()
            ],
            strategy=os.getenv("STRATEGY", "combined").lower(),
            timeframe=os.getenv("TIMEFRAME", "15m"),
            scan_all_markets=os.getenv("SCAN_ALL_MARKETS", "true").lower() == "true",
            min_volume_usdt=float(os.getenv("MIN_VOLUME_USDT", "5000000")),
            top_opportunities=int(os.getenv("TOP_OPPORTUNITIES", "5")),
            rebalance=os.getenv("REBALANCE", "true").lower() == "true",
            max_position_size_pct=float(os.getenv("MAX_POSITION_SIZE_PCT", "5.0")),
            stop_loss_pct=float(os.getenv("STOP_LOSS_PCT", "1.5")),
            take_profit_pct=float(os.getenv("TAKE_PROFIT_PCT", "3.0")),
            max_open_positions=int(os.getenv("MAX_OPEN_POSITIONS", "5")),
            max_daily_loss_pct=float(os.getenv("MAX_DAILY_LOSS_PCT", "10.0")),
            sma_fast_period=int(os.getenv("SMA_FAST_PERIOD", "8")),
            sma_slow_period=int(os.getenv("SMA_SLOW_PERIOD", "21")),
            rsi_period=int(os.getenv("RSI_PERIOD", "14")),
            rsi_oversold=float(os.getenv("RSI_OVERSOLD", "30")),
            rsi_overbought=float(os.getenv("RSI_OVERBOUGHT", "70")),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            log_file=os.getenv("LOG_FILE", "bot.log"),
        )
        cfg.validate()
        return cfg

    def validate(self):
        if self.trading_mode not in ("paper", "live"):
            raise ValueError(f"TRADING_MODE must be 'paper' or 'live', got '{self.trading_mode}'")
        if self.trading_mode == "live" and (not self.api_key or not self.api_secret):
            raise ValueError("BINANCE_API_KEY and BINANCE_API_SECRET are required for live trading")
        if self.stop_loss_pct <= 0 or self.stop_loss_pct >= 100:
            raise ValueError("STOP_LOSS_PCT must be between 0 and 100")
        if self.take_profit_pct <= 0 or self.take_profit_pct >= 100:
            raise ValueError("TAKE_PROFIT_PCT must be between 0 and 100")
        if self.max_position_size_pct <= 0 or self.max_position_size_pct > 100:
            raise ValueError("MAX_POSITION_SIZE_PCT must be between 0 and 100")
        if self.sma_fast_period >= self.sma_slow_period:
            raise ValueError("SMA_FAST_PERIOD must be less than SMA_SLOW_PERIOD")
        valid_timeframes = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "3d", "1w"}
        if self.timeframe not in valid_timeframes:
            raise ValueError(f"TIMEFRAME must be one of {valid_timeframes}")
        valid_strategies = {"sma_crossover", "rsi", "combined"}
        if self.strategy not in valid_strategies:
            raise ValueError(f"STRATEGY must be one of {valid_strategies}")


def setup_logging(cfg: Config) -> logging.Logger:
    level = getattr(logging, cfg.log_level, logging.INFO)
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if cfg.log_file:
        handlers.append(logging.FileHandler(cfg.log_file))
    logging.basicConfig(level=level, format=fmt, handlers=handlers)
    return logging.getLogger("binance_bot")
