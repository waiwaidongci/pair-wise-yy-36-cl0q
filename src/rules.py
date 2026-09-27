"""排放核查台业务规则层：只做纯判定，不接触存储与HTTP。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .domain import (FACILITY_STATES, SEVERITIES, STATUSES, ValidationError)

TITLE = '排放核查台'
ENTITY = '排放核查'

# 校准有效期：采样时刻距离最近一次校准超过该时长即视为校准过期
CALIBRATION_VALID_DAYS = 30
# 达到许可限值的该倍数（含）定性为重大事件
MAJOR_RATIO = 3.0

# 阻断原因代码 → 列表/页面展示文案
BLOCKER_LABELS = {
    "facility_stopped": "治污设施停机期间数据，不得直接定性",
    "calibration_expired": f"校准过期（超过{CALIBRATION_VALID_DAYS}天）或无校准记录",
    "instant_exceeded": "瞬时浓度超过瞬时许可限值",
    "daily_exceeded": "日均浓度超过日均许可限值",
}

# 复核结论
CONCLUSIONS = {'confirmed': '确认超标', 'rejected': '排除（波动/停机等）'}
# 复核后直接可结案的确认结论
CONFIRMED_CONCLUSION = 'confirmed'

# 状态机。正常路径由登记/复核/结案动作驱动；
# 更正记录后由存储层强制回退到待复核（bypass本图），旧结论留档。
TRANSITIONS = {
    'registered': [],
    'pending_review': ['confirmed'],   # 由复核动作携带结论落定
    'confirmed': ['closed'],
    'closed': [],
}
# 复核与结案需要的角色（另一名合规人员 / 主管）
REVIEW_ROLES = {'compliance_officer'}
CLOSE_ROLES = {'director'}
CREATE_ROLES = {'operator', 'compliance_officer'}
CORRECT_ROLES = {'compliance_officer'}
RECORD_ROLES = {'operator', 'compliance_officer'}
RECTIFY_CLOSE_ROLES = {'operator', 'compliance_officer'}
AUDIT_ROLES = {'director', 'viewer'}
VIEW_ROLES = {'operator', 'compliance_officer', 'director', 'viewer'}

STATUS_LABELS = {
    'registered': '已登记',
    'pending_review': '待复核',
    'confirmed': '已复核确认',
    'closed': '已结案',
}
SEVERITY_LABELS = {
    'normal': '正常', 'watch': '关注', 'exceedance': '超标', 'major': '重大',
}
FACILITY_LABELS = {'running': '运行中', 'stopped': '停机'}


def _parse(value: Any) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def instant_exceeded(instant: float, limit_instant: float) -> bool:
    return limit_instant > 0 and instant > limit_instant


def daily_exceeded(daily: Optional[float], limit_daily: float) -> bool:
    return daily is not None and limit_daily > 0 and daily > limit_daily


def exceedance_ratio(instant: float, limit_instant: float) -> float:
    return round(instant / limit_instant, 4) if limit_instant > 0 else 0.0


def calibration_ok(sampled_at: Any, calibrated_at: Optional[Any]) -> bool:
    """校准有效：采样前（含同一时刻）已有校准记录，且距今（相对采样时刻）不超过有效期。"""
    if calibrated_at is None:
        return False
    sampled = _parse(sampled_at)
    calibrated = _parse(calibrated_at)
    if calibrated > sampled:
        return False
    return sampled - calibrated <= timedelta(days=CALIBRATION_VALID_DAYS)


def evaluate_reading(*, facility_status: str, sampled_at: Any,
                     calibrated_at: Optional[Any], instant: float,
                     daily: Optional[float], limit_instant: float,
                     limit_daily: float) -> Dict[str, Any]:
    """核心判定：瞬时波动与日均超标分开评估，任一异常都不直接定性，转入待复核。

    返回阻断原因代码列表（有序、去重）与建议严重程度。
    """
    if facility_status not in FACILITY_STATES:
        raise ValidationError("facility_status不在允许范围内")
    blockers: List[str] = []
    stopped = facility_status == 'stopped'
    cal_ok = calibration_ok(sampled_at, calibrated_at)
    instant_bad = instant_exceeded(instant, limit_instant)
    daily_bad = daily_exceeded(daily, limit_daily)

    if stopped:
        blockers.append("facility_stopped")
    if not cal_ok:
        blockers.append("calibration_expired")
    if instant_bad:
        blockers.append("instant_exceeded")
    if daily_bad:
        blockers.append("daily_exceeded")

    ratio = exceedance_ratio(instant, limit_instant)
    if instant_bad or daily_bad:
        severity = 'major' if ratio >= MAJOR_RATIO else 'exceedance'
    elif stopped or not cal_ok:
        severity = 'watch'
    else:
        severity = 'normal'
    return {
        "blockers": blockers,
        "severity": severity,
        "instant_exceeded": instant_bad,
        "daily_exceeded": daily_bad,
        "facility_stopped": stopped,
        "calibration_ok": cal_ok,
        "exceedance_ratio": ratio,
    }


def initial_status(blockers: List[str]) -> str:
    return 'pending_review' if blockers else 'registered'


def severity_of(evaluation: Dict[str, Any]) -> str:
    severity = evaluation["severity"]
    if severity not in SEVERITIES:
        raise ValidationError("unknown severity")
    return severity


def validate_transition(current: str, target: str) -> None:
    if current not in STATUSES or target not in STATUSES:
        raise ValidationError("未知状态")
    if target not in TRANSITIONS.get(current, []):
        from .domain import ConflictError
        raise ConflictError(f"不能从{current}转换到{target}")


def reviewer_must_be_another(creator: Optional[str], last_corrected_by: Optional[str],
                             reviewer: str) -> bool:
    """四眼原则：复核人不能是登记人，也不能是最近一次更正记录的人。"""
    return reviewer not in {creator, last_corrected_by} - {None}


def review_eligible(check: Dict[str, Any], reviewer: str) -> List[str]:
    """返回复核动作的阻断原因；为空表示可以复核。"""
    problems: List[str] = []
    if check["status"] != 'pending_review':
        problems.append("当前不是待复核状态")
    if not reviewer_must_be_another(check.get("created_by"),
                                   check.get("last_corrected_by"), reviewer):
        problems.append("复核人必须是非登记人且非最近更正人的另一名合规人员")
    return problems


def closure_blockers(check: Dict[str, Any], open_rectifications: int,
                    passing_retest_after_review: bool) -> List[str]:
    """结案不变量。已确认超标且定性为重大的事件：必须有复核合格后的复测达标
    记录，且整改事项全部关闭。复核已排除（如瞬时波动）的不受该门槛限制。"""
    blockers: List[str] = []
    if check["status"] != 'confirmed':
        blockers.append("尚未经复核确认，不能结案")
        return blockers
    major_confirmed = (check.get("severity") == 'major'
                       and check.get("review_conclusion") == CONFIRMED_CONCLUSION)
    if major_confirmed:
        if not passing_retest_after_review:
            blockers.append("重大事件缺少复核合格后的复测达标记录")
        if open_rectifications > 0:
            blockers.append("仍有未关闭的整改事项")
    return blockers


def is_major(check: Dict[str, Any]) -> bool:
    return check.get("severity") == 'major'


def blocker_text(code: str) -> str:
    return BLOCKER_LABELS.get(code, code)
