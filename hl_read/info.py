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

__all__ = ["HLRead", "HLReadError", "ResilientStream"]


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
        api_url: Optional[str] = None,
        fallback_urls: Optional[list] = None,
    ) -> None:
        self.testnet = testnet
        primary = api_url or (constants.TESTNET_API_URL if testnet else constants.MAINNET_API_URL)
        # Endpoint list: primary first, then optional fallbacks tried on
        # persistent failure. Hyperliquid mainnet is a single official host, so
        # fallbacks are a no-op unless you point at a proxy/mirror of your own.
        self._urls = [primary] + [u for u in (fallback_urls or []) if u]
        self._url_idx = 0
        self.base_url = primary
        self.max_retries = max(0, int(max_retries))
        self.backoff_base = float(backoff_base)
        self.backoff_max = float(backoff_max)
        self.cache_ttl = float(cache_ttl)
        self.meta_ttl = float(meta_ttl)
        self._http_timeout = http_timeout
        self._min_interval = (60.0 / rate_limit_per_min) if rate_limit_per_min else 0.0

        self._lock = threading.Lock()        # guards the cache
        self._rate_lock = threading.Lock()   # guards only the rate-limiter clock
        self._cache: dict[str, tuple[float, Any]] = {}
        self._last_call = 0.0
        self._info = None
        self._connect_initial()

    def _connect_initial(self) -> None:
        """Build the initial ``Info``, walking fallbacks if the primary won't
        connect (the SDK constructor itself hits the network to load symbol
        tables, so a down primary must fail over here too, not just at read time).
        """
        last_err = None
        for i in range(len(self._urls)):
            self._url_idx = i
            self.base_url = self._urls[i]
            try:
                self._info = self._make_info(self.base_url)
                return
            except Exception as e:  # noqa: BLE001 - try the next configured host
                last_err = e
        raise HLReadError(
            f"could not connect to any endpoint {self._urls}: "
            f"{type(last_err).__name__}: {last_err}"
        ) from last_err

    def _make_info(self, url: str) -> Info:
        """Build a read-only ``Info`` for ``url`` with the request timeout enforced.

        The timeout goes to the constructor (where ``API.post`` reads it) AND is
        re-applied at the session layer, since the SDK forwards an explicit
        ``timeout=None`` that would beat a plain ``setdefault``.
        """
        info = Info(url, skip_ws=True, timeout=self._http_timeout)
        ht = self._http_timeout
        if ht:
            try:
                sess = getattr(info, "session", None)
                if sess is not None:
                    _orig = sess.request

                    def _request(*args: Any, **kwargs: Any) -> Any:
                        if kwargs.get("timeout") is None:
                            kwargs["timeout"] = ht
                        return _orig(*args, **kwargs)

                    sess.request = _request  # type: ignore[method-assign]
            except Exception:
                pass
        return info

    def _failover(self) -> bool:
        """Switch to the next configured fallback host (rebuilding ``Info``).

        Returns False when no further host is reachable. Hosts whose ``Info``
        can't even be constructed are skipped.
        """
        while self._url_idx + 1 < len(self._urls):
            self._url_idx += 1
            self.base_url = self._urls[self._url_idx]
            try:
                self._info = self._make_info(self.base_url)
                return True
            except Exception:  # noqa: BLE001 - that host is down too; try the next
                continue
        return False

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

    def _call(self, fn, *args: Any) -> Any:
        """Invoke an SDK read with rate limiting, retry/backoff, and (if
        configured) host failover.

        ``fn`` may be a method *name* (str) - re-resolved against the current
        ``Info`` on each attempt, so a failover transparently rebinds it - or a
        bound callable (used as-is; callables can't be rebound across a failover).
        """
        last_err: Optional[Exception] = None
        for host_i in range(max(1, len(self._urls))):
            attempt = 0
            while True:
                self._throttle()
                try:
                    target = getattr(self._info, fn) if isinstance(fn, str) else fn
                    return target(*args)
                except Exception as e:  # noqa: BLE001 - classified by _is_transient
                    if not _is_transient(e):
                        raise
                    last_err = e
                    attempt += 1
                    if attempt > self.max_retries:
                        break
                    delay = min(self.backoff_base * (2 ** (attempt - 1)), self.backoff_max)
                    time.sleep(delay * (0.7 + 0.6 * random.random()))  # full-ish jitter
            if host_i + 1 < len(self._urls) and self._failover():
                continue
            break
        across = f" across {len(self._urls)} hosts" if len(self._urls) > 1 else ""
        raise HLReadError(
            f"Hyperliquid read failed after {self.max_retries} retries{across}: "
            f"{type(last_err).__name__}: {last_err}"
        ) from last_err

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

    def health(self) -> dict:
        """Liveness probe: is the API reachable, and how fast?

        Does a single lightweight read (deliberately *not* through ``_call`` -
        this measures the real round-trip, not the retry layer) and reports
        latency plus a basic sanity signal. Never raises: a failure comes back
        as ``ok=False`` with an ``error`` string::

            {"api_url", "testnet", "ok": bool, "latency_ms": float|None,
             "markets": int|None, "error": str|None}
        """
        out = {
            "api_url": self.base_url,
            "testnet": self.testnet,
            "ok": False,
            "latency_ms": None,
            "markets": None,
            "error": None,
        }
        t0 = time.monotonic()
        try:
            mids = self._info.all_mids()
            out["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
            out["markets"] = len(mids) if hasattr(mids, "__len__") else None
            out["ok"] = bool(mids)
        except Exception as e:  # noqa: BLE001 - report, never raise from a probe
            out["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
            out["error"] = f"{type(e).__name__}: {e}"
        return out

    # -- raw cached fetchers ---------------------------------------------

    def _meta_raw(self) -> dict:
        return self._cached("meta", self.meta_ttl, lambda: self._call("meta"))

    def _spot_meta_raw(self) -> dict:
        return self._cached("spot_meta", self.meta_ttl, lambda: self._call("spot_meta"))

    def _all_mids_raw(self) -> dict:
        return self._cached("all_mids", self.cache_ttl, lambda: self._call("all_mids"))

    def _meta_ctxs_raw(self) -> tuple:
        return self._cached(
            "meta_ctxs", self.cache_ttl, lambda: self._call("meta_and_asset_ctxs")
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
        snap = self._call("l2_snapshot", coin)
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
        return self._call("funding_history", coin.upper(), _ms_ago(hours=hours))

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
            lambda: self._call("post", "/info", {"type": "predictedFundings"}),
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
            "candles_snapshot",
            coin.upper(),
            interval,
            _ms_ago(hours=hours),
            int(time.time() * 1000),
        )

    # -- per-address (all public data; address only, never a key) ---------

    def positions(self, address: str) -> dict:
        """Account value and open perp positions for any address."""
        st = self._call("user_state", address)
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
        raw = self._call("portfolio", address)
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
        st = self._call("spot_user_state", address)
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
        return self._call("open_orders", address)

    def fills(self, address: str, limit: int = 100) -> list[dict]:
        """Recent fills for any address (most recent first), capped at ``limit``."""
        f = self._call("user_fills", address)
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
            "user_fills_by_time",
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
            "user_non_funding_ledger_updates",
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
        return self._call("user_funding_history", address, _ms_ago(days=days))

    def referral(self, address: str) -> dict:
        """Referral / builder-code state for an address (cumulative fees etc.)."""
        return self._call("query_referral_state", address)

    def raw_user_state(self, address: str) -> dict:
        """Unmodified ``user_state`` payload, for callers that want everything."""
        return self._call("user_state", address)

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

    # -- resilient streaming (auto-reconnect; survives dropped connections) --

    def resilient_stream(
        self,
        subscriptions: list,
        *,
        on_reconnect: Optional[Callable[[], None]] = None,
        backoff_base: float = 1.0,
        backoff_max: float = 30.0,
    ) -> "ResilientStream":
        """Open auto-reconnecting websocket subscriptions.

        The SDK's websocket has no reconnect: when the socket drops, its thread
        dies and the subscriptions are lost. This supervises the connection and,
        on drop, rebuilds it and re-subscribes with exponential backoff.

        ``subscriptions`` is a list of ``(subscription_dict, callback)`` pairs,
        e.g. ``[({"type": "l2Book", "coin": "BTC"}, cb)]``. Returns a *started*
        ``ResilientStream``; call ``.close()`` to stop. Read-only: it only ever
        subscribes to public feeds.
        """
        return ResilientStream(
            self.base_url,
            subscriptions,
            on_reconnect=on_reconnect,
            backoff_base=backoff_base,
            backoff_max=backoff_max,
        ).start()

    def resilient_stream_book(self, coin: str, callback: Callable[[dict], None], **kw) -> "ResilientStream":
        """Auto-reconnecting L2 book stream for one coin."""
        return self.resilient_stream([({"type": "l2Book", "coin": coin.upper()}, callback)], **kw)

    def resilient_stream_trades(self, coin: str, callback: Callable[[dict], None], **kw) -> "ResilientStream":
        """Auto-reconnecting trade-tape stream for one coin."""
        return self.resilient_stream([({"type": "trades", "coin": coin.upper()}, callback)], **kw)

    def resilient_stream_user_events(self, address: str, callback: Callable[[dict], None], **kw) -> "ResilientStream":
        """Auto-reconnecting user-events stream (fills/funding/liquidations) for an address."""
        return self.resilient_stream([({"type": "userEvents", "user": address}, callback)], **kw)


class ResilientStream:
    """Keeps a set of Hyperliquid websocket subscriptions alive across drops.

    The SDK's ``WebsocketManager`` calls ``run_forever()`` with no reconnect and
    has no on-close handler, so a dropped socket ends the thread and silently
    loses every subscription. This runs a small supervisor thread that watches
    the underlying ws thread and, when it dies, tears the connection down and
    rebuilds it (re-subscribing all feeds) with exponential backoff.

    Read-only by construction: it only opens ``Info(skip_ws=False)`` and calls
    ``subscribe`` - there is no trade/sign path here either.
    """

    def __init__(
        self,
        base_url: str,
        subscriptions: list,
        *,
        on_reconnect: Optional[Callable[[], None]] = None,
        backoff_base: float = 1.0,
        backoff_max: float = 30.0,
        poll_interval: float = 0.5,
        _info_factory: Optional[Callable[[str], Info]] = None,
    ) -> None:
        self._base_url = base_url
        self._subs = list(subscriptions)
        self._on_reconnect = on_reconnect
        self._backoff_base = float(backoff_base)
        self._backoff_max = float(backoff_max)
        self._poll = float(poll_interval)
        self._info_factory = _info_factory or (lambda url: Info(url, skip_ws=False))
        self._info: Optional[Info] = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="hl-read-resilient-ws", daemon=True)

    def start(self) -> "ResilientStream":
        self._thread.start()
        return self

    def _connect(self) -> Info:
        info = self._info_factory(self._base_url)
        for sub, cb in self._subs:
            info.subscribe(sub, cb)
        return info

    def _safe_disconnect(self) -> None:
        info, self._info = self._info, None
        if info is not None:
            try:
                info.disconnect_websocket()
            except Exception:  # noqa: BLE001 - best-effort teardown
                pass

    def _run(self) -> None:
        attempt = 0
        first = True
        while not self._stop.is_set():
            try:
                self._info = self._connect()
            except Exception:  # noqa: BLE001 - connect failed; back off and retry
                self._stop.wait(min(self._backoff_base * (2 ** attempt), self._backoff_max))
                attempt += 1
                continue
            if not first and self._on_reconnect is not None:
                try:
                    self._on_reconnect()
                except Exception:  # noqa: BLE001 - user callback must not kill the loop
                    pass
            first = False
            attempt = 0
            mgr = getattr(self._info, "ws_manager", None)
            while not self._stop.is_set() and mgr is not None and mgr.is_alive():
                self._stop.wait(self._poll)
            if self._stop.is_set():
                break
            # connection dropped -> tear down, back off, then reconnect at loop top
            self._safe_disconnect()
            self._stop.wait(min(self._backoff_base * (2 ** attempt), self._backoff_max))
            attempt += 1
        self._safe_disconnect()

    def close(self) -> None:
        """Stop supervising and close the underlying websocket."""
        self._stop.set()
        self._safe_disconnect()

    @property
    def connected(self) -> bool:
        mgr = getattr(self._info, "ws_manager", None)
        return mgr is not None and mgr.is_alive()
