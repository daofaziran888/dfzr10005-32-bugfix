"""基线监控管理接口：维护基线配置，覆盖启用监控、调整基线、关闭基线等动作。"""
from __future__ import annotations

import csv
import io
import json
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from app.schemas import ActionPayload, ActionResult, EntryPayload, PageResult
from app.services.baseline import BaselineService

router = APIRouter(prefix="/api/baseline", tags=["基线监控管理"])

service = BaselineService()

LIST_FIELDS = ["基线编号", "基线名称", "承诺时间", "预警时间", "预警窗口", "风险窗口", "关键路径", "关键路径版本", "依赖范围", "监控状态", "最近产出", "核对结果"]
STATUSES = ["草稿", "监控中", "已延期", "已关闭"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按基线编号检索"),
    status: str | None = Query(default=None, description="草稿、监控中、已延期、已关闭"),
    page: int = 1,
    size: int = 20,
    commitment_start: str | None = Query(
        default=None, alias="commitment_start", description="承诺时间范围起点"
    ),
    commitment_end: str | None = Query(
        default=None, alias="commitment_end", description="承诺时间范围终点"
    ),
    commitment: str | None = Query(
        default=None,
        alias="承诺时间范围",
        description="承诺时间范围，支持 2026-09-03 或 2026-09-01~2026-09-30",
    ),
    start: str | None = Query(default=None, description="承诺时间范围起点（简写）"),
    end: str | None = Query(default=None, description="承诺时间范围终点（简写）"),
    start_time: str | None = Query(default=None, alias="start_time"),
    end_time: str | None = Query(default=None, alias="end_time"),
    commitment_from: str | None = Query(default=None, alias="承诺时间起"),
    commitment_to: str | None = Query(default=None, alias="承诺时间止"),
) -> PageResult[dict]:
    """按基线编号、状态与承诺时间范围过滤列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(
        keyword=keyword,
        status=status,
        page=page,
        size=size,
        commitment_start=commitment_start or start or start_time or commitment_from,
        commitment_end=commitment_end or end or end_time or commitment_to,
        commitment=commitment,
    )
    return PageResult(items=items, total=total, page=page, size=size)


# 导出必须注册在 /{entry_id} 之前，否则 "export" 会被当成整数路径参数而返回 422。
@router.get("/export")
def export_entries(
    format: str = Query(default="json", description="json 或 csv"),
    commitment_start: str | None = Query(default=None, alias="commitment_start"),
    commitment_end: str | None = Query(default=None, alias="commitment_end"),
    commitment: str | None = Query(default=None, alias="承诺时间范围"),
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
    start_time: str | None = Query(default=None, alias="start_time"),
    end_time: str | None = Query(default=None, alias="end_time"),
    commitment_from: str | None = Query(default=None, alias="承诺时间起"),
    commitment_to: str | None = Query(default=None, alias="承诺时间止"),
    version: str = Query(default="current", description="current 只导出当前版本，all 附带历史版本"),
    download: bool = Query(default=False, description="true 时以附件形式下载文件"),
) -> Any:
    """导出基线清单：范围与承诺时间范围一致，当前版本不会混入历史旧值。"""
    items = service.export_entries(
        commitment_start=commitment_start or start or start_time or commitment_from,
        commitment_end=commitment_end or end or end_time or commitment_to,
        commitment=commitment,
        version=version,
    )
    if format.lower() == "csv" or download:
        return _csv_response(items, filename="baseline-export.csv")
    return {"module": "baseline", "total": len(items), "items": items}


# 版本历史同样要早于 /{entry_id} 注册。
@router.get("/{entry_id}/versions", response_model=dict)
def get_versions(entry_id: int) -> dict:
    """读取基线的全部调整版本；历史版本保留各自原来的承诺/预警窗口。"""
    versions = service.versions(entry_id)
    if versions is None:
        raise HTTPException(status_code=404, detail=f"基线配置 {entry_id} 不存在或已归档")
    current = next((item for item in versions if item.get("是否有效")), None)
    return {"total": len(versions), "current": current, "items": versions}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条基线配置明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"基线配置 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条基线配置，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="基线配置已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    """执行启用监控、调整基线、关闭基线；越级、关闭后恢复与并发重复提交都会被拦下并说明原因。"""
    action = str(payload.fields().get("action") or "").strip()
    entry, message = service.run_action(entry_id, action, payload.fields())
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


def _csv_response(items: list[dict[str, Any]], *, filename: str) -> Response:
    columns = [
        "基线编号", "基线名称", "承诺时间", "预警时间", "预警窗口", "风险窗口",
        "关键路径", "关键路径版本", "依赖范围", "监控状态", "最近产出", "版本号",
        "核对结果", "更新时间",
    ]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for item in items:
        writer.writerow({key: _cell(item.get(key)) for key in columns})
    # 带 BOM，Excel 打开中文列名不乱码。
    body = "﻿" + buffer.getvalue()
    return Response(
        content=body.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _cell(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return "" if value is None else str(value)
