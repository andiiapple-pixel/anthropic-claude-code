"""
Binance exchange adapter.
Supports both live (real API) and paper (simulated) modes.
"""

from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional

import pandas as pd

logger = logging.getLogger("binance_bot.exchange")


class PaperExchange:
    """
    Simulates order execution against real market data without spending real money.
    Portfolio starts with a configurable USDT balance.
    """

    STARTING_BALANCE = 1000.0  # USDT

    def __init__(self):
        self.balances: Dict[str, float] = {"USDT": self.STARTING_BALANCE}
        self._order_counter = 0
        logger.info("Paper trading mode active – starting balance: %.2f USDT", self.STARTING_BALANCE)

    # ------------------------------------------------------------------
    # Market data (delegates to Binance public API via python-binance)
    # ------------------------------------------------------------------

    def get_klines(self, symbol: str, interval: str, limit: int = 100) -> pd.DataFrame:
        from binance.client import Client
        client = Client()  # public endpoints need no keys
        raw = client.get_klines(symbol=symbol, interval=interval, limit=limit)
        return _klines_to_df(raw)

    def get_ticker_price(self, symbol: str) -> float:
        from binance.client import Client
        client = Client()
        ticker = client.get_symbol_ticker(symbol=symbol)
        return float(ticker["price"])

    def get_portfolio_value(self) -> float:
        """Total value in USDT (simplified: counts USDT balance only for paper mode)."""
        return self.balances.get("USDT", 0.0)

    # ------------------------------------------------------------------
    # Order execution
    # ------------------------------------------------------------------

    def place_market_order(self, symbol: str, side: str, quantity: float) -> dict:
        price = self.get_ticker_price(symbol)
        self._order_counter += 1
        order_id = f"PAPER-{self._order_counter}"
        base_asset = symbol.replace("USDT", "")

        if side == "BUY":
            cost = price * quantity
            if self.balances.get("USDT", 0) < cost:
                raise ValueError(f"Insufficient USDT balance ({self.balances.get('USDT', 0):.2f}) for order cost ({cost:.2f})")
            self.balances["USDT"] = self.balances.get("USDT", 0) - cost
            self.balances[base_asset] = self.balances.get(base_asset, 0) + quantity

        elif side == "SELL":
            if self.balances.get(base_asset, 0) < quantity:
                raise ValueError(f"Insufficient {base_asset} balance")
            self.balances[base_asset] = self.balances.get(base_asset, 0) - quantity
            self.balances["USDT"] = self.balances.get("USDT", 0) + price * quantity

        logger.info("[PAPER] %s %s qty=%.6f @ %.4f USDT | order_id=%s", side, symbol, quantity, price, order_id)
        return {"orderId": order_id, "price": price, "executedQty": quantity, "status": "FILLED"}

    def get_symbol_info(self, symbol: str) -> dict:
        from binance.client import Client
        client = Client()
        return client.get_symbol_info(symbol)


class LiveExchange:
    """Executes real orders on Binance Spot."""

    def __init__(self, api_key: str, api_secret: str):
        from binance.client import Client
        self._client = Client(api_key, api_secret)
        logger.warning(
            "LIVE trading mode active – real money will be used. "
            "Ensure you understand the risks before proceeding."
        )

    def get_klines(self, symbol: str, interval: str, limit: int = 100) -> pd.DataFrame:
        raw = self._client.get_klines(symbol=symbol, interval=interval, limit=limit)
        return _klines_to_df(raw)

    def get_ticker_price(self, symbol: str) -> float:
        ticker = self._client.get_symbol_ticker(symbol=symbol)
        return float(ticker["price"])

    def get_portfolio_value(self) -> float:
        account = self._client.get_account()
        total = 0.0
        for bal in account["balances"]:
            free = float(bal["free"])
            locked = float(bal["locked"])
            amount = free + locked
            if amount == 0:
                continue
            asset = bal["asset"]
            if asset == "USDT":
                total += amount
            else:
                try:
                    price = self.get_ticker_price(f"{asset}USDT")
                    total += amount * price
                except Exception:
                    pass  # skip unsupported pairs
        return total

    def place_market_order(self, symbol: str, side: str, quantity: float) -> dict:
        info = self.get_symbol_info(symbol)
        quantity = _round_step_size(quantity, info)
        if quantity <= 0:
            raise ValueError(f"Rounded quantity is zero for {symbol}")

        order = self._client.create_order(
            symbol=symbol,
            side=side,
            type="MARKET",
            quantity=quantity,
        )
        logger.info(
            "[LIVE] %s %s qty=%.6f | order_id=%s status=%s",
            side, symbol, quantity, order["orderId"], order["status"],
        )
        return order

    def get_symbol_info(self, symbol: str) -> dict:
        return self._client.get_symbol_info(symbol)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _klines_to_df(raw: list) -> pd.DataFrame:
    df = pd.DataFrame(raw, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore",
    ])
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = df[col].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    df.set_index("open_time", inplace=True)
    return df


def _round_step_size(quantity: float, symbol_info: dict) -> float:
    """Round quantity to the exchange's required step size."""
    for f in symbol_info.get("filters", []):
        if f["filterType"] == "LOT_SIZE":
            step = float(f["stepSize"])
            if step == 0:
                return quantity
            precision = len(str(step).rstrip("0").split(".")[-1]) if "." in str(step) else 0
            return round(quantity - (quantity % step), precision)
    return quantity


def build_exchange(cfg) -> PaperExchange | LiveExchange:
    if cfg.trading_mode == "paper":
        return PaperExchange()
    return LiveExchange(cfg.api_key, cfg.api_secret)
