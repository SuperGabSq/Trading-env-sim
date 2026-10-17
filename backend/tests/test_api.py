"""API + replay session tests (in-process, synthetic data in a temporary store)."""

from datetime import date

import pytest
from fastapi.testclient import TestClient

import sim.db as db_mod
from sim.data.synthetic import generate


@pytest.fixture()
def client(tmp_store, tmp_path, monkeypatch):
    monkeypatch.setattr(db_mod, "_db", db_mod.DB(tmp_path / "sim.db"))
    for sym, (df, meta) in generate(["MES", "MNQ"], date(2026, 9, 1), date(2026, 9, 25)).items():
        tmp_store.write(sym, df, meta)
    from sim.app import app
    with TestClient(app) as c:
        yield c
        c.delete("/api/session")


def test_symbols_and_bars(client):
    syms = {s["symbol"]: s for s in client.get("/api/symbols").json()}
    assert syms["MES"]["synthetic"] and syms["MES"]["contract"]["tick_size"] == 0.25
    r = client.get("/api/bars", params={"symbol": "MES", "tf": "15m", "limit": 10,
                                         "indicators": '[{"id":"e","type":"ema","params":{"period":9}}]'}).json()
    assert len(r["bars"]["time"]) == 10 and len(r["indicators"]["e"]["value"]) == 10


def test_replay_trading_flow_and_no_lookahead(client):
    last = client.get("/api/symbols").json()[0]["last_ts"]
    start = last - 3 * 86400
    s = client.post("/api/session/replay", json={"symbols": ["MES"], "start": start, "balance": 25_000}).json()
    assert s["state"] == "paused" and s["account"]["equity"] == 25_000
    bars = client.get("/api/bars", params={"symbol": "MES", "tf": "1m", "limit": 5}).json()
    assert max(bars["bars"]["time"]) < start
    o = client.post("/api/orders", json={"symbol": "MES", "side": "buy", "qty": 1, "tp_ticks": 40, "sl_ticks": 40}).json()
    assert o["status"] == "working" and len(o["child_ids"]) == 2
    client.post("/api/session/control", json={"action": "step"})
    st = client.get("/api/state").json()
    assert st["positions"][0]["qty"] == 1
    client.post("/api/session/control", json={"action": "step", "count": 4000})
    st = client.get("/api/state").json()
    assert len(st["trades"]) == 1 and st["trades"][0]["exit_reason"] in ("tp", "sl")
    csv = client.get("/api/journal.csv").text
    assert csv.splitlines()[0].startswith("id,symbol,side") and len(csv.splitlines()) == 2
    # bars after stepping are still capped at the replay clock
    t = client.get("/api/session").json()["time"]
    bars = client.get("/api/bars", params={"symbol": "MES", "tf": "1m", "limit": 5}).json()
    assert max(bars["bars"]["time"]) <= t


def test_replay_start_mid_bucket_hides_later_minutes(client):
    """Starting at 10:07 must not reveal the 10:07-10:14 minutes inside the 10:00 15m bar."""
    last = client.get("/api/symbols").json()[0]["last_ts"]
    start = last - 2 * 86400
    start = start - start % 900 + 7 * 60
    client.post("/api/session/replay", json={"symbols": ["MES"], "start": start})
    bars = client.get("/api/bars", params={"symbol": "MES", "tf": "15m", "limit": 5}).json()["bars"]
    assert max(bars["time"]) < start - 7 * 60  # the bucket holding the start is not served as closed
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"op": "subscribe", "symbol": "MES", "tf": "15m", "limit": 20})
        snap = ws.receive_json()
        full = client.get("/api/bars", params={"symbol": "MES", "tf": "1m", "limit": 7}).json()["bars"]
        assert snap["bars"]["time"][-1] == start - 7 * 60
        assert snap["bars"]["close"][-1] == full["close"][-1]  # forming bar ends at the last minute before start


def test_order_errors(client):
    assert client.post("/api/orders", json={"symbol": "MES", "side": "buy"}).status_code == 409  # no session
    last = client.get("/api/symbols").json()[0]["last_ts"]
    client.post("/api/session/replay", json={"symbols": ["MES"], "start": last - 86400})
    assert client.post("/api/orders", json={"symbol": "MNQ", "side": "buy"}).status_code == 400
    r = client.post("/api/orders", json={"symbol": "MES", "side": "buy", "type": "limit"}).json()
    assert r["status"] == "rejected" and r["reason"] == "limit price required"


def test_backtest_api_and_sweep(client):
    import time as _t
    strategies = client.get("/api/strategies").json()
    pats = next(s for s in strategies if s.get("class") == "PatsSecondEntry")
    assert any(p["name"] == "target_ticks" for p in pats["params"])
    r = client.post("/api/backtests", json={"strategy": pats["id"], "symbol": "MES", "start": "2026-09-08"}).json()
    for _ in range(200):
        b = client.get(f"/api/backtests/{r['id']}").json()
        if b["status"] in ("done", "error"):
            break
        _t.sleep(0.05)
    assert b["status"] == "done", b
    assert b["result"]["summary"]["trades"] == len(b["result"]["trades"])
    s = client.post("/api/backtests/sweep", json={"base": {"strategy": pats["id"], "symbol": "MES", "start": "2026-09-15"},
                                                   "grid": {"target_ticks": [4, 8], "stop_ticks": [8, 12]}}).json()
    assert s["combinations"] == 4
    for _ in range(400):
        b = client.get(f"/api/backtests/{s['id']}").json()
        if b["status"] in ("done", "error"):
            break
        _t.sleep(0.05)
    assert b["status"] == "done" and len(b["result"]["rows"]) == 4
    assert any(x["id"] == s["id"] for x in client.get("/api/backtests").json())
    assert client.delete(f"/api/backtests/{s['id']}").json()["ok"]
