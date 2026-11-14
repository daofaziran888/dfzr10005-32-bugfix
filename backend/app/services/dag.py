"""任务依赖编排业务规则：状态流转、字段校验与筛选口径都收在这里。

节点清单除了节点自身的字段，还会带上由基线调整同步过来的承诺时间、预警时间、
关键路径版本、最近产出与核对结果，保证 DAG 页看到的风险窗口与基线一致。
"""
from __future__ import annotations

from typing import Any

from app.store import store
from app.services import timeutils

MODULE = "dag"
REQUIRED_FIELDS = ["节点编号", "节点名称", "上游节点"]
STATUS_ORDER = ["草稿", "运行中", "已暂停", "已完成"]
ACTION_RULES = {"启动节点": "草稿", "暂停节点": "运行中", "归档节点": "已暂停"}
NEGATIVE_ACTIONS = []

SYNC_FIELDS = [
    "基线编号",
    "承诺时间",
    "预警时间",
    "风险窗口",
    "关键路径版本",
    "最近产出",
    "核对结果",
    "更新时间",
]


class DagService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        baseline: str | None = None,
        page: int = 1,
        size: int = 20,
        commitment_start: str | None = None,
        commitment_end: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = [self._hydrate(row) for row in store.rows(MODULE)]
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("节点编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if baseline:
            rows = [row for row in rows if row.get("基线编号") == baseline]
        start_at, end_at = self._range(commitment_start, commitment_end)
        if start_at or end_at:
            rows = [row for row in rows if self._in_range(row.get("承诺时间"), start_at, end_at)]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = store.find(MODULE, entry_id)
        return self._hydrate(row) if row is not None else None

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        for field in SYNC_FIELDS:
            if values.get(field) not in (None, ""):
                entry[field] = values.get(field)
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return self._hydrate(entry), []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"依赖图节点 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于任务依赖编排可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return self._hydrate(entry), f"依赖图节点已{action}"

    def export_entries(
        self,
        *,
        commitment_start: str | None = None,
        commitment_end: str | None = None,
    ) -> list[dict[str, Any]]:
        rows, _ = self.list_entries(
            page=1,
            size=100000,
            commitment_start=commitment_start,
            commitment_end=commitment_end,
        )
        return rows

    def _hydrate(self, row: dict[str, Any]) -> dict[str, Any]:
        row.setdefault("核对结果", "待核对")
        if not row.get("风险窗口") and row.get("承诺时间"):
            warning = row.get("预警时间")
            if warning:
                row["风险窗口"] = f"{warning} ~ {row['承诺时间']}"
        return row

    def _range(
        self, start: str | None, end: str | None
    ) -> tuple[Any, Any]:
        start_at, _ = timeutils.date_bounds(start) if start else (None, None)
        _, end_at = timeutils.date_bounds(end, is_end=True) if end else (None, None)
        return start_at, end_at

    def _in_range(self, value: Any, start: Any, end: Any) -> bool:
        parsed = timeutils.parse_datetime(value)
        if parsed is None:
            return False
        if start and parsed < start:
            return False
        if end and parsed > end:
            return False
        return True
