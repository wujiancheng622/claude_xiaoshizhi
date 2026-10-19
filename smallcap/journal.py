"""单写者顺序 JSONL 事务日志、可重建快照与恢复（D13、H07.2）。

事件先计算和校验，单行写入并 flush/fsync 后才算已提交；快照写临时文件后原子 rename，
只缓存已提交 seq。恢复以日志为准：快照落后则重放，快照超前/序号断裂/内容冲突则停止增风险。
崩溃截断的尾行不视作已提交，保留损坏片段后截去；不删除任何完整已提交事件。
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path

from .account import Ledger, canon
from .contracts import ContractError


class Journal:
    def __init__(self, run_dir: str | Path):
        self.dir = Path(run_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.dir / "events.jsonl"
        self.snapshot_path = self.dir / "snapshot.json"
        self.lock_path = self.dir / "writer.lock"
        self._lock_fd = None
        self.last_seq = 0
        self.ids: dict[str, str] = {}

    # ---------------------------------------------------------------- 单写者锁
    def acquire(self) -> None:
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise ContractError("WRITER_LOCKED", str(self.dir)) from None
        self._lock_fd = fd
        # 取得写锁后先处理崩溃截断的尾行，否则后续追加会与半行拼成损坏行
        self.read_committed()

    def release(self) -> None:
        if self._lock_fd is not None:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
            self._lock_fd = None

    # ---------------------------------------------------------------- 读取
    def read_committed(self) -> list[dict]:
        """读取全部完整已提交事件；持有写锁时把截断尾行另存为 .torn 并从日志截去。"""
        if not self.events_path.exists():
            return []
        raw = self.events_path.read_bytes()
        events, good_len, seq = [], 0, 0
        pos = 0
        while pos < len(raw):
            nl = raw.find(b"\n", pos)
            if nl < 0:
                break                               # 无换行结尾：未完整提交
            line = raw[pos:nl]
            try:
                ev = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ContractError("JOURNAL_CORRUPT", f"complete line at byte {pos} unreadable") from None
            if ev.get("seq") != seq + 1:
                raise ContractError("JOURNAL_SEQ_BROKEN", f"expected {seq + 1}, got {ev.get('seq')}")
            seq = ev["seq"]
            events.append(ev)
            pos = nl + 1
            good_len = pos
        if good_len < len(raw) and self._lock_fd is not None:
            # 只有持有单写者锁时才截去未完整尾行（先保留损坏片段）；只读进程不修改日志
            torn = self.dir / f"events.torn.{len(raw)}"
            torn.write_bytes(raw[good_len:])
            with open(self.events_path, "r+b") as f:
                f.truncate(good_len)
                f.flush()
                os.fsync(f.fileno())
        self.last_seq = seq
        self.ids = {e["event_id"]: canon({"kind": e["kind"], "payload": e["payload"]}) for e in events}
        return events

    # ---------------------------------------------------------------- 写入
    def commit(self, ev: dict) -> dict | None:
        """持久化一个事件；同ID同内容返回 None（已提交），同ID异内容硬冲突。"""
        if self._lock_fd is None:
            raise ContractError("WRITER_LOCK_REQUIRED")
        content = canon({"kind": ev["kind"], "payload": ev["payload"]})
        if ev["event_id"] in self.ids:
            if self.ids[ev["event_id"]] != content:
                raise ContractError("EVENT_CONFLICT", ev["event_id"])
            return None
        rec = {"seq": self.last_seq + 1, "event_id": ev["event_id"], "kind": ev["kind"],
               "occurred_at": ev.get("occurred_at"), "known_at": ev.get("known_at"),
               "payload": ev["payload"], "input_refs": ev.get("input_refs", [])}
        line = (json.dumps(rec, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        with open(self.events_path, "ab") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
        self.last_seq = rec["seq"]
        self.ids[ev["event_id"]] = content
        return rec

    def write_snapshot(self, ledger: Ledger) -> None:
        snap = ledger.snapshot()
        if snap["seq"] > self.last_seq:
            raise ContractError("SNAPSHOT_AHEAD_OF_JOURNAL")
        tmp = self.snapshot_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(snap, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        with open(tmp, "rb") as f:
            os.fsync(f.fileno())
        os.replace(tmp, self.snapshot_path)


def recover(journal: Journal, account_id: str, fee_schedule=None, etf_ids=(), use_snapshot: bool = True) -> Ledger:
    """以日志为准恢复：快照可用则从其后重放；快照超前/冲突则拒绝直接使用快照。"""
    events = journal.read_committed()
    ledger = None
    if use_snapshot and journal.snapshot_path.exists():
        snap = json.loads(journal.snapshot_path.read_text(encoding="utf-8"))
        if snap["seq"] > journal.last_seq:
            raise ContractError("SNAPSHOT_AHEAD_OF_JOURNAL", f"snapshot {snap['seq']} > journal {journal.last_seq}")
        ledger = Ledger.restore(snap, fee_schedule, etf_ids)
        # 快照中已见事件须与日志前缀逐一一致
        for ev in events[:snap["seq"]]:
            if ledger.seen.get(ev["event_id"]) != canon({"kind": ev["kind"], "payload": ev["payload"]}):
                raise ContractError("SNAPSHOT_JOURNAL_MISMATCH", ev["event_id"])
        tail = events[snap["seq"]:]
    else:
        ledger = Ledger(account_id, fee_schedule, etf_ids)
        tail = events
    for ev in tail:
        ledger.apply(ev)
    return ledger
