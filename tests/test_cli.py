"""Offline tests for the CLI output layer (--format, csv/ndjson, time parsing).

Pure helpers only - no network. Run with pytest or ``python -m unittest``.
"""
import io
import json
import time
import unittest
from types import SimpleNamespace

from hl_read.cli import _csv_cell, _emit_data, _fmt, _parse_when, _write_csv


def ns(**kw):
    kw.setdefault("json", False)
    kw.setdefault("format", "table")
    return SimpleNamespace(**kw)


class TestFmt(unittest.TestCase):
    def test_json_flag_is_alias(self):
        self.assertEqual(_fmt(ns(json=True)), "json")

    def test_format_passthrough(self):
        self.assertEqual(_fmt(ns(format="csv")), "csv")
        self.assertEqual(_fmt(ns(format="ndjson")), "ndjson")

    def test_default_table(self):
        self.assertEqual(_fmt(ns()), "table")


class TestCsvCell(unittest.TestCase):
    def test_scalar(self):
        self.assertEqual(_csv_cell(3.5), 3.5)
        self.assertEqual(_csv_cell("x"), "x")

    def test_none_is_blank(self):
        self.assertEqual(_csv_cell(None), "")

    def test_nested_is_json(self):
        self.assertEqual(_csv_cell({"type": "x"}), '{"type": "x"}')
        self.assertEqual(_csv_cell([1, 2]), "[1, 2]")


class TestWriteCsv(unittest.TestCase):
    def test_header_and_rows(self):
        rows = [
            {"coin": "BTC", "px": 60000, "delta": {"type": "dep"}},
            {"coin": "ETH", "px": None, "delta": None},
        ]
        buf = io.StringIO()
        _write_csv(rows, buf)
        lines = buf.getvalue().splitlines()
        self.assertEqual(lines[0], "coin,px,delta")
        self.assertTrue(lines[1].startswith("BTC,60000,"))
        self.assertIn("type", lines[1])         # nested delta serialized into the cell
        self.assertEqual(lines[2], "ETH,,")     # None -> blank cells

    def test_empty(self):
        buf = io.StringIO()
        _write_csv([], buf)
        self.assertEqual(buf.getvalue(), "")


class TestEmitData(unittest.TestCase):
    def test_table_returns_false(self):
        self.assertFalse(_emit_data(ns(format="table"), {"a": 1}))

    def test_json_emits_native(self):
        buf = io.StringIO()
        import contextlib

        with contextlib.redirect_stdout(buf):
            handled = _emit_data(ns(format="json"), {"a": 1})
        self.assertTrue(handled)
        self.assertEqual(json.loads(buf.getvalue()), {"a": 1})

    def test_ndjson_uses_rows(self):
        buf = io.StringIO()
        import contextlib

        with contextlib.redirect_stdout(buf):
            _emit_data(ns(format="ndjson"), {"ignored": True}, rows=[{"x": 1}, {"x": 2}])
        lines = buf.getvalue().splitlines()
        self.assertEqual([json.loads(line) for line in lines], [{"x": 1}, {"x": 2}])


class TestParseWhen(unittest.TestCase):
    def test_relative_hours(self):
        delta_h = (time.time() * 1000 - _parse_when("24h")) / 3_600_000
        self.assertAlmostEqual(delta_h, 24, delta=0.1)

    def test_iso_date(self):
        self.assertEqual(
            _parse_when("2024-01-31"),
            int(time.mktime(time.strptime("2024-01-31", "%Y-%m-%d")) * 1000),
        )

    def test_raw_ms(self):
        self.assertEqual(_parse_when("1700000000000"), 1700000000000)

    def test_bad(self):
        with self.assertRaises(ValueError):
            _parse_when("garbage")


if __name__ == "__main__":
    unittest.main()
