"""Performance statistics for backtests and live/replay journals."""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import numpy as np


def _r(x, n=2):
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return None
    return round(float(x), n)


def max_drawdown(equity: np.ndarray) -> tuple[float, float, int, int]:
    """Returns (max drawdown $, max drawdown % of peak, peak index, trough index)."""
    if len(equity) == 0:
        return 0.0, 0.0, 0, 0
    peaks = np.maximum.accumulate(equity)
    dd = peaks - equity
    i = int(np.argmax(dd))
    j = int(np.argmax(equity[: i + 1])) if i > 0 else 0
    pct = dd[i] / peaks[i] * 100 if peaks[i] > 0 else 0.0
    return float(dd[i]), float(pct), j, i


def compute_stats(trades: list[dict], eq_ts: np.ndarray, eq: np.ndarray, days: list[dict], balance: float,
                  tick_value: float, tz: str) -> dict:
    zone = ZoneInfo(tz)
    n = len(trades)
    nets = np.array([t["net"] for t in trades], dtype=float) if n else np.zeros(0)
    gross = np.array([t["pnl"] for t in trades], dtype=float) if n else np.zeros(0)
    fees = float(sum(t["fees"] for t in trades))
    wins = nets[nets > 0]
    losses = nets[nets <= 0]
    gp, gl = float(wins.sum()), float(-losses.sum())
    final = float(eq[-1]) if len(eq) else balance
    dd, dd_pct, pk, tr = max_drawdown(eq) if len(eq) else (0.0, 0.0, 0, 0)

    # streaks
    best = worst = cur_w = cur_l = 0
    for x in nets:
        if x > 0:
            cur_w, cur_l = cur_w + 1, 0
        else:
            cur_l, cur_w = cur_l + 1, 0
        best, worst = max(best, cur_w), max(worst, cur_l)

    ticks = []
    for t in trades:
        q = t["qty"] or 1
        ticks.append(t["pnl"] / (q * tick_value) if tick_value else 0.0)
    ticks = np.array(ticks) if ticks else np.zeros(0)
    durations = [(t["exit_ts"] - t["entry_ts"]) for t in trades if t.get("exit_ts")]

    # daily returns (Sharpe/Sortino)
    sharpe = sortino = None
    if len(days) >= 5:
        eq_start = balance
        rets = []
        for d in days:
            rets.append(d["pnl"] / eq_start if eq_start else 0.0)
            eq_start = d["equity"]
        r = np.array(rets)
        if r.std() > 0:
            sharpe = r.mean() / r.std() * math.sqrt(252)
        downside = r[r < 0]
        if len(downside) and downside.std() > 0:
            sortino = r.mean() / np.sqrt((downside ** 2).mean()) * math.sqrt(252)

    span_days = (eq_ts[-1] - eq_ts[0]) / 86400 if len(eq_ts) > 1 else 0
    cagr = ((final / balance) ** (365 / span_days) - 1) * 100 if span_days > 30 and final > 0 and balance > 0 else None
    in_market = sum(durations)
    exposure = in_market / (eq_ts[-1] - eq_ts[0]) * 100 if len(eq_ts) > 1 and eq_ts[-1] > eq_ts[0] else 0

    summary = {
        "trades": n,
        "wins": int(len(wins)),
        "losses": int(len(losses)),
        "win_rate": _r(len(wins) / n * 100 if n else None, 1),
        "net_pnl": _r(nets.sum()),
        "gross_pnl": _r(gross.sum()),
        "fees": _r(fees),
        "fees_pct_of_gross_profit": _r(fees / float(gross[gross > 0].sum()) * 100 if (gross > 0).any() else None, 1),
        "gross_profit": _r(gp),
        "gross_loss": _r(gl),
        "profit_factor": _r(gp / gl if gl > 0 else (None if gp == 0 else math.inf)),
        "avg_trade": _r(nets.mean() if n else None),
        "avg_win": _r(wins.mean() if len(wins) else None),
        "avg_loss": _r(losses.mean() if len(losses) else None),
        "payoff_ratio": _r(wins.mean() / -losses.mean() if len(wins) and len(losses) and losses.mean() < 0 else None),
        "expectancy": _r(nets.mean() if n else None),
        "expectancy_ticks": _r(ticks.mean() if n else None),
        "largest_win": _r(nets.max() if n else None),
        "largest_loss": _r(nets.min() if n else None),
        "max_consecutive_wins": best,
        "max_consecutive_losses": worst,
        "max_drawdown": _r(dd),
        "max_drawdown_pct": _r(dd_pct),
        "max_drawdown_start": int(eq_ts[pk]) if len(eq_ts) else None,
        "max_drawdown_end": int(eq_ts[tr]) if len(eq_ts) else None,
        "start_balance": _r(balance),
        "final_equity": _r(final),
        "return_pct": _r((final / balance - 1) * 100 if balance else None),
        "cagr_pct": _r(cagr),
        "sharpe": _r(sharpe),
        "sortino": _r(sortino),
        "recovery_factor": _r(nets.sum() / dd if dd > 0 else None),
        "avg_duration_min": _r(np.mean(durations) / 60 if durations else None, 1),
        "exposure_pct": _r(exposure, 1),
        "avg_mae_ticks": _r(np.mean([t["mae_ticks"] for t in trades]) if n else None),
        "avg_mfe_ticks": _r(np.mean([t["mfe_ticks"] for t in trades]) if n else None),
        "trading_days": len(days),
    }

    def bucket(key_fn):
        agg = defaultdict(lambda: {"trades": 0, "net": 0.0, "wins": 0})
        for t in trades:
            k = key_fn(datetime.fromtimestamp(t["entry_ts"], tz=timezone.utc).astimezone(zone))
            a = agg[k]
            a["trades"] += 1
            a["net"] += t["net"]
            a["wins"] += 1 if t["net"] > 0 else 0
        return [{"key": k, "trades": v["trades"], "net": _r(v["net"]),
                 "win_rate": _r(v["wins"] / v["trades"] * 100, 1)} for k, v in sorted(agg.items())]

    def side(s):
        sub = [t for t in trades if t["side"] == s]
        if not sub:
            return {"trades": 0}
        nn = np.array([t["net"] for t in sub])
        w = nn[nn > 0]
        l_ = nn[nn <= 0]
        return {"trades": len(sub), "net": _r(nn.sum()), "win_rate": _r(len(w) / len(sub) * 100, 1),
                "profit_factor": _r(w.sum() / -l_.sum() if l_.sum() < 0 else None)}

    months = defaultdict(float)
    for d in days:
        months[d["date"][:7]] += d["pnl"]

    breakdown = {
        "by_hour": bucket(lambda d: f"{d.hour:02d}:00"),
        "by_weekday": bucket(lambda d: d.strftime("%u %a")),
        "by_month": [{"key": k, "net": _r(v)} for k, v in sorted(months.items())],
        "long": side("long"),
        "short": side("short"),
        "exit_reasons": [{"key": k, "trades": v} for k, v in
                         sorted(_count(t["exit_reason"] for t in trades).items(), key=lambda kv: -kv[1])],
    }
    return {"summary": summary, "breakdown": breakdown}


def _count(it) -> dict:
    out: dict = defaultdict(int)
    for x in it:
        out[x or "-"] += 1
    return out


def downsample(ts: np.ndarray, vals: np.ndarray, max_points: int = 4000) -> list[list[float]]:
    """Keep min/max of each chunk so drawdowns stay visible."""
    n = len(ts)
    if n <= max_points:
        return [[int(t), _r(v)] for t, v in zip(ts, vals)]
    step = int(math.ceil(n / (max_points / 2)))
    out = []
    for i in range(0, n, step):
        seg = vals[i:i + step]
        a, b = int(np.argmin(seg)) + i, int(np.argmax(seg)) + i
        for k in sorted({a, b}):
            out.append([int(ts[k]), _r(vals[k])])
    if out[-1][0] != int(ts[-1]):
        out.append([int(ts[-1]), _r(vals[-1])])
    return out
