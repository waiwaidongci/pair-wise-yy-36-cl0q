"""排放核查台用例编排：权限检查、规则调用、存储事务与审计。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .domain import (ConflictError, ValidationError, ensure_role, iso_z,
                     optional_number, optional_text, parse_datetime,
                     require_number, require_text)
from .repository import CORRECTABLE, Repository
from .rules import (AUDIT_ROLES, CLOSE_ROLES, CONFIRMED_CONCLUSION, CONCLUSIONS,
                    CORRECT_ROLES, CREATE_ROLES, RECORD_ROLES, RECTIFY_CLOSE_ROLES,
                    REVIEW_ROLES, VIEW_ROLES, ENTITY, blocker_text, closure_blockers,
                    evaluate_reading, initial_status, review_eligible, severity_of)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    # ---------- 工具 ----------
    @staticmethod
    def _view(role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    @staticmethod
    def _parse_check_fields(payload: Dict[str, Any], partial: bool = False) -> Dict[str, Any]:
        """partial=False 时要求整组字段齐全（登记）；True 时只取出现的字段（更正）。"""
        out: Dict[str, Any] = {}
        present = set()

        def require(key, parser):
            if key in payload:
                out[key] = parser(payload[key], key)
                present.add(key)
            elif not partial:
                raise ValidationError(f"{key}不能为空")

        require("outfall", lambda v, f: require_text(v, f, 100))
        if "sampled_at" in payload or not partial:
            out["sampled_at"] = iso_z(parse_datetime(
                payload.get("sampled_at"), "sampled_at"))
            present.add("sampled_at")
        if "instant_concentration" in payload or not partial:
            out["instant_concentration"] = require_number(
                payload.get("instant_concentration"), "instant_concentration")
            present.add("instant_concentration")
        if "daily_avg_concentration" in payload:
            out["daily_avg_concentration"] = optional_number(
                payload.get("daily_avg_concentration"), "daily_avg_concentration")
            present.add("daily_avg_concentration")
        elif not partial:
            out["daily_avg_concentration"] = None
        if "permit_limit_instant" in payload or not partial:
            out["permit_limit_instant"] = require_number(
                payload.get("permit_limit_instant"), "permit_limit_instant", 0.000001)
            present.add("permit_limit_instant")
        if "permit_limit_daily" in payload or not partial:
            out["permit_limit_daily"] = require_number(
                payload.get("permit_limit_daily"), "permit_limit_daily", 0.000001)
            present.add("permit_limit_daily")
        if "facility_status" in payload or not partial:
            status = require_text(payload.get("facility_status"), "facility_status", 20)
            if status not in ("running", "stopped"):
                raise ValidationError("facility_status必须是running或stopped")
            out["facility_status"] = status
            present.add("facility_status")
        if "calibrated_at" in payload:
            value = payload.get("calibrated_at")
            out["calibrated_at"] = None if value in (None, "") else iso_z(
                parse_datetime(value, "calibrated_at"))
            present.add("calibrated_at")
        elif not partial:
            out["calibrated_at"] = None
        if "note" in payload:
            out["note"] = optional_text(payload.get("note"), "note")
            present.add("note")
        elif not partial:
            out["note"] = None
        out["_present"] = present
        return out

    @staticmethod
    def _evaluate(data: Dict[str, Any]) -> Dict[str, Any]:
        return evaluate_reading(
            facility_status=data["facility_status"],
            sampled_at=data["sampled_at"],
            calibrated_at=data["calibrated_at"],
            instant=data["instant_concentration"],
            daily=data["daily_avg_concentration"],
            limit_instant=data["permit_limit_instant"],
            limit_daily=data["permit_limit_daily"],
        )

    # ---------- 登记 ----------
    def create_check(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        data = self._parse_check_fields(payload, partial=False)
        data.pop("_present", None)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        evaluation = self._evaluate(data)
        data["severity"] = severity_of(evaluation)
        data["status"] = initial_status(evaluation["blockers"])
        data["external_ref"] = external_ref
        check = self.repository.create_check(data, actor)
        self.repository.append_audit("register", ENTITY, check["id"], actor, {
            "outfall": check["outfall"], "sampled_at": check["sampled_at"],
            "instant_concentration": check["instant_concentration"],
            "daily_avg_concentration": check["daily_avg_concentration"],
            "permit_limit_instant": check["permit_limit_instant"],
            "permit_limit_daily": check["permit_limit_daily"],
            "facility_status": check["facility_status"],
            "blockers": evaluation["blockers"],
            "status": data["status"], "severity": data["severity"],
        })
        return self.enrich(check)

    # ---------- 限值/工况更正：旧结论留档，复核与结案资格立即失效 ----------
    def correct_check(self, check_id: int, payload: Dict[str, Any],
                      actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CORRECT_ROLES)
        actor = require_text(actor, "actor", 100)
        before = self.repository.get_check(check_id)
        expected_version = payload.get("expected_version")
        self._require_version(expected_version)
        parsed = self._parse_check_fields(payload, partial=True)
        present = parsed.pop("_present")
        fields = {k: v for k, v in parsed.items() if k in present}
        unknown = set(payload) - set(CORRECTABLE) - {"expected_version"}
        if unknown:
            raise ValidationError(f"不允许更正的字段：{sorted(unknown)}")
        if not fields:
            raise ValidationError("没有可更正的限值或工况字段")

        merged = {k: before[k] for k in CORRECTABLE}
        merged.update(fields)
        evaluation = self._evaluate(merged)
        severity = severity_of(evaluation)

        # 先留档既有复核/结案结论，再清空资格字段
        invalidations: List[Dict[str, Any]] = []
        if before["status"] in ("confirmed", "closed") or before["reviewed_by"]:
            invalidations.append({
                "kind": "review", "conclusion": before["review_conclusion"],
                "note": before["review_note"], "actor": before["reviewed_by"] or "",
                "concluded_at": before["reviewed_at"] or before["updated_at"],
            })
        if before["status"] == "closed":
            invalidations.append({
                "kind": "closure", "conclusion": "closed",
                "note": before["close_note"], "actor": before["closed_by"] or "",
                "concluded_at": before["closed_at"] or before["updated_at"],
            })

        # 已作过结论的记录：更正即资格失效，无论新值是否干净都必须重新复核；
        # 从未复核的记录按读数/工况触发条件重新判定。
        if invalidations:
            new_status = 'pending_review'
        else:
            new_status = initial_status(evaluation["blockers"])

        reason = "限值或工况记录更正，原结论作废待重新复核"
        updated = self.repository.correct_check(
            check_id, fields, expected_version, new_status, severity, actor)
        for item in invalidations:
            self.repository.archive_conclusion(
                check_id, item["kind"], item["conclusion"], item["note"],
                item["actor"], item["concluded_at"], reason, actor)
        self.repository.append_audit("correct", ENTITY, check_id, actor, {
            "changed": sorted(fields.keys()), "from_status": before["status"],
            "to_status": new_status, "severity": severity,
            "blockers": evaluation["blockers"], "invalidated": len(invalidations),
        })
        return self.enrich(updated)

    # ---------- 复核：另一名合规人员 ----------
    def review_check(self, check_id: int, payload: Dict[str, Any],
                     actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, REVIEW_ROLES)
        actor = require_text(actor, "actor", 100)
        expected_version = payload.get("expected_version")
        self._require_version(expected_version)
        conclusion = require_text(payload.get("conclusion"), "conclusion", 40)
        if conclusion not in CONCLUSIONS:
            raise ValidationError(f"conclusion必须是{list(CONCLUSIONS)}之一")
        note = optional_text(payload.get("note"), "review_note")
        check = self.repository.get_check(check_id)
        problems = review_eligible(check, actor)
        if problems:
            raise ConflictError("；".join(problems))
        updated = self.repository.mark_reviewed(
            check_id, expected_version, conclusion, note, actor)
        self.repository.append_audit("review", ENTITY, check_id, actor, {
            "conclusion": conclusion, "note": note,
            "registered_by": check["created_by"],
        })
        return self.enrich(updated)

    # ---------- 结案：重大事件需复测达标且整改关闭 ----------
    def close_check(self, check_id: int, payload: Dict[str, Any],
                    actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CLOSE_ROLES)
        actor = require_text(actor, "actor", 100)
        expected_version = payload.get("expected_version")
        self._require_version(expected_version)
        note = optional_text(payload.get("note"), "close_note")
        check = self.repository.get_check(check_id)
        passing = False
        if check.get("reviewed_at"):
            passing = self.repository.has_passing_retest_since(
                check_id, check["reviewed_at"])
        blockers = closure_blockers(
            check, self.repository.open_rectification_count(check_id), passing)
        if blockers:
            raise ConflictError("；".join(blockers))
        updated = self.repository.mark_closed(check_id, expected_version, note, actor)
        self.repository.append_audit("close", ENTITY, check_id, actor, {"note": note})
        return self.enrich(updated)

    # ---------- 复测 / 整改 / 佐证 ----------
    def add_record(self, check_id: int, payload: Dict[str, Any],
                   actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 40)
        if kind not in ("retest", "rectification", "evidence"):
            raise ValidationError("kind必须是retest、rectification或evidence")
        detail = require_text(payload.get("detail"), "detail")
        result = None
        sampled_at = None
        if kind == "retest":
            result = require_text(payload.get("result"), "result", 10)
            if result not in ("pass", "fail"):
                raise ValidationError("复测result必须是pass或fail")
            sampled_at = iso_z(parse_datetime(
                payload.get("sampled_at"), "sampled_at"))
        elif payload.get("result") is not None:
            raise ValidationError("仅复测记录可填写result")
        if kind != "retest" and payload.get("sampled_at") is not None:
            raise ValidationError("仅复测记录可填写sampled_at")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValidationError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        check = self.repository.get_check(check_id)
        if check["status"] == 'closed':
            raise ConflictError("已结案的核查不能再追加记录；如记录有误请先更正")
        record = self.repository.add_record(
            check_id, kind, detail, result, sampled_at, status, external_ref, actor)
        self.repository.append_audit("record", ENTITY, check_id, actor, {
            "record_id": record["id"], "kind": kind,
            "result": result, "status": status,
            "check_status": check["status"],
        })
        return record

    def close_rectification(self, check_id: int, record_id: int,
                            actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, RECTIFY_CLOSE_ROLES)
        actor = require_text(actor, "actor", 100)
        self.repository.get_check(check_id)
        record = self.repository.close_rectification(check_id, record_id)
        self.repository.append_audit("rectify_close", ENTITY, check_id, actor, {
            "record_id": record_id})
        return record

    # ---------- 查询 ----------
    def get_check(self, check_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        check = self.repository.get_check(check_id)
        return self.enrich(check, self._followup(check["id"]))

    def _followup(self, check_id: int) -> Dict[str, Any]:
        summary = self.repository.followup_summary()
        data = summary.get(check_id, {})
        if not data:
            data = {"open_rectifications": self.repository.open_rectification_count(check_id)}
        return data

    def list_checks(self, role: str, status: Optional[str] = None) -> List[Dict[str, Any]]:
        self._view(role)
        summary = self.repository.followup_summary()
        result = []
        for check in self.repository.list_checks(status):
            result.append(self.enrich(check, summary.get(check["id"], {})))
        return result

    def list_records(self, check_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(check_id)

    def list_history(self, check_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_history(check_id)

    def audit(self, role: str, check_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(check_id)

    # ---------- 组装 ----------
    @staticmethod
    def _require_version(value: Any) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValidationError("expected_version必须是正整数")

    @staticmethod
    def enrich(check: Dict[str, Any], followup: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        result = dict(check)
        evaluation = evaluate_reading(
            facility_status=check["facility_status"],
            sampled_at=check["sampled_at"],
            calibrated_at=check["calibrated_at"],
            instant=check["instant_concentration"],
            daily=check["daily_avg_concentration"],
            limit_instant=check["permit_limit_instant"],
            limit_daily=check["permit_limit_daily"],
        )
        result["blockers"] = evaluation["blockers"]
        result["blocker_reasons"] = [blocker_text(c) for c in evaluation["blockers"]]
        result["instant_exceeded"] = evaluation["instant_exceeded"]
        result["daily_exceeded"] = evaluation["daily_exceeded"]
        result["exceedance_ratio"] = evaluation["exceedance_ratio"]

        # 当前阻断原因：
        # 待复核时展示读数/工况触发条件 + 等待复核；已确认时展示结案未满足项
        followup = followup or {}
        open_rect = int(followup.get("open_rectifications", 0))
        last_pass = followup.get("last_passing_retest")
        current_blockers: List[str] = []
        if check["status"] == 'pending_review':
            current_blockers = list(evaluation["blockers"]) + ["awaiting_review"]
        elif check["status"] == 'confirmed':
            passing = bool(check.get("reviewed_at")) and (
                last_pass is not None and str(last_pass) >= str(check["reviewed_at"]))
            current_blockers = closure_blockers(check, open_rect, passing)
        result["open_rectifications"] = open_rect
        result["last_passing_retest"] = last_pass
        result["current_blockers"] = current_blockers
        result["current_blocker_reasons"] = [
            blocker_text(c) if c != "awaiting_review" else "等待另一名合规人员复核确认"
            for c in current_blockers
        ]
        return result
