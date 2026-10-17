"""Live pipeline (simulated feed at high speed), strategies inside sessions, resume after restart."""

import time
from datetime import date

import pytest
from fastapi.testclient import TestClient

import sim.db as db_mod
from sim.data.synthetic import generate

STRAT = '''
from sim.strategy import Strategy, Param

class EveryBar(Strategy):
    """Buys on every 5-minute bar close when flat, with a tight bracket."""
    timeframe = "5m"
    warmup = 10
    params = {"ticks": Param(4)}

    def on_start(self):
        self.seen = []

    def on_bar(self, bar):
        self.seen.append(bar.ts)
        assert bar.ts % 300 == 0
        if self.position == 0 and not self.open_orders:
            self.buy(1, tp_ticks=self.p.ticks, sl_ticks=self.p.ticks)
'''


@pytest.fixture()
def client(tmp_store, tmp_path, monkeypatch):
    monkeypatch.setattr(db_mod, "_db", db_mod.DB(tmp_path / "sim.db"))
    for sym, (df, meta) in generate(["MES"], date(2026, 9, 1), date(2026, 9, 18)).items():
        tmp_store.write(sym, df, meta)
    (tmp_path / "every_bar.py").write_text(STRAT)
    from sim.app import app
    with TestClient(app) as c:
        c.strategy_path = str(tmp_path / "every_bar.py")
        yield c
        c.delete("/api/session")


def _wait(pred, timeout=20.0):
    t = time.time()
    while time.time() - t < timeout:
        if pred():
            return True
        time.sleep(0.1)
    return False


def test_sources_listed(client):
    src = {s["id"]: s for s in client.get("/api/live/sources").json()}
    assert src["simulated"]["available"] and "MES" in src["simulated"]["symbols"]
    assert "BTCUSDT" in src["binance"]["symbols"]
    assert "MES" in src["ibkr"]["symbols"]


def test_simulated_live_with_strategy(client):
    last = client.get("/api/symbols").json()[0]["last_ts"]
    start = last - 6 * 3600
    s = client.post("/api/session/live", json={"source": "simulated", "symbols": ["MES"], "start": start,
                                               "speed": 1500}).json()
    assert s["mode"] == "live" and s["live"]["source"] == "simulated"
    r = client.post("/api/session/strategies", json={"strategy": client.strategy_path}).json()
    assert r["status"] == "running" and r["timeframe"] == "5m"
    assert _wait(lambda: client.get("/api/session/strategies").json()[0]["bars"] >= 6)
    st = client.get("/api/state").json()
    assert st["fills"], "the strategy should have traded"
    assert all(f["tag"] in ("strategy:EveryBar", "tp", "sl") for f in st["fills"])
    # the chart history extends with live minutes and never shows a bar past the live clock
    sess = client.get("/api/session").json()
    bars = client.get("/api/bars", params={"symbol": "MES", "tf": "1m", "limit": 3}).json()
    assert bars["bars"]["time"] and max(bars["bars"]["time"]) <= sess["time"]
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"op": "subscribe", "symbol": "MES", "tf": "15m", "limit": 50})
        snap = ws.receive_json()
        while snap["type"] != "snapshot":
            snap = ws.receive_json()
        clock = client.get("/api/session").json()["time"]
        assert max(snap["bars"]["time"]) <= clock
    # stop the strategy: its working orders are cancelled
    client.delete(f"/api/session/strategies/{r['id']}")
    assert all(o["tag"] not in ("strategy:EveryBar",) for o in client.get("/api/orders").json())
    assert sess["state"] == "running"


def test_live_session_resumes_after_restart(client):
    from sim.api.live import restore_on_startup
    from sim.session import manager
    import asyncio

    last = client.get("/api/symbols").json()[0]["last_ts"]
    client.post("/api/session/live", json={"source": "simulated", "symbols": ["MES"], "start": last - 3 * 3600,
                                           "speed": 600})
    client.post("/api/orders", json={"symbol": "MES", "side": "buy", "qty": 1, "type": "limit", "price": 1000})
    sess = manager.current
    sess.save()
    saved = db_mod.db().get("session:live")
    assert saved and saved["engine"]["orders"]
    order_id = saved["engine"]["orders"][0]["id"]
    # simulate a restart: drop the in-memory session (without clearing the saved state) and restore
    client.portal.call(sess.live.stop)
    manager.current = None
    client.portal.call(restore_on_startup)
    assert manager.current is not None and manager.current.mode == "live"
    assert order_id in manager.current.engine.orders
    assert manager.current.engine.orders[order_id].status.value == "working"
    del asyncio


def test_strategy_in_replay_matches_backtest(client):
    """Same strategy code, same prices -> same trades in a replay session and in the backtester."""
    from sim.session import manager
    from sim.strategy.runner import BacktestConfig, load_strategy, run_backtest

    syms = client.get("/api/symbols").json()
    start = syms[0]["last_ts"] - 7 * 86400
    start -= start % 86400  # a round UTC midnight inside the overnight session
    end = start + 4 * 86400
    client.post("/api/session/replay", json={"symbols": ["MES"], "start": start})
    client.post("/api/session/strategies", json={"strategy": "pats_second_entry"})
    client.post("/api/session/control", json={"action": "jump", "ts": end})
    replay = [(t["entry_ts"], t["side"], t["entry_price"], t["exit_price"], t["exit_reason"])
              for t in client.get("/api/state").json()["trades"]]
    cls = load_strategy("pats_second_entry")
    bt = run_backtest(cls, BacktestConfig(symbol="MES", timeframe="5m", start=start, end=end, close_at_end=False))
    back = [(t["entry_ts"], t["side"], t["entry_price"], t["exit_price"], t["exit_reason"]) for t in bt["trades"]]
    assert len(back) >= 3
    assert replay == back, (replay, back)
    assert manager.current.mode == "replay"


def test_binance_feed_parses_trades_and_quotes(monkeypatch):
    """Binance stream messages become engine prices and quotes (network replaced by a fake socket)."""
    import asyncio
    import json as _json
    import websockets

    from sim.feeds.binance import BinanceFeed
    from sim.session import TradingSession

    now = int(time.time())
    msgs = [
        {"stream": "btcusdt@bookTicker", "data": {"e": "bookTicker", "s": "BTCUSDT", "b": "60000.0", "a": "60000.1", "T": now * 1000}},
        {"stream": "btcusdt@aggTrade", "data": {"e": "aggTrade", "s": "BTCUSDT", "p": "60000.1", "q": "0.25", "T": now * 1000}},
    ]

    class FakeWS:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def __aiter__(self):
            async def gen():
                for m in msgs:
                    yield _json.dumps(m)
                await asyncio.sleep(3600)
            return gen()

    monkeypatch.setattr(websockets, "connect", lambda *a, **k: FakeWS())

    async def go():
        sess = TradingSession("live", ["BTCUSDT"], None, 10_000)
        feed = BinanceFeed(sess, ["BTCUSDT"])
        sess.live = feed
        feed._running = True
        task = asyncio.create_task(feed.run())
        await asyncio.sleep(0.2)
        task.cancel()
        return sess

    sess = asyncio.run(go())
    assert sess.engine.last["BTCUSDT"] == 60000.1
    assert sess.engine.quotes["BTCUSDT"][1:] == (60000.0, 60000.1)
    assert sess.partial["BTCUSDT"]["volume"] == 25  # 0.25 BTC = 25 contracts of 0.01 BTC


def test_lockstep_bot_matches_builtin_strategy(client, tmp_path):
    """A bot driving /ws/bot gets exactly the trades of the same logic written as a Strategy."""
    from sim.strategy.runner import BacktestConfig, load_strategy, run_backtest

    last = client.get("/api/symbols").json()[0]["last_ts"]
    start, end = "2026-09-08", "2026-09-11"
    cls = load_strategy(client.strategy_path)
    bt = run_backtest(cls, BacktestConfig(symbol="MES", timeframe="5m", start=start, end=end, warmup=10))
    want = [(t["entry_ts"], t["side"], t["entry_price"], t["exit_price"], t["exit_reason"]) for t in bt["trades"]]

    with client.websocket_connect("/ws/bot") as ws:
        ws.send_json({"op": "start", "symbol": "MES", "timeframe": "5m", "start": start, "end": end,
                      "warmup": 10, "name": "test bot"})
        assert ws.receive_json()["type"] == "started"
        assert len(ws.receive_json()["bars"]) == 10
        steps = 0
        while True:
            msg = ws.receive_json()
            if msg["type"] == "done":
                break
            steps += 1
            actions = []
            if msg["position"]["qty"] == 0 and not msg["orders"]:
                actions.append({"action": "place", "side": "buy", "qty": 1, "tp_ticks": 4, "sl_ticks": 4})
            ws.send_json({"op": "step", "actions": actions})
    got = [(t["entry_ts"], t["side"], t["entry_price"], t["exit_price"], t["exit_reason"]) for t in msg["trades"]]
    assert steps > 100 and len(want) > 20
    assert got == want
    assert any(r["id"] == msg["report_id"] for r in client.get("/api/backtests").json())
    del last
