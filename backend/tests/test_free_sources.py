"""Multi-asset support: forex/stock contract maths and the free downloaders (offline, faked HTTP)."""

import io
import json
import lzma
import re
import struct
import time
import zipfile
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

import sim.data.free_sources as fs
from sim.contracts import registry
from sim.engine import Engine, FillConfig, RiskConfig


# ---------------------------------------------------------------- fake HTTP

class Resp:
    def __init__(self, status=200, content=b"", data=None):
        self.status_code = status
        self.content = content
        self._data = data
        self.text = content.decode("latin-1") if content else json.dumps(data or {})

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeClient:
    def __init__(self, route):
        self.route = route
        self.calls = []

    def get(self, url, params=None, headers=None):
        self.calls.append((url, params, headers))
        return self.route(url, params or {}, headers or {})

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake(monkeypatch, route):
    c = FakeClient(route)
    monkeypatch.setattr(fs, "_client", lambda *a, **k: c)
    monkeypatch.setattr(fs.time, "sleep", lambda s: None)
    return c


def epoch(d):
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())


# ---------------------------------------------------------------- contract maths

def test_usd_base_forex_pair_converts_profit_at_current_rate():
    e = Engine(FillConfig(spread_model=False), RiskConfig())
    e.add_account("A", 10_000)
    t0 = int(datetime(2026, 10, 14, 10, 0, tzinfo=timezone.utc).timestamp())
    e.on_price("USDJPY", t0 - 60, 150.000, False)
    e.place("A", "USDJPY", "buy", 100)  # one standard lot = 100,000 USD
    e.on_bar("USDJPY", t0, 150.000, 150.000, 150.000, 150.000, 1000)
    e.on_bar("USDJPY", t0 + 60, 151.000, 151.000, 151.000, 151.000, 1000)
    assert e.unrealized("A") == pytest.approx(100_000 / 151.0 * 1.0, rel=1e-9)  # 100k JPY profit in USD
    e.place("A", "USDJPY", "sell", 100)
    e.on_bar("USDJPY", t0 + 120, 151.000, 151.000, 151.000, 151.000, 1000)
    tr = e.accounts["A"].trades[-1]
    assert tr.pnl == pytest.approx(100_000 / 151.0)
    assert tr.fees == pytest.approx(2 * 100 * 0.035)  # $3.50 per side per standard lot


def test_eurusd_pip_value_and_margin():
    s = registry().get("EURUSD")
    assert s.tick_value_usd(1.1) == pytest.approx(0.01)        # pipette per micro lot
    assert s.tick_value_usd(1.1) * 10 * 100 == pytest.approx(10)  # $10 per pip per standard lot
    assert s.margin_per_contract(1.1, True) == pytest.approx(1100 * 0.0334)  # 1:30


def test_stock_margin_day_vs_overnight_and_fees():
    s = registry().get("SPY")
    assert s.asset_class == "etf" and s.unit == "share"
    assert s.margin_per_contract(600, True) == pytest.approx(150)    # 4x buying power intraday
    assert s.margin_per_contract(600, False) == pytest.approx(300)   # 2x overnight
    assert s.fee(100, 600) == pytest.approx(0.5)
    sess = s.session
    ny = lambda h, m: int(pd.Timestamp(f"2026-10-14 {h:02d}:{m:02d}", tz="America/New_York").timestamp())
    assert sess.in_session(ny(4, 0)) and sess.is_rth(ny(9, 30)) and not sess.is_rth(ny(16, 0))
    assert sess.is_day_margin(ny(15, 59)) and not sess.is_day_margin(ny(16, 0))
    assert not sess.in_session(ny(20, 0))
    sat = int(pd.Timestamp("2026-10-17 12:00", tz="America/New_York").timestamp())
    assert not sess.in_session(sat)


def test_forex_session_rolls_at_5pm_new_york():
    sess = registry().get("EURUSD").session
    ny = lambda d, h: int(pd.Timestamp(f"{d} {h}", tz="America/New_York").timestamp())
    assert not sess.in_session(ny("2026-10-18", "16:59"))   # Sunday before the open
    assert sess.in_session(ny("2026-10-18", "17:00"))
    assert sess.trade_date(ny("2026-10-18", "17:00")).isoformat() == "2026-10-19"
    assert sess.in_session(ny("2026-10-23", "16:59")) and not sess.in_session(ny("2026-10-23", "17:00"))


def test_registry_lists_every_asset_class():
    classes = {}
    for c in registry().all():
        classes.setdefault(c.asset_class, []).append(c.symbol)
    for sym in ["BTCUSDT", "ETHUSDT", "SOLUSDT"]:
        assert sym in classes["crypto"]
    for sym in ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD"]:
        assert sym in classes["forex"]
    for sym in ["AAPL", "NVDA", "TSLA"]:
        assert sym in classes["stock"]
    for sym in ["SPY", "QQQ", "IWM"]:
        assert sym in classes["etf"]
    assert not any(s.startswith("_") for s in registry().contracts)


# ---------------------------------------------------------------- Binance archive

def _kline_csv(day0: date, days: int, header: bool, micro: bool, base=60000.0) -> bytes:
    rows = []
    for i in range(days * 1440):
        t = (epoch(day0) + 60 * i) * (1_000_000 if micro else 1000)
        p = base + i * 0.1
        rows.append(f"{t},{p},{p + 5},{p - 5},{p + 1},2.5,{t + 59999},0,10,0,0,0")
    head = "open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,taker_buy_quote_volume,ignore\n"
    return ((head if header else "") + "\n".join(rows) + "\n").encode()


def _zip(name: str, data: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, data)
    return buf.getvalue()


def test_binance_archive_monthly_and_daily(monkeypatch, tmp_store):
    today = datetime.now(timezone.utc).date()
    first_this = today.replace(day=1)
    prev = (first_this - timedelta(days=1)).replace(day=1)

    def route(url, params, headers):
        m = re.search(r"/monthly/klines/BTCUSDT/1m/BTCUSDT-1m-(\d{4})-(\d{2})\.zip$", url)
        if m:
            y, mo = int(m[1]), int(m[2])
            if (y, mo) != (prev.year, prev.month):
                return Resp(404)
            ndays = (first_this - prev).days
            return Resp(200, _zip("x.csv", _kline_csv(prev, ndays, header=True, micro=False)))
        m = re.search(r"/daily/klines/BTCUSDT/1m/BTCUSDT-1m-(\d{4}-\d{2}-\d{2})\.zip$", url)
        if m:
            d = date.fromisoformat(m[1])
            return Resp(200, _zip("x.csv", _kline_csv(d, 1, header=False, micro=True)))
        if "fapi" in url:
            return Resp(451)  # live API blocked: archive data is still saved
        return Resp(404)

    c = fake(monkeypatch, route)
    msgs = []
    meta = fs.download("BTCUSDT", "auto", prev.isoformat(), today.isoformat(), lambda m, f=None: msgs.append(m))
    expected_days = (today - prev).days  # today's daily file is not published yet
    assert meta.rows == expected_days * 1440
    assert meta.source == "binance:um-1m" and meta.first_ts == epoch(prev)
    bars, _ = tmp_store.load("BTCUSDT")
    assert bars.volume[0] == 250  # 2.5 BTC = 250 contracts of 0.01 BTC
    assert np.all(np.diff(bars.ts) == 60)
    assert any("top-up skipped" in m for m in msgs)
    assert not any("/daily/" in u and u.endswith(f"{today:%Y-%m-%d}.zip") for u, *_ in c.calls)


def test_binance_csv_header_and_microseconds():
    a = fs.parse_binance_csv(_kline_csv(date(2025, 3, 1), 1, header=True, micro=False), 0.01)
    b = fs.parse_binance_csv(_kline_csv(date(2025, 3, 1), 1, header=False, micro=True), 0.01)
    assert len(a) == len(b) == 1440
    assert a["ts"].iloc[0] == b["ts"].iloc[0] == epoch(date(2025, 3, 1))


# ---------------------------------------------------------------- Dukascopy

def _bi5(day: date, point: float, offset_ticks=0, weekend=False) -> bytes:
    recs = []
    for i in range(1440):
        p = 1.10000 + 0.00010 * np.sin(i / 50) + offset_ticks * 0.00001
        o, c = p, p + 0.00002
        lo, hi = min(o, c) - 0.00003, max(o, c) + 0.00003
        vol = 0.0 if weekend or i % 97 == 0 else 1.5  # flat zero-volume candles when closed
        if vol == 0:
            o = c = lo = hi = p
        recs.append(struct.pack(">5if", i * 60, round(o * point), round(c * point), round(lo * point),
                                round(hi * point), vol))
    return lzma.compress(b"".join(recs), format=lzma.FORMAT_ALONE)


def test_dukascopy_mid_prices_spread_and_sessions(monkeypatch, tmp_store):
    def route(url, params, headers):
        m = re.search(r"/EURUSD/(\d{4})/(\d{2})/(\d{2})/(BID|ASK)_candles_min_1\.bi5$", url)
        assert m, url
        d = date(int(m[1]), int(m[2]) + 1, int(m[3]))  # Dukascopy months are 0-based
        return Resp(200, _bi5(d, 100000, offset_ticks=2 if m[4] == "ASK" else 0, weekend=d.weekday() >= 5))

    c = fake(monkeypatch, route)
    meta = fs.download("EURUSD", "auto", "2026-09-04", "2026-09-08")  # Fri .. Tue
    urls = [u for u, *_ in c.calls]
    assert any("/2026/08/04/BID" in u for u in urls)          # September -> 08
    assert not any("/2026/08/05/" in u for u in urls)          # Saturday skipped
    assert meta.source == "dukascopy:mid-1m"
    bars, _ = tmp_store.load("EURUSD")
    sess = registry().get("EURUSD").session
    assert sess.in_session_mask(bars.ts).all()                 # Friday after 17:00 NY and Sunday before it dropped
    assert (bars.volume > 0).all()
    assert bars.volume.max() == pytest.approx(1500)           # 1.5 million units = 1,500 micro lots
    assert "Measured median bid-ask spread: 2.0 ticks" in meta.notes
    # mid = bid + 1 pipette; stored on the tick grid
    assert np.allclose(np.round(bars.close / 0.00001) * 0.00001, bars.close)


def test_dukascopy_decoder_handles_empty_files():
    assert len(fs.decode_bi5_candles(b"", 0, 100000)) == 0


# ---------------------------------------------------------------- Alpaca and Yahoo

def test_alpaca_pages_and_iex_fallback(monkeypatch, tmp_store):
    t0 = pd.Timestamp("2026-09-14 13:30", tz="UTC")

    def page(i0, n):
        return [{"t": (t0 + pd.Timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M:%SZ"), "o": 600 + i * 0.01,
                 "h": 600.05 + i * 0.01, "l": 599.95 + i * 0.01, "c": 600.01 + i * 0.01, "v": 1000 + i}
                for i in range(i0, i0 + n)]

    def route(url, params, headers):
        assert headers["APCA-API-KEY-ID"] == "k"
        if params["feed"] == "sip":
            return Resp(403, b'{"message":"subscription does not permit querying recent SIP data"}')
        if params.get("page_token") == "p2":
            return Resp(200, data={"bars": {"SPY": page(100, 50)}, "next_page_token": None})
        return Resp(200, data={"bars": {"SPY": page(0, 100)}, "next_page_token": "p2"})

    fake(monkeypatch, route)
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_API_SECRET", "s")
    meta = fs.download("SPY", "auto", "2026-09-14", "2026-09-14")
    assert meta.rows == 150 and meta.source == "alpaca:iex-1m"
    assert meta.first_ts == int(t0.timestamp())


def test_alpaca_without_key_explains(monkeypatch, tmp_store):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)
    assert fs.default_source(registry().get("AAPL")) == "yahoo"
    with pytest.raises(RuntimeError, match="free API key"):
        fs.download_alpaca("AAPL")


def test_yahoo_chunks_and_nulls(monkeypatch, tmp_store):
    now = int(time.time())

    def route(url, params, headers):
        s = params["period1"]
        # regular-hours minutes of the requested window, with one null bar
        ts = []
        d = datetime.fromtimestamp(s, timezone.utc).date()
        while epoch(d) < params["period2"]:
            if d.weekday() < 5:
                op = int(pd.Timestamp(f"{d} 09:30", tz="America/New_York").timestamp())
                ts += [op + 60 * i for i in range(390) if s <= op + 60 * i < params["period2"]]
            d += timedelta(days=1)
        n = len(ts)
        close = [100.0 + i * 0.01 for i in range(n)]
        if n:
            close[0] = None
        q = {"open": close, "high": close, "low": close, "close": close, "volume": [100] * n}
        return Resp(200, data={"chart": {"result": [{"timestamp": ts, "indicators": {"quote": [q]}}], "error": None}})

    c = fake(monkeypatch, route)
    meta = fs.download("AAPL", "yahoo", None, None)
    assert meta.source == "yahoo:1m" and meta.rows > 0
    assert len(c.calls) >= 4  # 1-minute data comes in 7-day chunks
    assert all(p["period2"] - p["period1"] <= 7 * 86400 for _, p, _ in c.calls)
    assert meta.first_ts >= now - 30 * 86400


def test_futures_have_no_free_source():
    with pytest.raises(RuntimeError, match="BARS, Databento"):
        fs.default_source(registry().get("MES"))


# ---------------------------------------------------------------- job API

def test_download_job_api(monkeypatch, tmp_store, tmp_path):
    import sim.db as db_mod
    from fastapi.testclient import TestClient
    monkeypatch.setattr(db_mod, "_db", db_mod.DB(tmp_path / "sim.db"))

    def fake_download(sym, source, start, end, progress, **kw):
        if sym == "BADUSD":
            raise RuntimeError("nope")
        progress("halfway", 0.5)
        ts = np.arange(epoch(date(2026, 9, 14)), epoch(date(2026, 9, 15)), 60)
        df = pd.DataFrame({"ts": ts, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0})
        return fs._save(registry().get(sym), df, "test:1m")

    monkeypatch.setattr(fs, "download", fake_download)
    from sim.app import app
    with TestClient(app) as client:
        assert "dukascopy" in client.get("/api/data/sources").json()["sources"]
        assert client.post("/api/data/download", json={"symbols": ["NOPE"]}).status_code == 400
        job = client.post("/api/data/download", json={"symbols": ["BTCUSDT", "SOLUSDT"]}).json()
        for _ in range(100):
            j = client.get(f"/api/data/jobs/{job['id']}").json()
            if j["status"] == "done":
                break
            time.sleep(0.05)
        assert j["status"] == "done" and j["progress"] == 1.0
        assert [r["ok"] for r in j["results"]] == [True, True]
        syms = {s["symbol"]: s for s in client.get("/api/symbols").json()}
        assert syms["SOLUSDT"]["contract"]["asset_class"] == "crypto"
        assert syms["BTCUSDT"]["rows"] == 1440
