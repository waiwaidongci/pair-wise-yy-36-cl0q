import tempfile
import unittest
from pathlib import Path

from src.repository import Repository
from src.service import Service

SAMPLED = "2026-09-26T08:00:00+00:00"
CAL_FRESH = "2026-09-20T00:00:00+00:00"


def base_payload(**overrides):
    payload = {
        "outfall": "DW001", "sampled_at": SAMPLED, "instant_concentration": 1,
        "daily_avg_concentration": 1, "permit_limit_instant": 10,
        "permit_limit_daily": 8, "facility_status": "running",
        "calibrated_at": CAL_FRESH,
    }
    payload.update(overrides)
    return payload


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def test_clean_reading_registers_and_duplicate_same_outfall_time_kept_first(self):
        first = self.service.create_check(
            base_payload(note="first wins"), "op1", "operator")
        self.assertEqual(first["status"], "registered")
        self.assertEqual(first["current_blockers"], [])

        # 同一排放口同一采样时刻：第二条直接拒收，只保留首条
        from src.domain import ConflictError
        with self.assertRaises(ConflictError):
            self.service.create_check(
                base_payload(instant_concentration=9), "op1", "operator")
        listed = self.service.list_checks("viewer")
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["note"], "first wins")

    def test_major_event_full_lifecycle_review_retest_rectify_close(self):
        # 瞬时30 / 限值10，超标3倍 → 重大，直接待复核
        check = self.service.create_check(
            base_payload(instant_concentration=30, daily_avg_concentration=9,
                         external_ref="MAJ-1"),
            "op1", "operator")
        self.assertEqual(check["status"], "pending_review")
        self.assertEqual(check["severity"], "major")
        self.assertIn("instant_exceeded", check["blockers"])
        self.assertIn("daily_exceeded", check["blockers"])

        # 登记人自己不能复核（四眼）
        from src.domain import PermissionDenied
        with self.assertRaises(PermissionDenied):
            self.service.review_check(check["id"],
                                      {"expected_version": check["version"],
                                       "conclusion": "confirmed"},
                                      "op1", "operator")
        # 另一名合规人员确认
        confirmed = self.service.review_check(
            check["id"], {"expected_version": check["version"],
                          "conclusion": "confirmed", "note": "核查属实"},
            "co2", "compliance_officer")
        self.assertEqual(confirmed["status"], "confirmed")
        self.assertEqual(confirmed["reviewed_by"], "co2")

        # 无复测达标、有未关闭整改 → 主管不能结案
        from src.domain import ConflictError
        with self.assertRaises(ConflictError):
            self.service.close_check(check["id"],
                                     {"expected_version": confirmed["version"]},
                                     "dir1", "director")

        # 登记整改事项
        rect = self.service.add_record(
            check["id"], {"kind": "rectification", "detail": "更换加药泵",
                          "status": "open"}, "op1", "operator")

        # 复核合格之前的复测达标不算数（按复测采样时刻判断）
        self.service.add_record(
            check["id"], {"kind": "retest", "detail": "复核前旧复测",
                          "result": "pass",
                          "sampled_at": "2026-09-26T01:00:00+00:00"},
            "co2", "compliance_officer")
        still = self.service.get_check(check["id"], "viewer")
        self.assertTrue(any("复测达标" in r for r in still["current_blocker_reasons"]))

        # 复核合格之后的达标复测；整改未关闭仍然阻断结案
        self.service.add_record(
            check["id"], {"kind": "retest", "detail": "第三方复测达标",
                          "result": "pass",
                          "sampled_at": "2026-09-27T09:00:00+00:00"},
            "co2", "compliance_officer")
        confirmed = self.service.get_check(check["id"], "viewer")
        self.assertIn("仍有未关闭的整改事项", confirmed["current_blocker_reasons"])

        # 关闭整改事项后，主管结案
        self.service.close_rectification(check["id"], rect["id"], "op1", "operator")
        confirmed = self.service.get_check(check["id"], "viewer")
        self.assertEqual(confirmed["current_blockers"], [])
        closed = self.service.close_check(
            check["id"], {"expected_version": confirmed["version"], "note": "闭环"},
            "dir1", "director")
        self.assertEqual(closed["status"], "closed")

        # 结案后不得再追加记录
        with self.assertRaises(ConflictError):
            self.service.add_record(
                check["id"], {"kind": "evidence", "detail": "late"}, "op1", "operator")

    def test_correction_invalidates_review_and_closure_old_conclusions_archived(self):
        check = self.service.create_check(
            base_payload(instant_concentration=30, external_ref="MAJ-2"),
            "op1", "operator")
        confirmed = self.service.review_check(
            check["id"], {"expected_version": check["version"],
                          "conclusion": "confirmed"}, "co2", "compliance_officer")
        rect = self.service.add_record(
            check["id"], {"kind": "rectification", "detail": "限期治理",
                          "status": "open"}, "op1", "operator")
        self.service.add_record(
            check["id"], {"kind": "retest", "detail": "复测达标", "result": "pass",
                          "sampled_at": "2026-09-27T09:00:00+00:00"},
            "co2", "compliance_officer")
        self.service.close_rectification(check["id"], rect["id"], "op1", "operator")
        cur = self.service.get_check(check["id"], "viewer")
        closed = self.service.close_check(
            check["id"], {"expected_version": cur["version"]},
            "dir1", "director")
        self.assertEqual(closed["status"], "closed")

        # 限值更正：原复核 + 原结案资格立即失效，回到待复核
        reset = self.service.correct_check(
            check["id"], {"expected_version": closed["version"],
                          "permit_limit_instant": 50},
            "co3", "compliance_officer")
        self.assertEqual(reset["status"], "pending_review")
        self.assertIsNone(reset["reviewed_by"])
        self.assertIsNone(reset["closed_by"])
        # 新限值50下30不再超标，程度恢复normal；但更正本身要求重新复核
        self.assertEqual(reset["severity"], "normal")

        history = self.service.list_history(check["id"], "viewer")
        kinds = sorted(h["kind"] for h in history)
        self.assertEqual(kinds, ["closure", "review"])
        self.assertTrue(all(h["invalidated_reason"] for h in history))

        # 最近更正人 co3 自己不能复核，须换另一名合规人员
        from src.domain import ConflictError
        with self.assertRaises(ConflictError):
            self.service.review_check(
                check["id"], {"expected_version": reset["version"],
                              "conclusion": "rejected"},
                "co3", "compliance_officer")
        reconfirmed = self.service.review_check(
            check["id"], {"expected_version": reset["version"],
                          "conclusion": "confirmed"}, "co2", "compliance_officer")
        self.assertEqual(reconfirmed["status"], "confirmed")

        # 更正后已降为非重大超标，确认后主管可直接再次结案
        reclosed = self.service.close_check(
            check["id"], {"expected_version": reconfirmed["version"]},
            "dir1", "director")
        self.assertEqual(reclosed["status"], "closed")
        events = self.service.audit("director", check["id"])
        actions = [e["action"] for e in events]
        self.assertIn("register", actions)
        self.assertIn("review", actions)
        self.assertIn("close", actions)
        self.assertIn("correct", actions)
        self.assertTrue(self.repo.verify_audit_chain())

    def test_instant_spike_rejected_by_review_can_close(self):
        # 瞬时单点超、日均未超、设施运行、校准有效 → 超标但非重大
        check = self.service.create_check(
            base_payload(instant_concentration=12, daily_avg_concentration=5),
            "op1", "operator")
        self.assertEqual(check["severity"], "exceedance")
        confirmed = self.service.review_check(
            check["id"], {"expected_version": check["version"],
                          "conclusion": "rejected", "note": "瞬时波动，非连续超标"},
            "co2", "compliance_officer")
        # 排除性结论不需要复测/整改即可结案
        closed = self.service.close_check(
            check["id"], {"expected_version": confirmed["version"]},
            "dir1", "director")
        self.assertEqual(closed["status"], "closed")

    def test_stopped_facility_data_is_separated_and_blocked_from_conclusion(self):
        check = self.service.create_check(
            base_payload(instant_concentration=1, daily_avg_concentration=1,
                         facility_status="stopped"),
            "op1", "operator")
        self.assertEqual(check["severity"], "watch")
        self.assertIn("facility_stopped", check["blockers"])
        self.assertEqual(check["status"], "pending_review")
        reasons = "；".join(check["blocker_reasons"])
        self.assertIn("停机", reasons)


if __name__ == "__main__":
    unittest.main()
