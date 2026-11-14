"""SLA告警管理业务规则：状态流转、字段校验与筛选口径都收在这里。

SLA 规则的承诺时间、预警时间、风险窗口与最晚处置时刻由基线调整同步写入；
列表与导出直接读同步后的值，避免相邻业务入口显示旧的预警。
"""
from __future__ import annotations

from typing import Any

from app.store import store
from app.services import timeutils

MODULE = "sla"
REQUIRED_FIELDS = ["规则编号", "监控对象", "产出时间"]
STATUS_ORDER = ["草稿", "监控中", "已触发", "已关闭"]
ACTION_RULES = {"启用监控": "草稿", "处理告警": "监控中", "关闭规则": "已触发"}
NEGATIVE_ACTIONS = []

SYNC_FIELDS = [
    "基线编号",
    "承诺时间",
    "预警时间",
    "风险窗口",
    "关键路径版本",
    "最近产出",
    "最晚处置时刻",
    "核对结果",
    "更新时间",
]


class SlaService:
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
            rows = [row for row in rows if keyword in str(row.get("规则编号", ""))]
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
            return None, f"SLA规则 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于SLA告警管理可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return self._hydrate(entry), f"SLA规则已{action}"

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
        """补齐由基线同步的展示字段；旧数据没有承诺时间时退回产出时间。"""
        if not row.get("承诺时间") and row.get("产出时间"):
            row["承诺时间"] = timeutils.normalize(row.get("产出时间"))
        if not row.get("风险窗口") and row.get("预警时间") and row.get("承诺时间"):
            row["风险窗口"] = f"{row['预警时间']} ~ {row['承诺时间']}"
        row.setdefault("风险窗口", row.get("预警时间"))
        if not row.get("最晚处置时刻") and row.get("承诺时间"):
            row["最晚处置时刻"] = timeutils.add_minutes(
                row.get("承诺时间"), timeutils.parse_minutes(row.get("延迟阈值"), 0)
            )
        row.setdefault("最晚处置时刻", row.get("承诺时间"))
        row.setdefault("核对结果", "待核对")
        return row

    def _range(self, start: str | None, end: str | None) -> tuple[Any, Any]:
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
