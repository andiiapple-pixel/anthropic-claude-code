"""
Main trading bot loop.

Each cycle:
  1. Fetch all liquid USDT pairs from Binance (or use configured list).
  2. Scan portfolio holdings – sell any asset whose signal has weakened
     if a stronger opportunity exists elsewhere (rebalancing).
  3. Evaluate every pair for entry signals, rank by confidence.
  4. Open positions in the top-ranked opportunities up to the max allowed.

Usage:
    python bot.py [--once]
"""

from __future__ import annotations

import argparse
import logging
import signal
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from config import Config, setup_logging
from exchange import build_exchange
from risk_manager import Position, RiskManager
from strategy import Signal, StrategyResult, build_strategy

logger = logging.getLogger("binance_bot")

_TIMEFRAME_SECONDS = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900,
    "30m": 1800, "1h": 3600, "2h": 7200, "4h": 14400,
    "6h": 21600, "8h": 28800, "12h": 43200, "1d": 86400,
}


@dataclass
class Opportunity:
    symbol: str
    price: float
    result: StrategyResult


class TradingBot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.exchange = build_exchange(cfg)
        self.strategy = build_strategy(cfg)
        self.risk = RiskManager(cfg)
        self._running = False
        self._candle_limit = max(cfg.sma_slow_period, cfg.rsi_period) + 5

    # ------------------------------------------------------------------
    # Market universe
    # ------------------------------------------------------------------

    def _get_universe(self) -> List[str]:
        """Return the list of symbols to scan this cycle."""
        if self.cfg.trading_pairs:
            return self.cfg.trading_pairs
        try:
            return self.exchange.get_all_usdt_pairs(self.cfg.min_volume_usdt)
        except Exception as exc:
            logger.error("Failed to fetch market universe: %s", exc)
            return []

    # ------------------------------------------------------------------
    # Signal evaluation (single pair)
    # ------------------------------------------------------------------

    def _evaluate(self, symbol: str) -> Optional[Opportunity]:
        """Fetch price + candles, run strategy. Returns None on error."""
        try:
            price = self.exchange.get_ticker_price(symbol)
        except Exception as exc:
            logger.debug("Price error %s: %s", symbol, exc)
            return None
        try:
            df = self.exchange.get_klines(symbol, self.cfg.timeframe, limit=self._candle_limit)
        except Exception as exc:
            logger.debug("Klines error %s: %s", symbol, exc)
            return None
        result = self.strategy.evaluate(df)
        return Opportunity(symbol=symbol, price=price, result=result)

    # ------------------------------------------------------------------
    # Portfolio rebalancing – sell weak holdings
    # ------------------------------------------------------------------

    def _rebalance_portfolio(self, buy_signals: List[Opportunity]):
        """
        For each open position, check if its current signal is HOLD/SELL.
        If there is a BUY opportunity with higher confidence, close the
        weaker position to free capital for the better trade.
        """
        if not self.cfg.rebalance or not buy_signals:
            return

        # Best available confidence across all pending BUY signals
        best_confidence = buy_signals[0].result.confidence if buy_signals else 0.0

        for symbol, pos in list(self.risk.open_positions.items()):
            opp = self._evaluate(symbol)
            if opp is None:
                continue

            current_signal = opp.result.signal
            current_confidence = opp.result.confidence

            # Sell the holding if:
            #  - strategy now says SELL, OR
            #  - strategy says HOLD and a meaningfully stronger BUY exists elsewhere
            should_exit = (
                current_signal == Signal.SELL
                or (current_signal == Signal.HOLD and best_confidence > current_confidence + 0.2)
            )

            if should_exit:
                logger.info(
                    "Rebalance: closing %s (signal=%s conf=%.2f) for better opportunity (conf=%.2f)",
                    symbol, current_signal.value, current_confidence, best_confidence,
                )
                self._close_position(symbol, opp.price, f"rebalance ({current_signal.value})")

    # ------------------------------------------------------------------
    # Open a new position
    # ------------------------------------------------------------------

    def _open_position(self, opp: Opportunity):
        symbol, price = opp.symbol, opp.price
        portfolio_value = self.exchange.get_portfolio_value()
        allowed, reason = self.risk.can_open_position(symbol, portfolio_value)
        if not allowed:
            logger.debug("Blocked %s: %s", symbol, reason)
            return

        quantity = self.risk.position_size(portfolio_value, price)
        if quantity <= 0:
            logger.warning("Zero quantity for %s – skipping", symbol)
            return

        side = opp.result.signal.value
        try:
            order = self.exchange.place_market_order(symbol, side, quantity)
        except Exception as exc:
            logger.error("Order failed %s: %s", symbol, exc)
            return

        exec_price = float(order.get("price") or price)
        position = Position(
            symbol=symbol,
            side=side,
            entry_price=exec_price,
            quantity=float(order.get("executedQty") or quantity),
            stop_loss=self.risk.stop_loss_price(exec_price, side),
            take_profit=self.risk.take_profit_price(exec_price, side),
            order_id=str(order.get("orderId")),
        )
        self.risk.record_open(position)

    # ------------------------------------------------------------------
    # Close an existing position
    # ------------------------------------------------------------------

    def _close_position(self, symbol: str, price: float, reason: str):
        pos = self.risk.open_positions.get(symbol)
        if pos is None:
            return
        close_side = "SELL" if pos.side == "BUY" else "BUY"
        try:
            self.exchange.place_market_order(symbol, close_side, pos.quantity)
        except Exception as exc:
            logger.error("Close failed %s: %s", symbol, exc)
            return
        pnl = self.risk.record_close(symbol, price)
        logger.info("Closed %s (%s) pnl=%.4f USDT", symbol, reason, pnl or 0)

    # ------------------------------------------------------------------
    # Check SL/TP on all open positions
    # ------------------------------------------------------------------

    def _check_open_positions(self):
        for symbol in list(self.risk.open_positions.keys()):
            try:
                price = self.exchange.get_ticker_price(symbol)
            except Exception as exc:
                logger.debug("Price error checking %s: %s", symbol, exc)
                continue
            reason = self.risk.check_exit_conditions(symbol, price)
            if reason:
                self._close_position(symbol, price, reason)

    # ------------------------------------------------------------------
    # Sell existing portfolio assets not tracked as positions
    # ------------------------------------------------------------------

    def _liquidate_untracked_holdings(self, buy_signals: List[Opportunity]):
        """
        Check real account holdings (live mode) or paper balances for assets
        not currently tracked as open positions. If their signal is weak and
        better BUY signals exist, sell them.
        """
        if not self.cfg.rebalance or not buy_signals:
            return

        try:
            holdings = self.exchange.get_portfolio_holdings()
        except Exception as exc:
            logger.debug("Could not fetch holdings: %s", exc)
            return

        best_confidence = buy_signals[0].result.confidence if buy_signals else 0.0

        for asset, qty in holdings.items():
            symbol = f"{asset}USDT"
            if symbol in self.risk.open_positions:
                continue  # already managed above

            opp = self._evaluate(symbol)
            if opp is None:
                continue

            should_sell = (
                opp.result.signal == Signal.SELL
                or (opp.result.signal == Signal.HOLD and best_confidence > 0.4)
            )

            if should_sell:
                logger.info(
                    "Liquidating untracked holding %s qty=%.6f (signal=%s) for better opportunity",
                    symbol, qty, opp.result.signal.value,
                )
                try:
                    self.exchange.place_market_order(symbol, "SELL", qty)
                    logger.info("Sold %s qty=%.6f @ %.4f", symbol, qty, opp.price)
                except Exception as exc:
                    logger.error("Failed to sell %s: %s", symbol, exc)

    # ------------------------------------------------------------------
    # Main cycle
    # ------------------------------------------------------------------

    def run_once(self):
        logger.info("=== Cycle start ===")

        # 1. Check SL/TP on open positions
        self._check_open_positions()

        # 2. Get universe of pairs to scan
        universe = self._get_universe()
        if not universe:
            logger.warning("Empty universe – skipping cycle")
            return

        logger.info("Scanning %d pairs...", len(universe))

        # 3. Evaluate every pair; collect BUY signals ranked by confidence
        buy_signals: List[Opportunity] = []
        evaluated = 0
        for symbol in universe:
            # Skip pairs already held at max capacity
            if len(self.risk.open_positions) >= self.cfg.max_open_positions and symbol not in self.risk.open_positions:
                pass  # still evaluate for rebalancing purposes
            opp = self._evaluate(symbol)
            if opp is None:
                continue
            evaluated += 1
            if opp.result.signal == Signal.BUY:
                buy_signals.append(opp)
            # Small delay to respect Binance rate limits (1200 req/min weight)
            time.sleep(0.05)

        buy_signals.sort(key=lambda o: o.result.confidence, reverse=True)
        logger.info(
            "Scanned %d pairs | BUY signals: %d | top opportunity: %s",
            evaluated,
            len(buy_signals),
            f"{buy_signals[0].symbol} conf={buy_signals[0].result.confidence:.2f}" if buy_signals else "none",
        )

        # 4. Liquidate untracked holdings if better options exist
        self._liquidate_untracked_holdings(buy_signals)

        # 5. Rebalance: close weak open positions in favour of stronger signals
        self._rebalance_portfolio(buy_signals)

        # 6. Open top-ranked BUY opportunities
        opened = 0
        for opp in buy_signals:
            if opened >= self.cfg.top_opportunities:
                break
            if opp.symbol in self.risk.open_positions:
                continue  # already holding
            self._open_position(opp)
            opened += 1

        logger.info("Cycle complete | opened=%d | %s", opened, self.risk.summary())

    # ------------------------------------------------------------------
    # Continuous run loop
    # ------------------------------------------------------------------

    def run(self):
        self._running = True
        sleep_secs = _TIMEFRAME_SECONDS.get(self.cfg.timeframe, 900)
        logger.info(
            "Bot started | mode=%s strategy=%s timeframe=%s cycle=%ds scan_all=%s",
            self.cfg.trading_mode, self.cfg.strategy,
            self.cfg.timeframe, sleep_secs, self.cfg.scan_all_markets,
        )

        def _stop(sig, frame):
            logger.info("Shutdown signal – stopping after current cycle")
            self._running = False

        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)

        while self._running:
            try:
                self.run_once()
            except Exception as exc:
                logger.exception("Unexpected error: %s", exc)

            if not self._running:
                break

            logger.info("Sleeping %ds until next cycle...", sleep_secs)
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
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit")
    args = parser.parse_args()

    cfg = Config.from_env()
    setup_logging(cfg)

    logger.info(
        "Config | mode=%s strategy=%s timeframe=%s scan_all=%s top=%d rebalance=%s",
        cfg.trading_mode, cfg.strategy, cfg.timeframe,
        cfg.scan_all_markets, cfg.top_opportunities, cfg.rebalance,
    )

    if cfg.trading_mode == "live":
        logger.warning("*** LIVE MODE – real funds will be traded. Ctrl+C within 5s to abort. ***")
        time.sleep(5)

    bot = TradingBot(cfg)
    bot.run_once() if args.once else bot.run()


if __name__ == "__main__":
    main()
