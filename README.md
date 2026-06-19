# hl-read

**A key-free, read-only toolkit for [Hyperliquid](https://hyperliquid.xyz).** Use it as a Python library, a CLI, or an **MCP server** that lets an LLM agent (Claude, n8n, …) observe Hyperliquid markets and any wallet's public state.

> **Why "read-only" is the feature.** `hl-read` imports only the read side of the Hyperliquid SDK (`Info`) — never `Exchange`. There is no code path that can sign a transaction, place an order, or move funds. **You never hand it a private key, so there is no key to leak.** Most Hyperliquid MCP servers ask for your key so the model can trade; this one is safe to point an autonomous agent at by construction.

Everything it reads is *public* on-chain / exchange data: prices, order books, funding, and any address's positions, orders and fills.

---

## Install

```bash
pip install hl-read            # library + CLI
pip install "hl-read[mcp]"     # also installs the MCP server deps
```

## CLI

```bash
hl-read mids                       # all mid prices
hl-read mids BTC ETH               # just these
hl-read book ETH --depth 5         # order book snapshot
hl-read funding --top 10           # markets with the most extreme funding
hl-read predicted BTC ETH          # predicted funding across venues (HL vs Binance/Bybit)
hl-read markets                    # every perp + max leverage
hl-read spot                       # every spot pair + mid price
hl-read spot PURR                  # filter by base coin / pair
hl-read positions 0xYourAddr...    # anyone's positions (public data)
hl-read positions 0xYourAddr... --watch    # live, re-polled every few seconds
hl-read portfolio 0xYourAddr...    # account value / PnL history by period
hl-read ledger 0xYourAddr...       # deposits / withdrawals / transfers (--since, --limit 0 = all)
hl-read balances 0xYourAddr...     # spot token balances
hl-read orders 0xYourAddr...       # resting orders
hl-read fills 0xYourAddr... --limit 20
hl-read fills 0xYourAddr... --since 7d     # fills within a time window (e.g. 24h/7d, 2024-01-31)
hl-read watch ETH                  # live order book over websocket
```

Global flags: `--testnet` (use the testnet API), `--json` (raw JSON, great for piping to `jq`), `--retries N` (retry transient failures), `--rate-limit N` (cap HTTP calls/min), `--no-cache` (always fetch fresh).

```bash
hl-read --json funding | jq '.[] | select(.funding > 0.0001)'
```

## Library

```python
from hl_read import HLRead

hl = HLRead()                     # mainnet; HLRead(testnet=True) for testnet
hl.mids()["BTC"]                  # current mid price
hl.book("ETH", depth=5)           # {"bids": [...], "asks": [...], "mid": ..., "spread": ...}
hl.positions("0xabc...")          # account value + open positions for any address
hl.portfolio("0xabc...")          # account-value / PnL history by period (day..allTime)
hl.funding()                      # funding / mark / oracle / OI per market
hl.predicted_fundings()           # predicted funding per coin across venues (HL + CEXes)
hl.fills("0xabc...", limit=20)    # recent fills
hl.fills_by_time("0xabc...", start_ms, end_ms)   # fills within an epoch-ms window
hl.ledger("0xabc...")             # deposits/withdrawals/transfers (non-funding ledger)
hl.spot_markets()                 # spot pairs: name, base/quote token, mid
hl.spot_balances("0xabc...")      # spot token balances for any address

# live streams (open their own websocket — keep the returned Info alive)
hl.stream_book("ETH", lambda msg: print(msg["data"]["levels"][0][0]))
hl.stream_user_events("0xabc...", print)   # live fills / funding / liquidations
```

### Resilience (built in, configurable)

Every HTTP read goes through a default timeout, exponential backoff with jitter on
transient failures (network errors, HTTP 429/5xx → `HLReadError` once exhausted), an
optional client-side rate limit, and short-lived caching of the high-frequency market
endpoints (so e.g. `mid()` in a loop costs one fetch per cache window, not one per call).

```python
hl = HLRead(
    max_retries=4,            # retry transient failures
    rate_limit_per_min=600,   # cap HTTP calls/min (None = off)
    cache_ttl=1.0,            # seconds to cache mids / funding ctxs (0 = always fresh)
    meta_ttl=300.0,           # seconds to cache the market/token tables
    http_timeout=10.0,        # per-request timeout
)
hl.clear_cache()              # drop cached data on demand
```

## MCP server (the differentiator)

Expose read-only Hyperliquid data to any MCP client over stdio.

```bash
hl-read-mcp                       # mainnet
HL_READ_TESTNET=1 hl-read-mcp     # testnet
```

**Claude Desktop** — add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "hl-read": { "command": "hl-read-mcp" }
  }
}
```

**Claude Code:**

```bash
claude mcp add hl-read -- hl-read-mcp
```

Tools exposed to the model (15): `list_markets`, `get_mids`, `get_book`, `get_funding`, `get_funding_history`, `get_predicted_fundings`, `get_positions`, `get_portfolio`, `get_open_orders`, `get_ledger`, `get_fills`, `get_fills_by_time`, `get_candles`, `get_spot_markets`, `get_spot_balances`. None of them can place an order.

> Ask Claude: *"What's the funding on the top 5 Hyperliquid perps right now, and what's 0xabc…'s open position on the highest one?"* — it answers using only public reads.

## Safety model

- **No key, ever.** The library has no parameter, env var, or file from which it reads a private key.
- **No trading code in the import graph.** `hyperliquid.exchange.Exchange` is never imported, so signing/order/cancel functions are not reachable.
- **Read-only network calls.** Only Hyperliquid's public `info` endpoint and public websocket subscriptions are used.

This makes `hl-read` a sound base for monitoring bots, dashboards, and **autonomous agents** where you want market awareness without ceding the ability to spend.

## Development

```bash
pip install -e ".[dev]"
python -m unittest discover -s tests   # or: pytest
```

The test suite is fully offline — the SDK is mocked, so it exercises the retry/backoff,
cache, and parsing logic deterministically without touching the network.

## License

MIT © akagifreeez. Not affiliated with Hyperliquid. Public market data only; nothing here is financial advice.
