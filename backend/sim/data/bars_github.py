"""Free ES/NQ 1-minute bars from the public BARS repository on GitHub (github.com/kfccolonel1/BARS).

The repository's series is an UNADJUSTED front month that switches contract at 00:00 ET one or two days
before each quarterly expiry: in the middle of a session and several days after volume has moved to the
next contract. Imported as-is, every switch would look like a +50..70 point ES (+200..320 NQ) price jump
and be booked as P&L on open positions. The importer therefore:

* detects every switch (largest minute-to-minute jump in the expiry week) and records it as a roll with
  its price gap, so positions roll without fake P&L and charts can back-adjust;
* labels every bar with its contract month;
* where per-contract files exist (ES from IBKR, Dec 2025 onwards) moves the roll to the standard volume
  roll, takes the exact gap from the overlapping contracts and rebuilds the thin expiring-contract days
  from the next contract's real bars;
* stores the series under the micro symbol by default (ES -> MES, NQ -> MNQ): micros trade at the same
  index level, so the bars drive micro tick values, fees and margin (volume stays the E-mini's).

Provenance: the repository has no license file and is stitched from several sources (its README reports
~98% minute agreement with TradingView for NQ and ~93% with IBKR for ES, differences mostly in roll weeks).
Use it for personal research only and do not redistribute it.
"""

from __future__ import annotations

import gzip
import io
import re
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

from ..config import settings
from ..contracts import ContractSpec, contract_code, registry, third_friday
from .importers import dt_to_epoch
from .store import Roll, SeriesMeta, store

BASE = "https://raw.githubusercontent.com/kfccolonel1/BARS/main"
REPO = "https://github.com/kfccolonel1/BARS"
DEFAULT_TARGET = {"ES": "MES", "NQ": "MNQ"}
FALLBACK = {
    "ES": "ES/1min/ES_1min_2025-06-20_2026-10-09_stitched.csv",
    "NQ": "NQ/1min/NQ_1min_continuous.csv.gz",
}


@dataclass
class RepairReport:
    symbol: str
    rows: int = 0
    rolls: list[dict] = field(default_factory=list)
    rebuilt_sessions: int = 0


def _client():
    import httpx
    return httpx.Client(timeout=120, follow_redirects=True)


def catalog() -> list[dict]:
    with _client() as c:
        r = c.get(f"{BASE}/catalog.json")
        r.raise_for_status()
        return r.json()


def _download(path: str, dest: Path, progress: Callable[[str], None], force: bool = False) -> Path:
    out = dest / Path(path).name
    if out.exists() and out.stat().st_size > 0 and not force:
        return out
    dest.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".part")
    with _client() as c, c.stream("GET", f"{BASE}/{path}") as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        done = 0
        with open(tmp, "wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
                done += len(chunk)
                progress(f"  {Path(path).name}: {done / 1e6:.1f}{f' / {total / 1e6:.1f}' if total else ''} MB")
    tmp.replace(out)
    return out


def parse_bars(content: bytes, name: str) -> pd.DataFrame:
    if name.endswith(".gz") or content[:2] == b"\x1f\x8b":
        content = gzip.decompress(content)
    raw = pd.read_csv(io.BytesIO(content))
    out = pd.DataFrame({
        "ts": dt_to_epoch(raw["datetime"]),
        "open": raw["open"].astype(float), "high": raw["high"].astype(float),
        "low": raw["low"].astype(float), "close": raw["close"].astype(float),
        "volume": pd.to_numeric(raw.get("volume", 0), errors="coerce").fillna(0.0).astype(float),
    })
    if "contract" in raw.columns:
        out["contract"] = raw["contract"].astype(str)
    return out.sort_values("ts").drop_duplicates("ts", keep="last").reset_index(drop=True)


def pick_files(catalog: list[dict], root: str) -> tuple[str, list[str]]:  # noqa: D401
    """(continuous 1-minute file, single-contract 1-minute files) for a root symbol."""
    minute = [f for f in catalog if f.get("symbol") == root and f.get("timeframe") == "1min"]
    cont = [f for f in minute if not f["path"].startswith("raw/") and f.get("source") == "stitched"]
    cont.sort(key=lambda f: ("continuous" in f["path"], f.get("rows", 0)), reverse=True)
    main = cont[0]["path"] if cont else FALLBACK[root]
    contracts = sorted(f["path"] for f in minute if f["path"].startswith(f"raw/{root}/1min/ib/"))
    return main, contracts


# ---------------------------------------------------------------- roll repair

def _expiries(spec: ContractSpec, start_ts: int, end_ts: int) -> list:
    out = []
    first = pd.Timestamp(start_ts, unit="s").year
    last = pd.Timestamp(end_ts, unit="s").year
    months = ["FGHJKMNQUVXZ".index(m) + 1 for m in spec.months]
    for y in range(first, last + 1):
        for m in months:
            out.append((y, m, third_friday(y, m)))
    return out


def detect_switches(df: pd.DataFrame, spec: ContractSpec) -> list[tuple[int, float, int, int]]:
    """Find the contract switch near each quarterly expiry.

    Returns [(row index of first bar on the new contract, gap, year, month of the expiring contract)].
    Only consecutive-minute jumps are considered (a switch inside a session), so weekend gaps and real
    news moves are not mistaken for rolls.
    """
    ts = df["ts"].to_numpy()
    o = df["open"].to_numpy()
    c = df["close"].to_numpy()
    consecutive = np.r_[False, np.diff(ts) == 60]
    out = []
    for y, m, exp in _expiries(spec, int(ts[0]), int(ts[-1])):
        lo = spec.session.session_start(exp - timedelta(days=4))
        hi = spec.session.session_end(exp + timedelta(days=1))
        sel = np.flatnonzero((ts >= lo) & (ts < hi) & consecutive)
        if len(sel) < 100:
            continue
        jumps = o[sel] - c[sel - 1]
        moves = np.diff(c[sel[0]:sel[-1] + 1])
        sigma = float(np.std(moves)) or spec.tick_size
        k = int(np.argmax(np.abs(jumps)))
        if abs(jumps[k]) / sigma < 10 or abs(jumps[k]) < 8 * spec.tick_size:
            continue
        out.append((int(sel[k]), float(jumps[k]), y, m))
    return out


def repair_rolls(df: pd.DataFrame, spec: ContractSpec, contract_frames: dict[str, pd.DataFrame] | None = None
                 ) -> tuple[pd.DataFrame, list[Roll], RepairReport]:
    rep = RepairReport(symbol=spec.symbol)
    df = df.sort_values("ts").reset_index(drop=True)
    switches = detect_switches(df, spec)
    contract_frames = contract_frames or {}
    rolls: list[Roll] = []
    pieces = []
    prev_idx = 0
    prev_code = None
    for idx, gap, y, m in switches:
        old = contract_code(spec.symbol, y, m)
        nxt_month = _next_month(spec, m)
        ny = y + (1 if nxt_month < m else 0)
        new = contract_code(spec.symbol, ny, nxt_month)
        switch_ts = int(df["ts"].iloc[idx])
        old_f, new_f = contract_frames.get(old), contract_frames.get(new)
        roll_ts, roll_idx, exact_gap, rebuilt = switch_ts, idx, None, None
        if old_f is not None and new_f is not None:
            roll_ts = _volume_roll_ts(old_f, new_f, spec, switch_ts)
            if roll_ts < switch_ts:
                rebuilt = new_f[(new_f["ts"] >= roll_ts) & (new_f["ts"] < switch_ts)]
                roll_idx = int(np.searchsorted(df["ts"].to_numpy(), roll_ts))
                both = old_f.merge(new_f, on="ts", suffixes=("_o", "_n"))
                both = both[both["ts"] < roll_ts].tail(30)
                if len(both):
                    exact_gap = float(np.median(both["close_n"] - both["close_o"]))
        g = spec.round_price(exact_gap if exact_gap is not None else gap)
        pieces.append(df.iloc[prev_idx:roll_idx].assign(contract=old))
        if rebuilt is not None and len(rebuilt):
            pieces.append(rebuilt[["ts", "open", "high", "low", "close", "volume"]].assign(contract=new))
            sessions = len(set(spec.session.trade_dates_ns(rebuilt["ts"].to_numpy()).tolist()))
            rep.rebuilt_sessions += sessions
        prev_idx = idx
        prev_code = new
        rolls.append(Roll(ts=int(roll_ts), from_contract=old, to_contract=new, gap=float(g)))
        rep.rolls.append({"from": old, "to": new, "gap": g, "at": pd.Timestamp(roll_ts, unit="s", tz="UTC")
                          .tz_convert(spec.session.tz).strftime("%Y-%m-%d %H:%M"),
                          "method": "volume roll, rebuilt from next contract" if rebuilt is not None and len(rebuilt)
                          else "dataset switch"})
    if prev_code is None:
        prev_code = spec.symbol
    pieces.append(df.iloc[prev_idx:].assign(contract=prev_code))
    out = pd.concat(pieces).sort_values("ts", kind="stable").drop_duplicates("ts", keep="last").reset_index(drop=True)
    rep.rows = len(out)
    return out, rolls, rep


def _next_month(spec: ContractSpec, m: int) -> int:
    months = ["FGHJKMNQUVXZ".index(x) + 1 for x in spec.months]
    later = [x for x in months if x > m]
    return later[0] if later else months[0]


def _volume_roll_ts(old_f: pd.DataFrame, new_f: pd.DataFrame, spec: ContractSpec, switch_ts: int) -> int:
    """Session start after the first trade date on which the next contract out-traded the old one."""
    def daily(f):
        d = spec.session.trade_dates_ns(f["ts"].to_numpy())
        return pd.Series(f["volume"].to_numpy(), index=d).groupby(level=0).sum()
    vo, vn = daily(old_f), daily(new_f)
    days = sorted(set(vo.index) & set(vn.index))
    for i, d in enumerate(days):
        if vn[d] > vo[d] and i + 1 < len(days):
            nxt = pd.Timestamp(days[i + 1]).date()
            ts = spec.session.session_start(nxt)
            return min(ts, switch_ts)
    return switch_ts



# ---------------------------------------------------------------- import

def _rename(code: str, root: str, target: str) -> str:
    return re.sub(rf"^{root}(?=[FGHJKMNQUVXZ]\d)", target, code) if target != root else code


def import_bars(root: str = "ES", target: str | None = None, progress: Callable[[str], None] = print,
                force: bool = False) -> SeriesMeta:
    """Download, repair and store one market. Returns the stored series' metadata."""
    root = root.upper()
    target = (target or DEFAULT_TARGET.get(root, root)).upper()
    spec = registry().get(root)
    registry().get(target)
    try:
        main_path, contract_paths = pick_files(catalog(), root)
    except Exception:
        main_path, contract_paths = FALLBACK[root], []
    dest = settings.data_dir / "downloads" / "bars"
    progress(f"Downloading {root} 1-minute bars from {REPO} ...")
    df = parse_bars(_download(main_path, dest, progress, force).read_bytes(), main_path)
    frames = {}
    for path in contract_paths:
        f = parse_bars(_download(path, dest, progress, force).read_bytes(), path)
        if "contract" in f.columns and f["contract"].nunique() == 1:
            frames[str(f["contract"].iloc[0])] = f.drop(columns=["contract"])
    cleaned, rolls, rep = repair_rolls(df, spec, frames)
    cleaned["contract"] = cleaned["contract"].map(lambda c: _rename(str(c), root, target))
    rolls = [Roll(r.ts, _rename(r.from_contract, root, target), _rename(r.to_contract, root, target), r.gap)
             for r in rolls]
    note = (f"BARS {root} 1-minute bars ({REPO}). No license file: personal research only, do not "
            f"redistribute. {len(rolls)} contract switches detected and recorded as rolls")
    if rep.rebuilt_sessions:
        note += f"; {rep.rebuilt_sessions} thin expiry sessions rebuilt from per-contract IBKR files"
    note += "."
    if target != root:
        note += f" {root} prices drive {target} contract specs; volume is {root} volume."
    meta = SeriesMeta(symbol=target, source=f"bars-github:{root}", base_tf="1m", synthetic=False, rolls=rolls,
                      notes=note)
    meta = store().write(target, cleaned, meta, merge=False)
    progress(f"Stored {meta.rows:,} bars for {target}")
    for r in rep.rolls:
        progress(f"    {_rename(r['from'], root, target)} -> {_rename(r['to'], root, target)}  gap {r['gap']:+g}  "
                 f"at {r['at']} CT  ({r['method']})")
    return meta
