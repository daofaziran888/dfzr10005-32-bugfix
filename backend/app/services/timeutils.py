"""时间解析与格式化工具。

基线承诺时间在示例数据、接口入参和历史版本里存在多种写法（只有时分、日期+时分、
ISO 8601、带秒等）。这里统一解析，并按原字段的风格回写，避免出现“错位承诺时间”。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")
_DATE_ONLY_RE = re.compile(r"^\d{4}([-/])\d{1,2}([-/])\d{1,2}$")
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")

_DATETIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d",
)

_DEFAULT_LEAD_MINUTES = 30


@dataclass(frozen=True)
class TimeStyle:
    """记录一个时间字符串的展示风格，回写时尽量保持原样。"""

    kind: str = "datetime"  # datetime / time / date
    seconds: bool = False
    separator: str = " "
    date_separator: str = "-"

    @property
    def has_date(self) -> bool:
        return self.kind in {"datetime", "date"}


def detect_style(value: object, fallback: TimeStyle | None = None) -> TimeStyle:
    text = str(value or "").strip()
    if not text:
        return fallback or TimeStyle()
    if _TIME_RE.match(text):
        return TimeStyle(kind="time", seconds=bool(_TIME_RE.match(text).group(3)))
    if _DATE_ONLY_RE.match(text):
        date_sep = _DATE_ONLY_RE.match(text).group(1)
        return TimeStyle(kind="date", date_separator=date_sep)
    date_sep = "/" if "/" in text.split("T")[0].split(" ")[0] else "-"
    return TimeStyle(
        kind="datetime",
        seconds=text.count(":") >= 2,
        separator="T" if "T" in text.upper() else " ",
        date_separator=date_sep,
    )


def parse_datetime(value: object, reference: object | datetime | None = None) -> datetime | None:
    """把各种既有时间写法解析成 datetime；无法解析时返回 None。

    - ``18:00`` / ``20:00:00``：只给时分时，日期取 reference（通常是原承诺时间）的日期。
    - ``2026-09-03``：按当天 00:00 处理。
    - ``2026-09-03T18:00:00+08:00``：带时区时转成 naive，避免与库里的 naive 时间比较报错。
    """
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    text = str(value or "").strip()
    if not text:
        return None

    time_match = _TIME_RE.match(text)
    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2))
        second = int(time_match.group(3) or 0)
        base = _reference_datetime(reference)
        return base.replace(hour=hour, minute=minute, second=second, microsecond=0)

    # 先尝试标准格式；带时区的 ISO 用 fromisoformat 兜底。
    for fmt in _DATETIME_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        # 业务时间以墙上时间比较，带时区时直接去掉偏移，避免服务器时区导致整体错位。
        parsed = parsed.replace(tzinfo=None)
    return parsed


def _reference_datetime(reference: object | datetime | None) -> datetime:
    if isinstance(reference, datetime):
        return reference
    parsed = parse_datetime(reference)
    return parsed or datetime.now().replace(second=0, microsecond=0)


def format_datetime(value: datetime, style: TimeStyle) -> str:
    date_part = value.strftime("%Y-%m-%d").replace("-", style.date_separator)
    if style.kind == "time":
        if style.seconds:
            return value.strftime("%H:%M:%S")
        return value.strftime("%H:%M")
    if style.kind == "date":
        return date_part
    time_part = "%H:%M:%S" if style.seconds else "%H:%M"
    return f"{date_part}{style.separator}{value.strftime(time_part)}"


def normalize(value: object, reference: object | None = None, *, force_style: TimeStyle | None = None) -> str:
    """解析并按目标风格输出；解析失败时退回原始文本，保证兼容既有脏数据。"""
    parsed = parse_datetime(value, reference)
    if parsed is None:
        return str(value or "").strip()
    style = force_style or detect_style(value, detect_style(reference))
    return format_datetime(parsed, style)


def resolve_output_style(new_value: object, existing_value: object) -> TimeStyle:
    """新值带日期时以新值为准；只给时分时沿用原字段的展示风格。"""
    new_style = detect_style(new_value)
    if new_style.kind in {"datetime", "date"}:
        return new_style
    return detect_style(existing_value, new_style)


def parse_minutes(value: object, default: int = _DEFAULT_LEAD_MINUTES) -> int:
    """解析 30 / "30分钟" / "1小时" / "PT30M" 之类的窗口长度。"""
    if value is None or value == "":
        return default
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    iso_match = re.search(r"PT(?:(\d+)H)?(?:(\d+)M)?", text, re.IGNORECASE)
    if iso_match and (iso_match.group(1) or iso_match.group(2)):
        return int(iso_match.group(1) or 0) * 60 + int(iso_match.group(2) or 0)
    number = _NUMBER_RE.search(text)
    if not number:
        return default
    amount = float(number.group(0))
    if "小时" in text or re.search(r"\bh\b", text, re.IGNORECASE):
        amount *= 60
    return int(amount)


def add_minutes(value: object, minutes: int, reference: object | None = None) -> str:
    parsed = parse_datetime(value, reference)
    if parsed is None:
        return str(value or "").strip()
    return format_datetime(parsed + timedelta(minutes=minutes), detect_style(value, detect_style(reference)))


def to_timestamp(value: datetime | None = None) -> str:
    return (value or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")


def date_bounds(value: object, *, is_end: bool = False) -> tuple[datetime | None, datetime | None]:
    """把范围条件解释成 [start, end]；只有日期时自动补全天。

    返回 ``(parsed, upper)``：解析失败时 parsed 为 None；当输入只有日期且是结束条件时，
    upper 为当天 23:59:59。
    """
    parsed = parse_datetime(value)
    if parsed is None:
        return None, None
    style = detect_style(value)
    if style.kind == "date" and is_end:
        return parsed, parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
    return parsed, parsed
