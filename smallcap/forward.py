"""FORWARD_MANUAL：只读输入 → 人工操作清单；人工导入已确认券商回报；对账；恢复（D01.2、D13.3、D21）。

本模块不访问任何券商买卖接口，不提交或撤销真实订单。“请求取消/查询/重试”只表示人工清单条目。
"""
from __future__ import annotations

import csv
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import copy

from .account import Ledger, canon
from .contracts import ContractError, Result, OK, INVALID, UNKNOWN, dec_str, money, reason, ts_str
from .journal import Journal
from .orders import CANCEL_PENDING, RECONCILE_REQUIRED, RESERVED, SENT_UNKNOWN

LIVE_MODES = frozenset({"LIVE_ORDER", "SEND_LIVE_ORDERS", "AUTO_ORDER"})


def dispatch_mode(mode: str) -> Result:
    """本交付不实现 LIVE_ORDER；工具可用不等于授权。"""
    if mode in LIVE_MODES:
        return Result(INVALID, None, [reason("LIVE_ORDER_NOT_AUTHORIZED", "MODE", mode,
                                             "本交付不提供真实报撤单；需另行授权与实现")])
    if mode not in ("REPLAY_DAILY_PROXY", "REPLAY_MINUTE_WINDOW", "FORWARD_MANUAL"):
        return Result(INVALID, None, [reason("MODE_INVALID", "MODE", mode)])
    return Result(OK, mode)


def forward_preconditions(bundle) -> list:
    """前向计划所需外部输入；缺失则对应阻断，不编造金额或权限。"""
    out = []
    boot = bundle.payload("bootstrap")
    if boot is None or boot.get("cash_amount") is None and boot.get("positions_ref") is None:
        out.append(reason("ACTUAL_CAPITAL_NOT_PROVIDED", "PROFILE", "bootstrap", "不编造实际划拨金额"))
    if boot is not None and boot["mode"] == "EXISTING_MANUAL_ACCOUNT" and boot.get("tax_lots_ref") is None:
        out.append(reason("TAX_LOTS_NOT_PROVIDED", "PROFILE", "bootstrap", "已有持仓初始信息不齐时仅对账，不假设全现金"))
    if boot is not None and not boot.get("retirement_history_checked"):
        out.append(reason("RETIREMENT_HISTORY_UNCHECKED", "PROFILE", "bootstrap", "停用历史未核对，不得重新启用"))
    uni = bundle.payload("universe")
    if uni is None:
        out.append(reason("PERMISSIONS_NOT_PROVIDED", "PROFILE", "universe", "不编造板块权限"))
    else:
        for p in uni["permissions"]:
            if p["status"] != "ALLOWED":
                out.append(reason("PERMISSION_" + p["status"], "BOARD", p["board"], "该板块不买"))
    for name, code in (("rules", "RULE_UNKNOWN"), ("fees", "FEE_SCHEDULE_INCOMPLETE"), ("sources", "SOURCE_UNKNOWN"),
                       ("factor", "FACTOR_PACKAGE_MISSING")):
        if bundle.payload(name) is None:
            out.append(reason(code, "PROFILE", name))
    return out


def manual_checklist(plan: dict | None, intents: list[dict], blocks: list) -> dict:
    """生成人工操作清单（JSON），不含任何自动下单调用。"""
    return {"kind": "FORWARD_MANUAL_CHECKLIST", "authorization": "NONE_AUTOMATIC — 仅供人工核对后手工操作",
            "plan_id": (plan or {}).get("plan_id"), "blocks": [b.to_json() if hasattr(b, "to_json") else b for b in blocks],
            "actions": intents,
            "notes": ["先卖后买；10:00—10:30 内操作；10:30 后不得新增委托",
                      "未确认的委托不得重复下单；撤单请求不等于撤单成功",
                      "成交后用 import-broker-events 导入券商确认回报（含稳定成交编号）"]}


# ---------------------------------------------------------------- 回报导入
def broker_rows_to_events(rows: list[dict]) -> list[dict]:
    """券商确认记录 → 账本事件。FILL 以券商稳定成交编号去重；控制事件以 broker_event_id 去重。"""
    out = []
    for r in rows:
        kind = r["kind"]
        at = r["occurred_at"]
        if kind == "FILL":
            payload = {"client_order_id": r["client_order_id"], "trade_key": r["trade_id"], "qty": int(r["qty"]),
                       "price": r["price"], "amount": r.get("amount"), "occurred_at": at,
                       "received_at": r.get("received_at", at), "trade_date": r["trade_date"],
                       "sellable_from": r.get("sellable_from"), "tax_acquisition_date": r.get("tax_acquisition_date"),
                       "tax_delivery_date": r.get("tax_delivery_date"), "fee_settlement": r.get("fee_settlement", "WITH_TRADE")}
            payload = {k: v for k, v in payload.items() if v is not None or k in ("amount",)}
            eid = f"broker/FILL/{r['trade_id']}"
            out.append({"event_id": eid, "kind": "BROKER_FILL", "occurred_at": at,
                        "known_at": r.get("received_at", at), "payload": payload})
        elif kind == "ACK":
            out.append({"event_id": f"broker/ACK/{r['broker_event_id']}", "kind": "BROKER_ACK", "occurred_at": at,
                        "known_at": at, "payload": {"client_order_id": r["client_order_id"],
                                                    "broker_order_id": r.get("broker_order_id")}})
        elif kind == "TERMINAL":
            out.append({"event_id": f"broker/TERMINAL/{r['broker_event_id']}", "kind": "BROKER_TERMINAL",
                        "occurred_at": at, "known_at": at,
                        "payload": {"client_order_id": r["client_order_id"], "status": r["status"],
                                    "cum_qty": int(r["cum_qty"]), "cum_value": r["cum_value"],
                                    "source_revision": int(r["source_revision"])}})
        elif kind == "MANUAL_SUBMITTED":
            out.append({"event_id": f"manual/SUBMITTED/{r['client_order_id']}", "kind": "ORDER_SUBMIT",
                        "occurred_at": at, "known_at": at,
                        "payload": {"client_order_id": r["client_order_id"], "at": at}})
        elif kind == "MANUAL_NOT_SUBMITTED":
            out.append({"event_id": f"manual/NOT_SUBMITTED/{r['client_order_id']}", "kind": "ORDER_LOCAL_CANCEL",
                        "occurred_at": at, "known_at": at,
                        "payload": {"client_order_id": r["client_order_id"], "reason": r.get("reason", "MANUAL_NOT_SUBMITTED")}})
        elif kind == "MANUAL_CANCEL_REQUESTED":
            out.append({"event_id": f"manual/CANCEL/{r['broker_event_id']}", "kind": "CANCEL_REQUEST",
                        "occurred_at": at, "known_at": at,
                        "payload": {"client_order_id": r["client_order_id"], "request_id": r["broker_event_id"],
                                    "reason": r.get("reason", "MANUAL")}})
        elif kind == "CANCEL_FAILED":
            v = r.get("original_still_open")
            out.append({"event_id": f"broker/CANCEL_FAILED/{r['broker_event_id']}", "kind": "CANCEL_FAILED",
                        "occurred_at": at, "known_at": at,
                        "payload": {"client_order_id": r["client_order_id"],
                                    "original_still_open": None if v in (None, "", "UNKNOWN") else str(v).lower() == "true"}})
        # ---- 人工核对的账户事实（券商对账单/银证流水为据；evidence_ref 必填）
        elif kind == "CAPITAL_FLOW":
            out.append({"event_id": f"manual/CAPITAL_FLOW/{r['flow_id']}", "kind": "CAPITAL_FLOW", "occurred_at": at,
                        "known_at": at, "payload": {"flow_id": r["flow_id"], "amount": r["amount"],
                                                    "confirmed_external": True, "evidence_ref": r["evidence_ref"]}})
        elif kind == "CASH_RESTRICTION":
            out.append({"event_id": f"manual/CASH_RESTRICTION/{r['broker_event_id']}", "kind": "CASH_RESTRICTION",
                        "occurred_at": at, "known_at": at,
                        "payload": {"restriction_id": r["restriction_id"], "amount": r["amount"],
                                    "until": r.get("until") or None, "reason": r["reason"],
                                    "evidence_ref": r["evidence_ref"]}})
        elif kind == "SHARE_FREEZE":
            out.append({"event_id": f"manual/SHARE_FREEZE/{r['broker_event_id']}", "kind": "SHARE_FREEZE",
                        "occurred_at": at, "known_at": at,
                        "payload": {"security_id": r["security_id"], "qty": int(r["qty"]),
                                    "reflected_in_broker_can_sell": str(r["reflected_in_broker_can_sell"]).lower() == "true",
                                    "evidence_ref": r["evidence_ref"]}})
        elif kind == "BOOTSTRAP_ACCOUNT":
            # 只接受 JSON 行：{"kind","bootstrap_id","occurred_at","cash","positions":[...],"evidence_ref"}
            if r.get("open_orders"):
                raise ContractError("BOOTSTRAP_OPEN_ORDERS_UNSUPPORTED", "接入前须先人工了结或撤销全部未结委托")
            out.append({"event_id": f"manual/BOOTSTRAP/{r['bootstrap_id']}", "kind": "BOOTSTRAP_ACCOUNT",
                        "occurred_at": at, "known_at": at,
                        "payload": {"cash": r["cash"], "positions": r["positions"], "evidence_ref": r["evidence_ref"]}})
        elif kind == "ORDER_RESOLVE":
            out.append({"event_id": f"manual/RESOLVE/{r['broker_event_id']}", "kind": "ORDER_RESOLVE",
                        "occurred_at": at, "known_at": at,
                        "payload": {"client_order_id": r["client_order_id"], "evidence_ref": r["evidence_ref"]}})
        elif kind == "LIMITATION_CLEAR":
            out.append({"event_id": f"manual/LIMITATION_CLEAR/{r['broker_event_id']}", "kind": "LIMITATION",
                        "occurred_at": at, "known_at": at,
                        "payload": {"code": r["code"], "clear": True, "evidence_ref": r["evidence_ref"]}})
        elif kind == "UNMAPPED_STATUS":
            out.append({"event_id": f"broker/UNMAPPED/{r['broker_event_id']}", "kind": "UNMAPPED_STATUS",
                        "occurred_at": at, "known_at": at,
                        "payload": {"client_order_id": r["client_order_id"], "broker_status": str(r["broker_status"])}})
        else:
            raise ContractError("BROKER_EVENT_KIND_UNSUPPORTED", kind)
    return out


TERMINAL_KINDS = frozenset({"FILLED", "CANCELED", "REJECTED"})


def map_broker_status_rows(rows: list[dict], status_map: dict) -> list[dict]:
    """券商委托状态行 → 标准 TERMINAL 行。status_map 由用户按实际SDK版本样例提供：{券商状态值: 标准状态}；
    标准状态只允许 NOT_SUBMITTED/SUBMITTED/ACKED/PARTIAL/CANCEL_PENDING/FILLED/CANCELED/REJECTED。
    只有终态生成 TERMINAL；未映射的状态不释放任何资源，抛 BROKER_STATUS_UNMAPPED 由导入记为冲突（H05.3）。"""
    allowed = {"NOT_SUBMITTED", "SUBMITTED", "ACKED", "PARTIAL", "CANCEL_PENDING"} | TERMINAL_KINDS
    bad = sorted(v for v in status_map.values() if v not in allowed)
    if bad:
        raise ContractError("BROKER_STATUS_MAP_INVALID", ",".join(bad))
    out = []
    for r in rows:
        if r.get("kind") != "ORDER_STATUS":
            out.append(r)
            continue
        std = status_map.get(str(r["broker_status"]))
        if std is None:
            out.append({**r, "kind": "UNMAPPED_STATUS"})
        elif std in TERMINAL_KINDS:
            out.append({"kind": "TERMINAL", "client_order_id": r["client_order_id"], "status": std,
                        "cum_qty": r["cum_qty"], "cum_value": r["cum_value"], "source_revision": r["source_revision"],
                        "broker_event_id": r["broker_event_id"], "occurred_at": r["occurred_at"]})
    return out


def commit_event(ledger: Ledger, journal: Journal | None, ev: dict) -> bool:
    """先在账本副本上计算和校验，通过后才写日志（fsync）并入账；返回是否新入账。
    校验失败抛 ContractError 且不落盘——日志里不能出现恢复时无法重放的事件（D13.2）。"""
    content = canon({"kind": ev["kind"], "payload": ev["payload"]})
    if ev["event_id"] in ledger.seen:
        if ledger.seen[ev["event_id"]] != content:
            raise ContractError("EVENT_CONFLICT", ev["event_id"])
        return False
    trial = copy.deepcopy(ledger, {id(ledger.fees): ledger.fees})
    trial.apply(ev)
    if journal is not None:
        journal.commit(ev)
    return ledger.apply(ev)


def _limitation(ledger: Ledger, journal: Journal | None, event_id: str, code: str, detail: str = "",
                clear: bool = False, at: str | None = None) -> None:
    commit_event(ledger, journal, {"event_id": event_id, "kind": "LIMITATION", "occurred_at": at, "known_at": at,
                                   "payload": {"code": code, "detail": detail, "clear": clear}})


def import_broker_events(ledger: Ledger, journal: Journal | None, rows: list[dict]) -> dict:
    """原子去重后入账；同ID异内容不择后一条。无法入账的确认回报不落盘，另以 LIMITATION 事件持久记录冲突并停止增风险。"""
    applied, duplicates, conflicts = 0, 0, []
    for ev in broker_rows_to_events(rows):
        try:
            if ev["kind"] == "UNMAPPED_STATUS":
                raise ContractError("BROKER_STATUS_UNMAPPED", ev["payload"]["broker_status"])
            if commit_event(ledger, journal, ev):
                applied += 1
            else:
                duplicates += 1
        except ContractError as e:
            conflicts.append({"event_id": ev["event_id"], "code": e.code, "detail": str(e)})
            _limitation(ledger, journal, f"conflict/{ev['event_id']}/{e.code}", f"EVENT_CONFLICT:{ev['event_id']}",
                        e.code, at=ev.get("known_at"))
    return {"applied": applied, "duplicates": duplicates, "conflicts": conflicts}


def read_rows(path: str | Path) -> list[dict]:
    p = Path(path)
    if p.suffix == ".json":
        return json.loads(p.read_text(encoding="utf-8"))
    with open(p, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------- 对账
def reconcile_account(ledger: Ledger, broker: dict, explained_cash: list | None = None,
                      journal: Journal | None = None, at: str | None = None) -> Result:
    """证券数量/可卖/冻结与现金逐项核对；差异必须找到缺失/重复事件，禁止余额覆盖。
    券商可卖量（can_use_volume）作为 BROKER_POSITION_VIEW 入账参与 free_sell；差异与解除均以 LIMITATION 事件持久化。"""
    at = at or ts_str(datetime.now().astimezone())
    tag = f"reconcile/{at}"
    diffs = []
    positions = broker.get("positions", {})
    for sid, b in sorted(positions.items()):
        p = ledger.positions.get(sid)
        q = p.quantity if p else 0
        if int(b["volume"]) != q:
            diffs.append(reason("POSITION_MISMATCH", "SECURITY", sid, f"broker {b['volume']} vs ledger {q}"))
    for sid, p in sorted(ledger.positions.items()):
        if p.quantity and sid not in positions:
            diffs.append(reason("POSITION_MISMATCH", "SECURITY", sid, f"broker 0 vs ledger {p.quantity}"))
    can_sell = {sid: int(b["can_use_volume"]) for sid, b in sorted(positions.items())
                if b.get("can_use_volume") not in (None, "") and sid in ledger.positions}
    if can_sell:
        commit_event(ledger, journal, {"event_id": f"{tag}/VIEW", "kind": "BROKER_POSITION_VIEW", "occurred_at": at,
                                       "known_at": at, "payload": {"can_sell": can_sell}})
    if broker.get("cash_book") is not None:
        exp = sum((money(x["amount"]) for x in (explained_cash or [])), Decimal(0))
        diff = money(broker["cash_book"]) - ledger.cash - exp
        if diff != 0:
            diffs.append(reason("CASH_RECONCILIATION_FAILED", "CASH", ledger.account_id, f"未解释差额 {diff}"))
    if diffs:
        for d in diffs:
            _limitation(ledger, journal, f"{tag}/{d.code}:{d.scope_id}", f"{d.code}:{d.scope_id}", d.message, at=at)
        return Result(INVALID, None, diffs)
    for k in sorted(k for k in ledger.limitations if k.startswith(("POSITION_MISMATCH", "CASH_RECONCILIATION_FAILED"))):
        _limitation(ledger, journal, f"{tag}/clear/{k}", k, "RECONCILED", clear=True, at=at)
    return Result(OK, None)


def resume_actions(ledger: Ledger, now: datetime) -> dict:
    """恢复：先对账/查询未知委托，不因本地没有ACK就重发；过期普通意图不补报；风险按当前剩余目标重算。"""
    query, expired = [], []
    for o in ledger.orders.values():
        if o.status in (SENT_UNKNOWN, CANCEL_PENDING, RECONCILE_REQUIRED):
            query.append({"client_order_id": o.client_order_id, "status": o.status,
                          "action": "人工查询券商委托/成交并导入确认回报；禁止重发"})
        elif o.status == RESERVED:
            query.append({"client_order_id": o.client_order_id, "status": o.status,
                          "action": "人工确认是否已报出：已报导入 MANUAL_SUBMITTED，未报导入 MANUAL_NOT_SUBMITTED；预约在此之前不释放"})
    plan = ledger.plans.get(ledger.current_plan_id) if ledger.current_plan_id else None
    if plan is not None:
        ex = plan.get("execute_session")
        if ex is not None and ex < now.date().isoformat():
            expired.append({"plan_id": plan["plan_id"], "reason": "ORDINARY_PLAN_EXPIRED_NOT_REPLAYED"})
    risk = [{"episode_id": e["episode_id"], "type": e["type"], "action": "按当前真实权益与股数重算剩余目标"}
            for e in ledger.episodes.values() if e["active"]]
    return {"query_orders": query, "expired_plans": expired, "risk_recompute": risk, "resend": []}


# ---------------------------------------------------------------- 前向会话（只读事实 → 人工清单）
def forward_account_id(bundle) -> str:
    boot = bundle.payload("bootstrap") or {}
    return f"{bundle.study['study_id']}/FORWARD/{boot.get('bootstrap_id', 'NONE')}"


def forward_ledger(bundle, journal: Journal) -> Ledger:
    """前向账户的唯一恢复入口：带费用表与ETF范围（缺费用表时成交费用只能记 FEE_UNKNOWN）。"""
    from .fees import FeeSchedule
    from .journal import recover
    fees = bundle.payload("fees")
    uni = bundle.payload("universe") or {"etf_security_ids": []}
    return recover(journal, forward_account_id(bundle), FeeSchedule(fees) if fees is not None else None,
                   uni["etf_security_ids"])


class ForwardSession:
    """以与回放相同的决策代码推进一个真实账户的相位；EXEC 相位只产生已批准未报出的预约与人工清单。
    同一 (日期, 相位) 已有 CURSOR 时不重复执行（恢复不能重复入账）。"""

    PHASES = ("INIT", "PREOPEN", "EXEC", "CLOSE", "CUTOFF")
    HARD = ("ACTUAL_CAPITAL_NOT_PROVIDED", "PERMISSIONS_NOT_PROVIDED", "RULE_UNKNOWN", "FEE_SCHEDULE_INCOMPLETE",
            "FACTOR_PACKAGE_MISSING", "SOURCE_UNKNOWN", "TAX_LOTS_NOT_PROVIDED", "RETIREMENT_HISTORY_UNCHECKED")

    def __init__(self, bundle, journal: Journal, market):
        from .fees import FeeSchedule
        from .replay import CellRunner, CellSpec
        from .rules import RuleBook
        if bundle.study["execution_mode"] != "FORWARD_MANUAL":
            raise ContractError("MODE_INVALID", "ForwardSession 需要 FORWARD_MANUAL")
        blocks = forward_preconditions(bundle)
        hard = [r for r in blocks if r.code in self.HARD]
        if hard:
            raise ContractError("FORWARD_BLOCKED", ";".join(r.code for r in hard))
        uni = bundle.payload("universe")
        boot = bundle.payload("bootstrap")
        self.bundle, self.journal, self.market = bundle, journal, market
        spec = CellSpec(bundle.study["study_id"], "B1", boot.get("cash_amount") or "0", 2024, "S0", market.data_end,
                        "FORWARD_MANUAL", bundle.payload("factor"),
                        frozenset(p["board"] for p in uni["permissions"] if p["status"] == "ALLOWED"),
                        frozenset(getattr(market, "financial_codes", ())), frozenset(uni["etf_security_ids"]),
                        "forward")
        self.runner = CellRunner(spec, market, RuleBook(bundle.payload("rules")), FeeSchedule(bundle.payload("fees")),
                                 journal)
        self.runner.ledger = forward_ledger(bundle, journal)
        self.runner.check_binding()
        self.boot = boot

    def done(self, d: date, phase: str) -> bool:
        return any(e["kind"] == "CURSOR" and e["payload"]["date"] == d.isoformat() and e["payload"]["phase"] == phase
                   for e in self.journal.read_committed())

    def step(self, d: date, phase: str) -> dict:
        from .contracts import sh_dt
        from datetime import time
        if phase not in self.PHASES:
            raise ContractError("PHASE_INVALID", phase)
        r = self.runner
        if self.done(d, phase):
            return {"status": "ALREADY_DONE", "phase": phase, "date": d.isoformat()}
        if phase == "INIT":
            if self.boot["mode"] == "REPLAY_ALL_CASH":
                if r.ledger.capital_started:
                    raise ContractError("ALREADY_INITIALIZED")
                if not self.boot.get("cash_amount"):
                    raise ContractError("FORWARD_BLOCKED", "ACTUAL_CAPITAL_NOT_PROVIDED")
            elif not r.ledger.capital_started:
                raise ContractError("FORWARD_BLOCKED", "已有持仓账户须先导入人工核对的 BOOTSTRAP_ACCOUNT（JSON 行）")
        r.phase_key, r.k, r._qc = f"{d.isoformat()}/{phase}", 0, {}
        r.checklist, r.intents = [], []
        r.now = r._phase_start(d, phase)
        getattr(r, "_phase_" + phase.lower())(d)
        r.emit("CURSOR", {"date": d.isoformat(), "phase": phase, "halt": False}, r._phase_time(d, phase))
        self.journal.write_snapshot(r.ledger)
        out = {"status": "OK", "phase": phase, "date": d.isoformat(), "checklist": r.checklist,
               "final_intents": r.intents}
        if phase == "INIT":
            out["startup"] = {k: (str(v) if not isinstance(v, (str, int, type(None), dict)) else v)
                              for k, v in r.startup.items() if k != "quantities"}
        return out
