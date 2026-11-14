"""基线监控管理业务规则：状态流转、承诺时间调整与下游联动都收在这里。

一次“调整基线”会同时完成四件事，并且只在一个锁内落库：

1. 按原预警提前量重算预警时间；
2. 关键路径版本自增，最近产出改为新的承诺时间；
3. 把核对结果同步到关联的 DAG 节点与相邻 SLA 规则；
4. 关闭上一版窗口、追加当前有效版本。

同一基线任意时刻只有一条有效窗口；重复或并发提交相同调整时只会产生一个版本，
后到的请求读到与先到请求相同的终态。
"""
from __future__ import annotations

import re
import threading
from datetime import datetime, timedelta
from typing import Any, Iterable

from app.store import store
from app.services import timeutils

MODULE = "baseline"
REQUIRED_FIELDS = ["基线编号", "基线名称", "承诺时间"]
STATUS_ORDER = ["草稿", "监控中", "已延期", "已关闭"]

# 动作 -> (允许的前置状态, 目标状态)。目标状态为 None 表示原地调整。
ACTION_RULES: dict[str, tuple[str, str | None]] = {
    "启用监控": ("草稿", "监控中"),
    "调整基线": ("监控中", None),
    "关闭基线": ("已延期", "已关闭"),
}
# 允许的延期动作别名，避免把“监控中 -> 已延期”这种正常推进误判成跳级。
DEFER_ACTIONS = {"标记延期", "基线延期", "进入延期", "延期"}
NEGATIVE_ACTIONS = {"标记延期", "基线延期", "进入延期", "延期"}

DEFAULT_WARNING_MINUTES = 30

# 动作别名：兼容前端按英文动作名提交，以及只写“调整/关闭”的简写。
_ACTION_ALIASES = {
    "启用监控": {"启用监控", "启用", "开始监控", "start_monitor", "start", "monitor"},
    "调整基线": {"调整基线", "调整", "调整承诺时间", "adjust", "adjust_baseline", "update", "update_baseline", "reschedule"},
    "关闭基线": {"关闭基线", "关闭", "close", "close_baseline", "archive"},
    "标记延期": {"标记延期", "延期", "基线延期", "delay", "defer", "mark_delayed"},
}

_COMMITMENT_KEYS = (
    "承诺时间",
    "承诺时间(新)",
    "新承诺时间",
    "调整后承诺时间",
    "承诺时间新值",
    "commitment_time",
    "commit_time",
    "new_commitment_time",
    "new_commit_time",
    "promised_at",
    "promise_time",
)
_WARNING_KEYS = ("预警时间", "warning_time", "alert_time")
_WARNING_WINDOW_KEYS = (
    "预警窗口",
    "预警提前分钟",
    "预警提前量",
    "预警提前时间",
    "提前分钟",
    "提前量",
    "warning_minutes",
    "warning_window",
    "warning_lead_minutes",
    "alert_before_minutes",
)
_CRITICAL_PATH_KEYS = ("关键路径", "关键路径节点", "critical_path", "critical_path_node", "dag_node")
_SCOPE_KEYS = ("依赖范围", "关联范围", "dependency_scope")
_RECENT_OUTPUT_KEYS = ("最近产出", "最近产出时间", "latest_output", "recent_output", "last_output")
_EXPECTED_VERSION_KEYS = (
    "expected_version",
    "base_version",
    "version",
    "关键路径版本",
    "版本号",
    "基线版本",
    "当前版本",
)
_REQUEST_KEY_KEYS = (
    "request_id",
    "调整请求号",
    "请求号",
    "幂等键",
    "idempotency_key",
    "request_key",
    "trace_id",
)
_OPERATOR_KEYS = ("operator", "操作人", "调整人", "updated_by")

_STATUS_LOCK = threading.RLock()
_VERSION_LOCK = threading.RLock()
_VERSIONS: dict[int, list[dict[str, Any]]] = {}
_SEEN_REQUESTS: dict[str, dict[str, Any]] = {}
_NEXT_VERSION_ID = 1


def _now() -> str:
    return timeutils.to_timestamp()


def _pick(values: dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in values and values[key] not in (None, ""):
            return values[key]
    return None


def _extract_refs(value: Any) -> set[str]:
    """从关键路径/依赖范围里提取 DAG-0003、SLA-0003 之类的引用。"""
    if value is None:
        return set()
    return {token.upper() for token in re.findall(r"[A-Za-z]{2,}-\d+", str(value))}


def _suffix(number: Any) -> str | None:
    match = re.search(r"(\d+)\s*$", str(number or ""))
    return match.group(1).lstrip("0") or "0" if match else None


def _row_refs(row: dict[str, Any]) -> set[str]:
    refs: set[str] = set()
    for key, value in row.items():
        if key.startswith("_"):
            continue
        refs |= _extract_refs(value)
    return refs


def _version_number(value: Any) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    match = re.search(r"\d+", str(value or ""))
    return int(match.group(0)) if match else 0


def _version_label(number: int) -> str:
    return f"v{number}"


def _parse_lead_minutes(row: dict[str, Any]) -> int:
    window = row.get("预警窗口")
    if window not in (None, ""):
        return max(timeutils.parse_minutes(window, DEFAULT_WARNING_MINUTES), 0)
    commitment = timeutils.parse_datetime(row.get("承诺时间"))
    warning = timeutils.parse_datetime(row.get("预警时间"), commitment)
    if commitment and warning:
        lead = int((commitment - warning).total_seconds() // 60)
        if lead >= 0:
            return lead
    return DEFAULT_WARNING_MINUTES


def _warning_for(commitment: datetime, lead_minutes: int) -> datetime:
    return commitment - timedelta(minutes=lead_minutes)


def _window_text(warning: datetime, commitment: datetime, style: timeutils.TimeStyle) -> str:
    return f"{timeutils.format_datetime(warning, style)} ~ {timeutils.format_datetime(commitment, style)}"


_ACTION_MESSAGES = {
    "启用监控": "基线配置已启用监控",
    "调整基线": "基线配置已调整",
    "关闭基线": "基线配置已关闭",
    "标记延期": "基线配置已标记延期",
}


def _action_message(action: str) -> str:
    return _ACTION_MESSAGES.get(action, f"基线配置已{action}")


def _canonical_action(action: str) -> str:
    text = str(action or "").strip()
    for canonical, aliases in _ACTION_ALIASES.items():
        if text in aliases:
            return canonical
    return text


class BaselineService:
    # ---------- 读取 ----------

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
        commitment_start: str | None = None,
        commitment_end: str | None = None,
        commitment: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = [self._hydrate(row) for row in store.rows(MODULE)]
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("基线编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        start_at, end_at = self._range(commitment, commitment_start, commitment_end)
        if start_at or end_at:
            rows = [row for row in rows if self._in_range(row.get("承诺时间"), start_at, end_at)]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = store.find(MODULE, entry_id)
        if row is None:
            return None
        return self._detail(row)

    def versions(self, entry_id: int) -> list[dict[str, Any]] | None:
        row = store.find(MODULE, entry_id)
        if row is None:
            return None
        self._ensure_initial_version(row)
        with _VERSION_LOCK:
            return [dict(item) for item in _VERSIONS.get(entry_id, [])]

    # ---------- 写入 ----------

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        for optional in ("关键路径", "依赖范围", "最近产出", "预警窗口", "预警时间"):
            if values.get(optional) not in (None, ""):
                entry[optional] = values.get(optional)

        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False

        commitment_text = str(entry.get("承诺时间") or "").strip()
        commitment = timeutils.parse_datetime(commitment_text)
        lead = _parse_lead_minutes(entry)
        if commitment is not None:
            style = timeutils.detect_style(commitment_text)
            entry["承诺时间"] = timeutils.format_datetime(commitment, style)
            if not entry.get("预警时间"):
                entry["预警时间"] = timeutils.format_datetime(
                    _warning_for(commitment, lead), style
                )
            else:
                entry["预警时间"] = timeutils.normalize(
                    entry["预警时间"], commitment, force_style=style
                )
                lead = max(_parse_lead_minutes(entry), 0)
            entry["预警窗口"] = f"{lead}分钟"
            entry["最近产出"] = (
                timeutils.normalize(entry["最近产出"], commitment, force_style=style)
                if entry.get("最近产出")
                else timeutils.format_datetime(commitment, style)
            )
            entry["风险窗口"] = _window_text(
                _warning_for(commitment, lead), commitment, style
            )
        entry["关键路径版本"] = _version_label(1)
        entry["版本号"] = 1
        entry["监控状态"] = entry["status"]
        entry["更新时间"] = _now()
        entry["核对结果"] = "待核对"

        rows.append(entry)
        with _VERSION_LOCK:
            self._append_version(entry, active=True, request_key=None, operator=None)
        return self._detail(entry), []

    def run_action(
        self,
        entry_id: int,
        action: str,
        values: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        values = dict(values or {})
        action = _canonical_action(action)
        if action == "调整基线" or action in DEFER_ACTIONS:
            return self._locked_action(entry_id, action, values)

        with _STATUS_LOCK:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"基线配置 {entry_id} 不存在或已归档"
            allowed, message = self._validate_action(entry, action)
            if not allowed:
                return None, message

            target = ACTION_RULES[action][1]
            if target is not None:
                entry["status"] = target
            entry["监控状态"] = entry["status"]
            entry["pending"] = entry["status"] != STATUS_ORDER[-1]
            entry["abnormal"] = entry["status"] == "已延期"
            entry["更新时间"] = _now()
            if action == "关闭基线":
                entry["关闭时间"] = entry["更新时间"]
                entry["可恢复"] = False
            return self._detail(entry), _action_message(action)

    # ---------- 动作校验 ----------

    def _validate_action(self, entry: dict[str, Any], action: str) -> tuple[bool, str]:
        status = str(entry.get("status") or STATUS_ORDER[0])
        if status == "已关闭":
            return False, "基线配置已关闭，关闭后不能原位恢复，也不能再次调整"
        if action == "调整基线":
            if status != "监控中":
                return False, f"调整基线只对监控中的对象生效，当前状态为{status}"
            return True, ""
        if action in DEFER_ACTIONS:
            if status != "监控中":
                return False, f"延期只允许从监控中进入，当前状态为{status}"
            return True, ""
        if action not in ACTION_RULES:
            return False, f"动作「{action}」不属于基线监控管理可执行范围"
        required, _ = ACTION_RULES[action]
        if status != required:
            return False, f"基线配置当前状态为{status}，不允许执行「{action}」"
        return True, ""

    # ---------- 调整基线（加锁 + 幂等） ----------

    def _locked_action(
        self, entry_id: int, action: str, values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str]:
        # 在锁外先做一次存在性校验，避免把明显无效的请求也串行化。
        if store.find(MODULE, entry_id) is None:
            return None, f"基线配置 {entry_id} 不存在或已归档"

        with _STATUS_LOCK:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"基线配置 {entry_id} 不存在或已归档"
            allowed, message = self._validate_action(entry, action)
            if not allowed:
                return None, message

            self._hydrate(entry)
            # 调整前先固化当前窗口为历史版本，保证旧承诺/预警时间可追溯。
            self._ensure_initial_version(entry)

            if action in DEFER_ACTIONS:
                entry["status"] = "已延期"
                entry["监控状态"] = "已延期"
                entry["pending"] = True
                entry["abnormal"] = True
                entry["更新时间"] = _now()
                return self._detail(entry), "基线配置已标记延期"

            request_key = _pick(values, _REQUEST_KEY_KEYS)
            expected_version = _pick(values, _EXPECTED_VERSION_KEYS)
            current_version = _version_number(entry.get("版本号") or entry.get("关键路径版本"))

            target_text = _pick(values, _COMMITMENT_KEYS)
            target = self._resolve_commitment(entry, target_text)
            if target is None:
                return None, "承诺时间格式无法识别，请使用 YYYY-MM-DD HH:MM 或 HH:MM"

            new_warning = self._resolve_warning(entry, target, values)
            changed = self._will_change(entry, target, new_warning, values)

            if request_key:
                with _VERSION_LOCK:
                    previous_request = _SEEN_REQUESTS.get(str(request_key))
                if previous_request is not None:
                    return self._detail(entry), "基线配置已按同一请求完成调整，已返回当前有效版本"

            # 过期版本号：如果目标值与当前值一致，视为幂等重放；否则明确报冲突。
            if expected_version is not None:
                expected_number = _version_number(expected_version)
                if expected_number and expected_number != current_version:
                    if not changed:
                        return self._detail(entry), "基线配置已是最新终态，无需重复调整"
                    return (
                        None,
                        f"基线配置已被其他请求调整为版本 v{current_version}，"
                        f"当前提交基于 v{expected_number}，请刷新后重试",
                    )

            # 完全相同的并发提交：先落地的那次已经关闭了旧窗口，这里直接返回同一终态。
            recent_replay = self._replay_if_identical(entry, target, new_warning, changed, values)
            if recent_replay is not None:
                return self._detail(entry), recent_replay

            if not changed:
                if request_key:
                    self._remember_request(str(request_key), entry_id)
                return self._detail(entry), "基线配置与当前有效窗口一致，未生成新版本"

            self._apply_adjustment(entry, target, new_warning, values, str(request_key or ""), current_version)
            if request_key:
                self._remember_request(str(request_key), entry_id)
            return self._detail(entry), "基线配置已调整，预警窗口与关联节点已同步"

    def _replay_if_identical(
        self,
        entry: dict[str, Any],
        target: datetime,
        warning: datetime,
        changed: bool,
        values: dict[str, Any],
    ) -> str | None:
        """无请求号时识别重复提交：最近一次成功调整的窗口与本次目标相同则视为重放。"""
        if changed:
            return None
        with _VERSION_LOCK:
            versions = _VERSIONS.get(int(entry.get("id", 0)), [])
            if len(versions) < 2:
                return None
            active = next((item for item in versions if item.get("是否有效")), None)
        if active is None:
            return None
        if (
            timeutils.parse_datetime(active.get("承诺时间")) == target
            and timeutils.parse_datetime(active.get("预警时间"), target) == warning
        ):
            entry["核对结果"] = self._verify(entry)
            return "基线配置已是最新有效版本，重复提交未生成新窗口"
        return None

    def _resolve_commitment(self, entry: dict[str, Any], value: Any) -> datetime | None:
        if value in (None, ""):
            return timeutils.parse_datetime(entry.get("承诺时间"))
        return timeutils.parse_datetime(value, entry.get("承诺时间"))

    def _resolve_warning(
        self, entry: dict[str, Any], target: datetime, values: dict[str, Any]
    ) -> datetime:
        window_value = _pick(values, _WARNING_WINDOW_KEYS)
        if window_value is not None:
            lead = max(timeutils.parse_minutes(window_value), 0)
            return _warning_for(target, lead)
        warning_value = _pick(values, _WARNING_KEYS)
        if warning_value is not None:
            warning = timeutils.parse_datetime(warning_value, target)
            if warning is not None:
                return warning
        return _warning_for(target, _parse_lead_minutes(entry))

    def _will_change(
        self,
        entry: dict[str, Any],
        target: datetime,
        warning: datetime,
        values: dict[str, Any],
    ) -> bool:
        current_commitment = timeutils.parse_datetime(entry.get("承诺时间"))
        current_warning = timeutils.parse_datetime(entry.get("预警时间"), current_commitment)
        if current_commitment != target or current_warning != warning:
            return True
        for keys, field in (
            (_CRITICAL_PATH_KEYS, "关键路径"),
            (_SCOPE_KEYS, "依赖范围"),
            (_RECENT_OUTPUT_KEYS, "最近产出"),
        ):
            value = _pick(values, keys)
            if value is not None and str(value) != str(entry.get(field) or ""):
                return True
        return False

    def _apply_adjustment(
        self,
        entry: dict[str, Any],
        target: datetime,
        warning: datetime,
        values: dict[str, Any],
        request_key: str,
        current_version: int,
    ) -> None:
        previous = dict(entry)
        commitment_style = timeutils.resolve_output_style(
            _pick(values, _COMMITMENT_KEYS) or entry.get("承诺时间"),
            entry.get("承诺时间"),
        )
        # 预警时间只给时分时沿用承诺时间的展示风格，避免同一行出现两种分隔符。
        new_warning_value = _pick(values, _WARNING_KEYS)
        if new_warning_value is None:
            warning_style = commitment_style
        else:
            warning_style = timeutils.resolve_output_style(
                new_warning_value, entry.get("预警时间")
            )
            if timeutils.detect_style(new_warning_value).kind == "time":
                warning_style = commitment_style

        lead = max(int((target - warning).total_seconds() // 60), 0)
        new_version = current_version + 1

        entry["承诺时间"] = timeutils.format_datetime(target, commitment_style)
        entry["预警时间"] = timeutils.format_datetime(warning, warning_style)
        entry["预警窗口"] = f"{lead}分钟"
        entry["风险窗口"] = _window_text(warning, target, commitment_style)

        critical_path = _pick(values, _CRITICAL_PATH_KEYS)
        if critical_path is not None:
            entry["关键路径"] = critical_path
        scope = _pick(values, _SCOPE_KEYS)
        if scope is not None:
            entry["依赖范围"] = scope
        recent_output = _pick(values, _RECENT_OUTPUT_KEYS)
        entry["最近产出"] = (
            timeutils.normalize(recent_output, target, force_style=commitment_style)
            if recent_output is not None
            else timeutils.format_datetime(target, commitment_style)
        )

        entry["关键路径版本"] = _version_label(new_version)
        entry["版本号"] = new_version
        entry["监控状态"] = entry.get("status") or "监控中"
        entry["更新时间"] = _now()
        entry["最近调整时间"] = entry["更新时间"]
        entry["核对结果"] = "已核对"

        operator = _pick(values, _OPERATOR_KEYS)
        if operator is not None:
            entry["操作人"] = operator

        with _VERSION_LOCK:
            self._close_versions(entry["id"])
            version = self._append_version(
                entry, active=True, request_key=request_key or None, operator=operator
            )
            version["是否有效"] = True
        self._propagate(entry, previous)
        entry["核对结果"] = self._verify(entry)

    # ---------- 版本台账 ----------

    def _append_version(
        self,
        entry: dict[str, Any],
        *,
        active: bool,
        request_key: str | None,
        operator: Any,
    ) -> dict[str, Any]:
        global _NEXT_VERSION_ID
        entry_id = int(entry.get("id", 0))
        version = {
            "版本ID": _NEXT_VERSION_ID,
            "版本号": entry.get("版本号", 1),
            "承诺时间": entry.get("承诺时间"),
            "预警时间": entry.get("预警时间"),
            "关键路径": entry.get("关键路径"),
            "关键路径版本": entry.get("关键路径版本"),
            "最近产出": entry.get("最近产出"),
            "风险窗口": entry.get("风险窗口"),
            "监控状态": entry.get("status"),
            "是否有效": bool(active),
            "生效开始": entry.get("更新时间") or _now(),
            "生效结束": None,
            "操作人": operator,
            "请求号": request_key,
        }
        _NEXT_VERSION_ID += 1
        _VERSIONS.setdefault(entry_id, []).append(version)
        return dict(version)

    def _close_versions(self, entry_id: int) -> None:
        now = _now()
        with _VERSION_LOCK:
            for version in _VERSIONS.get(entry_id, []):
                if version.get("是否有效"):
                    version["是否有效"] = False
                    version["生效结束"] = now

    def _remember_request(self, request_key: str, entry_id: int) -> None:
        with _VERSION_LOCK:
            _SEEN_REQUESTS[request_key] = {"entry_id": entry_id, "at": _now()}

    # ---------- 下游联动 ----------

    def _propagate(self, entry: dict[str, Any], previous: dict[str, Any]) -> None:
        dag_rows = self._related_rows("dag", "节点编号", entry)
        sla_rows = self._related_rows("sla", "规则编号", entry, related=dag_rows)

        style = timeutils.detect_style(entry.get("承诺时间"))
        warning_style = timeutils.detect_style(entry.get("预警时间"), style)
        commitment_text = entry.get("承诺时间")
        warning_text = entry.get("预警时间")
        version = entry.get("关键路径版本")
        result = "已核对"
        now = _now()

        for row in dag_rows:
            row["基线编号"] = entry.get("基线编号")
            row["承诺时间"] = commitment_text
            row["预警时间"] = warning_text
            row["风险窗口"] = entry.get("风险窗口")
            row["关键路径版本"] = version
            row["最近产出"] = entry.get("最近产出")
            row["核对结果"] = result
            row["更新时间"] = now

        for row in sla_rows:
            row["基线编号"] = entry.get("基线编号")
            row["承诺时间"] = commitment_text
            row["预警时间"] = warning_text
            row["产出时间"] = commitment_text
            row["风险窗口"] = entry.get("风险窗口")
            row["关键路径版本"] = version
            row["最近产出"] = entry.get("最近产出")
            row["最晚处置时刻"] = self._latest_handling(row, entry)
            row["核对结果"] = result
            row["更新时间"] = now

    def _latest_handling(self, rule: dict[str, Any], entry: dict[str, Any]) -> str:
        commitment = timeutils.parse_datetime(entry.get("承诺时间"))
        if commitment is None:
            return str(entry.get("承诺时间") or "")
        delay = timeutils.parse_minutes(rule.get("延迟阈值"), 0)
        style = timeutils.detect_style(entry.get("承诺时间"))
        return timeutils.format_datetime(commitment + timedelta(minutes=delay), style)

    def _related_rows(
        self,
        module: str,
        number_field: str,
        entry: dict[str, Any],
        related: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """按显式引用、基线编号、数字后缀、监控对象四层关系找下游记录。"""
        rows = store.rows(module)
        baseline_number = str(entry.get("基线编号") or "")
        refs = set()
        for key in ("关键路径", "依赖范围", "关联节点", "关联规则", "关联SLA", "下游节点", "监控对象"):
            refs |= _extract_refs(entry.get(key))
        if related:
            for row in related:
                refs |= _row_refs(row)

        suffix = _suffix(baseline_number)
        matched: list[dict[str, Any]] = []
        for row in rows:
            if row.get("基线编号") == baseline_number:
                matched.append(row)
                continue
            if _row_refs(row) & refs:
                matched.append(row)
                continue
            if suffix and _suffix(row.get(number_field)) == suffix:
                matched.append(row)
                continue
            if baseline_number and baseline_number in " ".join(
                str(value) for value in row.values()
            ):
                matched.append(row)
        return matched

    def _verify(self, entry: dict[str, Any]) -> str:
        commitment = timeutils.parse_datetime(entry.get("承诺时间"))
        warning = timeutils.parse_datetime(entry.get("预警时间"), commitment)
        if commitment is None or warning is None:
            return "时间格式待核对"
        if warning > commitment:
            return "预警时间晚于承诺时间"
        return "已核对"

    # ---------- 展示口径 ----------

    def _detail(self, row: dict[str, Any]) -> dict[str, Any]:
        detail = self._hydrate(row)
        detail["监控状态"] = detail.get("status")
        self._ensure_initial_version(row)
        with _VERSION_LOCK:
            detail["版本历史"] = [dict(item) for item in _VERSIONS.get(int(row.get("id", 0)), [])]
        detail["history"] = detail["版本历史"]
        return detail

    def _ensure_initial_version(self, row: dict[str, Any]) -> None:
        """给示例数据补一条 v1 快照，保留它最初的承诺/预警窗口。"""
        entry_id = int(row.get("id", 0))
        with _VERSION_LOCK:
            if entry_id in _VERSIONS:
                return
        current = dict(row)
        self._hydrate(current)
        with _VERSION_LOCK:
            if entry_id in _VERSIONS:
                return
            self._append_version(current, active=True, request_key=None, operator=None)

    def _hydrate(self, row: dict[str, Any]) -> dict[str, Any]:
        """补齐旧数据缺失的展示字段，并统一时间写法；不覆盖业务原始值。"""
        row["监控状态"] = row.get("status", row.get("监控状态"))
        version = row.get("版本号") or _version_number(row.get("关键路径版本")) or 1
        row["版本号"] = version
        row["关键路径版本"] = row.get("关键路径版本") or _version_label(version)

        commitment_text = row.get("承诺时间")
        commitment = timeutils.parse_datetime(commitment_text)
        if commitment is not None:
            style = timeutils.detect_style(commitment_text)
            row["承诺时间"] = timeutils.format_datetime(commitment, style)
            lead = _parse_lead_minutes(row)
            row["预警窗口"] = row.get("预警窗口") or f"{lead}分钟"
            warning_text = row.get("预警时间")
            warning = timeutils.parse_datetime(warning_text, commitment)
            if warning is None:
                warning = _warning_for(commitment, lead)
                row["预警时间"] = timeutils.format_datetime(warning, style)
            else:
                warning_style = timeutils.detect_style(warning_text, style)
                row["预警时间"] = timeutils.format_datetime(warning, warning_style)
            row["风险窗口"] = row.get("风险窗口") or _window_text(warning, commitment, style)
            if not row.get("最近产出") or timeutils.parse_datetime(row.get("最近产出")) is None:
                row["最近产出"] = timeutils.format_datetime(commitment, style)
        return row

    # ---------- 导出 ----------

    def export_entries(
        self,
        *,
        commitment_start: str | None = None,
        commitment_end: str | None = None,
        commitment: str | None = None,
        version: str = "current",
    ) -> list[dict[str, Any]]:
        rows, _ = self.list_entries(
            page=1,
            size=100000,
            commitment_start=commitment_start,
            commitment_end=commitment_end,
            commitment=commitment,
        )
        exported: list[dict[str, Any]] = []
        for row in rows:
            item = {
                "基线编号": row.get("基线编号"),
                "基线名称": row.get("基线名称"),
                "承诺时间": row.get("承诺时间"),
                "预警时间": row.get("预警时间"),
                "预警窗口": row.get("预警窗口"),
                "风险窗口": row.get("风险窗口"),
                "关键路径": row.get("关键路径"),
                "关键路径版本": row.get("关键路径版本"),
                "依赖范围": row.get("依赖范围"),
                "监控状态": row.get("监控状态"),
                "最近产出": row.get("最近产出"),
                "版本号": row.get("版本号"),
                "核对结果": row.get("核对结果"),
                "更新时间": row.get("更新时间"),
            }
            if version == "all":
                item["版本历史"] = [
                    dict(history) for history in _VERSIONS.get(int(row.get("id", 0)), [])
                ]
            exported.append(item)
        return exported

    # ---------- 过滤辅助 ----------

    def _range(
        self,
        commitment: str | None,
        start: str | None,
        end: str | None,
    ) -> tuple[datetime | None, datetime | None]:
        if commitment:
            if "~" in commitment:
                left, _, right = commitment.partition("~")
                start = start or left
                end = end or right
            elif "," in commitment:
                left, _, right = commitment.partition(",")
                start = start or left
                end = end or right
            else:
                start = start or commitment
                end = end or commitment
        start_at, _ = timeutils.date_bounds(start) if start else (None, None)
        _, end_at = timeutils.date_bounds(end, is_end=True) if end else (None, None)
        return start_at, end_at

    def _in_range(self, value: Any, start: datetime | None, end: datetime | None) -> bool:
        parsed = timeutils.parse_datetime(value)
        if parsed is None:
            return False
        if start and parsed < start:
            return False
        if end and parsed > end:
            return False
        return True
