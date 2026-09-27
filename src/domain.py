from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


class ErrorKind:
    VALIDATION = "validation"
    NOT_FOUND = "not_found"
    FORBIDDEN = "forbidden"
    CONFLICT = "conflict"


class DomainError(Exception):
    kind = ErrorKind.VALIDATION

    def __init__(self, message):
        super().__init__(message)
        self.message = message


class ValidationError(DomainError):
    kind = ErrorKind.VALIDATION


class NotFoundError(DomainError):
    kind = ErrorKind.NOT_FOUND


class PermissionDenied(DomainError):
    kind = ErrorKind.FORBIDDEN


class ConflictError(DomainError):
    kind = ErrorKind.CONFLICT


# 业务角色
ROLES = ['operator', 'compliance_officer', 'director', 'viewer']

# 治污设施状态
FACILITY_STATES = ['running', 'shutdown']

# 核查状态
# registered  已登记（读数无异常，等待后续观察）
# pending     待复核（停机/校准过期/任一限值超标）
# reviewed    已复核（非重大事件，合规人员复核结论成立）
# remediation 整改中（重大事件：日均超标，等待复测与整改）
# closed      已结案
STATES = ['registered', 'pending', 'reviewed', 'remediation', 'closed']
TERMINAL_STATES = {'reviewed', 'closed'}

# 附随记录类型
RECORD_KINDS = ['note', 'rectification', 'retest']
RECORD_STATUS = ['open', 'closed']

# 复核结论：确认超标 / 瞬时波动（不构成日均超标）/ 数据无效（停机、校准过期）
REVIEW_RESULTS = ['exceedance', 'transient', 'invalid_data']


def require_text(value, field, max_length=2000):
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field}不能为空")
    value = value.strip()
    if len(value) > max_length:
        raise ValidationError(f"{field}不能超过{max_length}个字符")
    return value


def require_optional_text(value, field, max_length=200):
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


def require_choice(value, field, choices):
    if value not in choices:
        raise ValidationError(f"{field}不在允许范围内")
    return value


def require_actor(value):
    return require_text(value, "actor", 100)


def normalize_sampling_time(value) -> str:
    """归一化采样时刻为UTC ISO字符串；无时区按本地输入原样补Z用于演示一致性。"""
    text = require_text(value, "sampling_time", 64).replace('Z', '+00:00')
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise ValidationError("sampling_time必须是ISO 8601时间")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def normalize_calibrated_until(value: Optional[str]) -> Optional[str]:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return normalize_sampling_time(value)


def ensure_role(role, allowed) -> None:
    if role not in allowed:
        raise PermissionDenied("当前角色无权执行该操作")
