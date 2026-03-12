# Binance Trading Bot

An autonomous trading bot for Binance Spot markets with built-in risk management.

## Features

- **Three strategies**: SMA crossover, RSI mean-reversion, or both combined
- **Risk management**: position sizing, stop-loss, take-profit, daily loss circuit breaker
- **Paper trading**: simulate against live market data before risking real capital
- **Live trading**: execute real orders via the Binance API
- **Configurable**: all parameters via a single `.env` file

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env – add your API keys and tune parameters
```

### 3. Run in paper mode first

```bash
python bot.py
```

To run a single evaluation cycle (e.g., from a cron job):

```bash
python bot.py --once
```

### 4. Switch to live mode

Only after you are satisfied with paper-mode results, set `TRADING_MODE=live` in `.env`.

---

## Configuration Reference

| Variable | Default | Description |
|---|---|---|
| `BINANCE_API_KEY` | – | Binance API key (required for live mode) |
| `BINANCE_API_SECRET` | – | Binance API secret (required for live mode) |
| `TRADING_MODE` | `paper` | `paper` or `live` |
| `TRADING_PAIRS` | `BTCUSDT` | Comma-separated pairs to trade |
| `STRATEGY` | `combined` | `sma_crossover`, `rsi`, or `combined` |
| `TIMEFRAME` | `1h` | Candle interval (`1m` … `1d`) |
| `MAX_POSITION_SIZE_PCT` | `5.0` | Max % of portfolio per trade |
| `STOP_LOSS_PCT` | `2.0` | Stop-loss distance (%) |
| `TAKE_PROFIT_PCT` | `4.0` | Take-profit distance (%) |
| `MAX_OPEN_POSITIONS` | `3` | Max simultaneous positions |
| `MAX_DAILY_LOSS_PCT` | `10.0` | Circuit breaker – halt trading if daily loss exceeds this |
| `SMA_FAST_PERIOD` | `10` | Fast SMA period |
| `SMA_SLOW_PERIOD` | `30` | Slow SMA period |
| `RSI_PERIOD` | `14` | RSI look-back period |
| `RSI_OVERSOLD` | `30` | RSI buy threshold |
| `RSI_OVERBOUGHT` | `70` | RSI sell threshold |

---

## Architecture

```
bot.py          – main loop, signal handling
config.py       – environment-based configuration with validation
strategy.py     – SMAStrategy, RSIStrategy, CombinedStrategy
risk_manager.py – RiskManager: sizing, SL/TP, circuit breaker
exchange.py     – PaperExchange (simulated) and LiveExchange (Binance API)
```

---

## Risk Warning

**Automated trading involves significant financial risk.**
Past performance of any strategy is not indicative of future results.
Always start with paper mode and only risk capital you can afford to lose.
