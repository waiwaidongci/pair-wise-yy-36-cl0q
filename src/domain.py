from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional


class ErrorKind:
    VALIDATION = "validation"; NOT_FOUND = "not_found"
    FORBIDDEN = "forbidden"; CONFLICT = "conflict"


class DomainError(Exception):
    kind = ErrorKind.VALIDATION
    def __init__(self, message):
        super().__init__(message); self.message = message


class ValidationError(DomainError):
    kind = ErrorKind.VALIDATION


class NotFoundError(DomainError):
    kind = ErrorKind.NOT_FOUND


class PermissionDenied(DomainError):
    kind = ErrorKind.FORBIDDEN


class ConflictError(DomainError):
    kind = ErrorKind.CONFLICT


ROLES = ['operator', 'compliance_officer', 'director', 'viewer']
# 治污设施状态：运行中 / 停机
FACILITY_STATES = ['running', 'stopped']
# 核查状态：已登记 / 待复核 / 已复核确认 / 已结案
STATUSES = ['registered', 'pending_review', 'confirmed', 'closed']
# 严重程度：正常 / 关注（停机或校准存疑但未超标）/ 超标 / 重大
SEVERITIES = ['normal', 'watch', 'exceedance', 'major']


@dataclass(frozen=True)
class EmissionCheck:
    id: int
    outfall: str
    sampled_at: str
    instant_concentration: float
    daily_avg_concentration: Optional[float]
    permit_limit_instant: float
    permit_limit_daily: float
    facility_status: str
    calibrated_at: Optional[str]
    note: Optional[str]
    severity: str
    status: str
    version: int
    external_ref: Optional[str]
    created_by: str
    created_at: str
    updated_at: str
    last_corrected_by: Optional[str]
    reviewed_by: Optional[str]
    reviewed_at: Optional[str]
    review_conclusion: Optional[str]
    review_note: Optional[str]
    closed_by: Optional[str]
    closed_at: Optional[str]
    close_note: Optional[str]


@dataclass(frozen=True)
class CheckRecord:
    id: int
    check_id: int
    kind: str
    detail: str
    result: Optional[str]
    status: str
    external_ref: Optional[str]
    created_by: str
    created_at: str


@dataclass(frozen=True)
class ConclusionHistory:
    id: int
    check_id: int
    kind: str
    conclusion: Optional[str]
    note: Optional[str]
    actor: str
    concluded_at: str
    invalidated_reason: str
    invalidated_by: str
    invalidated_at: str


@dataclass(frozen=True)
class AuditEntry:
    id: int; action: str; entity_type: str; entity_id: int; actor: str
    detail: Dict[str, Any]; previous_hash: str; entry_hash: str; created_at: str


def require_text(value, field, max_length=2000):
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field}不能为空")
    value = value.strip()
    if len(value) > max_length:
        raise ValidationError(f"{field}不能超过{max_length}个字符")
    return value


def optional_text(value, field, max_length=2000):
    if value is None:
        return None
    return require_text(value, field, max_length)


def require_number(value, field, minimum=0.0):
    if isinstance(value, bool):
        raise ValidationError(f"{field}必须是数字")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{field}必须是数字")
    if number < minimum:
        raise ValidationError(f"{field}不能小于{minimum}")
    return number


def optional_number(value, field, minimum=0.0):
    if value is None or value == "":
        return None
    return require_number(value, field, minimum)


def parse_datetime(value, field):
    """解析ISO8601时刻；朴素时间按UTC处理，返回UTC datetime。"""
    text = require_text(value, field, 60)
    raw = text.replace("Z", "+00:00").replace("/", "-")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        raise ValidationError(f"{field}必须是ISO8601日期时间，例如2026-09-26T08:00")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso_z(dt: datetime) -> str:
    """统一的UTC存储格式，保证同一时刻文本一致（唯一约束可靠）。"""
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z")


def ensure_role(role, allowed):
    if role not in allowed:
        raise PermissionDenied("当前角色无权执行该操作")
