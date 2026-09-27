from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from .audit import utc_now
from .domain import STATES, ConflictError, ValidationError

TITLE = '排放核查台'
ENTITY = '排放核查'

# 角色矩阵
CREATE_ROLES = {'operator', 'compliance_officer'}
RECORD_ROLES = {'operator', 'compliance_officer'}
REVIEW_ROLES = {'compliance_officer'}      # 复核、结案
CORRECT_ROLES = {'compliance_officer'}     # 限值/工况更正
AUDIT_ROLES = {'director', 'viewer'}
VIEW_ROLES = {'operator', 'compliance_officer', 'director', 'viewer'}

# 状态机
TRANSITIONS: Dict[str, List[str]] = {
    'registered': ['pending'],
    'pending': ['reviewed', 'remediation'],
    'reviewed': ['pending'],   # 更正后重新待复核
    'remediation': ['closed', 'pending'],
    'closed': ['pending'],     # 更正后旧结论失效，重新待复核
}
# 每个目标状态由谁触发（pending的二次回退来自更正，见service）
TRANSITION_ROLES = {
    'reviewed': {'compliance_officer'},
    'remediation': {'compliance_officer'},
    'closed': {'compliance_officer'},
}

# 阻断原因/待复核原因的中文标签
FLAG_LABELS = {
    'facility_shutdown': '治污设施停机期间数据',
    'calibration_expired': '仪器校准已过期',
    'instantaneous_exceedance': '瞬时浓度超过瞬时限值',
    'daily_exceedance': '日均浓度超过日均限值（重大事件）',
}
STATUS_LABELS = {
    'registered': '已登记',
    'pending': '待复核',
    'reviewed': '已复核',
    'remediation': '整改中',
    'closed': '已结案',
}


def evaluate_flags(case: Dict[str, Any], now: Optional[str] = None) -> List[str]:
    """评估读数的触发标记：设施停机、校准过期、瞬时超标、日均超标。

    瞬时波动与日均超标分别判定，不再把每次在线读数直接当成超标结论。
    """
    flags: List[str] = []
    if case.get('facility_state') == 'shutdown':
        flags.append('facility_shutdown')
    calibrated_until = case.get('calibrated_until')
    if calibrated_until:
        reference = _parse_dt(now or utc_now())
        if reference > _parse_dt(calibrated_until):
            flags.append('calibration_expired')
    limit_inst = case.get('instant_limit') or 0
    limit_daily = case.get('daily_limit') or 0
    inst = case.get('instant_value')
    daily = case.get('daily_value')
    if inst is not None and limit_inst > 0 and inst > limit_inst:
        flags.append('instantaneous_exceedance')
    if daily is not None and limit_daily > 0 and daily > limit_daily:
        flags.append('daily_exceedance')
    return flags


def is_major(flags: List[str]) -> bool:
    """日均超标即重大事件；仅瞬时超标属于瞬时波动，不自动升级。"""
    return 'daily_exceedance' in flags


def flag_labels(flags: List[str]) -> List[str]:
    return [FLAG_LABELS[f] for f in flags if f in FLAG_LABELS]


def review_target(result: str, flags: List[str]) -> str:
    """复核结论决定去向：确认超标按是否重大分流；非超标/无效数据直接复核成立。"""
    if result == 'exceedance':
        return 'remediation' if is_major(flags) else 'reviewed'
    return 'reviewed'


def validate_transition(current: str, target: str) -> None:
    if current not in STATES or target not in STATES:
        raise ValidationError("未知状态")
    if target not in TRANSITIONS.get(current, []):
        raise ConflictError(f"不能从{current}转换到{target}")


def role_for_transition(target: str) -> Set[str]:
    return set(TRANSITION_ROLES.get(target, set()))


def retest_passed(record_detail: Dict[str, Any], case: Dict[str, Any]) -> Optional[bool]:
    """复测是否达标：复测读数对照当前（更正后的）两类限值。"""
    inst = record_detail.get('instant_value')
    daily = record_detail.get('daily_value')
    if inst is None and daily is None:
        return None
    ok = True
    if inst is not None and (case.get('instant_limit') or 0) > 0:
        ok = ok and float(inst) <= float(case['instant_limit'])
    if daily is not None and (case.get('daily_limit') or 0) > 0:
        ok = ok and float(daily) <= float(case['daily_limit'])
    return ok


def close_blockers(open_rectifications: int, retest_ok: Optional[bool]) -> List[str]:
    """重大事件结案门槛：复测达标 + 整改事项全部关闭。"""
    blockers: List[str] = []
    if not retest_ok:
        blockers.append("尚无达标复测")
    if open_rectifications > 0:
        blockers.append(f"仍有{open_rectifications}项整改未关闭")
    return blockers


def pending_reasons(case: Dict[str, Any]) -> List[str]:
    """列表/详情中展示的当前状态阻断原因。"""
    status = case['status']
    flags = case.get('flags') or []
    if status == 'registered':
        return []
    if status == 'pending':
        reasons = flag_labels(flags)
        reasons.append("等待另一名合规人员复核确认")
        return reasons
    if status == 'reviewed':
        return ["复核已成立（限值/工况若更正，结论立即失效）"]
    if status == 'remediation':
        return close_blockers(
            case.get('open_rectifications', 0), case.get('retest_ok'))
    if status == 'closed':
        return ["已结案（限值/工况若更正，结案资格立即失效，旧结论留档）"]
    return []


def _parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
