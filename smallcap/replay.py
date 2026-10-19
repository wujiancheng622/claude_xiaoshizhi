"""小型事件回放：一个矩阵单元（角色×金额×起点×情景）的完整账户路径（D16—D18、H08）。

确定性相位：INIT(起点20:00) → 每个市场日 PREOPEN(09:00) → EXEC(09:30代理 / 10:00—10:30分钟) → CLOSE(15:30)
→ CUTOFF(周度最后市场日20:00)。每相位产生的事件ID可重算，因此可在任意已提交事件后崩溃并恢复：
先重放到最后一个 CURSOR，再从该相位重算（已提交事件以同ID同内容校验，不重复入账）。
决策模块只看到 available_at<=engine_now 的事实；撮合可读结算区间内的真实行情，但不回传给决策。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from fractions import Fraction

import json

from .account import Ledger, canon
from .clock import TradingCalendar
from .contracts import (BASE_SLIPPAGE, CENTER, DETAIL_VERSION, HIGH_SLIPPAGE, PROXY_TIME, SLOTS, START_IDS, WINDOW_END,
                        WINDOW_START,
                        ContractError, dec_str, money, sh_dt, ts_str, unwire_ratio, wire_ratio, MAX_ORDERS_PER_SIDE_WINDOW,
                        ORDER_WAIT_SECONDS, RETRY_MIN_SECONDS)
from .execution import (BookView, capacity_cap, check_sell_funding, ordinary_sell_qty, protection_bounds, proxy_price,
                        size_buy, startup_fit)
from .factors import FactorValue, b0_ranking, compute_factor, score_package, validate_package
from .fees import FeeSchedule
from .journal import Journal
from .pool import SecurityFacts, build_pool
from .portfolio import EXIT, HOLD_NO_ADD, TARGET_AMOUNT, HoldingView, plan_week
from .risk import (RiskView, SellLeg, SellPlanner, active, blocked_exit_value, detect, risk_decision, satisfied_now)
from .rules import RuleBook

PHASES = {"INIT": 0, "PREOPEN": 1, "EXEC": 2, "CLOSE": 3, "CUTOFF": 4}
BAD_RISK = ("ST", "DELISTING_ARRANGEMENT", "TERMINATED")


class SimulatedCrash(RuntimeError):
    pass


@dataclass
class CellSpec:
    study_id: str
    role: str
    capital: str
    start_year: int
    scenario: str
    end_date: date
    execution_mode: str = "REPLAY_DAILY_PROXY"
    package: dict | None = None
    allowed_boards: frozenset = frozenset({"MAIN_SH", "MAIN_SZ"})
    financial_codes: frozenset = frozenset({"SW_BANK", "SW_NONBANK"})
    etf_ids: frozenset = frozenset()
    permission_id: str = "perm"
    snapshot_every: int = 0

    @property
    def start_id(self) -> str:
        return START_IDS[self.start_year]

    @property
    def cell_key(self) -> str:
        pkg = (self.package or {}).get("package_id", "NONE") if self.role != "B0" else "B0_SIZE"
        return f"{self.study_id}/{self.start_id}/{self.scenario}/{self.capital}/{self.role}/{pkg}/{self.permission_id}"


class CellRunner:
    def __init__(self, spec: CellSpec, market, rulebook: RuleBook, fees: FeeSchedule, journal: Journal | None = None,
                 crash_after_seq: int | None = None):
        self.spec = spec
        self.m = market
        self.cal: TradingCalendar = market.calendar
        self.rb = rulebook
        self.fees = fees
        self.journal = journal
        self.crash_after_seq = crash_after_seq
        self.slip = BASE_SLIPPAGE if spec.scenario == "S0" else HIGH_SLIPPAGE
        self.ledger = Ledger(spec.cell_key, fees, spec.etf_ids)
        self.phase_key = ""
        self.k = 0
        self.startup: dict = {"status": "NOT_EVALUATED"}
        self.decision_inputs: list = []
        self._qc: dict = {}
        self.event_log: list | None = None       # 设为 [] 时记录本次实际产生的事件（测试/审计用）
        self.checklist: list = []                # FORWARD_MANUAL 人工操作清单（报单/撤单请求）
        self.intents: list = []                  # FORWARD_MANUAL 本次 FinalIntent（H06.2，只读经济目标）
        if spec.role != "B0":
            if spec.package is None:
                raise ContractError("FACTOR_PACKAGE_MISSING", spec.cell_key)
            validate_package(spec.package)

    # ================================================================ 事件
    def emit(self, kind: str, payload: dict, at: datetime) -> None:
        eid = f"{self.phase_key}/{self.k:04d}"
        self.k += 1
        ev = {"event_id": eid, "kind": kind, "occurred_at": ts_str(at), "known_at": ts_str(at), "payload": payload}
        if self.journal is not None:
            self.journal.commit(ev)
        self.ledger.apply(ev)
        if self.event_log is not None:
            self.event_log.append(ev)
        if self.journal is not None and self.crash_after_seq is not None and self.journal.last_seq >= self.crash_after_seq:
            raise SimulatedCrash(self.journal.last_seq)

    # ================================================================ 主循环
    def phases(self):
        start = self.cal.first_week_end_in_year(self.spec.start_year, self.m.data_end)
        if start is None:
            return None, []
        yield_list = [(start, "INIT")]
        week_ends = set(self.cal.week_ends(self.m.data_end))
        for d in self.cal.sessions_between(self.cal.next_session(start), self.spec.end_date):
            yield_list += [(d, "PREOPEN"), (d, "EXEC"), (d, "CLOSE")]
            if d in week_ends:
                yield_list.append((d, "CUTOFF"))
        return start, yield_list

    def run(self, resume_events: list | None = None) -> dict:
        start, plist = self.phases()
        if start is None:
            return {"startup_status": "NOT_STARTED_UNKNOWN", "reason": "START_ANCHOR_UNAVAILABLE"}
        if self.spec.end_date > self.m.data_end:
            raise ContractError("END_AFTER_DATA", str(self.spec.end_date))
        cursor = self.ledger.cursor
        done = (date.fromisoformat(cursor["date"]), PHASES[cursor["phase"]]) if cursor else None
        for d, ph in plist:
            if done is not None and (d, PHASES[ph]) <= done:
                continue
            if self.ledger.cursor and self.ledger.cursor.get("halt"):
                break
            self.phase_key = f"{d.isoformat()}/{ph}"
            self.k = 0
            self._qc = {}
            self.now = self._phase_start(d, ph)
            getattr(self, "_phase_" + ph.lower())(d)
            self.emit("CURSOR", {"date": d.isoformat(), "phase": ph,
                                 "halt": self.startup.get("status", "") in ("NOT_STARTED_UNFIT", "NOT_STARTED_UNKNOWN")},
                      self._phase_time(d, ph))
        return self.ledger.cursor

    def _phase_start(self, d: date, ph: str) -> datetime:
        """相位内最早决策时刻：规则/事实须在此之前已公开（RULE_NOT_YET_PUBLIC）。"""
        return sh_dt(d, {"INIT": time(20, 0), "PREOPEN": time(9, 0), "EXEC": WINDOW_START if self.minute else PROXY_TIME,
                         "CLOSE": time(15, 30), "CUTOFF": time(20, 0)}[ph])

    def _phase_time(self, d: date, ph: str) -> datetime:
        return sh_dt(d, {"INIT": time(20, 0), "PREOPEN": time(9, 0), "EXEC": time(10, 30) if self.minute else PROXY_TIME,
                         "CLOSE": time(15, 30), "CUTOFF": time(20, 0)}[ph])

    @property
    def minute(self) -> bool:
        return self.spec.execution_mode == "REPLAY_MINUTE_WINDOW"

    @property
    def manual(self) -> bool:
        """FORWARD_MANUAL：只生成已批准未报出的预约委托与人工清单，不模拟成交、不调用券商。"""
        return self.spec.execution_mode == "FORWARD_MANUAL"

    # ================================================================ 市场事实（按可见时间）
    def rule(self, sid: str, d: date):
        mm = self.m.master[sid]
        now = getattr(self, "now", None)
        asof = now if now is not None and now.date() == d else sh_dt(d, time(15, 30))
        return self.rb.lookup(mm["exchange"], mm["board"], d, now,
                              lambda: self.m.risk_status(sid, d, asof))

    def bounds(self, sid: str, d: date, bar: dict):
        r = self.rule(sid, d)
        lo, up = r.price_bounds(bar["ref_preclose"])
        return r, lo, up

    def security_facts(self, sid: str, d: date, asof: datetime) -> SecurityFacts:
        mm = self.m.master[sid]
        bar = self.m.bar(sid, d)
        listed = d >= mm["list_date"] and bar is not None
        amounts = []
        for x in self.cal.last_n_sessions(d, 20):
            b = self.m.bar(sid, x)
            if b is None:
                amounts.append((x, None, "MISSING"))
            else:
                amounts.append((x, b["amount"], "SUSPENDED" if b["status"] == "SUSPENDED" else "TRADING"))
        return SecurityFacts(sid, mm["trade_code"], mm["exchange"], mm["board"], mm["security_type"], listed,
                             mm["list_date"], self.m.industry(sid, d), self.m.risk_status(sid, d, asof) if listed else None,
                             bar["close"] if bar else None, amounts, bar["mv"] if bar else None,
                             bar["status"] if bar else None)

    def cap(self, sid: str, d: date) -> Decimal | None:
        key = ("cap", sid, d)
        if key not in self._qc:
            self._qc[key] = self._cap(sid, d)
        return self._qc[key]

    def _cap(self, sid: str, d: date) -> Decimal | None:
        prior = []
        for x in self.cal.last_n_sessions(d, 20, include=False):
            b = self.m.bar(sid, x)
            prior.append((None, "MISSING") if b is None else (b["amount"], b["status"]))
        return capacity_cap(prior)

    # ================================================================ 相位
    def binding(self) -> dict:
        """运行身份：规范化的单元定义与输入内容（规则值、因子包、权限、费用、规则、执行配置）。"""
        sp = self.spec
        b = {"detail_version": DETAIL_VERSION, "study_id": sp.study_id, "cell_key": sp.cell_key, "role": sp.role,
             "capital": sp.capital, "start_year": sp.start_year, "scenario": sp.scenario,
             "execution_mode": sp.execution_mode, "allowed_boards": sorted(sp.allowed_boards),
             "financial_codes": sorted(sp.financial_codes), "etf_ids": sorted(sp.etf_ids),
             "package": sp.package if sp.role != "B0" else None,
             "rulebook": getattr(self.rb, "payload", None), "fees": getattr(self.fees, "payload", None)}
        if not self.manual:
            b["end_date"] = sp.end_date.isoformat()     # 前向的数据终点逐日延伸，不属于身份
        return json.loads(json.dumps(b, sort_keys=True, ensure_ascii=False, default=str))

    def check_binding(self) -> None:
        if self.ledger.binding is not None and canon(self.ledger.binding) != canon(self.binding()):
            raise ContractError("STUDY_DEFINITION_MISMATCH", "日志属于不同的研究/输入内容，不能续跑")

    def _phase_init(self, d: date) -> None:
        at = sh_dt(d, time(20, 0))
        self.emit("RUN_BINDING", {"binding": self.binding()}, at)
        bootstrapped = self.ledger.capital_started     # 前向已有真实账户：人工核对的 BOOTSTRAP_ACCOUNT 已入账
        if not bootstrapped:
            self.emit("CAPITAL_FLOW", {"flow_id": "initial", "amount": self.spec.capital}, at)
        self.emit("EOD", {"date": d.isoformat()}, at)
        plan = self._weekly(d, at, first=True)
        if plan is None:
            self.startup = {"status": "NOT_STARTED_UNKNOWN", "reason": "FIRST_PLAN_UNAVAILABLE"}
        elif plan["pool_health"] == "POOL_UNCERTAIN":
            self.startup = {"status": "NOT_STARTED_UNKNOWN", "reason": "POOL_UNCERTAIN"}
        elif bootstrapped:
            # 全现金建仓测算只适用于新入金起点；已有账户接入不做假想建仓（D16.2 另走 Bootstrap）
            self.startup = {"status": "STARTED", "reason": "BOOTSTRAP_EXISTING_ACCOUNT"}
        else:
            self._startup_check(d, plan)
        self.emit("DECISION_LOG", {"kind": "STARTUP", **{k: (dec_str(v) if isinstance(v, Decimal) else v)
                                                         for k, v in self.startup.items() if k != "quantities"}}, at)
        if self.startup["status"] == "STARTED":
            self.emit("RUN_STATE", {"to": "NORMAL", "reason": "STARTUP_PASS"}, at)

    def _startup_check(self, d: date, plan: dict) -> None:
        names = plan["buy_candidates_ordered"]
        close = {}
        for sid in names:
            b = self.m.bar(sid, d)
            close[sid] = b["close"] if b else None

        def fee(sid, qty, p):
            mm = self.m.master[sid]
            return self.fees.preview_new_order_fees(mm["exchange"], "BUY", d, qty * p, qty)

        try:
            self.startup = startup_fit(capital=money(self.spec.capital), names=names, close=close,
                                       industry_of={s: self.m.industry(s, d) for s in names},
                                       grid_of=lambda s: self.rule(s, d).grid, fee_fn=fee, slippage=self.slip,
                                       tick_of=lambda s: self.rule(s, d).tick)
        except ContractError as e:
            self.startup = {"status": "NOT_STARTED_UNKNOWN", "reason": e.code}

    def _phase_preopen(self, d: date) -> None:
        at = sh_dt(d, time(9, 0))
        L = self.ledger
        for a in self.m.actions:
            sid = a["security_id"]
            if a["ex_date"] == d and a["action_id"] in L.entitlements:
                b = self.m.bar(sid, d)
                payload = {"action_id": a["action_id"], "ex_date": d.isoformat()}
                if b is not None:
                    payload["reference_price"] = dec_str(b["ref_preclose"])
                    if a["kind"] == "STOCK_DIVIDEND" and a.get("cash_in_lieu") == "AT_EX_REFERENCE":
                        payload["cash_in_lieu_per_share"] = dec_str(b["ref_preclose"])
                self.emit("CA_EX", payload, at)
            if a.get("pay_date") == d and a["action_id"] in L.entitlements and a["kind"] == "CASH_DIVIDEND":
                self.emit("CA_PAY", {"action_id": a["action_id"], "withheld": self._withholding(a)}, at)
            if a.get("credit_date") == d and a["action_id"] in L.entitlements:
                self.emit("CA_SHARE_CREDIT", {"action_id": a["action_id"], "sellable_from": a["listing_date"].isoformat(),
                                              "acquisition_inheritance": "INHERIT_PARENT"}, at)
                frac = L.receivables.get(f"FRAC:{a['action_id']}")
                if frac is not None:      # 零碎股按条款现金补偿，与股份同日到账（TEST_ONLY 合成条款）
                    self.emit("CA_CASH_COMPENSATION", {"action_id": a["action_id"], "phase": "FRACTION_CASH",
                                                       "amount": dec_str(frac)}, at)
        for slice_id, amt in sorted(L.tax_payables.items()):
            if amt > 0:
                self.emit("TAX_SETTLEMENT", {"slice_id": slice_id, "amount": dec_str(amt)}, at)
        # 延后实扣的已计提费用：到清算规则的实扣日按已评金额结算（研究模拟无券商差额）
        for coid, payable in sorted(L.fee_payables.items()):
            o = L.orders[coid]
            mm = self.m.master[o.security_id]
            rule = self.rule(o.security_id, date.fromisoformat(o.trade_date))
            due = self.rb.settlement(rule).dates(self.cal, date.fromisoformat(o.trade_date))["fee_settle_on"]
            if due <= d.isoformat():
                accrued = sum(o.fees_accrued.values(), Decimal(0))
                self.emit("FEE_SETTLEMENT", {"client_order_id": coid, "assessed_fee": dec_str(accrued),
                                             "already_accrued": dec_str(accrued),
                                             "already_paid": dec_str(accrued - payable),
                                             "settlement_cash_change": dec_str(payable)}, at)
        for ev in self.m.lifecycle_events:
            sid = ev["security_id"]
            p = L.positions.get(sid)
            if p is None or p.quantity == 0:
                continue
            if ev["date"] == d and p.lifecycle == "NORMAL":
                self.emit("LIFECYCLE", {"security_id": sid, "lifecycle": "RESIDUAL",
                                        "evidence_ref": "TEST_ONLY_SYNTHETIC_TERMINATION"}, at)
                self.emit("MARKS", {"marks": {sid: {"price": dec_str(Decimal(ev["recovery_per_share_cents"]) / 100),
                                                    "mark_date": d.isoformat(), "status": "PROVISIONAL_REFERENCE",
                                                    "bounds": ["0", dec_str(p.mark or Decimal(0))]}}}, at)
            if ev["recovery_date"] == d and p.lifecycle == "RESIDUAL":
                self.emit("RESIDUAL_RECOVERY", {"security_id": sid, "qty": p.quantity,
                                                "cash": dec_str(Decimal(ev["recovery_per_share_cents"]) * p.quantity / 100)}, at)
        # 09:30 前已可得的确认风险事实
        self._security_exit_facts(d, sh_dt(d, time(9, 29, 59)), at)

    def _withholding(self, a: dict) -> str:
        ent = self.ledger.entitlements[a["action_id"]]
        b = ent.get("binding") or {}
        rid = b.get("selected_rule_id")
        if rid is None:
            return "0"
        rule = self.fees.tax.by_id(rid)
        gross = money(self.ledger.receivables.get(f"DIV:{a['action_id']}", Decimal(0)))
        from .contracts import cents
        return dec_str(cents(gross * rule.initial_withholding_rate))

    def _security_exit_facts(self, d: date, asof: datetime, at: datetime) -> None:
        L = self.ledger
        for sid, p in sorted(L.positions.items()):
            if p.quantity == 0 or p.lifecycle != "NORMAL":
                continue
            st = self.m.risk_status(sid, d, asof)
            if st in BAD_RISK and not any(e["type"] == "SECURITY_EXIT" and e["key"] == sid for e in active(L.episodes)):
                fe = self._first_eligible(at)
                from .risk import new_episode
                ep = new_episode("SECURITY_EXIT", sid, len(L.episodes) + 1, ts_str(asof), fe, [sid], Fraction(0), st)
                self.emit("RISK_EPISODE_START", {"episode": ep}, at)
                self._stress_for(ep, at)

    def _first_eligible(self, at: datetime) -> str:
        t = self.cal.first_window_after(at) if self.minute else self.cal.first_proxy_after(at)
        return t.date().isoformat()

    def _effective_first(self, ep: dict) -> date:
        d0 = date.fromisoformat(ep["first_eligible_session"])
        return self.cal.next_session(d0) if self.spec.scenario == "S2" else d0

    def _stress_for(self, ep: dict, at: datetime) -> None:
        if self.spec.scenario != "S3":
            return
        d0 = date.fromisoformat(ep["first_eligible_session"])
        days = [d0, self.cal.next_session(d0), self.cal.next_session(d0, 2)]
        scope = "ALL" if ep["type"] in ("TOTAL_REDUCE", "EMERGENCY_EXIT") else (
            "INDUSTRY:" + ep["key"] if ep["type"] == "INDUSTRY_REDUCE" else "SECURITY:" + ep["key"])
        self.emit("STRESS_BLOCK", {"block": {"episode_id": ep["episode_id"], "scope": scope,
                                             "members": ep["scope_members"], "days": [x.isoformat() for x in days]}}, at)

    def stress_blocked(self, sid: str, d: date) -> bool:
        ds = d.isoformat()
        for b in self.ledger.stress_blocks:
            if ds not in b["days"]:
                continue
            if b["scope"] == "ALL" or sid in b["members"]:
                return True
            if b["scope"].startswith("INDUSTRY:"):
                p = self.ledger.positions.get(sid)
                if p is not None and p.industry == b["scope"].split(":", 1)[1]:
                    return True
        return False

    # ---------------------------------------------------------------- 收盘
    def _phase_close(self, d: date) -> None:
        at = sh_dt(d, time(15, 30))
        L = self.ledger
        marks = {}
        for sid, p in sorted(L.positions.items()):
            if p.quantity + p.share_receivable == 0 or p.lifecycle == "RESIDUAL":
                continue
            b = self.m.bar(sid, d)
            if b is None:
                marks[sid] = {"price": dec_str(p.mark) if p.mark is not None else None, "mark_date": p.mark_date,
                              "status": "UNRELIABLE"}
            elif b["status"] == "TRADING":
                marks[sid] = {"price": dec_str(b["close"]), "mark_date": d.isoformat(), "status": "NORMAL",
                              "industry": self.m.industry(sid, d)}
            else:
                marks[sid] = {"price": dec_str(b["close"]), "mark_date": p.mark_date if p.mark_status != "NORMAL"
                              else (p.mark_date or d.isoformat()), "status": "PROVISIONAL_SUSPENDED",
                              "industry": self.m.industry(sid, d)}
        if marks:
            self.emit("MARKS", {"marks": marks}, at)
        for a in self.m.actions:
            if a["record_date"] == d:
                sid = a["security_id"]
                p = L.positions.get(sid)
                if p is None or p.quantity == 0:
                    continue
                a = self.m.action_terms(a)
                payload = {"action_id": a["action_id"], "security_id": sid, "kind": a["kind"],
                           "record_date": d.isoformat(), "security_class": "INDIVIDUAL_A_SHARE",
                           "dividend_type": "CASH" if a["kind"] == "CASH_DIVIDEND" else "STOCK"}
                if a["kind"] == "CASH_DIVIDEND":
                    cps = Decimal(a.get("cash_per_share_cents", 0)) / 100
                    payload.update(cash_per_share=dec_str(cps), tax_base_per_share=dec_str(cps))
                else:
                    payload.update(shares_per_share=a["shares_per_share"], rounding="FLOOR_CASH_IN_LIEU",
                                   tax_treatment=a.get("tax_treatment"))
                self.emit("CA_RECORD", payload, at)
        self.emit("EOD", {"date": d.isoformat(), "frozen_exit_value": wire_ratio(self._frozen_exit_value())}, at)
        self._eod_risk(d, at)

    def _frozen_exit_value(self) -> Fraction:
        """应风险退出（SECURITY_EXIT/EMERGENCY_EXIT）而收盘仍持有的剩余市值（含冻结、残余与股份应收）。"""
        L = self.ledger
        sids = set(L.positions) if active(L.episodes, "EMERGENCY_EXIT") else \
            {ep["key"] for ep in active(L.episodes, "SECURITY_EXIT")}
        tot = Fraction(0)
        for sid in sids:
            p = L.positions.get(sid)
            if p is not None and p.quantity + p.share_receivable > 0 and p.mark is not None:
                tot += (p.quantity + p.share_receivable) * Fraction(p.mark)
        return tot

    def risk_view(self, prices: dict | None = None) -> RiskView:
        L = self.ledger
        e, st = L.equity(prices)
        values, ind, qty = {}, {}, {}
        for sid, p in L.positions.items():
            q = p.quantity + p.share_receivable
            if q == 0:
                continue
            m = (prices or {}).get(sid, p.mark)
            if m is None:
                continue
            values[sid] = q * Fraction(m)
            ind[sid] = p.industry
            qty[sid] = p.quantity
        return RiskView(e, values, ind, qty)

    def _eod_risk(self, d: date, at: datetime) -> None:
        L = self.ledger
        if not L.capital_started or L.run_state == "WAITING_START":
            return
        pt = L.ledger_points[-1]
        view = self.risk_view()
        view.dd = pt.get("dd")
        for sid in view.quantities:
            b = self.m.bar(sid, d)
            if b is not None and b["status"] == "TRADING":
                view.closes[sid] = b["close"]
            st = self.m.risk_status(sid, d, sh_dt(d, time(15, 30)))
            if st in BAD_RISK:
                view.risk_facts[sid] = st
            elif b is not None and b.get("mv") is not None and sid in self.m.master:
                # 适用市值底线失效（V5 §7.2）：用当日 RuleBook 底线；规则未知或不适用时不触发
                try:
                    r = self.rule(sid, d)
                except ContractError:
                    r = None
                if r is not None and r.marketcap_floor_status == "VALUE" and b["mv"] < r.marketcap_floor:
                    view.risk_facts[sid] = "MV_FLOOR_FAIL"
        fe = self._first_eligible(sh_dt(d, time(15, 30)))
        if pt.get("valuation_status") == "UNKNOWN":
            view.dd = None
        starts, ends, flags = detect(view, L.episodes, at=ts_str(at), first_eligible=fe, seq=len(L.episodes) + 1)
        for eid, why in ends:
            self.emit("RISK_EPISODE_END", {"episode_id": eid, "reason": why, "at": ts_str(at)}, at)
        for ep in starts:
            if ep["type"] == "SECURITY_EXIT" and L.positions[ep["key"]].lifecycle != "NORMAL":
                continue
            self.emit("RISK_EPISODE_START", {"episode": ep}, at)
            self._stress_for(ep, at)
        if flags["retire"] and not L.retirement_triggered:
            self.emit("RETIREMENT", {"at": ts_str(at), "dd": wire_ratio(view.dd)}, at)
        self._update_run_state(at)

    def _update_run_state(self, at: datetime) -> None:
        L = self.ledger
        if L.run_state == "WAITING_START":
            return
        if L.retirement_triggered:
            target = "EXITING"
            normal_left = [s for s, p in L.positions.items() if p.quantity > 0 and p.lifecycle == "NORMAL"]
            if not normal_left and not L.open_orders():
                target = "RETIRED"
            if L.run_state == "RETIRED":
                target = "RETIRED"
        else:
            g = self.account_global_blocks()
            target = "RESTRICTED" if g else "NORMAL"
        if target != L.run_state:
            self.emit("RUN_STATE", {"to": target, "reason": "DERIVED"}, at)

    def account_global_blocks(self) -> list:
        L = self.ledger
        e, st = L.equity()
        dec = risk_decision(account_revision=L.revision, episodes=L.episodes, run_state=L.run_state, equity=e,
                            valuation_status=st, account_limitations=L.limitations,
                            unknown_industry_holdings=[s for s, p in L.positions.items() if p.quantity and p.industry is None],
                            order_state_unknown=any(o.known_unresolved for o in L.open_orders()), batch_blocks=[],
                            security_exits={}, no_buy_in_batch=set(), hold_no_add=set(), risk_sell_sids=set(),
                            blocked_exit_value=Fraction(0))
        return dec["global_buy_blocks"]

    # ---------------------------------------------------------------- 周度截止
    def _phase_cutoff(self, d: date) -> None:
        self._weekly(d, sh_dt(d, time(20, 0)), first=False)

    def _weekly(self, d: date, at: datetime, first: bool) -> dict | None:
        L = self.ledger
        asof = at
        facts = [self.security_facts(sid, d, asof) for sid in self.m.universe()]
        pool = build_pool(f"{d.isoformat()}", d, facts, self.cal, self.rb, set(self.spec.allowed_boards),
                          set(self.spec.financial_codes), used_at=asof)
        if self.spec.role == "B0":
            score = b0_ranking(pool)
        else:
            fv_facts = {}
            for sid in pool["security_ids"]:
                v = self.m.factor_value(sid, d)
                if v is None:
                    fv_facts[sid] = {"fixture_value": (None, "SOURCE_UNKNOWN", sh_dt(d, time(18, 0)))}
                elif v[0] == "MISSING":
                    fv_facts[sid] = {"fixture_value": (None, "SOURCE_UNKNOWN", sh_dt(d, time(18, 0)))}
                else:
                    fv_facts[sid] = {"fixture_value": (dec_str(v[1]), "VALID", sh_dt(d, time(18, 0)))}
            fvals = {f["factor_id"]: compute_factor(f, pool["security_ids"], at, fv_facts)
                     for f in self.spec.package["factors"]}
            score = score_package(self.spec.package, pool, fvals, pool["codes"])
        status = "UNASSESSED" if pool["health"] == "POOL_UNCERTAIN" else ("HEALTHY" if score["healthy"] else "BAD")
        self.emit("WEEK_STAT", {"cutoff": d.isoformat(), "status": status, "pool_health": pool["health"],
                                "pool_size": pool["size"], "valid": score["valid_count"],
                                "retired": L.retirement_triggered, "exposure": self._exposure(d, pool)}, at)
        if L.run_state in ("EXITING", "RETIRED") or (not first and L.run_state == "WAITING_START"):
            return None
        e, st = L.equity()
        if st == "UNKNOWN" or e <= 0:
            self.emit("DECISION_LOG", {"kind": "PLAN_NOT_PUBLISHED", "reason": "VALUATION_UNKNOWN" if st == "UNKNOWN"
                                       else "EQUITY_NONPOSITIVE"}, at)
            return None
        holdings = []
        risk_exit_sids = {ep["key"] for ep in active(L.episodes, "SECURITY_EXIT")} | (
            set(L.positions) if active(L.episodes, "EMERGENCY_EXIT") else set())
        for sid, p in sorted(L.positions.items()):
            if p.quantity + p.share_receivable == 0:
                continue
            b = self.m.bar(sid, d)
            holdings.append(HoldingView(sid, p.quantity, L.position_value(sid), p.industry, p.lifecycle,
                                        frozen=(b is None or b["status"] != "TRADING"),
                                        open_sell=bool(L.open_orders(sid, "SELL")), risk_exit=sid in risk_exit_sids))
        execute = self.cal.next_session(d).isoformat()
        plan, counters = plan_week(role=self.spec.role, plan_id=f"plan-{d.isoformat()}", cutoff=ts_str(at),
                                   e_decision=e, holdings=holdings,
                                   open_buy_sids={o.security_id for o in L.open_orders(side="BUY")}, pool=pool,
                                   score=score, counters=L.counters, capital_revision=L.capital_revision,
                                   execute_session=execute)
        plan["industries"] = {s: pool["industries"].get(s) for s in plan["buy_candidates_ordered"]}
        ex = self.cal.next_session(d)
        if self.spec.scenario == "S2":
            ex = self.cal.next_session(ex)
        start = WINDOW_START if (self.minute or self.manual) else PROXY_TIME
        plan["execute_window"] = {"session": ex.isoformat(), "start": ts_str(sh_dt(ex, start)),
                                  "end": ts_str(sh_dt(ex, WINDOW_END))}
        plan["source_refs"] = {"pool_decision_id": pool["decision_id"], "package_id": score.get("package_id"),
                               "account_revision": L.revision, "rulebook_id": self.rb.rulebook_id,
                               "fee_profile_id": self.fees.fee_profile_id}
        if self.spec.role != "B0" and score["healthy"]:
            self.emit("COUNTERS", {"decision_id": plan["plan_id"], "counts": counters}, at)
        self.emit("PLAN", {"plan": plan}, at)
        self.decision_inputs.append({"decision_id": plan["plan_id"], "cutoff": d.isoformat()})
        return plan

    def _exposure(self, d: date, pool: dict) -> dict:
        """D19.4 暴露报告原料：截止时正常持仓的总市值、池内分位与行业权重（未知部分单列）。"""
        L = self.ledger
        mvs, pcts, ind, total, unknown_ind = [], [], {}, Fraction(0), Fraction(0)
        n = unknown_mv = 0
        for sid, p in sorted(L.positions.items()):
            if p.quantity + p.share_receivable == 0 or p.lifecycle != "NORMAL" or p.mark is None:
                continue
            n += 1
            v = (p.quantity + p.share_receivable) * Fraction(p.mark)
            total += v
            if p.industry is None:
                unknown_ind += v
            else:
                ind[p.industry] = ind.get(p.industry, Fraction(0)) + v
            b = self.m.bar(sid, d)
            if b is None or b.get("mv") is None or b["mv"] <= 0:
                unknown_mv += 1
            else:
                mvs.append(b["mv"])
            r = pool["size_order"].get(sid)
            if r is not None and pool["size"]:
                pcts.append(Fraction(r, pool["size"]))
        out = {"names": n, "unknown_mv": unknown_mv, "n_mv": len(mvs), "in_pool": len(pcts)}
        if mvs:
            import decimal as _dm
            with _dm.localcontext() as c:
                c.prec = 30
                out["log_mv_mean"] = str((sum((x.ln() for x in mvs), Decimal(0)) / len(mvs)).quantize(Decimal("1e-12")))
            srt = sorted(mvs)
            k = len(srt)
            out["median_mv"] = dec_str(srt[k // 2] if k % 2 else (srt[k // 2 - 1] + srt[k // 2]) / 2)
        if pcts:
            out["pool_pct_mean"] = wire_ratio(sum(pcts, Fraction(0)) / len(pcts))
        if total > 0:
            out["industry_weights"] = {k: wire_ratio(v / total) for k, v in sorted(ind.items())}
            out["unknown_industry_weight"] = wire_ratio(unknown_ind / total)
        return out

    # ---------------------------------------------------------------- 执行
    def _phase_exec(self, d: date) -> None:
        L = self.ledger
        if L.run_state == "WAITING_START" or L.run_state == "RETIRED":
            return
        if self.minute:
            from .minute import run_window
            run_window(self, d)
        else:
            self._proxy(d)

    def ordinary_plan_for(self, d: date) -> dict | None:
        L = self.ledger
        if L.current_plan_id is None:
            return None
        plan = L.plans[L.current_plan_id]
        base = date.fromisoformat(plan["execute_session"])
        eff = self.cal.next_session(base) if self.spec.scenario == "S2" else base
        return plan if eff == d else None

    def quote(self, sid: str, d: date) -> dict | None:
        """09:30 代理执行时可见的开盘状态（只用于成交可行性，不回到选股）。"""
        key = (sid, d)
        if key in self._qc:
            return self._qc[key]
        self._qc[key] = self._quote(sid, d)
        return self._qc[key]

    def _quote(self, sid: str, d: date) -> dict | None:
        b = self.m.bar(sid, d)
        if b is None:
            return None
        try:
            r, lo, up = self.bounds(sid, d, b)
        except ContractError:
            return None
        return {"bar": b, "rule": r, "lower": lo, "upper": up, "tradable": b["status"] == "TRADING"}

    def _proxy(self, d: date) -> None:
        L = self.ledger
        at = sh_dt(d, PROXY_TIME)
        ds = d.isoformat()
        plan = self.ordinary_plan_for(d)
        quotes = {sid: self.quote(sid, d) for sid, p in L.positions.items() if p.quantity > 0}
        prices = {sid: q["bar"]["open"] for sid, q in quotes.items() if q and q["tradable"]}
        # -- 执行前复核风险段（以当前实际股数和可信原始估值）
        view = self.risk_view(prices)
        for ep in active(L.episodes):
            if ep["type"] in ("SINGLE_REDUCE", "INDUSTRY_REDUCE", "TOTAL_REDUCE") and satisfied_now(ep, view):
                self.emit("RISK_EPISODE_END", {"episode_id": ep["episode_id"], "reason": "TARGET_SATISFIED",
                                               "at": ts_str(at)}, at)
        eligible = [ep for ep in active(L.episodes) if self._effective_first(ep) <= d]
        emergency = any(ep["type"] == "EMERGENCY_EXIT" for ep in eligible)
        exits = sorted(set(L.positions) if emergency else {ep["key"] for ep in eligible if ep["type"] == "SECURITY_EXIT"})
        singles = sorted(ep["key"] for ep in eligible if ep["type"] == "SINGLE_REDUCE")
        inds = sorted(ep["key"] for ep in eligible if ep["type"] == "INDUSTRY_REDUCE")
        total = any(ep["type"] == "TOTAL_REDUCE" for ep in eligible)
        # -- 腿
        legs, sell_px = {}, {}
        for sid, p in sorted(L.positions.items()):
            if p.quantity == 0:
                continue
            q = quotes.get(sid)
            m = prices.get(sid, p.mark)
            if m is None:
                continue
            fs = 0
            capq = 0
            px = None
            if q and q["tradable"] and p.lifecycle == "NORMAL" and not self.stress_blocked(sid, d):
                px = self._exec_price("SELL", q)
                cap = self.cap(sid, d)
                if q["upper"] is None:
                    # 没有可靠当日价格上界：卖单容量无法给出可信保证，只人工核查，不绕过容量规则
                    self.emit("DECISION_LOG", {"kind": "SELL_SKIPPED", "security_id": sid,
                                               "reason": "CAPACITY_PRICE_BOUND_UNKNOWN"}, at)
                    px = None
                if px is not None and cap is not None:
                    left = cap - L.capacity_used_on(sid, ds) - L.capacity_pending(sid, ds)
                    capq = max(int(left / q["upper"]), 0) if left > 0 else 0
                    fs = L.free_sell(sid, ds)
            sell_px[sid] = px
            grid = q["rule"].grid if q else self.rule(sid, d).grid
            legs[sid] = SellLeg(sid, p.quantity, Fraction(m), px if px is not None else Decimal(m), p.industry, fs,
                                grid, capq, (p.quantity + p.share_receivable) * Fraction(m))
        ordinary = {}
        if plan is not None and not emergency:
            for sid, mode in plan["modes"].items():
                if sid not in legs or legs[sid].free_sell == 0:
                    continue
                if mode["mode"] == EXIT:
                    ordinary[sid] = legs[sid].grid.max_sell(min(legs[sid].free_sell, legs[sid].cap_qty),
                                                            legs[sid].free_sell, legs[sid].q)
                elif mode["mode"] == TARGET_AMOUNT and mode.get("target_amount") is not None:
                    tgt = unwire_ratio(mode["target_amount"])
                    x = ordinary_sell_qty(legs[sid].grid, legs[sid].q, prices.get(sid, L.positions[sid].mark), tgt,
                                          min(legs[sid].free_sell, legs[sid].cap_qty))
                    if x:
                        ordinary[sid] = x

        def fee_fn(sid, qty, p):
            mm = self.m.master[sid]
            return self.fees.preview_new_order_fees(mm["exchange"], "SELL", d, qty * p, qty)

        tax_fn = self.sale_tax_fn(d)
        planner = SellPlanner(view.equity, legs, fee_fn, tax_fn)
        res = planner.run(exits=exits, singles=singles, industries=inds, total=total, ordinary=ordinary)
        sold = set()
        risk_sids = set(exits) | set(singles) | {s for s, lg in legs.items() if lg.industry in inds} | \
            (set(legs) if total else set())
        if self.manual:
            # D09.4：人工前向只把需撤销的旧委托列入清单；撤单请求以人工导入 MANUAL_CANCEL_REQUESTED 为准
            from .risk import open_order_cancellations
            for coid, why in open_order_cancellations(L, res["sell"]):
                self.checklist.append({"client_order_id": coid, "action": "REQUEST_CANCEL", "reason": why,
                                       "after": "撤单请求导入 MANUAL_CANCEL_REQUESTED；撤单结果以券商终态回报导入"})
        for sid, x in sorted(res["sell"].items()):
            px = sell_px.get(sid)
            if px is None or x <= 0:
                continue
            if L.open_orders(sid):
                # 每证券每方向只一张未结单、买卖不交叉未结：已有未结委托先等其终态（人工前向跨日委托）
                self.emit("DECISION_LOG", {"kind": "SELL_SKIPPED", "security_id": sid, "reason": "OPEN_ORDER_EXISTS"}, at)
                continue
            mm = self.m.master[sid]
            tax = tax_fn(sid, x)
            costs = self.fees.preview_new_order_fees(mm["exchange"], "SELL", d, x * px, x) + (tax or Decimal(0))
            if check_sell_funding(x * px, costs, L.own_free_cash(ds)):
                self.emit("DECISION_LOG", {"kind": "SELL_SKIPPED", "security_id": sid, "reason": "COST_UNFUNDABLE"}, at)
                continue
            kind = "RISK" if sid in risk_sids and x > ordinary.get(sid, 0) else "ORDINARY"
            self._proxy_trade(d, at, sid, "SELL", x, px, kind, plan)
            sold.add(sid)
        if res["unreachable"]:
            self.emit("DECISION_LOG", {"kind": "RISK_TARGET_UNREACHABLE_NOW", "targets": res["unreachable"]}, at)
        # -- 两级禁买裁决
        required = {}
        for ep in eligible:
            if ep["type"] in ("SECURITY_EXIT", "EMERGENCY_EXIT"):
                for sid in ([ep["key"]] if ep["type"] == "SECURITY_EXIT" else list(L.positions)):
                    if L.positions.get(sid) and L.positions[sid].quantity > 0 and L.positions[sid].lifecycle == "NORMAL":
                        required[sid] = L.positions[sid].quantity
        # 已有未结卖单（人工前向的预约/在途）覆盖的股数不算“当前确实不能执行”
        executable = {s: sum(o.remaining_possible for o in L.open_orders(s, "SELL")) for s in required}
        blocked_val = blocked_exit_value(required, executable,
                                         {s: prices.get(s, L.positions[s].mark) for s in required})
        e_now = self.risk_view(prices).equity
        batch_blocks = []
        if plan is not None:
            if plan["capital_revision"] != L.capital_revision:
                batch_blocks.append("CAPITAL_CHANGED")
            if not plan["ordinary_buy_allowed"]:
                batch_blocks.append(plan["score_reason"] or "PLAN_NO_BUY")
        hold_no_add = {s for s, md in (plan or {}).get("modes", {}).items() if md["mode"] == HOLD_NO_ADD or md.get("prohibit_buy")}
        dec = risk_decision(account_revision=L.revision, episodes=L.episodes, run_state=L.run_state, equity=e_now,
                            valuation_status=L.equity()[1], account_limitations=L.limitations,
                            unknown_industry_holdings=[s for s, p in L.positions.items() if p.quantity and p.industry is None],
                            order_state_unknown=any(o.known_unresolved for o in L.open_orders()),
                            batch_blocks=batch_blocks,
                            security_exits={ep["key"]: 1 for ep in active(L.episodes, "SECURITY_EXIT")},
                            no_buy_in_batch=sold, hold_no_add=hold_no_add, risk_sell_sids=set(res["sell"]),
                            blocked_exit_value=blocked_val)
        if self.manual:
            from .risk import final_intents
            win = f"{ds}T10:00:00+08:00/{ds}T10:30:00+08:00"
            self.intents = final_intents(plan=plan, decision=dec, episodes=L.episodes, sell=res["sell"],
                                         account_revision=L.revision, quote_basis=f"OPEN_REFERENCE:{ds}",
                                         valid_window=win, cap_reference_date=ds)
        if plan is None:
            return
        if dec["global_buy_blocks"]:
            self.emit("DECISION_LOG", {"kind": "BUYS_BLOCKED", "plan_id": plan["plan_id"],
                                       "global_buy_blocks": dec["global_buy_blocks"]}, at)
            return
        self._proxy_buys(d, at, plan, dec)

    def sale_tax_fn(self, d: date):
        """卖出预览的新增股息差别税：按结算规则的税务交割日，与成交入账同一FIFO与取整。"""
        def tax(sid: str, qty: int):
            q = self.quote(sid, d)
            rule = q["rule"] if q else self.rule(sid, d)
            delivery = self.rb.settlement(rule).dates(self.cal, d)["tax_delivery_date"]
            return self.ledger.preview_sale_tax(sid, qty, delivery)
        return tax

    def _exec_price(self, side: str, q: dict) -> Decimal | None:
        """日线代理：开盘±滑点按不利方向取整；人工前向：以参考价为锚的保护边界作为限价（不模拟成交价）。"""
        if not self.manual:
            return proxy_price(side, q["bar"]["open"], self.slip, q["rule"].tick, q["lower"], q["upper"])
        ref = q["bar"]["open"]
        if (side == "BUY" and q["upper"] is not None and ref >= q["upper"]) or \
                (side == "SELL" and q["lower"] is not None and ref <= q["lower"]):
            return None
        up, lo = protection_bounds(ref, q["rule"].tick, q["lower"], q["upper"])
        return up if side == "BUY" else lo

    def book_view(self, prices: dict, d: date) -> BookView:
        """一致估值时点的账户预览；未结买单按全部成交的数量、保护价和尚未入账费用同时计入（D07.2）。"""
        L = self.ledger
        view = self.risk_view(prices)
        ind = {}
        for s, v in view.values.items():
            ind[view.industry_of[s]] = ind.get(view.industry_of[s], Fraction(0)) + v
        open_adj, open_val = Fraction(0), {}
        for o in L.open_orders(side="BUY"):
            mo = prices.get(o.security_id) or (L.positions[o.security_id].mark if o.security_id in L.positions else None)
            if mo is None:
                mo = o.limit
            fee_res = L.buy_cash_reserved(o) - o.remaining_possible * o.limit
            open_adj += o.remaining_possible * (Fraction(mo) - Fraction(o.limit)) - Fraction(fee_res)
            open_val[o.security_id] = open_val.get(o.security_id, Fraction(0)) + o.remaining_possible * Fraction(mo)
        own = L.own_free_cash(d.isoformat())
        return BookView(equity=view.equity, stock_value=sum(view.values.values(), Fraction(0)),
                        holdings_value=dict(view.values), industry_value=ind, industry_of=dict(view.industry_of),
                        open_buy_adj=open_adj, open_buy_value=open_val, cash_free=own)

    def _proxy_buys(self, d: date, at: datetime, plan: dict, dec: dict) -> None:
        L = self.ledger
        e_dec = unwire_ratio(plan["E_decision"])
        target = CENTER * e_dec
        topups = sorted((md.get("rank", 10 ** 9), sid) for sid, md in plan["modes"].items()
                        if md["mode"] == TARGET_AMOUNT and md["reason"] != "NEW_BUY" and not md.get("prohibit_buy")
                        and L.positions.get(sid) and L.positions[sid].quantity > 0)
        order = [sid for _, sid in topups] + list(plan["buy_candidates_ordered"])
        for sid in order:
            if sid in dec["security_buy_blocks"]:
                continue
            if L.open_orders(sid):
                continue                                   # 每证券同方向只一张未结单，买卖不交叉未结
            p = L.positions.get(sid)
            held = p is not None and p.quantity + p.share_receivable > 0
            if not held and len(L.actual_slots()) >= SLOTS:
                self.emit("DECISION_LOG", {"kind": "BUY_SKIPPED", "security_id": sid, "reason": "SLOT_BLOCKED"}, at)
                continue
            q = self.quote(sid, d)
            if q is None or not q["tradable"]:
                self.emit("DECISION_LOG", {"kind": "BUY_SKIPPED", "security_id": sid,
                                           "reason": "QUOTE_UNAVAILABLE" if q is None else "NOT_TRADING"}, at)
                continue
            px = self._exec_price("BUY", q)
            if px is None:
                self.emit("DECISION_LOG", {"kind": "BUY_SKIPPED", "security_id": sid, "reason": "PRICE_UNFILLABLE"}, at)
                continue
            m = q["bar"]["open"]
            prices = {}
            for s2, pp in L.positions.items():
                if pp.quantity > 0:
                    q2 = self.quote(s2, d)
                    if q2 and q2["tradable"]:
                        prices[s2] = q2["bar"]["open"]
            prices[sid] = m
            view = self.book_view(prices, d)
            if L.open_orders(sid, "BUY"):
                continue                                   # 每证券同方向只一张未结单
            cur = (p.quantity + p.share_receivable) * Fraction(m) if held else Fraction(0)
            cap = self.cap(sid, d)
            capl = None if cap is None else cap - L.capacity_used_on(sid, d.isoformat()) - L.capacity_pending(sid, d.isoformat())
            ind = (p.industry if held else plan.get("industries", {}).get(sid)) or self.m.industry(sid, d)
            mm = self.m.master[sid]
            b, why = size_buy(view, sid=sid, industry=ind, grid=q["rule"].grid, p=px, m=m, gap_value=target - cur,
                              fee_fn=lambda x: self.fees.preview_new_order_fees(mm["exchange"], "BUY", d, x * px, x),
                              capacity_left_value=capl)
            if b <= 0:
                self.emit("DECISION_LOG", {"kind": "BUY_SKIPPED", "security_id": sid, "reason": why}, at)
                continue
            self._proxy_trade(d, at, sid, "BUY", b, px, "ORDINARY", plan, industry=ind)

    def _proxy_trade(self, d: date, at: datetime, sid: str, side: str, qty: int, px: Decimal, kind: str,
                     plan: dict | None, industry: str | None = None) -> None:
        if self.manual:
            return self._manual_reserve(d, at, sid, side, qty, px, kind, plan, industry)
        L = self.ledger
        mm = self.m.master[sid]
        q = self.quote(sid, d)
        buy_upper, sell_lower = protection_bounds(q["bar"]["open"], q["rule"].tick, q["lower"], q["upper"])
        coid = f"{self.phase_key}/{sid}/{side}"
        settle = self.rb.settlement(q["rule"])
        order = {"client_order_id": coid, "batch_id": f"{d.isoformat()}/{kind}", "plan_id": (plan or {}).get("plan_id", ""),
                 "security_id": sid, "market": mm["exchange"], "board": mm["board"], "side": side, "qty": qty,
                 "limit": dec_str(buy_upper if side == "BUY" else sell_lower), "kind": kind, "trade_date": d.isoformat(),
                 "capital_revision": L.capital_revision, "window_end": ts_str(sh_dt(d, WINDOW_END)),
                 "upper_price_bound": dec_str(q["upper"]), "industry": industry}
        self.emit("ORDER_CREATE", {"order": order}, at)
        self.emit("ORDER_SUBMIT", {"client_order_id": coid, "at": ts_str(at)}, at)
        sd = settle.dates(self.cal, d)
        self.emit("BROKER_FILL", {"client_order_id": coid, "trade_key": f"SIM/{coid}/1", "qty": qty, "price": dec_str(px),
                                  "amount": None, "occurred_at": ts_str(at), "received_at": ts_str(at),
                                  "trade_date": d.isoformat(), "sellable_from": sd["sellable_from"],
                                  "tax_acquisition_date": sd["tax_acquisition_date"],
                                  "tax_delivery_date": sd["tax_delivery_date"],
                                  "proceeds_tradeable_from": sd["proceeds_tradeable_from"],
                                  "fee_settlement": sd["fee_settlement"]}, at)
        self.emit("BROKER_TERMINAL", {"client_order_id": coid, "status": "FILLED", "cum_qty": qty,
                                      "cum_value": dec_str(qty * px), "source_revision": 1}, at)


    def _manual_reserve(self, d: date, at: datetime, sid: str, side: str, qty: int, limit: Decimal, kind: str,
                        plan: dict | None, industry: str | None) -> None:
        """人工前向：建立已批准未报出的委托（锁定现金/股数/槽位/容量），输出人工清单条目；不提交、不模拟成交。"""
        L = self.ledger
        mm = self.m.master[sid]
        q = self.quote(sid, d)
        coid = f"{self.phase_key}/{sid}/{side}"
        order = {"client_order_id": coid, "batch_id": f"{d.isoformat()}/{kind}", "plan_id": (plan or {}).get("plan_id", ""),
                 "security_id": sid, "market": mm["exchange"], "board": mm["board"], "side": side, "qty": qty,
                 "limit": dec_str(limit), "kind": kind, "trade_date": d.isoformat(),
                 "capital_revision": L.capital_revision, "window_end": ts_str(sh_dt(d, WINDOW_END)),
                 "upper_price_bound": dec_str(q["upper"]) if q["upper"] is not None else None, "industry": industry}
        self.emit("ORDER_CREATE", {"order": order}, at)
        self.checklist.append({"action": "SUBMIT_ORDER", "client_order_id": coid, "security_id": sid, "side": side,
                               "qty": qty,
                               "limit": dec_str(limit), "kind": kind, "window": f"{d.isoformat()} 10:00—10:30（北京时间）",
                               "rule": ("买入限价不高于保护上界；对手价超界则不报" if side == "BUY"
                                        else "卖出限价不低于保护下界；对手价超界则不报"),
                               "after_submit": "报出后导入 MANUAL_SUBMITTED；成交/撤单以券商确认回报导入"})


def run_cell(spec: CellSpec, market, rulebook: RuleBook, fees: FeeSchedule, journal: Journal | None = None) -> CellRunner:
    r = CellRunner(spec, market, rulebook, fees, journal)
    r.run()
    return r


def resume_cell(spec: CellSpec, market, rulebook: RuleBook, fees: FeeSchedule, journal: Journal) -> CellRunner:
    """从已提交日志恢复：重放至最后一个完整相位（CURSOR），再从下一相位重算并继续。"""
    events = journal.read_committed()
    last = max((i for i, e in enumerate(events) if e["kind"] == "CURSOR"), default=-1)
    r = CellRunner(spec, market, rulebook, fees, journal)
    for ev in events[:last + 1]:
        r.ledger.apply(ev)
    if events and r.ledger.binding is None:
        bind = next((e for e in events if e["kind"] == "RUN_BINDING"), None)
        if bind is not None:
            r.ledger.apply(bind)
    r.check_binding()
    r.decision_inputs = [{"decision_id": e["payload"]["plan"]["plan_id"], "cutoff": e["payload"]["plan"]["source_cutoff"]}
                         for e in events[:last + 1] if e["kind"] == "PLAN"]
    for e in events[:last + 1]:
        if e["kind"] == "DECISION_LOG" and e["payload"].get("kind") == "STARTUP":
            r.startup = dict(e["payload"])
    r.run()
    return r
