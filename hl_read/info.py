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

Every HTTP read goes through a small resilience layer: a default request
timeout, exponential backoff with jitter on transient failures (network
errors, HTTP 429/5xx), an optional client-side rate limit, and short-lived
caching for the high-frequency market endpoints. All of it is configurable on
the constructor and safe to leave at the defaults.
"""
from __future__ import annotations

import random
import threading
import time
from typing import Any, Callable, Optional

from hyperliquid.info import Info
from hyperliquid.utils import constants

__all__ = ["HLRead", "HLReadError"]


class HLReadError(RuntimeError):
    """A Hyperliquid read failed after exhausting retries (transient errors)."""


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


# Exception class names that always mean "retry might help" regardless of the
# concrete library (requests / urllib3 / the SDK's own ServerError).
_TRANSIENT_NAMES = frozenset(
    {
        "ConnectionError",
        "ConnectTimeout",
        "ConnectionResetError",
        "ChunkedEncodingError",
        "ProtocolError",
        "ReadTimeout",
        "ReadTimeoutError",
        "RemoteDisconnected",
        "ServerError",
        "Timeout",
    }
)
_TRANSIENT_STATUS = frozenset({429, 500, 502, 503, 504})


def _is_transient(e: Exception) -> bool:
    """True if ``e`` is a transient error worth retrying (vs a real 4xx/bug)."""
    if type(e).__name__ in _TRANSIENT_NAMES:
        return True
    code = getattr(e, "status_code", None)
    if code is None:
        code = getattr(e, "code", None)
    try:
        if int(code) in _TRANSIENT_STATUS:
            return True
    except (TypeError, ValueError):
        pass
    msg = str(e).lower()
    return "429" in msg or "rate limit" in msg or "too many requests" in msg


class HLRead:
    """A read-only window onto Hyperliquid public data.

    Construction never opens a websocket (``skip_ws=True``), but it does make a
    couple of synchronous HTTP calls (``meta``/``spotMeta``) to build the
    symbol tables - so it is cheap, not free. Reuse one instance rather than
    constructing per call. Streaming helpers open their own connection on demand.

    Resilience knobs (all keyword-only, sensible defaults):

    * ``max_retries`` - retry attempts for transient failures (default 4).
    * ``backoff_base`` / ``backoff_max`` - exponential backoff window, seconds.
    * ``rate_limit_per_min`` - if set, space HTTP calls to at most N per minute.
    * ``cache_ttl`` - seconds to cache fast market data (mids / funding ctxs).
      Set to 0 to always fetch fresh.
    * ``meta_ttl`` - seconds to cache the slow-changing market/token tables.
    * ``http_timeout`` - per-request timeout so a stalled socket can't hang.
    """

    def __init__(
        self,
        testnet: bool = False,
        *,
        max_retries: int = 4,
        backoff_base: float = 0.4,
        backoff_max: float = 8.0,
        rate_limit_per_min: Optional[float] = None,
        cache_ttl: float = 1.0,
        meta_ttl: float = 300.0,
        http_timeout: Optional[float] = 10.0,
    ) -> None:
        self.testnet = testnet
        self.base_url = constants.TESTNET_API_URL if testnet else constants.MAINNET_API_URL
        self.max_retries = max(0, int(max_retries))
        self.backoff_base = float(backoff_base)
        self.backoff_max = float(backoff_max)
        self.cache_ttl = float(cache_ttl)
        self.meta_ttl = float(meta_ttl)
        self._min_interval = (60.0 / rate_limit_per_min) if rate_limit_per_min else 0.0

        # Pass the timeout where the SDK actually applies it: ``API.post`` sends
        # ``timeout=self.timeout`` on every call, so a stalled socket raises (and
        # the retry loop can react) instead of hanging forever.
        self._info = Info(self.base_url, skip_ws=True, timeout=http_timeout)
        self._lock = threading.Lock()        # guards the cache
        self._rate_lock = threading.Lock()   # guards only the rate-limiter clock
        self._cache: dict[str, tuple[float, Any]] = {}
        self._last_call = 0.0

        # Belt-and-suspenders: also enforce the timeout at the session layer in
        # case the SDK forwards an explicit ``timeout=None`` (which would beat a
        # plain setdefault), so we override None specifically.
        if http_timeout:
            try:
                sess = getattr(self._info, "session", None)
                if sess is not None:
                    _orig = sess.request

                    def _request(*args: Any, **kwargs: Any) -> Any:
                        if kwargs.get("timeout") is None:
                            kwargs["timeout"] = http_timeout
                        return _orig(*args, **kwargs)

                    sess.request = _request  # type: ignore[method-assign]
            except Exception:
                pass

    # -- internal: rate limit + retry + cache ----------------------------

    def _throttle(self) -> None:
        # Uses its own lock, never the cache lock, so a throttle sleep can never
        # block an unrelated cache read.
        if self._min_interval <= 0:
            return
        with self._rate_lock:
            wait = self._min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()

    def _call(self, fn: Callable[..., Any], *args: Any) -> Any:
        """Invoke an SDK method with rate limiting + retry/backoff on transients."""
        attempt = 0
        while True:
            self._throttle()
            try:
                return fn(*args)
            except Exception as e:  # noqa: BLE001 - classified by _is_transient
                if not _is_transient(e):
                    raise
                attempt += 1
                if attempt > self.max_retries:
                    raise HLReadError(
                        f"Hyperliquid read failed after {self.max_retries} "
                        f"retries: {type(e).__name__}: {e}"
                    ) from e
                delay = min(self.backoff_base * (2 ** (attempt - 1)), self.backoff_max)
                time.sleep(delay * (0.7 + 0.6 * random.random()))  # full-ish jitter

    def _cached(self, key: str, ttl: float, producer: Callable[[], Any]) -> Any:
        if ttl <= 0:
            return producer()
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None and (now - hit[0]) < ttl:
                return hit[1]
        value = producer()  # produced outside the lock (network call)
        with self._lock:
            self._cache[key] = (time.monotonic(), value)
        return value

    def clear_cache(self) -> None:
        """Drop all cached market/meta data; the next read fetches fresh."""
        with self._lock:
            self._cache.clear()

    # -- raw cached fetchers ---------------------------------------------

    def _meta_raw(self) -> dict:
        return self._cached("meta", self.meta_ttl, lambda: self._call(self._info.meta))

    def _spot_meta_raw(self) -> dict:
        return self._cached("spot_meta", self.meta_ttl, lambda: self._call(self._info.spot_meta))

    def _all_mids_raw(self) -> dict:
        return self._cached("all_mids", self.cache_ttl, lambda: self._call(self._info.all_mids))

    def _meta_ctxs_raw(self) -> tuple:
        return self._cached(
            "meta_ctxs", self.cache_ttl, lambda: self._call(self._info.meta_and_asset_ctxs)
        )

    # -- markets / prices -------------------------------------------------

    def markets(self) -> list[dict]:
        """List every perp market and its key parameters."""
        meta = self._meta_raw()
        return [
            {
                "name": a.get("name"),
                "sz_decimals": a.get("szDecimals"),
                "max_leverage": a.get("maxLeverage"),
                "only_isolated": a.get("onlyIsolated", False),
            }
            for a in meta.get("universe", [])
        ]

    def spot_markets(self) -> list[dict]:
        """List every spot market: pair name, base/quote token, and mid price.

        Spot mids live in ``all_mids`` under either the pair name or an ``@idx``
        key; we resolve both so ``mid`` is populated whichever form is used.
        """
        meta = self._spot_meta_raw()
        tokens = {t.get("index"): t for t in meta.get("tokens", [])}
        mids = self.mids()
        out: list[dict] = []
        for u in meta.get("universe", []):
            name = u.get("name")
            idx = u.get("index")
            pair = (u.get("tokens") or [None, None])
            base = tokens.get(pair[0] if len(pair) > 0 else None, {})
            quote = tokens.get(pair[1] if len(pair) > 1 else None, {})
            at_key = f"@{idx}" if idx is not None else None
            mid = mids.get(name)
            if mid is None and at_key is not None:
                mid = mids.get(at_key)
            out.append(
                {
                    "name": name,
                    "index": idx,
                    "base": base.get("name"),
                    "quote": quote.get("name"),
                    "sz_decimals": base.get("szDecimals"),
                    "mid": _f(mid),
                }
            )
        return out

    def mids(self) -> dict[str, float]:
        """Mid price for every market, keyed by coin (spot keyed as ``@idx``)."""
        return {k: float(v) for k, v in self._all_mids_raw().items()}

    def mid(self, coin: str) -> Optional[float]:
        """Mid price for one market (tries the symbol as given and upper-cased).

        Backed by the cached ``mids`` table, so calling it in a loop costs at
        most one fetch per ``cache_ttl`` window rather than one per call.
        """
        m = self.mids()
        return m.get(coin) if coin in m else m.get(coin.upper())

    def book(self, coin: str, depth: int = 10) -> dict:
        """Normalized order-book snapshot: ``bids``/``asks`` plus mid & spread."""
        coin = coin.upper()
        snap = self._call(self._info.l2_snapshot, coin)
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
        meta, ctxs = self._meta_ctxs_raw()
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
        return self._call(self._info.funding_history, coin.upper(), _ms_ago(hours=hours))

    def predicted_fundings(self) -> list[dict]:
        """Predicted upcoming funding per coin across venues (Hyperliquid + CEXes).

        One row per coin with a list of per-venue predictions, so you can
        compare Hyperliquid's predicted funding against Binance/Bybit/etc.
        Rates are per the venue's own ``funding_interval_hours`` (HL is hourly,
        most CEXes are 4-8h), so normalize before comparing magnitudes::

            [{"coin": "BTC",
              "venues": [{"venue": "HlPerp", "funding_rate": 0.0000125,
                          "next_funding_time": 1781848800000,
                          "funding_interval_hours": 1}, ...]}, ...]
        """
        raw = self._cached(
            "predicted_fundings",
            self.cache_ttl,
            lambda: self._call(self._info.post, "/info", {"type": "predictedFundings"}),
        )
        out: list[dict] = []
        for entry in raw or []:
            if not entry or len(entry) < 2:
                continue
            coin, venues_raw = entry[0], entry[1] or []
            venues = []
            for v in venues_raw:
                if not v or len(v) < 2:
                    continue
                name, d = v[0], (v[1] or {})
                venues.append(
                    {
                        "venue": name,
                        "funding_rate": _f(d.get("fundingRate")),
                        "next_funding_time": d.get("nextFundingTime"),
                        "funding_interval_hours": d.get("fundingIntervalHours"),
                    }
                )
            out.append({"coin": coin, "venues": venues})
        return out

    def candles(self, coin: str, interval: str = "1h", hours: float = 24) -> list[dict]:
        """OHLC candles for one market. ``interval`` e.g. 1m/15m/1h/4h/1d."""
        return self._call(
            self._info.candles_snapshot,
            coin.upper(),
            interval,
            _ms_ago(hours=hours),
            int(time.time() * 1000),
        )

    # -- per-address (all public data; address only, never a key) ---------

    def positions(self, address: str) -> dict:
        """Account value and open perp positions for any address."""
        st = self._call(self._info.user_state, address)
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

    def portfolio(self, address: str) -> dict:
        """Account-value / PnL history for any address across time windows.

        One entry per period (``day``/``week``/``month``/``allTime`` and their
        ``perp``-only variants), each with the time series plus a small summary
        (start/end account value, cumulative period PnL, and volume)::

            {"address": "0x..",
             "periods": {"day": {"account_value_history": [{"time", "value"}],
                                 "pnl_history": [{"time", "pnl"}],
                                 "vlm": float, "start_value": float,
                                 "end_value": float, "period_pnl": float}, ...}}
        """
        raw = self._call(self._info.portfolio, address)
        periods: dict[str, dict] = {}
        for entry in raw or []:
            if not entry or len(entry) < 2:
                continue
            name, d = entry[0], (entry[1] or {})
            avh = [
                {"time": pt[0], "value": _f(pt[1])}
                for pt in (d.get("accountValueHistory") or [])
                if pt and len(pt) >= 2
            ]
            pnl = [
                {"time": pt[0], "pnl": _f(pt[1])}
                for pt in (d.get("pnlHistory") or [])
                if pt and len(pt) >= 2
            ]
            periods[name] = {
                "account_value_history": avh,
                "pnl_history": pnl,
                "vlm": _f(d.get("vlm")),
                "start_value": avh[0]["value"] if avh else None,
                "end_value": avh[-1]["value"] if avh else None,
                "period_pnl": pnl[-1]["pnl"] if pnl else None,
            }
        return {"address": address, "periods": periods}

    def spot_balances(self, address: str) -> dict:
        """Spot token balances for any address (public; address only, no key)."""
        st = self._call(self._info.spot_user_state, address)
        balances = [
            {
                "coin": b.get("coin"),
                "token": b.get("token"),
                "total": _f(b.get("total")),
                "hold": _f(b.get("hold")),
                "entry_ntl": _f(b.get("entryNtl")),
            }
            for b in st.get("balances", [])
        ]
        return {"address": address, "balances": balances}

    def open_orders(self, address: str) -> list[dict]:
        """Resting orders for any address."""
        return self._call(self._info.open_orders, address)

    def fills(self, address: str, limit: int = 100) -> list[dict]:
        """Recent fills for any address (most recent first), capped at ``limit``."""
        f = self._call(self._info.user_fills, address)
        return f[:limit] if limit else f

    def fills_by_time(
        self, address: str, start_ms: int, end_ms: Optional[int] = None, *, aggregate: bool = False
    ) -> list[dict]:
        """Fills for an address within a time window (epoch milliseconds).

        ``end_ms`` defaults to now. Set ``aggregate`` to combine the partial
        fills of a single crossing order. The API pages at 2000 fills per call,
        so narrow the window if you hit that. Window-bounded, hence no ``limit``.
        """
        return self._call(
            self._info.user_fills_by_time,
            address,
            int(start_ms),
            int(end_ms) if end_ms is not None else None,
            aggregate,
        )

    def ledger(self, address: str, start_ms: int = 0, end_ms: Optional[int] = None) -> list[dict]:
        """Non-funding ledger updates for any address within a time window (epoch ms).

        Covers deposits, withdrawals, transfers, and vault moves - everything
        except funding payments (use ``user_funding`` for those). ``start_ms`` 0
        means from the beginning; ``end_ms`` defaults to now. The API pages at
        2000 updates per call, so narrow the window for very active accounts.

        Each row keeps the raw ``delta`` and surfaces ``time``/``type``/``usdc``::

            [{"time": int, "hash": str, "type": "deposit",
              "usdc": 250.5, "delta": {...full raw delta...}}, ...]
        """
        raw = self._call(
            self._info.user_non_funding_ledger_updates,
            address,
            int(start_ms),
            int(end_ms) if end_ms is not None else None,
        )
        out: list[dict] = []
        for u in raw or []:
            d = u.get("delta") or {}
            out.append(
                {
                    "time": u.get("time"),
                    "hash": u.get("hash"),
                    "type": d.get("type"),
                    "usdc": _f(d.get("usdc")),
                    "delta": d,
                }
            )
        return out

    def user_funding(self, address: str, days: float = 30) -> list[dict]:
        """Funding payments received/paid by an address over the last ``days``."""
        return self._call(self._info.user_funding_history, address, _ms_ago(days=days))

    def referral(self, address: str) -> dict:
        """Referral / builder-code state for an address (cumulative fees etc.)."""
        return self._call(self._info.query_referral_state, address)

    def raw_user_state(self, address: str) -> dict:
        """Unmodified ``user_state`` payload, for callers that want everything."""
        return self._call(self._info.user_state, address)

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

    def stream_user_events(self, address: str, callback: Callable[[dict], None]) -> Info:
        """Subscribe to an address's live events (fills, funding, liquidations).

        Returns the live Info; keep it alive and close with
        ``disconnect_websocket()``. Read-only: it observes events, it can't act.
        """
        info = Info(self.base_url, skip_ws=False)
        info.subscribe({"type": "userEvents", "user": address}, callback)
        return info
