"""Trading strategies: SMA crossover, RSI, and a combined approach."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import pandas as pd
import numpy as np

logger = logging.getLogger("binance_bot.strategy")


class Signal(Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass
class StrategyResult:
    signal: Signal
    reason: str
    confidence: float = 0.0  # 0.0 – 1.0


def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _rsi(series: pd.Series, period: int) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


class SMAStrategy:
    """Golden/death cross on simple moving averages."""

    def __init__(self, fast: int = 10, slow: int = 30):
        if fast >= slow:
            raise ValueError("fast period must be less than slow period")
        self.fast = fast
        self.slow = slow

    def evaluate(self, df: pd.DataFrame) -> StrategyResult:
        if len(df) < self.slow + 2:
            return StrategyResult(Signal.HOLD, "not enough data")

        close = df["close"].astype(float)
        sma_fast = close.rolling(self.fast).mean()
        sma_slow = close.rolling(self.slow).mean()

        prev_diff = sma_fast.iloc[-2] - sma_slow.iloc[-2]
        curr_diff = sma_fast.iloc[-1] - sma_slow.iloc[-1]

        if prev_diff <= 0 and curr_diff > 0:
            confidence = min(abs(curr_diff) / close.iloc[-1], 1.0)
            return StrategyResult(Signal.BUY, f"SMA golden cross (fast={sma_fast.iloc[-1]:.2f}, slow={sma_slow.iloc[-1]:.2f})", confidence)
        if prev_diff >= 0 and curr_diff < 0:
            confidence = min(abs(curr_diff) / close.iloc[-1], 1.0)
            return StrategyResult(Signal.SELL, f"SMA death cross (fast={sma_fast.iloc[-1]:.2f}, slow={sma_slow.iloc[-1]:.2f})", confidence)

        return StrategyResult(Signal.HOLD, "no crossover")


class RSIStrategy:
    """Oversold / overbought reversal based on RSI."""

    def __init__(self, period: int = 14, oversold: float = 30.0, overbought: float = 70.0):
        self.period = period
        self.oversold = oversold
        self.overbought = overbought

    def evaluate(self, df: pd.DataFrame) -> StrategyResult:
        if len(df) < self.period + 2:
            return StrategyResult(Signal.HOLD, "not enough data")

        close = df["close"].astype(float)
        rsi = _rsi(close, self.period)

        prev_rsi = rsi.iloc[-2]
        curr_rsi = rsi.iloc[-1]

        if pd.isna(prev_rsi) or pd.isna(curr_rsi):
            return StrategyResult(Signal.HOLD, "RSI not yet computed")

        # Buy when RSI crosses back above oversold threshold
        if prev_rsi <= self.oversold and curr_rsi > self.oversold:
            confidence = (self.oversold - min(prev_rsi, curr_rsi)) / self.oversold
            return StrategyResult(Signal.BUY, f"RSI oversold recovery ({curr_rsi:.1f})", min(confidence, 1.0))

        # Sell when RSI crosses back below overbought threshold
        if prev_rsi >= self.overbought and curr_rsi < self.overbought:
            confidence = (max(prev_rsi, curr_rsi) - self.overbought) / (100 - self.overbought)
            return StrategyResult(Signal.SELL, f"RSI overbought rejection ({curr_rsi:.1f})", min(confidence, 1.0))

        return StrategyResult(Signal.HOLD, f"RSI neutral ({curr_rsi:.1f})")


class CombinedStrategy:
    """
    Requires both SMA and RSI to agree before generating a signal.
    Reduces false positives at the cost of fewer trades.
    """

    def __init__(
        self,
        sma_fast: int = 10,
        sma_slow: int = 30,
        rsi_period: int = 14,
        rsi_oversold: float = 30.0,
        rsi_overbought: float = 70.0,
    ):
        self.sma = SMAStrategy(sma_fast, sma_slow)
        self.rsi = RSIStrategy(rsi_period, rsi_oversold, rsi_overbought)

    def evaluate(self, df: pd.DataFrame) -> StrategyResult:
        sma_result = self.sma.evaluate(df)
        rsi_result = self.rsi.evaluate(df)

        logger.debug("SMA: %s | RSI: %s", sma_result, rsi_result)

        if sma_result.signal == Signal.BUY and rsi_result.signal == Signal.BUY:
            confidence = (sma_result.confidence + rsi_result.confidence) / 2
            return StrategyResult(Signal.BUY, f"Combined BUY – {sma_result.reason}; {rsi_result.reason}", confidence)

        if sma_result.signal == Signal.SELL and rsi_result.signal == Signal.SELL:
            confidence = (sma_result.confidence + rsi_result.confidence) / 2
            return StrategyResult(Signal.SELL, f"Combined SELL – {sma_result.reason}; {rsi_result.reason}", confidence)

        # Partial agreement – use higher-confidence individual signal with reduced weight
        if sma_result.signal != Signal.HOLD:
            return StrategyResult(Signal.HOLD, f"Partial signal (SMA={sma_result.signal.value}, RSI={rsi_result.signal.value}) – waiting for confirmation")

        return StrategyResult(Signal.HOLD, "no combined signal")


def build_strategy(cfg) -> SMAStrategy | RSIStrategy | CombinedStrategy:
    name = cfg.strategy
    if name == "sma_crossover":
        return SMAStrategy(cfg.sma_fast_period, cfg.sma_slow_period)
    if name == "rsi":
        return RSIStrategy(cfg.rsi_period, cfg.rsi_oversold, cfg.rsi_overbought)
    return CombinedStrategy(
        cfg.sma_fast_period,
        cfg.sma_slow_period,
        cfg.rsi_period,
        cfg.rsi_oversold,
        cfg.rsi_overbought,
    )
