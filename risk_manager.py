"""
Risk management: position sizing, stop-loss, take-profit, and circuit breakers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, Optional

logger = logging.getLogger("binance_bot.risk")


@dataclass
class Position:
    symbol: str
    side: str           # "BUY" (long) or "SELL" (short)
    entry_price: float
    quantity: float
    stop_loss: float
    take_profit: float
    order_id: Optional[str] = None

    @property
    def notional(self) -> float:
        return self.entry_price * self.quantity


@dataclass
class DailyStats:
    date: date = field(default_factory=date.today)
    realised_pnl: float = 0.0
    trade_count: int = 0

    def reset_if_new_day(self):
        today = date.today()
        if self.date != today:
            self.date = today
            self.realised_pnl = 0.0
            self.trade_count = 0


class RiskManager:
    def __init__(self, cfg):
        self.cfg = cfg
        self.open_positions: Dict[str, Position] = {}
        self.daily = DailyStats()
        self._circuit_open = False

    # ------------------------------------------------------------------
    # Checks
    # ------------------------------------------------------------------

    def can_open_position(self, symbol: str, portfolio_value: float) -> tuple[bool, str]:
        """Return (allowed, reason)."""
        self.daily.reset_if_new_day()

        if self._circuit_open:
            return False, "circuit breaker is open (daily loss limit reached)"

        if symbol in self.open_positions:
            return False, f"already have an open position in {symbol}"

        if len(self.open_positions) >= self.cfg.max_open_positions:
            return False, f"max open positions ({self.cfg.max_open_positions}) reached"

        daily_loss_limit = portfolio_value * self.cfg.max_daily_loss_pct / 100
        if self.daily.realised_pnl < -daily_loss_limit:
            self._circuit_open = True
            logger.warning("Circuit breaker triggered: daily loss %.2f exceeds limit %.2f", self.daily.realised_pnl, daily_loss_limit)
            return False, "daily loss limit reached – trading halted for today"

        return True, "ok"

    # ------------------------------------------------------------------
    # Sizing
    # ------------------------------------------------------------------

    def position_size(self, portfolio_value: float, entry_price: float) -> float:
        """Return quantity to buy given portfolio value and entry price."""
        max_notional = portfolio_value * self.cfg.max_position_size_pct / 100
        qty = max_notional / entry_price
        return qty

    def stop_loss_price(self, entry_price: float, side: str) -> float:
        factor = self.cfg.stop_loss_pct / 100
        return entry_price * (1 - factor) if side == "BUY" else entry_price * (1 + factor)

    def take_profit_price(self, entry_price: float, side: str) -> float:
        factor = self.cfg.take_profit_pct / 100
        return entry_price * (1 + factor) if side == "BUY" else entry_price * (1 - factor)

    # ------------------------------------------------------------------
    # Position lifecycle
    # ------------------------------------------------------------------

    def record_open(self, position: Position):
        self.open_positions[position.symbol] = position
        logger.info(
            "Position opened: %s %s qty=%.6f entry=%.4f sl=%.4f tp=%.4f",
            position.side, position.symbol, position.quantity,
            position.entry_price, position.stop_loss, position.take_profit,
        )

    def record_close(self, symbol: str, exit_price: float) -> Optional[float]:
        pos = self.open_positions.pop(symbol, None)
        if pos is None:
            logger.warning("record_close called for unknown position %s", symbol)
            return None

        if pos.side == "BUY":
            pnl = (exit_price - pos.entry_price) * pos.quantity
        else:
            pnl = (pos.entry_price - exit_price) * pos.quantity

        self.daily.realised_pnl += pnl
        self.daily.trade_count += 1
        logger.info(
            "Position closed: %s %s exit=%.4f pnl=%.4f (daily pnl=%.4f)",
            pos.side, symbol, exit_price, pnl, self.daily.realised_pnl,
        )
        return pnl

    # ------------------------------------------------------------------
    # Monitor existing positions for SL/TP
    # ------------------------------------------------------------------

    def check_exit_conditions(self, symbol: str, current_price: float) -> Optional[str]:
        """Return 'stop_loss', 'take_profit', or None."""
        pos = self.open_positions.get(symbol)
        if pos is None:
            return None

        if pos.side == "BUY":
            if current_price <= pos.stop_loss:
                return "stop_loss"
            if current_price >= pos.take_profit:
                return "take_profit"
        else:
            if current_price >= pos.stop_loss:
                return "stop_loss"
            if current_price <= pos.take_profit:
                return "take_profit"

        return None

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def summary(self) -> dict:
        self.daily.reset_if_new_day()
        return {
            "open_positions": len(self.open_positions),
            "symbols": list(self.open_positions.keys()),
            "daily_pnl": round(self.daily.realised_pnl, 4),
            "daily_trades": self.daily.trade_count,
            "circuit_open": self._circuit_open,
        }
