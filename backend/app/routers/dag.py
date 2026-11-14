"""任务依赖编排接口：维护 DAG 节点，并展示由基线同步过来的风险窗口。"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.dag import DagService

router = APIRouter(prefix="/api/dag", tags=["任务依赖编排"])

service = DagService()

LIST_FIELDS = ["节点编号", "节点名称", "上游节点", "下游节点", "执行条件", "超时分钟", "失败策略", "节点状态", "基线编号", "承诺时间", "预警时间", "风险窗口", "关键路径版本", "最近产出", "核对结果"]
STATUSES = ["草稿", "运行中", "已暂停", "已完成"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按节点编号检索"),
    status: str | None = Query(default=None, description="草稿、运行中、已暂停、已完成"),
    baseline: str | None = Query(default=None, description="按基线编号过滤"),
    page: int = 1,
    size: int = 20,
    commitment_start: str | None = Query(default=None, alias="commitment_start"),
    commitment_end: str | None = Query(default=None, alias="commitment_end"),
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
) -> PageResult[dict]:
    """按节点编号、状态、基线编号与承诺时间范围过滤；节点清单带同步后的风险窗口。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(
        keyword=keyword,
        status=status,
        baseline=baseline,
        page=page,
        size=size,
        commitment_start=commitment_start or start,
        commitment_end=commitment_end or end,
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries(
    format: str = Query(default="json", description="json 或 csv"),
    commitment_start: str | None = Query(default=None, alias="commitment_start"),
    commitment_end: str | None = Query(default=None, alias="commitment_end"),
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
    download: bool = Query(default=False),
) -> Any:
    """导出节点清单；承诺时间字段已随基线调整更新。"""
    items = service.export_entries(
        commitment_start=commitment_start or start,
        commitment_end=commitment_end or end,
    )
    if format.lower() == "csv" or download:
        return _csv_response(items, filename="dag-export.csv")
    return {"module": "dag", "total": len(items), "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"依赖图节点 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="依赖图节点已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


def _csv_response(items: list[dict[str, Any]], *, filename: str) -> Response:
    columns = LIST_FIELDS
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for item in items:
        writer.writerow({key: "" if item.get(key) is None else str(item.get(key)) for key in columns})
    return Response(
        content=("﻿" + buffer.getvalue()).encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
