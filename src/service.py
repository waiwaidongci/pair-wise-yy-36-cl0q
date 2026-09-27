from __future__ import annotations

from typing import Any, Dict, List, Optional

from .domain import (ConflictError, PermissionDenied, ensure_role,
                     normalize_calibrated_until, normalize_sampling_time,
                     require_actor, require_choice, require_number,
                     require_optional_text, require_text,
                     FACILITY_STATES, RECORD_KINDS, REVIEW_RESULTS)
from .repository import Repository
from .rules import (AUDIT_ROLES, CORRECT_ROLES, CREATE_ROLES, RECORD_ROLES,
                    REVIEW_ROLES, STATUS_LABELS, VIEW_ROLES, close_blockers,
                    evaluate_flags, flag_labels, is_major, pending_reasons,
                    retest_passed, review_target, validate_transition)
from .audit import utc_now


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    # ---------- 登记 ----------

    def register(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_actor(actor)
        data = self._read_reading(payload)
        flags = evaluate_flags(data)
        # 任一日均/瞬时超标、设施停机、校准过期 -> 直接进入待复核
        data["status"] = "pending" if flags else "registered"
        data["flags"] = flags
        case = self.repository.create_case(data, actor)
        self.repository.append_audit("register", "case", case["id"], actor, {
            "outfall": data["outfall"], "sampling_time": data["sampling_time"],
            "flags": flags, "major": is_major(flags),
            "status": data["status"],
        })
        return self.enrich(case)

    def _read_reading(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        outfall = require_text(payload.get("outfall"), "outfall", 100)
        sampling_time = normalize_sampling_time(payload.get("sampling_time"))

        def optional_number(field):
            value = payload.get(field)
            if value is None or value == "":
                return None
            return require_number(value, field)

        instant_value = optional_number("instant_value")
        daily_value = optional_number("daily_value")
        if instant_value is None and daily_value is None:
            from .domain import ValidationError
            raise ValidationError("瞬时浓度与日均浓度至少填写一项")
        instant_limit = require_number(payload.get("instant_limit"), "instant_limit", 0.000001)
        daily_limit = require_number(payload.get("daily_limit"), "daily_limit", 0.000001)
        facility_state = require_choice(payload.get("facility_state"), "facility_state",
                                        FACILITY_STATES)
        calibrated_until = normalize_calibrated_until(payload.get("calibrated_until"))
        external_ref = require_optional_text(payload.get("external_ref"), "external_ref", 100)
        return {
            "outfall": outfall, "sampling_time": sampling_time,
            "instant_value": instant_value, "daily_value": daily_value,
            "instant_limit": instant_limit, "daily_limit": daily_limit,
            "facility_state": facility_state,
            "calibrated_until": calibrated_until,
            "external_ref": external_ref,
        }

    # ---------- 复核（另一名合规人员确认） ----------

    def review(self, case_id: int, payload: Dict[str, Any], actor: str,
               role: str) -> Dict[str, Any]:
        ensure_role(role, REVIEW_ROLES)
        actor = require_actor(actor)
        result = require_choice(payload.get("result"), "result", REVIEW_RESULTS)
        note = require_optional_text(payload.get("note"), "note", 1000)
        expected_version = self._require_version(payload)
        case = self.repository.get_case(case_id)
        if case["status"] != "pending":
            raise ConflictError("仅待复核记录可以复核（更正后需重新复核）")
        if actor == case["registered_by"]:
            raise PermissionDenied("复核必须由登记人之外的另一名合规人员确认")
        target = review_target(result, case["flags"])
        validate_transition("pending", target)
        now = utc_now()
        updated = self.repository.update_case_decision(
            case_id, target, expected_version,
            {"reviewed_by": actor, "reviewed_at": now,
             "review_result": result, "review_note": note},
            actor,
        )
        self.repository.archive(case_id, "review", self._snapshot(case), actor,
                                case["revision"], result=result)
        self.repository.append_audit("review", "case", case_id, actor, {
            "result": result, "to": target, "major": is_major(case["flags"]),
        })
        return self.enrich(updated)

    # ---------- 更正（限值/工况）：旧结论立即失效并留档 ----------

    def correct(self, case_id: int, payload: Dict[str, Any], actor: str,
                role: str) -> Dict[str, Any]:
        ensure_role(role, CORRECT_ROLES)
        actor = require_actor(actor)
        expected_version = self._require_version(payload)
        case = self.repository.get_case(case_id)
        fields = {
            "instant_limit": require_number(payload.get("instant_limit"), "instant_limit", 0.000001),
            "daily_limit": require_number(payload.get("daily_limit"), "daily_limit", 0.000001),
            "facility_state": require_choice(payload.get("facility_state"),
                                             "facility_state", FACILITY_STATES),
        }
        reason = require_optional_text(payload.get("reason"), "reason", 1000)
        merged = dict(case)
        merged.update(fields)
        flags = evaluate_flags(merged)
        previous_status = case["status"]
        # 无论是否仍有超标，已做出的复核/结案一律失效，必须重新待复核
        updated = self.repository.apply_correction(
            case_id, "pending", expected_version, fields, flags, actor)
        if case["review_result"] or case["closed_at"]:
            kind = "closure" if case["closed_at"] else "review"
            self.repository.archive(
                case_id, kind, self._snapshot(case), actor, case["revision"],
                result=case["review_result"],
                reason=f"限值/工况更正，原{('结案' if kind == 'closure' else '复核')}资格失效"
                       + (f"：{reason}" if reason else ""),
            )
        self.repository.append_audit("correct", "case", case_id, actor, {
            "from_status": previous_status, "revision_before": case["revision"],
            "instant_limit": fields["instant_limit"],
            "daily_limit": fields["daily_limit"],
            "facility_state": fields["facility_state"],
            "flags_after": flags, "reason": reason,
        })
        return self.enrich(updated)

    # ---------- 整改 / 复测 ----------

    def add_record(self, case_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_actor(actor)
        kind = require_choice(payload.get("kind"), "kind", RECORD_KINDS)
        case = self.repository.get_case(case_id)
        if kind in ("rectification", "retest") and case["status"] != "remediation":
            raise ConflictError("整改事项与复测只能在整改中（重大事件）阶段登记")
        detail = self._record_detail(kind, payload)
        record = self.repository.add_record(case_id, kind, detail, "open", actor)
        if kind == "retest":
            passed = retest_passed(detail, self.repository.get_case(case_id))
        else:
            passed = None
        self.repository.append_audit("record", "case", case_id, actor, {
            "record_id": record["id"], "kind": kind, "retest_passed": passed,
        })
        return record

    def close_record(self, case_id: int, record_id: int, actor: str,
                     role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_actor(actor)
        case = self.repository.get_case(case_id)
        record = self.repository.get_record(record_id)
        if record["case_id"] != case["id"]:
            from .domain import NotFoundError
            raise NotFoundError("附随记录不存在")
        if record["kind"] != "rectification":
            raise ConflictError("仅整改事项需要逐项关闭")
        updated = self.repository.close_record(record_id, actor)
        self.repository.append_audit("record_close", "case", case_id, actor, {
            "record_id": record_id,
        })
        return updated

    # ---------- 结案 ----------

    def close_case(self, case_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, REVIEW_ROLES)
        actor = require_actor(actor)
        expected_version = self._require_version(payload)
        case = self.repository.get_case(case_id)
        if case["status"] != "remediation":
            raise ConflictError("仅整改中的重大事件可以结案")
        open_rect = self.repository.open_rectification_count(case_id)
        latest_retest = self.repository.latest_retest(case_id)
        retest_ok = retest_passed(latest_retest["detail"], case) if latest_retest else None
        blockers = close_blockers(open_rect, retest_ok)
        if blockers:
            raise ConflictError("；".join(blockers))
        now = utc_now()
        updated = self.repository.update_case_decision(
            case_id, "closed", expected_version,
            {"closed_by": actor, "closed_at": now}, actor,
        )
        self.repository.archive(case_id, "closure", self._snapshot(case), actor,
                                case["revision"], result=case["review_result"])
        self.repository.append_audit("close", "case", case_id, actor, {
            "retest_passed": retest_ok,
        })
        return self.enrich(updated)

    # ---------- 查询 ----------

    def get_case(self, case_id: int, role: str) -> Dict[str, Any]:
        ensure_role(role, VIEW_ROLES)
        return self.enrich(self.repository.get_case(case_id))

    def list_cases(self, role: str, status: Optional[str] = None) -> List[Dict[str, Any]]:
        ensure_role(role, VIEW_ROLES)
        return [self.enrich(case) for case in self.repository.list_cases(status)]

    def list_records(self, case_id: int, role: str) -> List[Dict[str, Any]]:
        ensure_role(role, VIEW_ROLES)
        return self.repository.list_records(case_id)

    def list_archives(self, case_id: int, role: str) -> List[Dict[str, Any]]:
        ensure_role(role, VIEW_ROLES)
        return self.repository.list_archives(case_id)

    def audit(self, role: str, case_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(case_id)

    # ---------- 组装 ----------

    def enrich(self, case: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(case)
        result["major"] = is_major(case["flags"])
        result["flag_labels"] = flag_labels(case["flags"])
        result["open_rectifications"] = self.repository.open_rectification_count(case["id"])
        latest_retest = self.repository.latest_retest(case["id"])
        result["retest_ok"] = (retest_passed(latest_retest["detail"], case)
                               if latest_retest else None)
        result["blockers"] = pending_reasons(result)
        result["status_label"] = STATUS_LABELS.get(case["status"], case["status"])
        return result

    @staticmethod
    def _record_detail(kind: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if kind == "note":
            return {"text": require_text(payload.get("detail"), "detail", 2000)}
        if kind == "rectification":
            detail = {
                "title": require_text(payload.get("title"), "title", 200),
                "description": require_optional_text(payload.get("description"),
                                                      "description", 2000),
            }
            return detail
        # retest
        from .domain import ValidationError
        inst = payload.get("instant_value")
        daily = payload.get("daily_value")
        if inst in (None, "") and daily in (None, ""):
            raise ValidationError("复测至少填写瞬时浓度或日均浓度")
        detail: Dict[str, Any] = {}
        if inst not in (None, ""):
            detail["instant_value"] = require_number(inst, "instant_value")
        if daily not in (None, ""):
            detail["daily_value"] = require_number(daily, "daily_value")
        detail["note"] = require_optional_text(payload.get("note"), "note", 1000)
        return detail

    @staticmethod
    def _require_version(payload: Dict[str, Any]) -> int:
        version = payload.get("expected_version")
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            from .domain import ValidationError
            raise ValidationError("expected_version必须是正整数")
        return version

    @staticmethod
    def _snapshot(case: Dict[str, Any]) -> Dict[str, Any]:
        keys = ("status", "revision", "instant_value", "daily_value",
                "instant_limit", "daily_limit", "facility_state",
                "calibrated_until", "flags", "review_result", "review_note",
                "reviewed_by", "reviewed_at", "closed_by", "closed_at")
        return {key: case.get(key) for key in keys}
