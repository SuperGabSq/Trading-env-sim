"""Synthetic but realistic-looking 1-minute futures data for development and practice.

The generator follows the real CME equity-index schedule (Sun 17:00 CT - Fri 16:00 CT with the
daily 16:00-17:00 halt, holidays and early closes), a U-shaped intraday volatility and volume
profile with the 08:30 CT cash open spike, 07:30 CT economic releases, occasional FOMC-style jumps,
fat-tailed returns, stochastic volatility, trend regimes, correlated symbols (MES/MNQ) and a
quarterly contract cycle whose futures basis creates realistic roll gaps.

It is NOT market data. The UI marks synthetic series clearly.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd

from ..contracts import ContractSpec, contract_cycle, registry
from .store import Roll, SeriesMeta

SUBSTEPS = 12  # 5-second steps inside each minute


def _holidays(years: range) -> tuple[set[date], dict[date, int]]:
    closed: set[date] = set()
    early: dict[date, int] = {}  # trade date -> close minute of day (local)
    for y in years:
        closed.add(date(y, 1, 1))
        closed.add(date(y, 7, 4))
        closed.add(date(y, 12, 25))
        nov1 = date(y, 11, 1)
        thanksgiving = nov1 + timedelta(days=(3 - nov1.weekday()) % 7 + 21)
        closed.add(thanksgiving)
        early[thanksgiving + timedelta(days=1)] = 12 * 60 + 15
        if date(y, 12, 24).weekday() < 5:
            early[date(y, 12, 24)] = 12 * 60 + 15
    return closed, early


def _vol_profile(tod: np.ndarray) -> np.ndarray:
    """Relative volatility by local (CT) minute of day."""
    m = np.full(tod.shape, 0.35)
    m[(tod >= 17 * 60) & (tod < 20 * 60)] = 0.5
    m[(tod >= 2 * 60) & (tod < 7 * 60)] = 0.6
    m[(tod >= 7 * 60) & (tod < 8 * 60 + 30)] = 0.8
    rth_open = (tod >= 8 * 60 + 30) & (tod < 9 * 60 + 30)
    m[rth_open] = 1.15 + 1.4 * np.exp(-(tod[rth_open] - 510) / 12.0)
    m[(tod >= 9 * 60 + 30) & (tod < 11 * 60)] = 1.05
    m[(tod >= 11 * 60) & (tod < 13 * 60)] = 0.75
    m[(tod >= 13 * 60) & (tod < 14 * 60 + 30)] = 0.9
    m[(tod >= 14 * 60 + 30) & (tod < 15 * 60)] = 1.3
    m[(tod >= 15 * 60) & (tod < 15 * 60 + 15)] = 0.85
    m[(tod >= 15 * 60 + 15) & (tod < 16 * 60)] = 0.5
    return m


def _volume_profile(tod: np.ndarray) -> np.ndarray:
    v = np.full(tod.shape, 0.08)
    v[(tod >= 17 * 60) & (tod < 19 * 60)] = 0.12
    v[(tod >= 2 * 60) & (tod < 7 * 60)] = 0.18
    v[(tod >= 7 * 60) & (tod < 8 * 60 + 30)] = 0.3
    rth = (tod >= 8 * 60 + 30) & (tod < 15 * 60)
    x = (tod[rth] - 510) / 390.0
    v[rth] = 0.75 + 1.6 * np.exp(-x * 9) + 0.9 * np.exp((x - 1) * 10)
    v[(tod >= 15 * 60) & (tod < 16 * 60)] = 0.35
    return v


def generate(symbols: list[str] | None = None, start: date | None = None, end: date | None = None,
             seed: int = 6, correlation: float = 0.88) -> dict[str, tuple[pd.DataFrame, SeriesMeta]]:
    reg = registry()
    symbols = symbols or ["MES", "MNQ"]
    specs: list[ContractSpec] = [reg.get(s) for s in symbols]
    end = end or (date.today() - timedelta(days=1))
    start = start or (end - timedelta(days=365))
    rng = np.random.default_rng(seed)
    session = specs[0].session  # all generated symbols share the CME equity schedule

    closed, early = _holidays(range(start.year, end.year + 1))
    trade_dates = [d for d in pd.date_range(start, end, freq="D").date
                   if d.weekday() < 5 and d not in closed]
    n_days = len(trade_dates)
    n_min = 23 * 60  # 17:00 -> 16:00

    tod = (17 * 60 + np.arange(n_min)) % 1440
    volp = _vol_profile(tod)
    volp = volp / np.sqrt(np.mean(volp ** 2))  # unit average variance per minute
    vlmp = _volume_profile(tod)

    # Shared daily state: stochastic vol, trend regime, news days
    x = np.zeros(n_days)
    trend = np.zeros(n_days)
    for i in range(1, n_days):
        x[i] = 0.92 * x[i - 1] + 0.22 * rng.standard_normal()
        trend[i] = 0.96 * trend[i - 1] + 0.12 * rng.standard_normal()
    volf = np.exp(x - 0.5 * np.var(x))
    news = rng.random(n_days) < 0.22
    fomc = np.zeros(n_days, dtype=bool)
    fomc[rng.choice(n_days, size=max(1, n_days // 31), replace=False)] = True

    # Correlated fat-tailed innovations (multivariate t, df=4), shape (symbols, days, minutes, substeps)
    df_t = 4.0
    k = len(specs)
    z_common = rng.standard_normal((n_days, n_min, SUBSTEPS))
    chi = np.sqrt(df_t / rng.chisquare(df_t, size=(n_days, n_min, 1)))
    scale = chi / np.sqrt(df_t / (df_t - 2))

    out: dict[str, tuple[pd.DataFrame, SeriesMeta]] = {}
    for si, spec in enumerate(specs):
        p = {"start_price": 5000.0, "annual_vol": 0.18, "drift": 0.05, "basis_rate": 0.03,
             "base_volume": 1500, **spec.synthetic}
        if si == 0:
            z = z_common
        else:
            z = correlation * z_common + np.sqrt(1 - correlation ** 2) * rng.standard_normal(z_common.shape)
        z = z * scale
        sig_day = p["annual_vol"] / np.sqrt(252)
        sig_min = sig_day / np.sqrt(n_min)
        sig = (sig_min * volp)[None, :, None] * volf[:, None, None] / np.sqrt(SUBSTEPS)
        steps = sig * z

        # drift: annual drift + trend regime, spread over RTH minutes
        rth_mask = ((tod >= 8 * 60 + 30) & (tod < 15 * 60)).astype(float)
        rth_mask /= rth_mask.sum()
        day_drift = p["drift"] / 252 + trend * sig_day * 0.12
        steps += (day_drift[:, None] * rth_mask[None, :])[:, :, None] / SUBSTEPS

        # news at 07:30 CT and FOMC at 13:00 CT
        i0730 = int(np.flatnonzero(tod == 7 * 60 + 30)[0])
        i1300 = int(np.flatnonzero(tod == 13 * 60)[0])
        jump = rng.standard_normal(n_days)
        steps[news, i0730, 0] += 0.35 * sig_day * jump[news]
        steps[fomc, i1300, 0] += 0.7 * sig_day * jump[fomc]
        steps[fomc, i1300:i1300 + 40, :] *= 2.0

        # overnight / weekend gaps (applied at the session open)
        gaps = rng.standard_normal(n_days) * sig_day * 0.12
        for i in range(1, n_days):
            if (trade_dates[i] - trade_dates[i - 1]).days > 1:
                gaps[i] *= 3.0
        steps[:, 0, 0] += gaps

        logp = np.log(p["start_price"]) + np.cumsum(steps.reshape(-1)).reshape(steps.shape)
        spot = np.exp(logp)  # (days, minutes, substeps)

        # contract cycle + basis
        cycle = contract_cycle(spec, start, end)
        ts_days = np.array([session.session_start(d) for d in trade_dates], dtype="int64")
        ts = (ts_days[:, None] + np.arange(n_min)[None, :] * 60)
        codes = np.empty(n_days, dtype=object)
        expiry_ts = np.empty(n_days, dtype="int64")
        for i, d in enumerate(trade_dates):
            for code, roll_date, exp in cycle:
                if d < roll_date:
                    codes[i] = code
                    expiry_ts[i] = session.session_end(exp)
                    break
        years_to_exp = (expiry_ts[:, None] - ts) / (365.0 * 86400)
        basis = 1.0 + p["basis_rate"] * np.clip(years_to_exp, 0, None)
        fut = spot * basis[:, :, None]
        fut = np.round(fut / spec.tick_size) * spec.tick_size

        # OHLC per minute (open = previous substep price inside the session)
        prev = np.concatenate([fut[:, :1, :1], fut[:, :-1, -1:]], axis=1)
        o = prev[:, :, 0]
        o[:, 0] = fut[:, 0, 0]
        h = np.maximum(fut.max(axis=2), o)
        lo = np.minimum(fut.min(axis=2), o)
        c = fut[:, :, -1]

        r_abs = np.abs(np.diff(np.log(fut[:, :, [0, -1]]), axis=2))[:, :, 0]
        sig_ref = (sig_min * volp)[None, :] * volf[:, None]
        noise = rng.lognormal(0, 0.35, size=(n_days, n_min))
        vol = p["base_volume"] * vlmp[None, :] * (0.6 + 0.8 * r_abs / np.maximum(sig_ref, 1e-12)) * noise
        vol[fomc, i1300:i1300 + 40] *= 2.5
        vol = np.maximum(1, np.round(vol))

        # rolls: gap between new and old contract at the first bar of the new contract
        rolls: list[Roll] = []
        for i in range(1, n_days):
            if codes[i] != codes[i - 1]:
                old_basis = 1.0 + p["basis_rate"] * max(0.0, (expiry_ts[i - 1] - ts[i, 0]) / (365.0 * 86400))
                gap = spot[i, 0, 0] * (basis[i, 0] - old_basis)
                gap = round(round(gap / spec.tick_size) * spec.tick_size, spec.price_decimals)
                rolls.append(Roll(ts=int(ts[i, 0]), from_contract=str(codes[i - 1]),
                                  to_contract=str(codes[i]), gap=float(gap)))

        # early closes: truncate minutes after the close
        keep = np.ones((n_days, n_min), dtype=bool)
        for i, d in enumerate(trade_dates):
            if d in early:
                close_idx = int(np.flatnonzero(tod == early[d])[0])
                keep[i, close_idx:] = False

        frame = pd.DataFrame({
            "ts": ts[keep],
            "open": o[keep],
            "high": h[keep],
            "low": lo[keep],
            "close": c[keep],
            "volume": vol[keep],
            "contract": np.repeat(codes, n_min).reshape(n_days, n_min)[keep].astype(str),
        })
        meta = SeriesMeta(symbol=spec.symbol, source="synthetic", base_tf="1m", synthetic=True,
                          rolls=rolls, notes=f"Synthetic sample data (seed {seed}). Not real prices.")
        out[spec.symbol] = (frame, meta)
    return out


def generate_into_store(symbols: list[str] | None = None, days: int = 365, seed: int = 6) -> list[SeriesMeta]:
    from .store import store

    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=days)
    metas = []
    for sym, (df, meta) in generate(symbols, start, end, seed).items():
        metas.append(store().write(sym, df, meta))
    return metas
