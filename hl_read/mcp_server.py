"""hl-read MCP server - expose read-only Hyperliquid data to LLM agents.

This is the differentiator: existing Hyperliquid MCP servers want a private
key so the model can trade. This one is read-only by construction - it never
imports the trading side of the SDK, so an agent connected to it can observe
markets and any address's public state but *cannot* place an order or move
funds. There is no key to leak.

Run it over stdio (the transport Claude Desktop, Claude Code, and n8n use):

    hl-read-mcp                      # mainnet
    HL_READ_TESTNET=1 hl-read-mcp    # testnet

Claude Desktop config (claude_desktop_config.json):

    {
      "mcpServers": {
        "hl-read": { "command": "hl-read-mcp" }
      }
    }

Requires the optional MCP dependency:  pip install "hl-read[mcp]"
"""
from __future__ import annotations

import os

from .info import HLRead

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as e:  # pragma: no cover - friendly message instead of a traceback
    raise SystemExit(
        "The MCP server needs the 'mcp' package. Install it with:\n"
        '    pip install "hl-read[mcp]"'
    ) from e

_TESTNET = os.environ.get("HL_READ_TESTNET", "").lower() in ("1", "true", "yes")
hl = HLRead(testnet=_TESTNET)

mcp = FastMCP("hl-read")


@mcp.tool()
def list_markets() -> list[dict]:
    """List every Hyperliquid perp market with max leverage and size decimals."""
    return hl.markets()


@mcp.tool()
def get_mids(coins: list[str] | None = None) -> dict:
    """Current mid prices. Pass a list of coins (e.g. ["BTC","ETH"]) to filter, or omit for all."""
    mids = hl.mids()
    if coins:
        return {c.upper(): mids.get(c.upper()) for c in coins}
    return mids


@mcp.tool()
def get_book(coin: str, depth: int = 10) -> dict:
    """Order-book snapshot for one market: bids, asks, mid, and spread."""
    return hl.book(coin, depth=depth)


@mcp.tool()
def get_funding(top: int = 0) -> list[dict]:
    """Funding rate, mark/oracle price and open interest per market, sorted by |funding|.

    Set `top` to limit to the N most extreme funding markets (0 = all).
    """
    rows = [r for r in hl.funding() if r["funding"] is not None]
    rows.sort(key=lambda r: abs(r["funding"]), reverse=True)
    return rows[:top] if top else rows


@mcp.tool()
def get_funding_history(coin: str, hours: float = 24) -> list[dict]:
    """Historical funding rates for one market over the last `hours`."""
    return hl.funding_history(coin, hours=hours)


@mcp.tool()
def get_positions(address: str) -> dict:
    """Open perp positions and account value for any address (public data; no key needed)."""
    return hl.positions(address)


@mcp.tool()
def get_open_orders(address: str) -> list[dict]:
    """Resting (open) orders for any address."""
    return hl.open_orders(address)


@mcp.tool()
def get_fills(address: str, limit: int = 50) -> list[dict]:
    """Recent fills for any address, most recent first (capped at `limit`)."""
    return hl.fills(address, limit=limit)


@mcp.tool()
def get_candles(coin: str, interval: str = "1h", hours: float = 24) -> list[dict]:
    """OHLC candles for one market. interval e.g. 1m/15m/1h/4h/1d; lookback `hours`."""
    return hl.candles(coin, interval=interval, hours=hours)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
