"""Golden scenarios for the simulation engine (MES: tick 0.25 = $1.25, $0.62 per side)."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from sim.engine import Engine, FillConfig, OrderType, RiskConfig, Status

CT = ZoneInfo("America/Chicago")


def ct(y, m, d, hh, mm, ss=0):
    return int(datetime(y, m, d, hh, mm, ss, tzinfo=CT).timestamp())


T0 = ct(2026, 10, 14, 9, 0)  # Wednesday 09:00 CT (RTH, day margin)
FEE = 0.62


def make(balance=50_000.0, risk=None, **fill):
    # Classic model for these scenarios: a flat 1 tick of slippage, no spread model.
    e = Engine(FillConfig(**{"slippage_ticks": 1.0, "spread_model": False, **fill}), risk or RiskConfig())
    e.add_account("A", balance)
    return e


def bar(e, i, o, h, l, c, sym="MES", t0=T0):
    e.on_bar(sym, t0 + 60 * i, o, h, l, c, 100)


def start(e, price=5000.0, sym="MES", t0=T0):
    e.on_price(sym, t0 - 60, price, False)


def pos(e, sym="MES", acct="A"):
    return e.accounts[acct].position(sym)


# ------------------------------------------------------------------ market orders & P&L

def test_market_buy_fills_next_open_plus_slippage():
    e = make()
    start(e)
    o = e.place("A", "MES", "buy", 1)
    assert o.status == Status.WORKING
    bar(e, 0, 5001, 5002, 5000, 5001.5)
    assert o.status == Status.FILLED and o.avg_fill == 5001.25
    assert pos(e).qty == 1 and pos(e).avg_price == 5001.25


def test_market_sell_no_slippage():
    e = make(slippage_ticks=0)
    start(e)
    o = e.place("A", "MES", "sell", 2)
    bar(e, 0, 5001, 5002, 5000, 5001.5)
    assert o.avg_fill == 5001 and pos(e).qty == -2


def test_round_trip_pnl_to_the_cent():
    e = make(slippage_ticks=0)
    start(e)
    e.place("A", "MES", "buy", 2)
    bar(e, 0, 5000, 5003, 4999, 5002)
    e.place("A", "MES", "sell", 2)
    bar(e, 1, 5002, 5002.5, 5001, 5001)
    a = e.accounts["A"]
    # 8 ticks * $1.25 * 2 contracts = $20, fees 4 * 0.62 = 2.48
    assert a.balance == pytest.approx(50_000 + 20 - 2.48)
    t = a.trades[0]
    assert (t.side, t.qty, t.entry_price, t.exit_price) == ("long", 2, 5000, 5002)
    assert t.pnl == pytest.approx(20) and t.fees == pytest.approx(2.48) and t.net == pytest.approx(17.52)


def test_short_pnl():
    e = make(slippage_ticks=0)
    start(e)
    e.place("A", "MES", "sell", 1)
    bar(e, 0, 5000, 5000, 4990, 4991)
    e.place("A", "MES", "buy", 1)
    bar(e, 1, 4990, 4991, 4989, 4990)
    assert e.accounts["A"].trades[0].pnl == pytest.approx(10 * 5.0)


def test_average_price_when_adding_and_partial_close():
    e = make(slippage_ticks=0)
    start(e)
    e.place("A", "MES", "buy", 1)
    bar(e, 0, 5000, 5001, 4999, 5000)
    e.place("A", "MES", "buy", 3)
    bar(e, 1, 5004, 5005, 5003, 5004)
    assert pos(e).qty == 4 and pos(e).avg_price == pytest.approx(5003)
    e.place("A", "MES", "sell", 2)
    bar(e, 2, 5005, 5006, 5004, 5005)
    assert pos(e).qty == 2 and pos(e).avg_price == pytest.approx(5003)
    assert pos(e).realized == pytest.approx(2 * 2 * 5.0)
    assert e.accounts["A"].trades == []  # still open


def test_flip_creates_two_trades():
    e = make(slippage_ticks=0)
    start(e)
    e.place("A", "MES", "buy", 1)
    bar(e, 0, 5000, 5001, 4999, 5000)
    e.place("A", "MES", "sell", 3)
    bar(e, 1, 5002, 5003, 5001, 5002)
    assert pos(e).qty == -2 and pos(e).avg_price == 5002
    a = e.accounts["A"]
    assert len(a.trades) == 1 and a.trades[0].pnl == pytest.approx(10)
    assert a.open_trades["MES"].side == "short" and a.open_trades["MES"].qty == 2
    # fees: 1 open + 3 on the flip fill, split 1/3 closing and 2/3 to the new trade
    assert a.trades[0].fees == pytest.approx(2 * FEE)
    assert a.open_trades["MES"].fees == pytest.approx(2 * FEE)


# ------------------------------------------------------------------ limit orders

def test_buy_limit_needs_one_tick_through():
    e = make()
    start(e, 5005)
    o = e.place("A", "MES", "buy", 1, "limit", price=5000)
    bar(e, 0, 5005, 5006, 5000, 5004)          # touches 5000 only
    assert o.status == Status.WORKING
    bar(e, 1, 5004, 5004.5, 4999.75, 5002)     # trades 1 tick through
    assert o.status == Status.FILLED and o.avg_fill == 5000


def test_limit_fills_on_touch_with_zero_through():
    e = make(limit_through_ticks=0)
    start(e, 5005)
    o = e.place("A", "MES", "buy", 1, "limit", price=5000)
    bar(e, 0, 5005, 5006, 5000, 5004)
    assert o.status == Status.FILLED and o.avg_fill == 5000


def test_limit_gap_fills_at_better_open():
    e = make()
    start(e, 5005)
    o = e.place("A", "MES", "buy", 1, "limit", price=5000)
    bar(e, 0, 5005, 5006, 5003, 5004)
    bar(e, 1, 4995, 4996, 4990, 4992)  # gap below the limit
    assert o.avg_fill == 4995


def test_sell_limit():
    e = make()
    start(e, 5000)
    o = e.place("A", "MES", "sell", 1, "limit", price=5004)
    bar(e, 0, 5000, 5004.25, 4999, 5003)
    assert o.status == Status.FILLED and o.avg_fill == 5004 and pos(e).qty == -1


# ------------------------------------------------------------------ stops

def test_buy_stop_triggers_inside_bar():
    e = make()
    start(e, 5000)
    o = e.place("A", "MES", "buy", 1, "stop", stop=5003)
    bar(e, 0, 5000, 5004, 4999, 5002)
    assert o.avg_fill == 5003.25  # stop + 1 tick slippage


def test_sell_stop_gap_fills_at_gap_price():
    e = make()
    start(e, 5000)
    e.place("A", "MES", "buy", 1)
    bar(e, 0, 5000, 5001, 4999, 5000)
    sl = e.place("A", "MES", "sell", 1, "stop", stop=4995, reduce_only=True)
    bar(e, 1, 4990, 4991, 4985, 4988)
    assert sl.avg_fill == 4989.75


def test_stop_limit_triggers_then_limits():
    e = make(limit_through_ticks=0)
    start(e, 5000)
    o = e.place("A", "MES", "buy", 1, "stop_limit", stop=5003, price=5003.5)
    bar(e, 0, 5000, 5005, 4999, 5004)
    assert o.status == Status.FILLED and o.avg_fill == 5003


def test_stop_limit_gap_past_limit_stays_working():
    e = make()
    start(e, 5000)
    o = e.place("A", "MES", "buy", 1, "stop_limit", stop=5003, price=5003.5)
    bar(e, 0, 5006, 5008, 5005, 5007)
    assert o.status == Status.WORKING and o.triggered
    bar(e, 1, 5007, 5007, 5002, 5003)  # comes back below the limit
    assert o.status == Status.FILLED and o.avg_fill == 5003.5


def test_sell_stop_limit():
    e = make(limit_through_ticks=0)
    start(e, 5000)
    o = e.place("A", "MES", "sell", 1, "stop_limit", stop=4998, price=4997.5)
    bar(e, 0, 5000, 5000, 4996, 4997)
    assert o.avg_fill == 4998


def test_trailing_stop_follows_and_triggers():
    e = make()
    start(e, 5000)
    e.place("A", "MES", "buy", 1)
    bar(e, 0, 5000, 5000.5, 4999.5, 5000)
    tr = e.place("A", "MES", "sell", 1, "trailing_stop", trail_ticks=8, reduce_only=True)
    bar(e, 1, 5001, 5006, 5001, 5005)   # anchor 5006 -> stop 5004
    assert tr.stop == 5004
    bar(e, 2, 5005, 5005, 5003, 5003.5)
    assert tr.status == Status.FILLED and tr.avg_fill == 5003.75


def test_stop_on_wrong_side_triggers_at_next_price():
    e = make()
    start(e, 5000)
    o = e.place("A", "MES", "sell", 1, "stop", stop=5002)  # above market
    bar(e, 0, 5000, 5001, 4999, 5000)
    assert o.avg_fill == 4999.75


# ------------------------------------------------------------------ brackets / OCO

def test_bracket_legs_priced_from_fill():
    e = make()
    start(e, 5000)
    entry = e.place("A", "MES", "buy", 2, tp_ticks=6, sl_ticks=8)
    tp, sl = (e.orders[i] for i in entry.child_ids)
    assert tp.status == Status.PENDING and sl.status == Status.PENDING
    bar(e, 0, 5001, 5001.5, 5000.5, 5001)
    assert entry.avg_fill == 5001.25
    assert tp.status == Status.WORKING and tp.price == 5002.75 and tp.qty == 2
    assert sl.status == Status.WORKING and sl.stop == 4999.25


def test_bracket_take_profit_cancels_stop():
    e = make()
    start(e, 5000)
    entry = e.place("A", "MES", "buy", 1, tp_ticks=6, sl_ticks=8)
    tp, sl = (e.orders[i] for i in entry.child_ids)
    bar(e, 0, 5000, 5000.5, 4999.75, 5000.25)    # fill 5000.25 -> tp 5001.75, sl 4998.25
    bar(e, 1, 5000.5, 5002.25, 5000, 5002)
    assert tp.status == Status.FILLED and tp.avg_fill == 5001.75
    assert sl.status == Status.CANCELED and sl.reason == "OCO"
    t = e.accounts["A"].trades[0]
    assert t.exit_reason == "tp" and t.pnl == pytest.approx(6 * 1.25)


def test_bracket_stop_cancels_target():
    e = make()
    start(e, 5000)
    entry = e.place("A", "MES", "sell", 1, tp_ticks=6, sl_ticks=8)
    tp, sl = (e.orders[i] for i in entry.child_ids)
    bar(e, 0, 5000, 5000.25, 4999.5, 5000)       # fill 4999.75 -> tp 4998.25, sl 5001.75
    bar(e, 1, 5000, 5003, 4999.5, 5002)
    assert sl.status == Status.FILLED and sl.avg_fill == 5002.0
    assert tp.status == Status.CANCELED


def test_both_legs_in_one_bar_tv_heuristic_and_worst_case():
    for mode, expected in (("tv", "tp"), ("worst", "sl")):
        e = make(intrabar=mode)
        start(e, 5000)
        e.place("A", "MES", "buy", 1, tp_ticks=6, sl_ticks=8)
        bar(e, 0, 5000, 5000.5, 4999.75, 5000.25)  # fill 5000.25
        # high (+3) is closer to the open than the low (-4): TV assumes O->H->L->C
        bar(e, 1, 5000, 5003, 4996, 4998)
        assert e.accounts["A"].trades[0].exit_reason == expected, mode


def test_entry_and_target_in_same_bar():
    e = make()
    start(e, 5000)
    entry = e.place("A", "MES", "buy", 1, "stop", stop=5001, tp_ticks=4, sl_ticks=8)
    tp = e.orders[entry.child_ids[0]]
    bar(e, 0, 5000, 5004, 4999.5, 5003)   # O->L->H->C: stop at 5001 (+slip 5001.25), tp 5002.25
    assert entry.status == Status.FILLED and tp.status == Status.FILLED
    assert tp.avg_fill == 5002.25


def test_cancel_parent_cancels_pending_children():
    e = make()
    start(e, 5000)
    entry = e.place("A", "MES", "buy", 1, "limit", price=4990, tp_ticks=4, sl_ticks=4)
    e.cancel(entry.id)
    assert all(e.orders[c].status == Status.CANCELED for c in entry.child_ids)
    assert e.open_orders("A") == []


def test_moved_pending_leg_keeps_absolute_price():
    e = make()
    start(e, 5000)
    entry = e.place("A", "MES", "buy", 1, "limit", price=4995, tp_ticks=8, sl_ticks=8)
    tp, sl = (e.orders[i] for i in entry.child_ids)
    assert tp.price == 4997 and sl.stop == 4993
    e.modify(tp.id, price=5010)
    bar(e, 0, 5000, 5000, 4994, 4996)  # entry fills at 4995
    assert tp.price == 5010 and sl.stop == 4993


def test_flatten_cancels_legs_and_closes_at_next_price():
    e = make()
    start(e, 5000)
    entry = e.place("A", "MES", "buy", 1, tp_ticks=40, sl_ticks=40)
    bar(e, 0, 5000, 5001, 4999, 5000)
    e.flatten("A")
    assert all(e.orders[c].status == Status.CANCELED for c in entry.child_ids)
    bar(e, 1, 5002, 5003, 5001, 5002)
    assert pos(e).qty == 0 and e.accounts["A"].trades[0].exit_reason == "flatten"


def test_manual_close_cancels_working_reduce_only_legs():
    e = make()
    start(e, 5000)
    entry = e.place("A", "MES", "buy", 1, tp_ticks=40, sl_ticks=40)
    bar(e, 0, 5000, 5001, 4999, 5000)
    e.place("A", "MES", "sell", 1)
    bar(e, 1, 5002, 5003, 5001, 5002)
    assert all(e.orders[c].status == Status.CANCELED for c in entry.child_ids)
    assert e.open_orders("A") == []


def test_reduce_only_without_position_is_canceled():
    e = make()
    start(e, 5000)
    o = e.place("A", "MES", "sell", 1, reduce_only=True)
    bar(e, 0, 5000, 5001, 4999, 5000)
    assert o.status == Status.CANCELED and pos(e).qty == 0


def test_reduce_only_is_trimmed_to_position():
    e = make(slippage_ticks=0)
    start(e, 5000)
    e.place("A", "MES", "buy", 1)
    bar(e, 0, 5000, 5001, 4999, 5000)
    o = e.place("A", "MES", "sell", 3, reduce_only=True)
    bar(e, 1, 5001, 5002, 5000, 5001)
    assert o.filled_qty == 1 and pos(e).qty == 0


def test_oco_group_without_bracket():
    e = make()
    start(e, 5000)
    a = e.place("A", "MES", "buy", 1, "stop", stop=5005, oco="breakout")
    b = e.place("A", "MES", "sell", 1, "stop", stop=4995, oco="breakout")
    bar(e, 0, 5000, 5006, 4999, 5005)
    assert a.status == Status.FILLED and b.status == Status.CANCELED


# ------------------------------------------------------------------ lifecycle / validation

def test_orders_only_see_prices_after_submission():
    e = make()
    start(e, 5000)
    bar(e, 0, 5000, 5001, 4990, 4999)  # traded down to 4990 before the order existed
    o = e.place("A", "MES", "buy", 1, "limit", price=4995)
    bar(e, 1, 4999, 5000, 4998, 4999)
    assert o.status == Status.WORKING


def test_modify_to_marketable_fills_at_next_price():
    e = make()
    start(e, 5000)
    o = e.place("A", "MES", "buy", 1, "limit", price=4990)
    bar(e, 0, 5000, 5001, 4999, 5000)
    e.modify(o.id, price=5010)
    bar(e, 1, 5001, 5002, 5000, 5001)
    assert o.avg_fill == 5001


def test_ioc_unfilled_is_canceled():
    e = make()
    start(e, 5000)
    o = e.place("A", "MES", "buy", 1, "limit", price=4990, tif="ioc")
    bar(e, 0, 5000, 5001, 4980, 4990)
    assert o.status == Status.CANCELED and o.reason == "IOC not filled"


def test_day_orders_expire_at_session_end_gtc_survive():
    e = make()
    t = ct(2026, 10, 14, 15, 58)
    e.on_price("MES", t, 5000, False)
    day = e.place("A", "MES", "buy", 1, "limit", price=4900, tif="day")
    gtc = e.place("A", "MES", "buy", 1, "limit", price=4900, tif="gtc")
    e.on_bar("MES", ct(2026, 10, 14, 15, 59), 5000, 5001, 4999, 5000)
    e.on_bar("MES", ct(2026, 10, 14, 17, 0), 5000, 5001, 4999, 5000)  # next session
    assert day.status == Status.CANCELED and day.reason == "DAY order expired"
    assert gtc.status == Status.WORKING


def test_rejections():
    e = make(risk=None)
    start(e, 5000)
    assert e.place("A", "MES", "buy", 0).status == Status.REJECTED
    assert e.place("A", "MES", "buy", 1, "limit").reason == "limit price required"
    assert e.place("A", "MES", "buy", 1, "stop").reason == "stop price required"
    assert e.place("A", "MES", "buy", 1, "trailing_stop").reason == "trail_ticks required"


def test_max_position():
    e = make(risk=RiskConfig(max_position=3))
    start(e, 5000)
    assert e.place("A", "MES", "buy", 4).reason.startswith("max position")
    assert e.place("A", "MES", "buy", 3).status == Status.WORKING


def test_insufficient_margin_rejected():
    e = make(balance=1_000)
    e.on_price("MES", ct(2026, 10, 14, 20, 0), 5000, False)  # evening: still day margin ($50)
    assert e.place("A", "MES", "buy", 20).status == Status.WORKING
    assert "insufficient margin" in e.place("A", "MES", "buy", 21).reason


# ------------------------------------------------------------------ margin & risk

def test_overnight_margin_liquidation_at_1545():
    e = make(balance=2_000, slippage_ticks=0)  # MES overnight margin 2,400 > 2,000
    e.on_price("MES", ct(2026, 10, 14, 15, 30), 5000, False)
    e.place("A", "MES", "buy", 1)
    e.on_bar("MES", ct(2026, 10, 14, 15, 31), 5000, 5001, 4999, 5000)
    assert pos(e).qty == 1
    e.on_bar("MES", ct(2026, 10, 14, 15, 45), 5000, 5001, 4999, 5000)
    assert pos(e).qty == 0
    assert e.accounts["A"].trades[0].exit_reason == "liquidation"


def test_overnight_position_kept_with_enough_margin():
    e = make(balance=10_000)
    e.on_price("MES", ct(2026, 10, 14, 15, 30), 5000, False)
    e.place("A", "MES", "buy", 1)
    e.on_bar("MES", ct(2026, 10, 14, 15, 31), 5000, 5001, 4999, 5000)
    e.on_bar("MES", ct(2026, 10, 14, 15, 46), 5000, 5001, 4999, 5000)
    assert pos(e).qty == 1


def test_maintenance_margin_call():
    e = make(balance=1_100, slippage_ticks=0)
    start(e, 5000)
    e.place("A", "MES", "buy", 20)        # day margin 20 * 50 = 1,000
    bar(e, 0, 5000, 5000, 5000, 5000)
    bar(e, 1, 5000, 5000, 4998, 4998)     # -2 pts * 5 * 20 = -200 -> equity 900 < 1,000
    assert pos(e).qty == 0 and e.accounts["A"].trades[0].exit_reason == "liquidation"


def test_daily_loss_limit_locks_until_next_session():
    e = make(risk=RiskConfig(daily_loss_limit=100), slippage_ticks=0)
    e.on_price("MES", ct(2026, 10, 14, 9, 0), 5000, False)
    e.on_bar("MES", ct(2026, 10, 14, 9, 1), 5000, 5000, 5000, 5000)
    e.place("A", "MES", "buy", 2)
    e.on_bar("MES", ct(2026, 10, 14, 9, 2), 5000, 5000, 4985, 4985)  # -15 pts * 10 = -150
    a = e.accounts["A"]
    assert pos(e).qty == 0 and a.locked == "daily loss limit"
    assert e.place("A", "MES", "buy", 1).status == Status.REJECTED
    e.on_bar("MES", ct(2026, 10, 14, 17, 0), 4985, 4986, 4984, 4985)
    assert a.locked == "" and e.place("A", "MES", "buy", 1).status == Status.WORKING


# ------------------------------------------------------------------ rolls, gaps, excursions

def test_roll_keeps_unrealized_and_shifts_orders():
    e = make(slippage_ticks=0)
    start(e, 5000)
    e.place("A", "MES", "buy", 2)
    bar(e, 0, 5000, 5001, 4999, 5000)
    sl = e.place("A", "MES", "sell", 2, "stop", stop=4990, reduce_only=True)
    before = e.unrealized("A")
    bal = e.accounts["A"].balance
    e.on_roll("MES", T0 + 120, "MESZ6", "MESH7", 40.0)
    assert pos(e).avg_price == 5040 and sl.stop == 5030
    assert e.unrealized("A") == pytest.approx(before)
    assert e.accounts["A"].balance == pytest.approx(bal - 4 * FEE)


def test_weekend_gap_through_stop():
    e = make()
    e.on_price("MES", ct(2026, 10, 16, 15, 58), 5000, False)
    e.place("A", "MES", "buy", 1)
    e.on_bar("MES", ct(2026, 10, 16, 15, 59), 5000, 5000, 5000, 5000)
    sl = e.place("A", "MES", "sell", 1, "stop", stop=4990, reduce_only=True)
    e.on_bar("MES", ct(2026, 10, 18, 17, 0), 4970, 4975, 4965, 4972)  # Sunday open gap
    assert sl.avg_fill == 4969.75


def test_bar_path_heuristic():
    e = make()
    assert [p for _, p, _ in e.bar_path("MES", 0, 100, 101, 96, 99)] == [100, 101, 96, 99]
    assert [p for _, p, _ in e.bar_path("MES", 0, 100, 104, 99, 103)] == [100, 99, 104, 103]


def test_mae_mfe():
    e = make(slippage_ticks=0)
    start(e, 5000)
    e.place("A", "MES", "buy", 1)
    bar(e, 0, 5000, 5000, 5000, 5000)
    bar(e, 1, 5000, 5003, 4998, 5001)
    e.place("A", "MES", "sell", 1)
    bar(e, 2, 5001, 5001, 5001, 5001)
    t = e.accounts["A"].trades[0]
    assert t.mfe_ticks == 12 and t.mae_ticks == -8


# ------------------------------------------------------------------ accounts, persistence, crypto

def test_account_summary_and_day_records():
    e = make(slippage_ticks=0)
    e.on_price("MES", ct(2026, 10, 14, 9, 0), 5000, False)
    e.on_bar("MES", ct(2026, 10, 14, 9, 1), 5000, 5000, 5000, 5000)
    e.place("A", "MES", "buy", 1)
    e.on_bar("MES", ct(2026, 10, 14, 9, 2), 5000, 5004, 5000, 5004)
    s = e.account_summary("A")
    assert s["unrealized"] == pytest.approx(20) and s["margin_used"] == 50
    assert s["equity"] == pytest.approx(50_000 + 20 - FEE)
    assert s["overnight_margin"] == 2400
    e.on_bar("MES", ct(2026, 10, 14, 17, 0), 5004, 5005, 5003, 5004)
    d = e.accounts["A"].days[-1]
    assert d["date"] == "2026-10-14" and d["pnl"] == pytest.approx(20 - FEE)


def test_multiple_accounts_are_independent():
    e = make()
    e.add_account("B", 10_000)
    start(e, 5000)
    e.place("A", "MES", "buy", 1)
    e.place("B", "MES", "sell", 2)
    bar(e, 0, 5000, 5001, 4999, 5000)
    assert pos(e, acct="A").qty == 1 and pos(e, acct="B").qty == -2


def test_reverse():
    e = make()
    start(e, 5000)
    e.place("A", "MES", "buy", 2)
    bar(e, 0, 5000, 5001, 4999, 5000)
    e.reverse("A", "MES")
    bar(e, 1, 5000, 5001, 4999, 5000)
    assert pos(e).qty == -2


def test_snapshot_restore_round_trip():
    e = make()
    start(e, 5000)
    entry = e.place("A", "MES", "buy", 1, tp_ticks=8, sl_ticks=8)
    bar(e, 0, 5000, 5000.5, 4999.75, 5000)
    state = e.snapshot()
    e2 = Engine()
    e2.restore(state)
    assert e2.accounts["A"].position("MES").qty == 1
    tp_id = entry.child_ids[0]
    assert e2.orders[tp_id].status == Status.WORKING
    e2.on_bar("MES", T0 + 120, 5000, 5003, 5000, 5002)
    assert e2.orders[tp_id].status == Status.FILLED
    assert e2.accounts["A"].trades[0].exit_reason == "tp"
    new = e2.place("A", "MES", "buy", 1)
    assert new.id > max(e.orders)


def test_crypto_perp_rate_fees_and_margin():
    e = make(balance=10_000, slippage_ticks=0)
    t = ct(2026, 10, 17, 3, 0)  # Saturday: crypto trades 24/7
    e.on_price("BTCUSDT", t, 60_000.0, False)
    e.place("A", "BTCUSDT", "buy", 10)           # 0.1 BTC, notional 6,000
    e.on_bar("BTCUSDT", t + 60, 60_000, 60_100, 59_900, 60_050)
    a = e.accounts["A"]
    assert a.total_fees == pytest.approx(6_000 * 0.0004)
    assert e.margin("A") == pytest.approx(60_050 * 0.01 * 10 * 0.05)
    assert e.unrealized("A") == pytest.approx(50 * 0.01 * 10)


def test_scripted_sequence_matches_hand_calculation():
    """Three trades with brackets, a manual exit and a flip, checked against a hand calculation."""
    e = make(slippage_ticks=1, limit_through_ticks=1)
    start(e, 5000)
    # 1) long 2 @ market with +8/-8 bracket. fill 5000.25; TP 5002.25 hits -> +8 ticks * 2 = +$20
    e.place("A", "MES", "buy", 2, tp_ticks=8, sl_ticks=8)
    bar(e, 0, 5000, 5000.5, 4999.75, 5000.25)
    bar(e, 1, 5000.5, 5002.5, 5000.25, 5002)
    # 2) short 1 @ limit 5003 (needs 5003.25), stop hits at 5005 -> fill 5005.25: -9 ticks = -$11.25
    e.place("A", "MES", "sell", 1, "limit", price=5003, sl=5005)
    bar(e, 2, 5002, 5003.5, 5001.75, 5003)
    bar(e, 3, 5003, 5006, 5002.75, 5005.5)
    # 3) long 1 @ market (fill 5005.5 + 0.25 = 5005.75), flatten at 5007 - 0.25 = 5006.75: +4 ticks = +$5
    e.place("A", "MES", "buy", 1)
    bar(e, 4, 5005.5, 5006, 5005, 5006)
    e.flatten("A")
    bar(e, 5, 5007, 5008, 5006.5, 5007.5)
    a = e.accounts["A"]
    gross = [round(t.pnl, 2) for t in a.trades]
    assert gross == [20.0, -11.25, 5.0]
    fees = (2 + 2 + 1 + 1 + 1 + 1) * FEE
    assert a.balance == pytest.approx(50_000 + 20 - 11.25 + 5.0 - fees)
    assert a.total_fees == pytest.approx(fees)
    assert pos(e).qty == 0 and e.open_orders("A") == []


def test_events_are_emitted():
    e = make()
    seen = []
    e.subscribe(lambda kind, payload: seen.append(kind))
    start(e, 5000)
    e.place("A", "MES", "buy", 1, tp_ticks=4, sl_ticks=4)
    bar(e, 0, 5000, 5002, 4999.5, 5001)
    for kind in ("order", "fill", "position", "account", "trade"):
        assert kind in seen


# ------------------------------------------------------------------ execution model v2

def test_spread_model_by_time_of_day():
    # MES: spread 1 tick in RTH/ETH, 2 ticks in the opening minute -> half spread rounds up to 1 tick
    # MNQ: 1 tick RTH, 2 overnight, 3 at the open -> 1, 1 and 2 ticks
    e = Engine(FillConfig(slippage_ticks=0, spread_model=True))
    e.add_account("A", 100_000)
    cases = [(ct(2026, 10, 14, 8, 30), 0.5), (ct(2026, 10, 14, 10, 0), 0.25), (ct(2026, 10, 14, 20, 0), 0.25)]
    for t, cost in cases:
        e.on_price("MNQ", t - 60, 20000, False)
        o = e.place("A", "MNQ", "buy", 1)
        e.on_bar("MNQ", t, 20000, 20001, 19999, 20000)
        assert o.avg_fill == 20000 + cost, (t, o.avg_fill)
        e.flatten("A", immediate=True)


def test_live_quotes_drive_market_fills():
    e = Engine(FillConfig(slippage_ticks=0))
    e.add_account("A")
    t = ct(2026, 10, 14, 10, 0)
    e.on_price("MES", t, 5000, False)
    e.on_quote("MES", t, 4999.75, 5000.5)   # wide quote
    o = e.place("A", "MES", "buy", 1)
    e.on_price("MES", t + 1, 5000.25, False)
    assert o.avg_fill == 5000.5             # paid the ask, not last + half spread
    e.on_quote("MES", t + 2, 5000.0, 5000.25)
    s = e.place("A", "MES", "sell", 1)
    e.on_price("MES", t + 3, 5000.25, False)
    assert s.avg_fill == 5000.0             # sold at the bid
    # stale quotes (older than 5 s) fall back to the spread model
    o2 = e.place("A", "MES", "buy", 1)
    e.on_price("MES", t + 30, 5001, False)
    assert o2.avg_fill == 5001.25


def test_stop_fills_at_ask_but_never_better_than_stop():
    e = Engine(FillConfig(slippage_ticks=0))
    e.add_account("A")
    t = ct(2026, 10, 14, 10, 0)
    e.on_price("MES", t, 5000, False)
    st = e.place("A", "MES", "buy", 1, "stop", stop=5002)
    e.on_quote("MES", t + 1, 5001.5, 5001.75)
    e.on_price("MES", t + 1, 5001.5, False)
    e.on_price("MES", t + 2, 5002, True)
    assert st.avg_fill == 5002              # quote ask below the stop -> filled at the stop level


def test_partial_fills_capped_by_volume_and_legs_grow():
    e = Engine(FillConfig(slippage_ticks=0, spread_model=False, max_volume_pct=10))
    e.add_account("A", 1_000_000)
    e.on_price("MES", T0 - 60, 5000, False)
    entry = e.place("A", "MES", "buy", 25, tp_ticks=40, sl_ticks=40)
    tp = e.orders[entry.child_ids[0]]
    e.on_bar("MES", T0, 5000, 5001, 4999, 5000, v=100)     # 10% of 100 = 10 contracts
    assert entry.filled_qty == 10 and entry.status == Status.WORKING and tp.qty == 10
    assert e.accounts["A"].position("MES").qty == 10
    e.on_bar("MES", T0 + 60, 5000, 5001, 4999, 5000, v=100)
    e.on_bar("MES", T0 + 120, 5000, 5001, 4999, 5000, v=100)
    assert entry.status == Status.FILLED and entry.filled_qty == 25 and tp.qty == 25
    assert len([f for f in e.accounts["A"].fills if f.order_id == entry.id]) == 3


def test_latency_delays_orders():
    e = Engine(FillConfig(slippage_ticks=0, spread_model=False, latency_ms=20_000))
    e.add_account("A")
    e.on_price("MES", T0 - 60, 5000, False)
    o = e.place("A", "MES", "buy", 1, ts=T0)
    e.on_price("MES", T0 + 10, 5001, False)
    assert o.status == Status.WORKING      # still in flight
    e.on_price("MES", T0 + 25, 5003, False)
    assert o.avg_fill == 5003
