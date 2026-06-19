"""Offline unit tests for the hl-read resilience + parsing layer.

No network: the SDK ``Info`` is mocked at construction and a fake is injected
for method tests. Run with ``pytest`` or ``python -m unittest``.
"""
import unittest
from unittest import mock

import time

from hl_read.info import HLRead, HLReadError, ResilientStream, _f, _is_transient


def make_hl(**kw):
    """Construct HLRead without the real SDK touching the network."""
    with mock.patch("hl_read.info.Info"):
        return HLRead(**kw)


class Timeout(Exception):
    """Stand-in whose class name is in the transient allow-list."""


class FakeErr(Exception):
    def __init__(self, msg="", status_code=None):
        super().__init__(msg)
        if status_code is not None:
            self.status_code = status_code


class TestIsTransient(unittest.TestCase):
    def test_name_based(self):
        self.assertTrue(_is_transient(Timeout()))

    def test_status_code(self):
        self.assertTrue(_is_transient(FakeErr("x", status_code=429)))
        self.assertTrue(_is_transient(FakeErr("x", status_code=503)))
        self.assertFalse(_is_transient(FakeErr("bad request", status_code=400)))

    def test_message_based(self):
        self.assertTrue(_is_transient(Exception("HTTP 429 Too Many Requests")))
        self.assertTrue(_is_transient(Exception("rate limit exceeded")))
        self.assertFalse(_is_transient(ValueError("genuine bug")))


class TestFloat(unittest.TestCase):
    def test_f(self):
        for bad in (None, "", "abc", [1]):
            self.assertIsNone(_f(bad))
        self.assertEqual(_f("3.5"), 3.5)
        self.assertEqual(_f(2), 2.0)


class TestCall(unittest.TestCase):
    def test_retries_then_succeeds(self):
        hl = make_hl(max_retries=3, backoff_base=0.0)
        state = {"n": 0}

        def fn():
            state["n"] += 1
            if state["n"] < 3:
                raise Timeout()
            return "ok"

        with mock.patch("hl_read.info.time.sleep"):
            self.assertEqual(hl._call(fn), "ok")
        self.assertEqual(state["n"], 3)

    def test_non_transient_raises_immediately(self):
        hl = make_hl(max_retries=5)
        with self.assertRaises(ValueError):
            hl._call(lambda: (_ for _ in ()).throw(ValueError("real bug")))

    def test_exhausts_to_hlreaderror(self):
        hl = make_hl(max_retries=2, backoff_base=0.0)

        def fn():
            raise Timeout("boom")

        with mock.patch("hl_read.info.time.sleep"):
            with self.assertRaises(HLReadError):
                hl._call(fn)


class TestCache(unittest.TestCase):
    def test_caches_within_ttl(self):
        hl = make_hl()
        state = {"n": 0}

        def producer():
            state["n"] += 1
            return state["n"]

        self.assertEqual(hl._cached("k", 100.0, producer), 1)
        self.assertEqual(hl._cached("k", 100.0, producer), 1)
        self.assertEqual(state["n"], 1)

    def test_ttl_zero_always_fetches(self):
        hl = make_hl()
        state = {"n": 0}

        def producer():
            state["n"] += 1
            return state["n"]

        hl._cached("k", 0.0, producer)
        hl._cached("k", 0.0, producer)
        self.assertEqual(state["n"], 2)

    def test_clear_cache(self):
        hl = make_hl()
        state = {"n": 0}

        def producer():
            state["n"] += 1
            return state["n"]

        hl._cached("k", 100.0, producer)
        hl.clear_cache()
        hl._cached("k", 100.0, producer)
        self.assertEqual(state["n"], 2)


class TestConstruction(unittest.TestCase):
    def test_timeout_passed_to_sdk(self):
        # The headline resilience knob must reach the SDK, where API.post reads it.
        with mock.patch("hl_read.info.Info") as FakeInfo:
            HLRead(http_timeout=12.5)
        _, kwargs = FakeInfo.call_args
        self.assertEqual(kwargs.get("timeout"), 12.5)

    def test_separate_locks(self):
        hl = make_hl(rate_limit_per_min=60)
        self.assertIsNot(hl._lock, hl._rate_lock)

    def test_api_url_override(self):
        with mock.patch("hl_read.info.Info"):
            hl = HLRead(api_url="https://proxy.example")
        self.assertEqual(hl.base_url, "https://proxy.example")
        self.assertEqual(hl._urls, ["https://proxy.example"])

    def test_default_single_host(self):
        hl = make_hl()
        self.assertEqual(len(hl._urls), 1)


class TestFailover(unittest.TestCase):
    def test_failover_to_fallback_host(self):
        info_a = mock.MagicMock()
        info_a.all_mids.side_effect = Timeout("host A down")
        info_b = mock.MagicMock()
        info_b.all_mids.return_value = {"BTC": "1"}
        with mock.patch("hl_read.info.Info", side_effect=[info_a, info_b]):
            hl = HLRead(api_url="https://a", fallback_urls=["https://b"],
                        max_retries=1, backoff_base=0.0)
            with mock.patch("hl_read.info.time.sleep"):
                out = hl._call("all_mids")
        self.assertEqual(out, {"BTC": "1"})    # succeeded on the fallback
        self.assertEqual(hl.base_url, "https://b")  # switched host

    def test_construct_fails_over_when_primary_unreachable(self):
        # The SDK Info() constructor hits the network, so a down primary must
        # fall over at construction time, not only on later reads.
        info_b = mock.MagicMock()
        with mock.patch("hl_read.info.Info", side_effect=[ConnectionError("dns"), info_b]):
            hl = HLRead(api_url="https://bad", fallback_urls=["https://good"])
        self.assertEqual(hl.base_url, "https://good")
        self.assertIs(hl._info, info_b)

    def test_construct_raises_when_all_hosts_down(self):
        with mock.patch("hl_read.info.Info", side_effect=ConnectionError("dns")):
            with self.assertRaises(HLReadError):
                HLRead(api_url="https://bad", fallback_urls=["https://also-bad"])

    def test_no_failover_when_single_host(self):
        info_a = mock.MagicMock()
        info_a.all_mids.side_effect = Timeout("down")
        with mock.patch("hl_read.info.Info", side_effect=[info_a]):
            hl = HLRead(api_url="https://a", max_retries=1, backoff_base=0.0)
            with mock.patch("hl_read.info.time.sleep"):
                with self.assertRaises(HLReadError):
                    hl._call("all_mids")
        self.assertEqual(hl.base_url, "https://a")


class TestHealth(unittest.TestCase):
    def _hl_with(self, fake):
        hl = make_hl()
        hl._info = fake
        return hl

    def test_ok(self):
        fake = mock.MagicMock()
        fake.all_mids.return_value = {"BTC": "1", "ETH": "2"}
        h = self._hl_with(fake).health()
        self.assertTrue(h["ok"])
        self.assertEqual(h["markets"], 2)
        self.assertIsNone(h["error"])
        self.assertIsNotNone(h["latency_ms"])

    def test_down_does_not_raise(self):
        fake = mock.MagicMock()
        fake.all_mids.side_effect = Timeout("boom")
        h = self._hl_with(fake).health()
        self.assertFalse(h["ok"])
        self.assertIn("Timeout", h["error"])
        self.assertIsNotNone(h["latency_ms"])  # elapsed time still recorded

    def test_no_retry(self):
        # health probes once - it must not invoke the retry layer.
        fake = mock.MagicMock()
        fake.all_mids.side_effect = Timeout("boom")
        self._hl_with(fake).health()
        self.assertEqual(fake.all_mids.call_count, 1)


class _FakeMgr:
    """Stand-in for the SDK WebsocketManager thread. is_alive() drains a script."""

    def __init__(self, alive_sequence):
        self._seq = list(alive_sequence)

    def is_alive(self):
        return self._seq.pop(0) if self._seq else False


class _FakeWsInfo:
    def __init__(self, mgr):
        self.ws_manager = mgr
        self.subscribed = []
        self.disconnected = False

    def subscribe(self, sub, cb):
        self.subscribed.append(sub)

    def disconnect_websocket(self):
        self.disconnected = True


class TestResilientStream(unittest.TestCase):
    def test_reconnects_and_resubscribes_on_drop(self):
        infos = []

        def factory(_url):
            fi = _FakeWsInfo(_FakeMgr([True, False]))  # alive once, then dropped
            infos.append(fi)
            return fi

        sub = {"type": "l2Book", "coin": "BTC"}
        rs = ResilientStream(
            "https://x", [(sub, lambda m: None)],
            backoff_base=0.0, backoff_max=0.0, poll_interval=0.01, _info_factory=factory,
        ).start()
        time.sleep(0.2)  # enough cycles for at least one reconnect
        rs.close()
        self.assertGreaterEqual(len(infos), 2)          # it rebuilt the connection
        self.assertEqual(infos[0].subscribed, [sub])    # subscribed on first connect
        self.assertTrue(infos[0].disconnected)          # old connection torn down
        self.assertEqual(infos[1].subscribed, [sub])    # re-subscribed on reconnect

    def test_close_stops_and_disconnects(self):
        infos = []

        def factory(_url):
            fi = _FakeWsInfo(_FakeMgr([True] * 1000))  # stays "alive"
            infos.append(fi)
            return fi

        rs = ResilientStream(
            "https://x", [({"type": "trades", "coin": "ETH"}, lambda m: None)],
            poll_interval=0.01, _info_factory=factory,
        ).start()
        time.sleep(0.05)
        rs.close()
        time.sleep(0.05)
        self.assertEqual(len(infos), 1)            # no spurious reconnects while healthy
        self.assertTrue(infos[0].disconnected)     # close() tore it down


class TestParsing(unittest.TestCase):
    def _hl_with(self, fake):
        hl = make_hl()
        hl._info = fake
        return hl

    def test_positions(self):
        fake = mock.MagicMock()
        fake.user_state.return_value = {
            "marginSummary": {"accountValue": "1000", "totalMarginUsed": "10", "totalNtlPos": "5"},
            "withdrawable": "900",
            "assetPositions": [
                {
                    "position": {
                        "coin": "BTC", "szi": "0.5", "entryPx": "60000",
                        "unrealizedPnl": "12.5", "liquidationPx": "40000",
                        "positionValue": "30000", "returnOnEquity": "0.1",
                        "leverage": {"type": "cross", "value": 10}, "marginUsed": "3000",
                    }
                }
            ],
        }
        p = self._hl_with(fake).positions("0xabc")
        self.assertEqual(p["account_value"], 1000.0)
        self.assertEqual(p["positions"][0]["coin"], "BTC")
        self.assertEqual(p["positions"][0]["liquidation_px"], 40000.0)

    def test_book(self):
        fake = mock.MagicMock()
        fake.l2_snapshot.return_value = {
            "time": 1,
            "levels": [[{"px": "99", "sz": "1", "n": 2}], [{"px": "101", "sz": "2", "n": 3}]],
        }
        b = self._hl_with(fake).book("btc", depth=5)
        self.assertEqual(b["coin"], "BTC")
        self.assertEqual(b["mid"], 100.0)
        self.assertEqual(b["spread"], 2.0)

    def test_spot_markets(self):
        fake = mock.MagicMock()
        fake.spot_meta.return_value = {
            "tokens": [
                {"index": 0, "name": "USDC", "szDecimals": 2},
                {"index": 1, "name": "PURR", "szDecimals": 0},
            ],
            "universe": [{"name": "PURR/USDC", "tokens": [1, 0], "index": 0}],
        }
        fake.all_mids.return_value = {"PURR/USDC": "0.25"}
        rows = self._hl_with(fake).spot_markets()
        self.assertEqual(rows[0]["base"], "PURR")
        self.assertEqual(rows[0]["quote"], "USDC")
        self.assertEqual(rows[0]["mid"], 0.25)

    def test_ledger(self):
        fake = mock.MagicMock()
        fake.user_non_funding_ledger_updates.return_value = [
            {"time": 100, "hash": "0xh", "delta": {"type": "deposit", "usdc": "250.5", "fee": "0"}},
            {"time": 200, "hash": "0xj", "delta": {"type": "withdraw"}},  # no usdc field
        ]
        out = self._hl_with(fake).ledger("0xabc", 0)
        self.assertEqual(out[0]["type"], "deposit")
        self.assertEqual(out[0]["usdc"], 250.5)
        self.assertEqual(out[0]["delta"]["fee"], "0")  # raw delta preserved
        self.assertIsNone(out[1]["usdc"])
        fake.user_non_funding_ledger_updates.assert_called_once_with("0xabc", 0, None)

    def test_fills_by_time(self):
        fake = mock.MagicMock()
        fake.user_fills_by_time.return_value = [
            {"coin": "BTC", "time": 100, "px": "60000", "sz": "0.1", "dir": "Open Long", "closedPnl": "0"},
        ]
        out = self._hl_with(fake).fills_by_time("0xabc", 50, 200)
        self.assertEqual(out[0]["coin"], "BTC")
        fake.user_fills_by_time.assert_called_once_with("0xabc", 50, 200, False)

    def test_fills_by_time_default_end(self):
        fake = mock.MagicMock()
        fake.user_fills_by_time.return_value = []
        self._hl_with(fake).fills_by_time("0xabc", 50)
        fake.user_fills_by_time.assert_called_once_with("0xabc", 50, None, False)

    def test_portfolio(self):
        fake = mock.MagicMock()
        fake.portfolio.return_value = [
            ["day", {"accountValueHistory": [[1, "100"], [2, "110"]],
                     "pnlHistory": [[1, "0"], [2, "10"]], "vlm": "5000"}],
            ["allTime", {"accountValueHistory": [], "pnlHistory": [], "vlm": "0"}],
        ]
        p = self._hl_with(fake).portfolio("0xabc")
        self.assertEqual(p["address"], "0xabc")
        d = p["periods"]["day"]
        self.assertEqual(d["account_value_history"][0], {"time": 1, "value": 100.0})
        self.assertEqual(d["start_value"], 100.0)
        self.assertEqual(d["end_value"], 110.0)
        self.assertEqual(d["period_pnl"], 10.0)
        self.assertEqual(d["vlm"], 5000.0)
        self.assertIsNone(p["periods"]["allTime"]["end_value"])

    def test_predicted_fundings(self):
        fake = mock.MagicMock()
        fake.post.return_value = [
            ["BTC", [
                ["HlPerp", {"fundingRate": "0.0001", "nextFundingTime": 123, "fundingIntervalHours": 1}],
                ["BinPerp", {"fundingRate": "-0.0002", "nextFundingTime": 456, "fundingIntervalHours": 8}],
            ]],
            ["ETH", [
                ["HlPerp", {"fundingRate": "0.00005", "nextFundingTime": 789, "fundingIntervalHours": 1}],
            ]],
            ["BADCOIN", []],   # tolerate a coin with no venues
        ]
        rows = self._hl_with(fake).predicted_fundings()
        self.assertEqual(rows[0]["coin"], "BTC")
        self.assertEqual(rows[0]["venues"][0]["venue"], "HlPerp")
        self.assertEqual(rows[0]["venues"][0]["funding_rate"], 0.0001)
        self.assertEqual(rows[0]["venues"][0]["funding_interval_hours"], 1)
        self.assertEqual(rows[0]["venues"][1]["venue"], "BinPerp")
        self.assertEqual(rows[1]["coin"], "ETH")
        self.assertEqual(rows[2]["venues"], [])

    def test_spot_balances(self):
        fake = mock.MagicMock()
        fake.spot_user_state.return_value = {
            "balances": [{"coin": "USDC", "token": 0, "total": "100.5", "hold": "0", "entryNtl": "100"}]
        }
        b = self._hl_with(fake).spot_balances("0xabc")
        self.assertEqual(b["balances"][0]["coin"], "USDC")
        self.assertEqual(b["balances"][0]["total"], 100.5)


if __name__ == "__main__":
    unittest.main()
