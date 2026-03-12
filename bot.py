"""
Main trading bot loop.

Usage:
    python bot.py [--once]

    --once  Run a single evaluation cycle and exit (useful for cron jobs).
            Without this flag the bot runs continuously.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time

from config import Config, setup_logging
from exchange import build_exchange
from risk_manager import Position, RiskManager
from strategy import Signal, build_strategy

logger = logging.getLogger("binance_bot")

# Map config timeframe strings to sleep seconds between cycles
_TIMEFRAME_SECONDS = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900,
    "30m": 1800, "1h": 3600, "2h": 7200, "4h": 14400,
    "6h": 21600, "8h": 28800, "12h": 43200, "1d": 86400,
}


class TradingBot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.exchange = build_exchange(cfg)
        self.strategy = build_strategy(cfg)
        self.risk = RiskManager(cfg)
        self._running = False

    # ------------------------------------------------------------------
    # Single evaluation cycle
    # ------------------------------------------------------------------

    def evaluate_pair(self, symbol: str):
        try:
            current_price = self.exchange.get_ticker_price(symbol)
        except Exception as exc:
            logger.error("Failed to fetch price for %s: %s", symbol, exc)
            return

        # --- Check exit conditions for existing positions ---
        exit_reason = self.risk.check_exit_conditions(symbol, current_price)
        if exit_reason:
            self._close_position(symbol, current_price, exit_reason)
            return  # re-enter logic will run on next cycle

        # --- Skip if already in a position ---
        if symbol in self.risk.open_positions:
            pos = self.risk.open_positions[symbol]
            logger.debug(
                "%s: holding %s position (entry=%.4f sl=%.4f tp=%.4f current=%.4f)",
                symbol, pos.side, pos.entry_price, pos.stop_loss, pos.take_profit, current_price,
            )
            return

        # --- Fetch candles and evaluate strategy ---
        min_candles = max(self.cfg.sma_slow_period, self.cfg.rsi_period) + 5
        try:
            df = self.exchange.get_klines(symbol, self.cfg.timeframe, limit=min_candles)
        except Exception as exc:
            logger.error("Failed to fetch klines for %s: %s", symbol, exc)
            return

        result = self.strategy.evaluate(df)
        logger.info("%s @ %.4f | strategy → %s (%s)", symbol, current_price, result.signal.value, result.reason)

        if result.signal == Signal.HOLD:
            return

        # --- Risk checks ---
        portfolio_value = self.exchange.get_portfolio_value()
        allowed, reason = self.risk.can_open_position(symbol, portfolio_value)
        if not allowed:
            logger.info("Trade blocked for %s: %s", symbol, reason)
            return

        # --- Size the order ---
        quantity = self.risk.position_size(portfolio_value, current_price)
        if quantity <= 0:
            logger.warning("Calculated quantity is zero for %s – skipping", symbol)
            return

        # --- Place order ---
        side = result.signal.value  # "BUY" or "SELL"
        try:
            order = self.exchange.place_market_order(symbol, side, quantity)
        except Exception as exc:
            logger.error("Order failed for %s: %s", symbol, exc)
            return

        exec_price = float(order.get("price") or current_price)
        sl = self.risk.stop_loss_price(exec_price, side)
        tp = self.risk.take_profit_price(exec_price, side)

        position = Position(
            symbol=symbol,
            side=side,
            entry_price=exec_price,
            quantity=float(order.get("executedQty") or quantity),
            stop_loss=sl,
            take_profit=tp,
            order_id=str(order.get("orderId")),
        )
        self.risk.record_open(position)

    def _close_position(self, symbol: str, price: float, reason: str):
        pos = self.risk.open_positions.get(symbol)
        if pos is None:
            return
        close_side = "SELL" if pos.side == "BUY" else "BUY"
        try:
            self.exchange.place_market_order(symbol, close_side, pos.quantity)
        except Exception as exc:
            logger.error("Failed to close position %s: %s", symbol, exc)
            return
        pnl = self.risk.record_close(symbol, price)
        logger.info("Closed %s (%s) pnl=%.4f USDT", symbol, reason, pnl or 0)

    # ------------------------------------------------------------------
    # Run loop
    # ------------------------------------------------------------------

    def run_once(self):
        logger.info("=== Evaluation cycle start ===")
        for symbol in self.cfg.trading_pairs:
            self.evaluate_pair(symbol)
        logger.info("Risk summary: %s", self.risk.summary())

    def run(self):
        self._running = True
        sleep_secs = _TIMEFRAME_SECONDS.get(self.cfg.timeframe, 3600)
        logger.info(
            "Bot started | mode=%s strategy=%s pairs=%s timeframe=%s cycle=%ds",
            self.cfg.trading_mode, self.cfg.strategy,
            self.cfg.trading_pairs, self.cfg.timeframe, sleep_secs,
        )

        def _handle_stop(sig, frame):
            logger.info("Shutdown signal received – stopping after current cycle")
            self._running = False

        signal.signal(signal.SIGINT, _handle_stop)
        signal.signal(signal.SIGTERM, _handle_stop)

        while self._running:
            try:
                self.run_once()
            except Exception as exc:
                logger.exception("Unexpected error in main loop: %s", exc)

            if not self._running:
                break

            logger.info("Sleeping %ds until next cycle...", sleep_secs)
            # Sleep in small increments so SIGINT is handled promptly
            for _ in range(sleep_secs):
                if not self._running:
                    break
                time.sleep(1)

        logger.info("Bot stopped.")


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Binance trading bot")
    parser.add_argument("--once", action="store_true", help="Run a single cycle and exit")
    args = parser.parse_args()

    cfg = Config.from_env()
    setup_logging(cfg)

    logger.info("Configuration loaded | mode=%s pairs=%s strategy=%s", cfg.trading_mode, cfg.trading_pairs, cfg.strategy)

    if cfg.trading_mode == "live":
        logger.warning(
            "*** LIVE MODE ENABLED ***  Real funds will be traded. "
            "Press Ctrl+C within 5 seconds to abort."
        )
        time.sleep(5)

    bot = TradingBot(cfg)

    if args.once:
        bot.run_once()
    else:
        bot.run()


if __name__ == "__main__":
    main()
