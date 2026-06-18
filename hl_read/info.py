"""Read-only Hyperliquid data access.

This module imports ONLY ``hyperliquid.info.Info`` - never ``Exchange``.
There is no code path that can sign a transaction or place/cancel an order,
so ``hl-read`` is *structurally* incapable of moving funds. You never give it
a private key; it only ever reads public on-chain / exchange data.

    from hl_read import HLRead
    hl = HLRead()                 # mainnet
    hl.mids()["BTC"]              # current mid prices
    hl.positions("0xabc...")      # anyone's open positions (public data)
    hl.book("ETH", depth=5)       # normalized order book snapshot
"""
from __future__ import annotations

import time
from typing import Any, Callable, Optional

from hyperliquid.info import Info
from hyperliquid.utils import constants

__all__ = ["HLRead"]


def _f(v: Any) -> Optional[float]:
    """Best-effort float; returns None for missing/empty values."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _ms_ago(*, hours: float = 0, days: float = 0) -> int:
    """Epoch milliseconds for `now - (hours/days)`."""
    return int((time.time() - hours * 3600 - days * 86400) * 1000)


class HLRead:
    """A read-only window onto Hyperliquid public data.

    Construction never opens a websocket (``skip_ws=True``), but it does make a
    couple of synchronous HTTP calls (``meta``/``spotMeta``) to build the
    symbol tables - so it is cheap, not free. Reuse one instance rather than
    constructing per call. Streaming helpers open their own connection on demand.
    """

    def __init__(self, testnet: bool = False) -> None:
        self.testnet = testnet
        self.base_url = constants.TESTNET_API_URL if testnet else constants.MAINNET_API_URL
        self._info = Info(self.base_url, skip_ws=True)

    # -- markets / prices -------------------------------------------------

    def markets(self) -> list[dict]:
        """List every perp market and its key parameters."""
        meta = self._info.meta()
        return [
            {
                "name": a.get("name"),
                "sz_decimals": a.get("szDecimals"),
                "max_leverage": a.get("maxLeverage"),
                "only_isolated": a.get("onlyIsolated", False),
            }
            for a in meta.get("universe", [])
        ]

    def mids(self) -> dict[str, float]:
        """Mid price for every market, keyed by coin (spot keyed as ``@idx``)."""
        return {k: float(v) for k, v in self._info.all_mids().items()}

    def mid(self, coin: str) -> Optional[float]:
        """Mid price for one market (tries the symbol as given and upper-cased)."""
        m = self.mids()
        return m.get(coin) if coin in m else m.get(coin.upper())

    def book(self, coin: str, depth: int = 10) -> dict:
        """Normalized order-book snapshot: ``bids``/``asks`` plus mid & spread."""
        coin = coin.upper()
        snap = self._info.l2_snapshot(coin)
        levels = snap.get("levels") or [[], []]

        def _side(rows: list) -> list[dict]:
            return [
                {"px": float(r["px"]), "sz": float(r["sz"]), "n": r.get("n")}
                for r in rows[:depth]
            ]

        bids, asks = _side(levels[0]), _side(levels[1])
        mid = spread = None
        if bids and asks:
            mid = (bids[0]["px"] + asks[0]["px"]) / 2
            spread = asks[0]["px"] - bids[0]["px"]
        return {
            "coin": coin,
            "time": snap.get("time"),
            "bids": bids,
            "asks": asks,
            "mid": mid,
            "spread": spread,
        }

    def funding(self) -> list[dict]:
        """Current funding / mark / oracle / open-interest context per market."""
        meta, ctxs = self._info.meta_and_asset_ctxs()
        universe = meta.get("universe", [])
        out: list[dict] = []
        for asset, ctx in zip(universe, ctxs):
            out.append(
                {
                    "coin": asset.get("name"),
                    "funding": _f(ctx.get("funding")),
                    "mark_px": _f(ctx.get("markPx")),
                    "oracle_px": _f(ctx.get("oraclePx")),
                    "mid_px": _f(ctx.get("midPx")),
                    "premium": _f(ctx.get("premium")),
                    "open_interest": _f(ctx.get("openInterest")),
                    "day_volume": _f(ctx.get("dayNtlVlm")),
                    "prev_day_px": _f(ctx.get("prevDayPx")),
                }
            )
        return out

    def funding_history(self, coin: str, hours: float = 24) -> list[dict]:
        """Historical funding rates for one market over the last ``hours``."""
        return self._info.funding_history(coin.upper(), _ms_ago(hours=hours))

    def candles(self, coin: str, interval: str = "1h", hours: float = 24) -> list[dict]:
        """OHLC candles for one market. ``interval`` e.g. 1m/15m/1h/4h/1d."""
        return self._info.candles_snapshot(
            coin.upper(), interval, _ms_ago(hours=hours), int(time.time() * 1000)
        )

    # -- per-address (all public data; address only, never a key) ---------

    def positions(self, address: str) -> dict:
        """Account value and open perp positions for any address."""
        st = self._info.user_state(address)
        ms = st.get("marginSummary", {})
        out = {
            "address": address,
            "account_value": _f(ms.get("accountValue")),
            "total_margin_used": _f(ms.get("totalMarginUsed")),
            "total_ntl_pos": _f(ms.get("totalNtlPos")),
            "withdrawable": _f(st.get("withdrawable")),
            "positions": [],
        }
        for ap in st.get("assetPositions", []):
            p = ap.get("position", {})
            out["positions"].append(
                {
                    "coin": p.get("coin"),
                    "size": _f(p.get("szi")),
                    "entry_px": _f(p.get("entryPx")),
                    "position_value": _f(p.get("positionValue")),
                    "unrealized_pnl": _f(p.get("unrealizedPnl")),
                    "return_on_equity": _f(p.get("returnOnEquity")),
                    "liquidation_px": _f(p.get("liquidationPx")),
                    "leverage": p.get("leverage"),
                    "margin_used": _f(p.get("marginUsed")),
                }
            )
        return out

    def open_orders(self, address: str) -> list[dict]:
        """Resting orders for any address."""
        return self._info.open_orders(address)

    def fills(self, address: str, limit: int = 100) -> list[dict]:
        """Recent fills for any address (most recent first), capped at ``limit``."""
        f = self._info.user_fills(address)
        return f[:limit] if limit else f

    def user_funding(self, address: str, days: float = 30) -> list[dict]:
        """Funding payments received/paid by an address over the last ``days``."""
        return self._info.user_funding_history(address, _ms_ago(days=days))

    def referral(self, address: str) -> dict:
        """Referral / builder-code state for an address (cumulative fees etc.)."""
        return self._info.query_referral_state(address)

    def raw_user_state(self, address: str) -> dict:
        """Unmodified ``user_state`` payload, for callers that want everything."""
        return self._info.user_state(address)

    # -- live streaming (opens its own websocket) ------------------------

    def open_stream(self) -> Info:
        """Open one live websocket Info for many subscriptions.

        Add feeds with ``info.subscribe({"type": "trades"|"l2Book", "coin": C}, cb)``
        and close with ``info.disconnect_websocket()``. Cheaper than one stream
        per coin since all feeds share a single socket.
        """
        return Info(self.base_url, skip_ws=False)

    def stream_book(self, coin: str, callback: Callable[[dict], None]) -> Info:
        """Subscribe to live L2 book updates. Returns the live Info; keep it alive."""
        info = Info(self.base_url, skip_ws=False)
        info.subscribe({"type": "l2Book", "coin": coin.upper()}, callback)
        return info

    def stream_trades(self, coin: str, callback: Callable[[dict], None]) -> Info:
        """Subscribe to the live trade tape. Returns the live Info; keep it alive."""
        info = Info(self.base_url, skip_ws=False)
        info.subscribe({"type": "trades", "coin": coin.upper()}, callback)
        return info
