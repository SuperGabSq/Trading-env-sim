"""Build a continuous front-month series from individual futures contracts."""

from __future__ import annotations

import re

import numpy as np
import pandas as pd

from ..contracts import MONTH_CODES, ContractSpec
from .store import Roll


def contract_sort_key(code: str, ref_year: int) -> tuple[int, int]:
    """'MESZ6' -> (2026, 12) using the decade closest to ref_year."""
    m = re.match(r"^([A-Z0-9]+?)([FGHJKMNQUVXZ])(\d{1,2})$", code)
    if not m:
        return (9999, 99)
    month = MONTH_CODES.index(m.group(2)) + 1
    digits = m.group(3)
    if len(digits) == 2:
        year = 2000 + int(digits)
    else:
        year = ref_year - ref_year % 10 + int(digits)
        if year < ref_year - 1:
            year += 10
    return (year, month)


def outright_mask(symbols: pd.Series, root: str) -> pd.Series:
    return symbols.astype(str).str.fullmatch(rf"{re.escape(root)}[FGHJKMNQUVXZ]\d{{1,2}}")


def build_continuous(df: pd.DataFrame, spec: ContractSpec) -> tuple[pd.DataFrame, list[Roll]]:
    """df: ts, open, high, low, close, volume, contract (several contracts overlapping in time).

    Volume roll: when the next contract out-trades the current one over a full session, the series
    switches to it from the following session. Contracts never roll backwards.
    """
    df = df.sort_values(["ts", "contract"]).reset_index(drop=True)
    ts = df["ts"].to_numpy("int64")
    df["tdate"] = spec.session.trade_dates_ns(ts)
    ref_year = pd.Timestamp(int(ts[0]), unit="s").year
    contracts = sorted(df["contract"].unique(), key=lambda c: contract_sort_key(c, ref_year))
    order = {c: i for i, c in enumerate(contracts)}

    daily = df.groupby(["tdate", "contract"])["volume"].sum().unstack(fill_value=0.0)
    days = list(daily.index)
    active: dict[int, str] = {}
    current = None
    pending = None
    for d in days:
        row = daily.loc[d]
        traded = row[row > 0]
        if current is None:
            current = traded.idxmax()
        if pending is not None:
            current, pending = pending, None
        if current not in traded.index or traded.get(current, 0) == 0:
            later = [c for c in traded.index if order[c] > order[current]]
            if later:
                current = max(later, key=lambda c: traded[c])
        active[d] = current
        best = traded.idxmax()
        if order.get(best, -1) > order[current] and traded[best] > traded.get(current, 0):
            pending = best

    df["active"] = df["tdate"].map(active)
    cont = df[df["contract"] == df["active"]].copy()

    rolls: list[Roll] = []
    prev_c = None
    for d in days:
        c = active[d]
        if prev_c is not None and c != prev_c:
            first_ts = int(cont.loc[cont["tdate"] == d, "ts"].min())
            # gap measured on the last bar where both contracts traded before the roll
            before = df[(df["ts"] < first_ts) & (df["contract"].isin([c, prev_c]))]
            both = before.pivot_table(index="ts", columns="contract", values="close", aggfunc="last").dropna()
            if len(both):
                last = both.iloc[-1]
                gap = float(last[c] - last[prev_c])
            else:
                nxt = cont[cont["ts"] >= first_ts]["open"].iloc[0]
                prv = cont[cont["ts"] < first_ts]["close"].iloc[-1]
                gap = float(nxt - prv)
            gap = spec.round_price(gap)
            rolls.append(Roll(ts=first_ts, from_contract=str(prev_c), to_contract=str(c), gap=gap))
        prev_c = c
    cont = cont.drop(columns=["tdate", "active"]).reset_index(drop=True)
    return cont, rolls
