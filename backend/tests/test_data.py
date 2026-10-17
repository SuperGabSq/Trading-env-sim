from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from sim.contracts import registry, root_symbol
from sim.data.bars import BarBuilder, bucket_keys, resample
from sim.data.importers import import_csv, parse_binance_klines, read_csv_bars
from sim.data.synthetic import generate

CT = ZoneInfo("America/Chicago")


def ct(y, m, d, hh, mm):
    return int(datetime(y, m, d, hh, mm, tzinfo=CT).timestamp())


def test_root_symbol():
    assert root_symbol("MESZ6") == "MES"
    assert root_symbol("MES1!") == "MES"
    assert root_symbol("MCLF27") == "MCL"
    assert root_symbol("BTCUSDT") == "BTCUSDT"


def test_session_trade_date_and_margin_windows():
    s = registry().get("MES").session
    assert s.trade_date(ct(2026, 10, 14, 17, 30)) == date(2026, 10, 15)  # Wed evening -> Thu
    assert s.trade_date(ct(2026, 10, 15, 15, 59)) == date(2026, 10, 15)
    assert s.in_session(ct(2026, 10, 15, 16, 30)) is False               # daily halt
    assert s.in_session(ct(2026, 10, 17, 10, 0)) is False                # Saturday
    assert s.in_session(ct(2026, 10, 18, 17, 0)) is True                 # Sunday open
    assert s.is_day_margin(ct(2026, 10, 15, 15, 44)) is True
    assert s.is_day_margin(ct(2026, 10, 15, 15, 46)) is False


def test_four_hour_buckets_align_to_session_open():
    s = registry().get("MES").session
    ts = np.array([ct(2026, 10, 14, 17, 0), ct(2026, 10, 14, 20, 59), ct(2026, 10, 14, 21, 0),
                   ct(2026, 10, 15, 15, 50)])
    keys = bucket_keys(ts, s, "4h")
    hours = [datetime.fromtimestamp(k, tz=timezone.utc).astimezone(CT).hour for k in keys]
    assert hours == [17, 17, 21, 13]


def test_daily_bars_use_trade_date_across_dst():
    s = registry().get("MES").session
    # Sunday 2026-11-01 is the DST change; the Monday session opens Sunday 17:00 CST
    ts = np.array([ct(2026, 11, 1, 17, 0), ct(2026, 11, 2, 15, 59), ct(2026, 11, 2, 17, 0)])
    keys = bucket_keys(ts, s, "1D")
    days = [datetime.fromtimestamp(k, tz=timezone.utc).date() for k in keys]
    assert days == [date(2026, 11, 2), date(2026, 11, 2), date(2026, 11, 3)]


def test_resample_ohlcv_matches_manual():
    out = generate(["MES"], date(2026, 9, 1), date(2026, 9, 10))
    df, meta = out["MES"]
    from sim.data.bars import Bars
    b = Bars(df.ts.to_numpy(), df.open.to_numpy(), df.high.to_numpy(), df.low.to_numpy(),
             df.close.to_numpy(), df.volume.to_numpy())
    s = registry().get("MES").session
    r5 = resample(b, s, "5m")
    i = 7
    sel = (b.ts >= r5.ts[i]) & (b.ts < r5.ts[i] + 300)
    assert r5.open[i] == b.open[sel][0]
    assert r5.close[i] == b.close[sel][-1]
    assert r5.high[i] == b.high[sel].max()
    assert r5.low[i] == b.low[sel].min()
    assert r5.volume[i] == b.volume[sel].sum()


def test_synthetic_schedule_and_rolls():
    out = generate(["MES", "MNQ"], date(2025, 11, 1), date(2026, 4, 1))
    df, meta = out["MES"]
    s = registry().get("MES").session
    assert all(s.in_session(int(t)) for t in df.ts.to_numpy()[::997])
    # ticks are multiples of 0.25
    assert np.allclose((df.close.to_numpy() / 0.25) % 1, 0)
    assert (df.high >= df[["open", "close"]].max(axis=1)).all()
    assert [r.to_contract for r in meta.rolls] == ["MESH6", "MESM6"]
    assert 20 < meta.rolls[0].gap < 80
    # Thanksgiving closed, Friday after closes early
    tdates = {s.trade_date(int(t)) for t in df.ts.to_numpy()[::60]}
    assert date(2025, 11, 27) not in tdates
    friday = df[[s.trade_date(int(t)) == date(2025, 11, 28) for t in df.ts.to_numpy()]]
    last = datetime.fromtimestamp(int(friday.ts.max()), tz=timezone.utc).astimezone(CT)
    assert (last.hour, last.minute) == (12, 14)


def test_bar_builder_matches_resample():
    out = generate(["MES"], date(2026, 9, 1), date(2026, 9, 3))
    df, _ = out["MES"]
    s = registry().get("MES").session
    bb = BarBuilder(s, "15m")
    closed = []
    for r in df.itertuples():
        c, cur = bb.update(int(r.ts), r.open, r.high, r.low, r.close, r.volume)
        if c:
            closed.append(c)
    from sim.data.bars import Bars
    b = Bars(df.ts.to_numpy(), df.open.to_numpy(), df.high.to_numpy(), df.low.to_numpy(),
             df.close.to_numpy(), df.volume.to_numpy())
    r = resample(b, s, "15m")
    assert len(closed) == len(r) - 1
    assert closed[10]["high"] == r.high[10] and closed[10]["time"] == r.ts[10]


def test_tradingview_csv(tmp_path, tmp_store):
    p = tmp_path / "tv.csv"
    p.write_text("time,open,high,low,close,Volume,EMA\n"
                 "1792015200,6700,6701,6699.5,6700.5,120,1\n"
                 "1792015260,6700.5,6702,6700,6701.75,80,1\n"
                 "1792015320,6701.75,6702.25,6701,6701,60,1\n")
    meta = import_csv(p, "MES")
    assert meta.rows == 3 and meta.base_tf == "1m" and not meta.synthetic
    b = tmp_store.bars("MES", "1m")
    assert b.ts[0] == 1792015200 and b.close[1] == 6701.75


def test_tradingview_iso_times_with_offset(tmp_path):
    p = tmp_path / "tv_iso.csv"
    p.write_text("time,open,high,low,close,Volume\n"
                 "2026-10-14T17:00:00-05:00,1,2,0.5,1.5,10\n"
                 "2026-10-14T17:05:00-05:00,1,2,0.5,1.5,10\n")
    df = read_csv_bars(p)
    assert df.ts.iloc[0] == ct(2026, 10, 14, 17, 0)


def test_ninjatrader_csv_uses_bar_close_times(tmp_path):
    p = tmp_path / "MES 12-26.Last.txt"
    p.write_text("20261014 170100;6700;6701;6699.5;6700.5;120\n"
                 "20261014 170200;6700.5;6702;6700;6701.75;80\n"
                 "20261014 170300;6701.75;6702.25;6701;6701;60\n")
    df = read_csv_bars(p, tz="America/Chicago")
    assert df.ts.iloc[0] == ct(2026, 10, 14, 17, 0)


def test_databento_csv_multi_contract_builds_continuous(tmp_path, tmp_store):
    rows = ["ts_event,rtype,publisher_id,instrument_id,open,high,low,close,volume,symbol"]
    # Old contract MESZ6 trades heavy on day 1, MESH7 heavier on day 2 -> roll from day 3
    days = [ct(2026, 12, 9, 17, 0), ct(2026, 12, 10, 17, 0), ct(2026, 12, 13, 17, 0)]
    for di, start in enumerate(days):
        for k in range(30):
            t = start + k * 60
            iso = datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000000Z")
            vz, vh = (100, 10) if di == 0 else (10, 100)
            rows.append(f"{iso},33,1,1,7000,7001,6999,7000.5,{vz},MESZ6")
            rows.append(f"{iso},33,1,2,7040,7041,7039,7040.5,{vh},MESH7")
            rows.append(f"{iso},33,1,3,40,41,39,40.5,5,MESZ6-MESH7")
    p = tmp_path / "glbx-mdp3.ohlcv-1m.csv"
    p.write_text("\n".join(rows))
    meta = import_csv(p, "MES")
    assert [(r.from_contract, r.to_contract, r.gap) for r in meta.rolls] == [("MESZ6", "MESH7", 40.0)]
    b = tmp_store.bars("MES", "1m")
    assert b.contracts[b.contract[0]] == "MESZ6" and b.contracts[b.contract[-1]] == "MESH7"
    # day 2 still on the old contract (switch happens the session AFTER volume crosses)
    assert b.close[30] == 7000.5 and b.close[60] == 7040.5
    adj = tmp_store.bars("MES", "1m", adjusted=True)
    assert adj.close[0] == 7040.5 and adj.close[-1] == 7040.5


def test_binance_klines_parse():
    rows = [[1792015200000, "60000.1", "60010.0", "59990.0", "60005.5", "12.5", 0, "0", 0, "0", "0", "0"]]
    df = parse_binance_klines(rows, point_value=0.01)
    assert df.ts.iloc[0] == 1792015200 and df.volume.iloc[0] == 1250
