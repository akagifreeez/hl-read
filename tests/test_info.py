"""Offline unit tests for the hl-read resilience + parsing layer.

No network: the SDK ``Info`` is mocked at construction and a fake is injected
for method tests. Run with ``pytest`` or ``python -m unittest``.
"""
import unittest
from unittest import mock

from hl_read.info import HLRead, HLReadError, _f, _is_transient


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
