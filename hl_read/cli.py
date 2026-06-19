"""hl-read command line - read-only Hyperliquid data in your terminal.

    hl-read mids                     # all mid prices
    hl-read mids BTC ETH             # just these
    hl-read book ETH --depth 5       # order book snapshot
    hl-read positions 0xABC...       # anyone's positions (public)
    hl-read positions 0xABC... --watch   # live, re-polled every few seconds
    hl-read portfolio 0xABC...       # account value / PnL history by period
    hl-read ledger 0xABC...          # deposits / withdrawals / transfers
    hl-read orders 0xABC...          # resting orders
    hl-read fills 0xABC... --limit 20
    hl-read fills 0xABC... --since 7d        # fills in a time window (e.g. last 7 days)
    hl-read funding                  # funding / OI table, sorted by |funding|
    hl-read predicted BTC ETH        # predicted funding across venues (HL vs CEXes)
    hl-read markets                  # every perp + max leverage
    hl-read spot                     # every spot pair + mid price
    hl-read balances 0xABC...        # spot token balances
    hl-read watch ETH                # live order book over websocket
    hl-read health                   # is the API up? round-trip latency (exit 1 if down)
    hl-read --format csv fills 0xABC... --since 30d   # any list command as CSV
    hl-read export ledger 0xABC... --out ledger.csv   # write straight to a UTF-8 file

Global flags: --testnet (testnet API), --json (raw JSON), --format table|json|csv|ndjson,
--retries N, --rate-limit N (max HTTP calls/min), --no-cache (always fetch fresh).
No private key is ever read or accepted.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import __version__
from .info import HLRead

# -- terminal helpers (ANSI, Windows-safe) -------------------------------


def _enable_vt() -> bool:
    """Turn on ANSI escape handling on Windows. Returns True if usable."""
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


_VT = _enable_vt()
RED, GREEN, DIM, BOLD, RESET = (
    ("\033[31m", "\033[32m", "\033[2m", "\033[1m", "\033[0m") if _VT else ("", "", "", "", "")
)


def _clear() -> None:
    if _VT:
        sys.stdout.write("\033[H\033[2J\033[3J")  # home, clear screen, clear scrollback
    else:
        os.system("cls" if os.name == "nt" else "clear")


def _fmt(args) -> str:
    """Resolve the output format. ``--json`` is a back-compat alias for json."""
    if getattr(args, "json", False):
        return "json"
    return getattr(args, "format", "table") or "table"


def _csv_cell(v):
    """CSV-safe cell: JSON-encode nested structures, blank for None."""
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return "" if v is None else v


def _write_csv(rows: list, fh) -> None:
    """Write a list of flat dicts as CSV. Columns come from the first row."""
    if not rows:
        return
    import csv as _csv

    cols = list(rows[0].keys())
    w = _csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({c: _csv_cell(r.get(c)) for c in cols})


def _emit_data(args, native, rows=None) -> bool:
    """Emit ``native``/``rows`` in the requested non-table format.

    Returns True if it produced output (json/csv/ndjson); False for ``table``
    so the caller renders its own table. ``native`` is what ``--json`` emits
    (kept byte-for-byte compatible); ``rows`` is the flat list-of-dicts used for
    csv/ndjson, falling back to ``native`` when it is already a list.
    """
    fmt = _fmt(args)
    if fmt == "table":
        return False
    if fmt == "json":
        print(json.dumps(native, indent=2))
        return True
    data = rows if rows is not None else (native if isinstance(native, list) else [native])
    if fmt == "ndjson":
        for r in data:
            print(json.dumps(r, ensure_ascii=False))
    else:  # csv
        _write_csv(data, sys.stdout)
    return True


def _parse_when(s: str) -> int:
    """Parse a time spec to epoch ms: '24h'/'7d' (ago), 'YYYY-MM-DD', or raw ms."""
    import re

    s = s.strip()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([hd])", s)
    if m:
        secs = float(m.group(1)) * (3600 if m.group(2) == "h" else 86400)
        return int((time.time() - secs) * 1000)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return int(time.mktime(time.strptime(s, "%Y-%m-%d")) * 1000)  # local midnight
    if s.isdigit():
        return int(s)
    raise ValueError(f"bad time '{s}' (use e.g. 24h, 7d, 2024-01-31, or epoch ms)")


# -- commands ------------------------------------------------------------


def cmd_mids(hl: HLRead, args) -> None:
    mids = hl.mids()
    if args.coins:
        mids = {c.upper(): mids.get(c.upper()) for c in args.coins}
    rows = [{"coin": c, "mid": px} for c, px in sorted(mids.items(), key=lambda kv: kv[0])]
    if _emit_data(args, mids, rows):
        return
    for coin, px in sorted(mids.items(), key=lambda kv: kv[0]):
        px_s = "n/a" if px is None else f"{px:,.6g}"
        print(f"  {coin:<10} {px_s:>16}")


def cmd_book(hl: HLRead, args) -> None:
    b = hl.book(args.coin, depth=args.depth)
    rows = (
        [{"side": "ask", **lv} for lv in b["asks"]]
        + [{"side": "bid", **lv} for lv in b["bids"]]
    )
    if _emit_data(args, b, rows):
        return
    _render_book(b)


def _render_book(b: dict) -> None:
    coin = b["coin"]
    print(f"  {BOLD}{coin}-PERP{RESET}   (snapshot)")
    print("  " + "-" * 34)
    for lv in reversed(b["asks"]):
        print(f"  {RED}{lv['px']:>14,.6g}   {lv['sz']:>12}{RESET}")
    if b["mid"] is not None:
        print(f"  {DIM}---- mid {b['mid']:,.6g}   spread {b['spread']:,.6g} ----{RESET}")
    for lv in b["bids"]:
        print(f"  {GREEN}{lv['px']:>14,.6g}   {lv['sz']:>12}{RESET}")


def _render_positions(p: dict) -> None:
    av = p["account_value"]
    print(f"  address       : {p['address']}")
    print(f"  account value : {('n/a' if av is None else f'{av:,.2f}')} USDC")
    print(f"  withdrawable  : {p['withdrawable']}")
    if not p["positions"]:
        print("  positions     : none")
        return
    upnl = sum(pos["unrealized_pnl"] or 0 for pos in p["positions"])
    ucol = GREEN if upnl >= 0 else RED
    print(f"  open uPnL     : {ucol}{upnl:,.2f}{RESET} USDC   ({len(p['positions'])} positions)")
    print("  positions:")
    print(f"    {'coin':<7}{'size':>14}{'entry':>14}{'uPnL':>14}{'liq':>14}")
    for pos in p["positions"]:
        col = GREEN if (pos["unrealized_pnl"] or 0) >= 0 else RED
        print(
            f"    {pos['coin']:<7}{(pos['size'] or 0):>14,.6g}"
            f"{(pos['entry_px'] or 0):>14,.6g}"
            f"{col}{(pos['unrealized_pnl'] or 0):>14,.2f}{RESET}"
            f"{(pos['liquidation_px'] or 0):>14,.6g}"
        )


def cmd_positions(hl: HLRead, args) -> None:
    if _fmt(args) != "table":
        p = hl.positions(args.address)
        _emit_data(args, p, p["positions"])
        return
    if not getattr(args, "watch", False):
        _render_positions(hl.positions(args.address))
        return
    net = "testnet" if hl.testnet else "mainnet"
    interval = max(0.5, args.interval)
    try:
        while True:
            p = hl.positions(args.address)
            _clear()
            print(f"  {BOLD}positions{RESET}   {net}   (updated {time.strftime('%H:%M:%S')}, every {interval:g}s)")
            print("  " + "-" * 60)
            _render_positions(p)
            sys.stdout.flush()
            time.sleep(interval)
    except KeyboardInterrupt:
        pass


def cmd_ledger(hl: HLRead, args) -> None:
    start = _parse_when(args.since) if args.since else 0
    end = _parse_when(args.until) if args.until else None
    rows = hl.ledger(args.address, start, end)
    rows.sort(key=lambda r: r.get("time") or 0, reverse=True)  # newest first
    total = len(rows)
    if args.limit:
        rows = rows[: args.limit]
    if _emit_data(args, rows):
        return
    if not rows:
        print("  ledger        : none in window")
        return
    extra = f", showing newest {len(rows)}" if len(rows) < total else ""
    print(f"  ledger   {args.address}   ({total} updates{extra})")
    print(f"    {'time':<20}{'type':<22}{'usdc':>16}")
    for r in rows:
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime((r["time"] or 0) / 1000))
        usdc_s = "" if r["usdc"] is None else f"{r['usdc']:,.2f}"
        print(f"    {ts:<20}{(r['type'] or ''):<22}{usdc_s:>16}")


def cmd_portfolio(hl: HLRead, args) -> None:
    p = hl.portfolio(args.address)
    prows = [
        {"period": k, "start_value": v["start_value"], "end_value": v["end_value"],
         "period_pnl": v["period_pnl"], "vlm": v["vlm"]}
        for k, v in p["periods"].items()
    ]
    if _emit_data(args, p, prows):
        return
    print(f"  portfolio   {p['address']}")
    if not p["periods"]:
        print("    (no history)")
        return
    print(f"    {'period':<12}{'acct value':>16}{'PnL':>16}{'volume':>18}")
    for name, d in p["periods"].items():
        ev, pnl, vlm = d["end_value"], d["period_pnl"], d["vlm"]
        ev_s = "n/a" if ev is None else f"{ev:,.2f}"
        if pnl is None:
            pnl_s, col = "n/a", ""
        else:
            pnl_s, col = f"{pnl:+,.2f}", (GREEN if pnl >= 0 else RED)
        vlm_s = "n/a" if vlm is None else f"{vlm:,.0f}"
        print(f"    {name:<12}{ev_s:>16}{col}{pnl_s:>16}{RESET}{vlm_s:>18}")


def cmd_balances(hl: HLRead, args) -> None:
    b = hl.spot_balances(args.address)
    if _emit_data(args, b, b["balances"]):
        return
    print(f"  address : {b['address']}")
    if not b["balances"]:
        print("  balances: none")
        return
    print(f"    {'coin':<10}{'total':>18}{'hold':>18}")
    for bal in b["balances"]:
        print(f"    {(bal['coin'] or ''):<10}{(bal['total'] or 0):>18,.6g}{(bal['hold'] or 0):>18,.6g}")


def cmd_spot(hl: HLRead, args) -> None:
    rows = hl.spot_markets()
    if args.coins:
        wanted = {c.upper() for c in args.coins}
        rows = [r for r in rows if (r["name"] or "").upper() in wanted or (r["base"] or "").upper() in wanted]
    if _emit_data(args, rows):
        return
    rows = [r for r in rows if r["mid"] is not None] or rows
    print(f"  {len(rows)} spot markets")
    print(f"    {'pair':<14}{'base':<10}{'quote':<8}{'mid':>16}")
    for r in rows:
        mid = "n/a" if r["mid"] is None else f"{r['mid']:,.6g}"
        print(f"    {(r['name'] or ''):<14}{(r['base'] or ''):<10}{(r['quote'] or ''):<8}{mid:>16}")


def cmd_orders(hl: HLRead, args) -> None:
    orders = hl.open_orders(args.address)
    if _emit_data(args, orders):
        return
    if not orders:
        print("  open orders   : none")
        return
    print(f"    {'oid':<12}{'coin':<7}{'side':<6}{'sz':>14}{'px':>14}")
    for o in orders:
        print(f"    {o['oid']:<12}{o['coin']:<7}{o['side']:<6}{o['sz']:>14}{o['limitPx']:>14}")


def cmd_fills(hl: HLRead, args) -> None:
    if getattr(args, "since", None):
        start = _parse_when(args.since)
        end = _parse_when(args.until) if getattr(args, "until", None) else None
        fills = hl.fills_by_time(args.address, start, end)
        fills.sort(key=lambda f: f.get("time", 0), reverse=True)  # newest first, like `fills`
        if args.limit:
            fills = fills[: args.limit]
    else:
        fills = hl.fills(args.address, limit=args.limit)
    if _emit_data(args, fills):
        return
    if not fills:
        print("  fills         : none")
        return
    print(f"    {'time':<20}{'coin':<7}{'dir':<10}{'sz':>12}{'px':>14}{'closedPnL':>14}")
    for f in fills:
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(f.get("time", 0) / 1000))
        print(
            f"    {ts:<20}{f.get('coin',''):<7}{f.get('dir',''):<10}"
            f"{f.get('sz',''):>12}{f.get('px',''):>14}{f.get('closedPnl',''):>14}"
        )


def cmd_funding(hl: HLRead, args) -> None:
    rows = hl.funding()
    rows = [r for r in rows if r["funding"] is not None]
    rows.sort(key=lambda r: abs(r["funding"]), reverse=True)
    if args.coins:
        wanted = {c.upper() for c in args.coins}
        rows = [r for r in rows if r["coin"] in wanted]
    if _emit_data(args, rows):
        return
    print(f"    {'coin':<8}{'funding%/hr':>14}{'mark':>14}{'oracle':>14}{'OI':>16}")
    for r in rows[: args.top if args.top else len(rows)]:
        fr = r["funding"] * 100
        col = GREEN if fr >= 0 else RED
        print(
            f"    {r['coin']:<8}{col}{fr:>13.4f}%{RESET}"
            f"{(r['mark_px'] or 0):>14,.6g}{(r['oracle_px'] or 0):>14,.6g}"
            f"{(r['open_interest'] or 0):>16,.2f}"
        )


def _hl_venue_rate(row: dict):
    """The HlPerp predicted rate for a coin row, or None if absent."""
    for v in row["venues"]:
        if v["venue"] == "HlPerp":
            return v["funding_rate"]
    return None


def cmd_predicted(hl: HLRead, args) -> None:
    rows = hl.predicted_fundings()
    if args.coins:
        wanted = {c.upper() for c in args.coins}
        rows = [r for r in rows if (r["coin"] or "").upper() in wanted]
    flat = [
        {"coin": r["coin"], "venue": v["venue"], "funding_rate": v["funding_rate"],
         "funding_interval_hours": v["funding_interval_hours"],
         "next_funding_time": v["next_funding_time"]}
        for r in rows for v in r["venues"]
    ]
    if _emit_data(args, rows, flat):
        return
    # Sort by |Hyperliquid predicted funding| so the spiciest markets surface.
    rows.sort(key=lambda r: abs(_hl_venue_rate(r) or 0), reverse=True)
    shown = rows[: args.top] if args.top else rows
    print(f"  predicted funding - {len(rows)} coins (sorted by |HlPerp|)")
    for r in shown:
        print(f"  {BOLD}{r['coin']}{RESET}")
        for v in r["venues"]:
            rate = v["funding_rate"]
            if rate is None:
                rate_s = "n/a"
                col = ""
            else:
                rate_s = f"{rate * 100:+.4f}%"
                col = GREEN if rate >= 0 else RED
            iv = v["funding_interval_hours"]
            iv_s = f"/{iv}h" if iv is not None else ""
            print(f"    {v['venue']:<10}{col}{rate_s:>11}{RESET} {DIM}{iv_s:<4}{RESET}")


def cmd_markets(hl: HLRead, args) -> None:
    m = hl.markets()
    if _emit_data(args, m):
        return
    print(f"  {len(m)} perp markets")
    print(f"    {'coin':<10}{'maxLev':>8}{'szDec':>8}{'isolated':>10}")
    for a in m:
        print(
            f"    {a['name']:<10}{str(a['max_leverage']):>8}"
            f"{str(a['sz_decimals']):>8}{str(a['only_isolated']):>10}"
        )


def cmd_health(hl: HLRead, args) -> int:
    h = hl.health()
    if _emit_data(args, h):
        return 0 if h["ok"] else 1
    status = f"{GREEN}OK{RESET}" if h["ok"] else f"{RED}DOWN{RESET}"
    net = "testnet" if h["testnet"] else "mainnet"
    print(f"  hl-read health   {net}")
    print(f"    endpoint : {h['api_url']}")
    print(f"    status   : {status}")
    lat = "n/a" if h["latency_ms"] is None else f"{h['latency_ms']} ms"
    print(f"    latency  : {lat}")
    if h["markets"] is not None:
        print(f"    markets  : {h['markets']}")
    if h["error"]:
        print(f"    error    : {h['error']}")
    return 0 if h["ok"] else 1


def cmd_export(hl: HLRead, args) -> None:
    """Fetch fills/ledger/candles and write them to a UTF-8 file (csv default).

    A direct file writer (vs shell redirect) so output is always UTF-8 - the
    Windows/PowerShell ``>`` redirect defaults to UTF-16 and corrupts CSVs.
    """
    fmt = _fmt(args)
    if fmt == "table":
        fmt = "csv"  # export defaults to csv; --format json/ndjson to change
    if args.what == "fills":
        if args.since:
            rows = hl.fills_by_time(
                args.target, _parse_when(args.since), _parse_when(args.until) if args.until else None
            )
        else:
            rows = hl.fills(args.target, limit=0)
        rows = sorted(rows, key=lambda r: r.get("time", 0) or 0, reverse=True)
    elif args.what == "ledger":
        rows = hl.ledger(
            args.target,
            _parse_when(args.since) if args.since else 0,
            _parse_when(args.until) if args.until else None,
        )
        rows = sorted(rows, key=lambda r: r.get("time") or 0, reverse=True)
    else:  # candles
        rows = hl.candles(args.target, interval=args.interval, hours=args.hours)
    if args.limit:
        rows = rows[: args.limit]
    with open(args.out, "w", encoding="utf-8", newline="") as fh:
        if fmt == "json":
            json.dump(rows, fh, indent=2, ensure_ascii=False)
        elif fmt == "ndjson":
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        else:
            _write_csv(rows, fh)
    print(f"wrote {len(rows)} {args.what} rows to {args.out} ({fmt})")


def cmd_watch(hl: HLRead, args) -> None:
    coin = args.coin.upper()
    depth = args.depth

    def render(msg) -> None:
        data = msg.get("data", {})
        levels = data.get("levels") or [[], []]
        bids, asks = levels[0][:depth], levels[1][:depth]
        net = "testnet" if hl.testnet else "mainnet"
        out = [
            f"  {BOLD}{coin}-PERP{RESET}   {net}   (updated {time.strftime('%H:%M:%S')})",
            "  " + "-" * 34,
        ]
        for lv in reversed(asks):
            out.append(f"  {RED}{float(lv['px']):>14,.6g}   {float(lv['sz']):>12}{RESET}")
        if bids and asks:
            bb, ba = float(bids[0]["px"]), float(asks[0]["px"])
            out.append(f"  {DIM}---- mid {(bb+ba)/2:,.6g}   spread {ba-bb:,.6g} ----{RESET}")
        for lv in bids:
            out.append(f"  {GREEN}{float(lv['px']):>14,.6g}   {float(lv['sz']):>12}{RESET}")
        _clear()
        sys.stdout.write("\n".join(out) + "\n")
        sys.stdout.flush()

    def on_reconnect() -> None:
        sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] reconnected to {coin} book\n")
        sys.stderr.flush()

    stream = hl.resilient_stream_book(coin, render, on_reconnect=on_reconnect)
    print(
        f"subscribed to {coin} order book ({'testnet' if hl.testnet else 'mainnet'}), "
        "auto-reconnecting. Ctrl+C to quit."
    )
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        stream.close()


# -- argument parsing ----------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hl-read", description="Read-only Hyperliquid data. No keys, ever.")
    p.add_argument("--version", action="version", version=f"hl-read {__version__}")
    p.add_argument("--testnet", action="store_true", help="use the testnet API")
    p.add_argument("--json", action="store_true", help="emit raw JSON (alias for --format json)")
    p.add_argument("--format", choices=["table", "json", "csv", "ndjson"], default="table",
                   help="output format (default table; csv/ndjson for list data)")
    p.add_argument("--retries", type=int, default=4, help="retry attempts on transient errors")
    p.add_argument("--rate-limit", type=float, default=None, dest="rate_limit",
                   help="cap HTTP calls to at most N per minute")
    p.add_argument("--no-cache", action="store_true", dest="no_cache",
                   help="always fetch fresh (disable the short market-data cache)")
    p.add_argument("--api-url", dest="api_url", default=None,
                   help="override the API endpoint (e.g. a proxy/mirror)")
    p.add_argument("--fallback-url", dest="fallback_urls", action="append",
                   help="fallback endpoint, tried on persistent failure; repeatable")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("mids", help="mid prices")
    sp.add_argument("coins", nargs="*", help="optional coins to filter")
    sp.set_defaults(func=cmd_mids)

    sp = sub.add_parser("book", help="order book snapshot")
    sp.add_argument("coin")
    sp.add_argument("--depth", type=int, default=10)
    sp.set_defaults(func=cmd_book)

    sp = sub.add_parser("positions", help="positions for an address")
    sp.add_argument("address")
    sp.add_argument("--watch", action="store_true", help="live: re-poll and redraw")
    sp.add_argument("--interval", type=float, default=3.0, help="poll seconds when --watch")
    sp.set_defaults(func=cmd_positions)

    sp = sub.add_parser("portfolio", help="account value / PnL history for an address")
    sp.add_argument("address")
    sp.set_defaults(func=cmd_portfolio)

    sp = sub.add_parser("ledger", help="deposits/withdrawals/transfers for an address")
    sp.add_argument("address")
    sp.add_argument("--limit", type=int, default=50, help="show newest N (0 = all, for export)")
    sp.add_argument("--since", help="window start: 24h / 7d (ago), 2024-01-31, or epoch ms (default all)")
    sp.add_argument("--until", help="window end (default now): same formats as --since")
    sp.set_defaults(func=cmd_ledger)

    sp = sub.add_parser("balances", help="spot token balances for an address")
    sp.add_argument("address")
    sp.set_defaults(func=cmd_balances)

    sp = sub.add_parser("orders", help="open orders for an address")
    sp.add_argument("address")
    sp.set_defaults(func=cmd_orders)

    sp = sub.add_parser("fills", help="fills for an address (recent, or a time window)")
    sp.add_argument("address")
    sp.add_argument("--limit", type=int, default=50)
    sp.add_argument("--since", help="window start: 24h / 7d (ago), 2024-01-31, or epoch ms")
    sp.add_argument("--until", help="window end (default now): same formats as --since")
    sp.set_defaults(func=cmd_fills)

    sp = sub.add_parser("funding", help="funding / OI table")
    sp.add_argument("coins", nargs="*", help="optional coins to filter")
    sp.add_argument("--top", type=int, default=0, help="show only the top N by |funding|")
    sp.set_defaults(func=cmd_funding)

    sp = sub.add_parser("predicted", help="predicted funding per coin across venues (HL vs CEXes)")
    sp.add_argument("coins", nargs="*", help="optional coins to filter")
    sp.add_argument("--top", type=int, default=0, help="show only the top N by |HlPerp funding|")
    sp.set_defaults(func=cmd_predicted)

    sp = sub.add_parser("markets", help="list perp markets")
    sp.set_defaults(func=cmd_markets)

    sp = sub.add_parser("spot", help="list spot markets + mid prices")
    sp.add_argument("coins", nargs="*", help="optional pairs/base coins to filter")
    sp.set_defaults(func=cmd_spot)

    sp = sub.add_parser("watch", help="live order book (websocket)")
    sp.add_argument("coin")
    sp.add_argument("--depth", type=int, default=10)
    sp.set_defaults(func=cmd_watch)

    sp = sub.add_parser("health", help="probe API reachability + latency (exit 1 if down)")
    sp.set_defaults(func=cmd_health)

    sp = sub.add_parser("export", help="write fills/ledger/candles to a UTF-8 file (csv default)")
    sp.add_argument("what", choices=["fills", "ledger", "candles"])
    sp.add_argument("target", help="address (fills/ledger) or coin (candles)")
    sp.add_argument("--out", required=True, help="output file path")
    sp.add_argument("--since", help="window start: 24h/7d (ago), 2024-01-31, or epoch ms")
    sp.add_argument("--until", help="window end (default now)")
    sp.add_argument("--limit", type=int, default=0, help="cap rows (0 = all)")
    sp.add_argument("--interval", default="1h", help="candles interval (e.g. 1m/1h/1d)")
    sp.add_argument("--hours", type=float, default=168, help="candles lookback hours")
    sp.set_defaults(func=cmd_export)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    hl = HLRead(
        testnet=args.testnet,
        max_retries=args.retries,
        rate_limit_per_min=args.rate_limit,
        cache_ttl=0.0 if args.no_cache else 1.0,
        meta_ttl=0.0 if args.no_cache else 300.0,
        api_url=args.api_url,
        fallback_urls=args.fallback_urls,
    )
    try:
        rc = args.func(hl, args)
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # surface SDK / network errors cleanly
        print(f"error: {e}", file=sys.stderr)
        return 1
    return rc or 0


if __name__ == "__main__":
    raise SystemExit(main())
