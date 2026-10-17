"""Roll repair for late-switching stitched front-month series (free ES/NQ dataset)."""

import gzip
from datetime import date, timedelta

import numpy as np
import pandas as pd

from sim.contracts import registry
from sim.data.bars_github import detect_switches, parse_bars, pick_files, repair_rolls


def _two_contracts(gap=40.0):
    spec = registry().get("ES")
    rng = np.random.default_rng(1)
    ts = []
    d = date(2026, 9, 8)
    while d <= date(2026, 9, 22):
        if d.weekday() < 5:
            s, e = spec.session.session_start(d), spec.session.session_end(d)
            ts.extend(range(s, e, 60))
        d += timedelta(days=1)
    ts = np.array(ts)
    px = 6600 + np.round(np.cumsum(rng.normal(0, 0.5, len(ts))) / 0.25) * 0.25
    tdate = spec.session.trade_dates_ns(ts)
    tdate = pd.to_datetime(tdate).date
    # volume moves to the new contract from Monday 2026-09-14 (trade date)
    new_heavy = np.array([t >= date(2026, 9, 14) for t in tdate])
    old = pd.DataFrame({"ts": ts, "open": px, "high": px + 0.5, "low": px - 0.5, "close": px,
                        "volume": np.where(new_heavy, 50.0, 1000.0)})
    new = old.copy()
    for c in ("open", "high", "low", "close"):
        new[c] = old[c] + gap
    new["volume"] = np.where(new_heavy, 1000.0, 50.0)
    # the dataset switches at 00:00 ET on the expiry Friday (= 23:00 CT Thursday)
    switch = int(pd.Timestamp("2026-09-18 00:00", tz="America/New_York").timestamp())
    stitched = pd.concat([old[old.ts < switch], new[new.ts >= switch]]).reset_index(drop=True)
    return spec, stitched, old, new, switch


def test_detects_mid_session_switch_and_gap():
    spec, stitched, _, _, switch = _two_contracts()
    sw = detect_switches(stitched, spec)
    assert len(sw) == 1
    idx, gap, y, m = sw[0]
    assert int(stitched.ts.iloc[idx]) == switch and abs(gap - 40) <= 1.5 and (y, m) == (2026, 9)


def test_repair_without_contract_files_records_roll():
    spec, stitched, _, _, switch = _two_contracts()
    out, rolls, rep = repair_rolls(stitched, spec)
    assert len(rolls) == 1 and rolls[0].ts == switch and rolls[0].from_contract == "ESU6" and rolls[0].to_contract == "ESZ6"
    assert out.loc[out.ts < switch, "contract"].unique().tolist() == ["ESU6"]
    assert out.loc[out.ts >= switch, "contract"].unique().tolist() == ["ESZ6"]


def test_repair_with_contract_files_moves_to_volume_roll_and_rebuilds():
    spec, stitched, old, new, switch = _two_contracts()
    out, rolls, rep = repair_rolls(stitched, spec, {"ESU6": old, "ESZ6": new})
    r = rolls[0]
    # volume crossed on trade date 09-14, so the series moves at the start of the 09-15 session
    assert r.ts == spec.session.session_start(date(2026, 9, 15))
    assert r.gap == 40.0
    rebuilt = out[(out.ts >= r.ts) & (out.ts < switch)]
    assert (rebuilt.contract == "ESZ6").all() and (rebuilt.volume == 1000).all()
    assert rep.rebuilt_sessions == 4  # 09-15, 09-16, 09-17 and the evening part of 09-18
    # back-adjusted series is continuous at the roll
    i = int(np.searchsorted(out.ts.to_numpy(), r.ts))
    assert abs((out.open.iloc[i] - r.gap) - out.close.iloc[i - 1]) < 3


def test_parse_gz_and_pick_files():
    csv = "datetime,open,high,low,close,volume,source,trading_date,session\n" \
          "2026-09-14 18:00:00-0400,1,2,0.5,1.5,10,x,2026-09-15,ETH\n"
    df = parse_bars(gzip.compress(csv.encode()), "a.csv.gz")
    assert df.ts.iloc[0] == int(pd.Timestamp("2026-09-14 22:00", tz="UTC").timestamp())
    cat = [{"symbol": "NQ", "timeframe": "1min", "path": "NQ/1min/NQ_1min_continuous.csv.gz", "source": "stitched", "rows": 9},
           {"symbol": "NQ", "timeframe": "1min", "path": "NQ/1min/NQ_1min_2025.csv", "source": "stitched", "rows": 5},
           {"symbol": "ES", "timeframe": "1min", "path": "raw/ES/1min/ib/ES_ibkr-ESH6.csv", "source": "ibkr-ESH6", "rows": 5}]
    assert pick_files(cat, "NQ") == ("NQ/1min/NQ_1min_continuous.csv.gz", [])
    assert pick_files(cat, "ES")[1] == ["raw/ES/1min/ib/ES_ibkr-ESH6.csv"]
