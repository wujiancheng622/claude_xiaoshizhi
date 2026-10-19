"""分钟窗口确定撮合（D17.3、D09.5、H08.1）。

Bar 为 [start, start+1min)，仅在 bar_end 后可读。10:00 用刚完成的上一 Bar 决策并提交，首次可成交区间
[10:00,10:01)；成交结果到 10:01 才可见，回款到 10:01 才可用于下一买单。同一 10:01 先处理上一 Bar
内已有委托的成交，再处理 60 秒到期撤单。每证券每方向每窗口最多 3 张实际新委托；锚与保护边界不扩大。
无盘口队列证据：这是显式分钟报价代理，不是真实排队保证。
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from fractions import Fraction

from .contracts import (CENTER, MAX_ORDERS_PER_SIDE_WINDOW, ORDER_WAIT_SECONDS, RETRY_MIN_SECONDS, SLOTS, dec_str,
                        sh_dt, ts_str, unwire_ratio)
from .execution import BookView, check_sell_funding, ordinary_sell_qty, protection_bounds, size_buy
from .portfolio import EXIT, HOLD_NO_ADD, TARGET_AMOUNT
from .rules import ceil_tick, floor_tick
from .risk import (SellLeg, SellPlanner, active, blocked_exit_value, open_order_cancellations, risk_decision,
                   satisfied_now)


def _bars(runner, sid: str, d: date) -> dict:
    key = ("min", sid, d)
    if key not in runner._qc:
        bars = {}
        for b in runner.m.minute_bars(sid, d):
            start = sh_dt(d, b["start"].time())
            bars[start] = b
        runner._qc[key] = bars
    return runner._qc[key]


def run_window(runner, d: date) -> None:
    L = runner.ledger
    plan = runner.ordinary_plan_for(d)
    st = {"attempts": {}, "last_submit": {}, "anchor": {}, "sell_done": False, "n": 0}
    t = sh_dt(d, time(10, 0))
    end = sh_dt(d, time(10, 30))
    while t <= end:
        _settle_bar(runner, d, t, st)
        _timeouts(runner, d, t, st, final=(t == end))
        if t < end and L.run_state not in ("WAITING_START", "RETIRED"):
            _decide(runner, d, t, plan, st)
        t += timedelta(minutes=1)


def _quote_at(runner, sid: str, d: date, t: datetime) -> dict | None:
    """t 时刻可见的上一完成 Bar（[t−1min, t)）作为分钟报价代理。"""
    prev = _bars(runner, sid, d).get(t - timedelta(minutes=1))
    q = runner.quote(sid, d)
    if prev is None or q is None or not q["tradable"]:
        return None
    return {"ref": prev["close"], "rule": q["rule"], "lower": q["lower"], "upper": q["upper"]}


def _settle_bar(runner, d: date, t: datetime, st: dict) -> None:
    """处理刚结束 Bar [t−1, t) 内已有委托的模拟成交；结果在 t 可见。"""
    L = runner.ledger
    bar_start = t - timedelta(minutes=1)
    for o in sorted(L.open_orders(), key=lambda x: x.client_order_id):
        if o.trade_date != d.isoformat() or o.submitted_at is None or not o.submitted:
            continue
        if datetime.fromisoformat(o.submitted_at) > bar_start:
            continue
        bar = _bars(runner, o.security_id, d).get(bar_start)
        if bar is None:
            continue                                   # 关键 Bar 缺失：不成交，不转日线有利价
        q = runner.quote(o.security_id, d)
        tick = q["rule"].tick
        if o.side == "BUY":
            px = ceil_tick(bar["open"] * (1 + runner.slip), tick)
            ok = px <= o.limit and bar["low"] <= px <= bar["high"] and (q["upper"] is None or px <= q["upper"])
        else:
            px = floor_tick(bar["open"] * (1 - runner.slip), tick)
            ok = px >= o.limit and bar["low"] <= px <= bar["high"] and (q["lower"] is None or px >= q["lower"])
        if not ok:
            continue
        rem = o.remaining_possible
        cap = runner.cap(o.security_id, d)
        if cap is None:
            continue
        used = L.capacity_used_on(o.security_id, d.isoformat())
        others = L.capacity_pending(o.security_id, d.isoformat()) - (
            rem * o.limit if o.side == "BUY" else rem * Decimal(o.upper_price_bound))
        cap_qty = int(max(cap - used - others, Decimal(0)) / px)
        qty = min(rem, bar["volume"], cap_qty)
        if qty <= 0:
            continue
        st["n"] += 1
        sd = runner.rb.settlement(q["rule"]).dates(runner.cal, d)
        runner.emit("BROKER_FILL", {"client_order_id": o.client_order_id, "trade_key": f"SIM/{o.client_order_id}/{st['n']}",
                                    "qty": qty, "price": dec_str(px), "amount": None,
                                    "occurred_at": ts_str(bar_start + timedelta(seconds=30)), "received_at": ts_str(t),
                                    "trade_date": d.isoformat(), "sellable_from": sd["sellable_from"],
                                    "tax_acquisition_date": sd["tax_acquisition_date"],
                                    "tax_delivery_date": sd["tax_delivery_date"],
                                    "proceeds_tradeable_from": sd["proceeds_tradeable_from"],
                                    "fee_settlement": sd["fee_settlement"]}, t)
        o2 = L.orders[o.client_order_id]
        if o2.filled_qty == o2.qty:
            runner.emit("BROKER_TERMINAL", {"client_order_id": o.client_order_id, "status": "FILLED",
                                            "cum_qty": o2.filled_qty, "cum_value": dec_str(o2.filled_value),
                                            "source_revision": st["n"]}, t)


def _timeouts(runner, d: date, t: datetime, st: dict, final: bool) -> None:
    """60 秒未完成请求撤销；模拟券商在同一时刻确认撤单终态（累计与已记逐笔一致）。窗口结束撤销全部未结普通单。"""
    L = runner.ledger
    for o in sorted(L.open_orders(), key=lambda x: x.client_order_id):
        if o.trade_date != d.isoformat() or not o.submitted:
            continue
        age = (t - datetime.fromisoformat(o.submitted_at)).total_seconds()
        if age >= ORDER_WAIT_SECONDS or final or o.cancel_required:
            st["n"] += 1
            if not o.cancel_pending:
                runner.emit("CANCEL_REQUEST", {"client_order_id": o.client_order_id, "request_id": f"cx{st['n']}",
                                               "reason": "WINDOW_END" if final else (o.cancel_reason or "TIMEOUT")}, t)
            runner.emit("BROKER_TERMINAL", {"client_order_id": o.client_order_id, "status": "CANCELED",
                                            "cum_qty": o.filled_qty, "cum_value": dec_str(o.filled_value),
                                            "source_revision": 10 ** 6 + st["n"]}, t)


def _can_attempt(st: dict, key: tuple, t: datetime) -> bool:
    if st["attempts"].get(key, 0) >= MAX_ORDERS_PER_SIDE_WINDOW:
        return False
    last = st["last_submit"].get(key)
    return last is None or (t - last).total_seconds() >= RETRY_MIN_SECONDS


def _submit(runner, d: date, t: datetime, st: dict, sid: str, side: str, qty: int, limit: Decimal, kind: str,
            plan: dict | None, upper: Decimal, industry: str | None = None) -> None:
    L = runner.ledger
    key = (sid, side)
    n = st["attempts"].get(key, 0) + 1
    mm = runner.m.master[sid]
    coid = f"{runner.phase_key}/{sid}/{side}/{n}"
    order = {"client_order_id": coid, "batch_id": f"{d.isoformat()}/{kind}", "plan_id": (plan or {}).get("plan_id", ""),
             "security_id": sid, "market": mm["exchange"], "board": mm["board"], "side": side, "qty": qty,
             "limit": dec_str(limit), "kind": kind, "trade_date": d.isoformat(), "capital_revision": L.capital_revision,
             "window_end": ts_str(sh_dt(d, time(10, 30))), "attempt": n, "upper_price_bound": dec_str(upper),
             "industry": industry}
    runner.emit("ORDER_CREATE", {"order": order}, t)
    runner.emit("ORDER_SUBMIT", {"client_order_id": coid, "at": t.isoformat()}, t)
    runner.emit("BROKER_ACK", {"client_order_id": coid, "broker_order_id": f"B/{coid}"}, t)
    st["attempts"][key] = n
    st["last_submit"][key] = t


def _decide(runner, d: date, t: datetime, plan: dict | None, st: dict) -> None:
    L = runner.ledger
    ds = d.isoformat()
    # 窗口内才确认的风险事实：只影响确认后的行动（下一整分钟起）
    runner._security_exit_facts(d, t, t)
    quotes = {sid: _quote_at(runner, sid, d, t) for sid, p in L.positions.items() if p.quantity > 0}
    prices = {sid: q["ref"] for sid, q in quotes.items() if q}
    view = runner.risk_view(prices)
    for ep in active(L.episodes):
        if ep["type"] in ("SINGLE_REDUCE", "INDUSTRY_REDUCE", "TOTAL_REDUCE") and satisfied_now(ep, view):
            runner.emit("RISK_EPISODE_END", {"episode_id": ep["episode_id"], "reason": "TARGET_SATISFIED",
                                             "at": ts_str(t)}, t)
    eligible = [ep for ep in active(L.episodes) if runner._effective_first(ep) <= d]
    emergency = any(ep["type"] == "EMERGENCY_EXIT" for ep in eligible)
    exits = sorted(set(L.positions) if emergency else {ep["key"] for ep in eligible if ep["type"] == "SECURITY_EXIT"})
    singles = sorted(ep["key"] for ep in eligible if ep["type"] == "SINGLE_REDUCE")
    inds = sorted(ep["key"] for ep in eligible if ep["type"] == "INDUSTRY_REDUCE")
    total = any(ep["type"] == "TOTAL_REDUCE" for ep in eligible)
    legs, lim = {}, {}
    for sid, p in sorted(L.positions.items()):
        if p.quantity == 0:
            continue
        q = quotes.get(sid)
        m = prices.get(sid, p.mark)
        if m is None:
            continue
        fs, capq, px, upper = 0, 0, None, None
        if q and p.lifecycle == "NORMAL" and not runner.stress_blocked(sid, d):
            anchor = st["anchor"].setdefault((sid, "SELL"), q["ref"])
            _, sell_lower = protection_bounds(anchor, q["rule"].tick, q["lower"], q["upper"])
            px = sell_lower
            upper = q["upper"]
            cap = runner.cap(sid, d)
            if upper is None:
                px = None                     # CAPACITY_PRICE_BOUND_UNKNOWN：不报卖单
            if cap is not None and upper is not None:
                left = cap - L.capacity_used_on(sid, ds) - L.capacity_pending(sid, ds)
                capq = max(int(left / upper), 0) if left > 0 else 0
                # 以当前实际股数计算合并所需卖量：把本证券在途卖单剩余视作尚未卖出
                own_open = sum(o.remaining_possible for o in L.open_orders(sid, "SELL"))
                fs = L.free_sell(sid, ds) + own_open
                capq += own_open
        lim[sid] = (px, upper)
        grid = runner.rule(sid, d).grid
        legs[sid] = SellLeg(sid, p.quantity, Fraction(m), px if px is not None else Decimal(m), p.industry, fs, grid,
                            capq, (p.quantity + p.share_receivable) * Fraction(m))
    ordinary = {}
    if plan is not None and not emergency:
        for sid, mode in plan["modes"].items():
            if sid not in legs or legs[sid].free_sell == 0:
                continue
            if mode["mode"] == EXIT:
                ordinary[sid] = legs[sid].grid.max_sell(min(legs[sid].free_sell, legs[sid].cap_qty), legs[sid].free_sell,
                                                        legs[sid].q)
            elif mode["mode"] == TARGET_AMOUNT and mode.get("target_amount") is not None:
                x = ordinary_sell_qty(legs[sid].grid, legs[sid].q, prices.get(sid, L.positions[sid].mark),
                                      unwire_ratio(mode["target_amount"]), min(legs[sid].free_sell, legs[sid].cap_qty))
                if x:
                    ordinary[sid] = x

    def fee_fn(sid, qty, p):
        return runner.fees.preview_new_order_fees(runner.m.master[sid]["exchange"], "SELL", d, qty * p, qty)

    res = SellPlanner(view.equity, legs, fee_fn, runner.sale_tax_fn(d)).run(
        exits=exits, singles=singles, industries=inds, total=total, ordinary=ordinary)
    # D09.4：以当前实际股数比较，不再需要或需要量小于旧单未成量的在途卖单请求取消
    for coid, why in open_order_cancellations(L, res["sell"], buy_block_reason=None):
        st["n"] += 1
        runner.emit("CANCEL_REQUEST", {"client_order_id": coid, "request_id": f"d94/{st['n']}", "reason": why}, t)
    created = False
    risk_sids = set(exits) | set(singles) | {s for s, lg in legs.items() if lg.industry in inds} | (set(legs) if total else set())
    for sid, x in sorted(res["sell"].items()):
        px, upper = lim.get(sid, (None, None))
        if px is None or x <= 0 or L.open_orders(sid, "SELL") or not _can_attempt(st, (sid, "SELL"), t) \
                or x > L.free_sell(sid, ds):
            continue
        is_risk = sid in risk_sids and x > ordinary.get(sid, 0)
        if not is_risk and st["sell_done"]:
            continue                    # 普通卖出只在本批卖出阶段；风险卖出全窗口可行
        if L.open_orders(sid, "BUY"):
            continue                    # 先取消买单并核对最终成交，同证券买卖不交叉未结
        tax = runner.sale_tax_fn(d)(sid, x)
        if check_sell_funding(x * px, fee_fn(sid, x, px) + (tax or Decimal(0)), L.own_free_cash(ds)):
            continue                    # COST_UNFUNDABLE：不虚构融资
        _submit(runner, d, t, st, sid, "SELL", x, px, "RISK" if is_risk else "ORDINARY", plan, upper)
        created = True
    if created or L.open_orders(side="SELL"):
        # 卖出阶段：所有计划卖单终态或明确受阻后才进入买入；期间仍执行禁买取消
        if plan is not None:
            _cancel_conflicting_buys(runner, L, t, st, plan, res, prices)
        return
    st["sell_done"] = True
    if plan is None:
        return
    dec = _buy_decision(runner, L, plan, res, prices, ds)
    for coid, why in open_order_cancellations(L, None, decision=dec, plan_capital_revision=plan["capital_revision"]):
        st["n"] += 1
        runner.emit("CANCEL_REQUEST", {"client_order_id": coid, "request_id": f"d94/{st['n']}", "reason": why}, t)
    if dec["global_buy_blocks"]:
        return
    _buy_phase(runner, L, d, t, st, plan, dec, prices, ds)


def _cancel_conflicting_buys(runner, L, t, st, plan, res, prices) -> None:
    dec = _buy_decision(runner, L, plan, res, prices, t.date().isoformat())
    for coid, why in open_order_cancellations(L, None, decision=dec, plan_capital_revision=plan["capital_revision"]):
        st["n"] += 1
        runner.emit("CANCEL_REQUEST", {"client_order_id": coid, "request_id": f"d94/{st['n']}", "reason": why}, t)


def _buy_decision(runner, L, plan, res, prices, ds) -> dict:
    """两级禁买裁决（D08.3）：全局集合 + 证券集合；执行器不另加“任一风险段即全局禁买”。"""
    batch_blocks = []
    if plan["capital_revision"] != L.capital_revision:
        batch_blocks.append("CAPITAL_CHANGED")
    if not plan["ordinary_buy_allowed"]:
        batch_blocks.append(plan["score_reason"] or "PLAN_NO_BUY")
    sold = {tr["sid"] for tr in L.trade_log if tr["date"] == ds and tr["side"] == "SELL"}
    hold_no_add = {s for s, md in plan["modes"].items() if md["mode"] == HOLD_NO_ADD or md.get("prohibit_buy")}
    e_now, vst = L.equity(prices)
    # 风险退出受阻：应退出而当前没有未结卖单覆盖的剩余股数（D08.3）
    d = date.fromisoformat(ds)
    eligible = [ep for ep in active(L.episodes) if runner._effective_first(ep) <= d]
    emergency = any(ep["type"] == "EMERGENCY_EXIT" for ep in eligible)
    req_sids = set(L.positions) if emergency else {ep["key"] for ep in eligible if ep["type"] == "SECURITY_EXIT"}
    required = {s: L.positions[s].quantity for s in req_sids
                if s in L.positions and L.positions[s].quantity > 0 and L.positions[s].lifecycle == "NORMAL"}
    executable = {s: sum(o.remaining_possible for o in L.open_orders(s, "SELL")) for s in required}
    blocked_val = blocked_exit_value(required, executable, {s: prices.get(s, L.positions[s].mark) for s in required})
    dec = risk_decision(account_revision=L.revision, episodes=L.episodes, run_state=L.run_state, equity=e_now,
                        valuation_status=vst, account_limitations=L.limitations,
                        unknown_industry_holdings=[s for s, p in L.positions.items() if p.quantity and p.industry is None],
                        order_state_unknown=any(o.known_unresolved for o in L.open_orders()),
                        batch_blocks=batch_blocks,
                        security_exits={ep["key"]: 1 for ep in active(L.episodes, "SECURITY_EXIT")},
                        no_buy_in_batch=sold, hold_no_add=hold_no_add, risk_sell_sids=set(res["sell"]),
                        blocked_exit_value=blocked_val)
    return dec


def _buy_phase(runner, L, d, t, st, plan, dec, prices, ds) -> None:
    target = CENTER * unwire_ratio(plan["E_decision"])
    tops = sorted((md.get("rank", 10 ** 9), sid) for sid, md in plan["modes"].items()
                  if md["mode"] == TARGET_AMOUNT and md["reason"] != "NEW_BUY" and not md.get("prohibit_buy")
                  and L.positions.get(sid) and L.positions[sid].quantity > 0)
    for sid in [s for _, s in tops] + list(plan["buy_candidates_ordered"]):
        if sid in dec["security_buy_blocks"] or L.open_orders(sid, "BUY") or not _can_attempt(st, (sid, "BUY"), t):
            continue
        p = L.positions.get(sid)
        held = p is not None and p.quantity + p.share_receivable > 0
        if not held and len(L.actual_slots()) >= SLOTS:
            continue
        q = _quote_at(runner, sid, d, t)
        if q is None:
            continue
        anchor = st["anchor"].setdefault((sid, "BUY"), q["ref"])
        buy_upper, _ = protection_bounds(anchor, q["rule"].tick, q["lower"], q["upper"])
        m = q["ref"]
        pr = dict(prices)
        pr[sid] = m
        v = runner.risk_view(pr)
        ind_vals = {}
        for s2, val in v.values.items():
            ind_vals[v.industry_of[s2]] = ind_vals.get(v.industry_of[s2], Fraction(0)) + val
        open_adj = Fraction(0)
        open_val = {}
        for o in L.open_orders(side="BUY"):
            mo = pr.get(o.security_id) or (L.positions[o.security_id].mark if o.security_id in L.positions else None)
            if mo is None:
                continue
            fee_res = L.buy_cash_reserved(o) - o.remaining_possible * o.limit
            open_adj += o.remaining_possible * (Fraction(mo) - Fraction(o.limit)) - Fraction(fee_res)
            open_val[o.security_id] = open_val.get(o.security_id, Fraction(0)) + o.remaining_possible * Fraction(mo)
        view_b = BookView(equity=v.equity, stock_value=sum(v.values.values(), Fraction(0)), holdings_value=dict(v.values),
                          industry_value=ind_vals, industry_of=dict(v.industry_of), open_buy_adj=open_adj,
                          open_buy_value=open_val, cash_free=L.own_free_cash(ds))
        cur = (p.quantity + p.share_receivable) * Fraction(m) if held else Fraction(0)
        cap = runner.cap(sid, d)
        capl = None if cap is None else cap - L.capacity_used_on(sid, ds) - L.capacity_pending(sid, ds)
        ind = (p.industry if held else plan.get("industries", {}).get(sid)) or runner.m.industry(sid, d)
        mm = runner.m.master[sid]
        b, why = size_buy(view_b, sid=sid, industry=ind, grid=q["rule"].grid, p=buy_upper, m=m,
                          gap_value=target - cur - open_val.get(sid, Fraction(0)),
                          fee_fn=lambda x: runner.fees.preview_new_order_fees(mm["exchange"], "BUY", d, x * buy_upper, x),
                          capacity_left_value=capl)
        if b > 0:
            _submit(runner, d, t, st, sid, "BUY", b, buy_upper, "ORDINARY", plan, q["upper"], ind)
