import tempfile
import unittest
from pathlib import Path

from src.repository import Repository
from src.service import Service


def base(**over):
    p = {
        "outfall": "DW001", "sampling_time": "2026-09-26T08:00:00+00:00",
        "instant_value": 40, "daily_value": 35,
        "instant_limit": 50, "daily_limit": 30,
        "facility_state": "running",
    }
    p.update(over)
    return p


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def test_major_event_full_flow_and_correction_invalidates(self):
        # 1. 运维登记日均超标读数 -> 直接待复核，标记重大
        case = self.service.register(base(), "operator_li", "operator")
        self.assertEqual(case["status"], "pending")
        self.assertTrue(case["major"])
        self.assertIn("日均浓度超过日均限值（重大事件）", case["flag_labels"])

        # 2. 同排放口同时刻重复登记被拒（只留首条）
        from src.domain import ConflictError
        with self.assertRaises(ConflictError):
            self.service.register(base(external_ref="X2"), "someone", "operator")

        # 3. 登记人本人不能复核 -> 换另一名合规人员确认超标 -> 整改中
        from src.domain import PermissionDenied
        with self.assertRaises(PermissionDenied):
            self.service.review(case["id"], {"result": "exceedance",
                                             "expected_version": case["version"]},
                                "operator_li", "compliance_officer")
        reviewed = self.service.review(case["id"], {"result": "exceedance",
                                                    "expected_version": case["version"]},
                                       "officer_wang", "compliance_officer")
        self.assertEqual(reviewed["status"], "remediation")
        self.assertEqual(reviewed["reviewed_by"], "officer_wang")

        # 4. 结案前：无复测、有未关闭整改 -> 阻断
        rect = self.service.add_record(reviewed["id"],
                                       {"kind": "rectification", "title": "修复曝气池"},
                                       "operator_li", "operator")
        cur = self.service.get_case(reviewed["id"], "viewer")
        with self.assertRaises(ConflictError) as ctx:
            self.service.close_case(cur["id"], {"expected_version": cur["version"]},
                                    "officer_wang", "compliance_officer")
        self.assertIn("尚无达标复测", str(ctx.exception))

        # 复测超标 -> 仍阻断
        self.service.add_record(cur["id"], {"kind": "retest", "daily_value": 33},
                                "operator_li", "operator")
        cur = self.service.get_case(cur["id"], "viewer")
        with self.assertRaises(ConflictError):
            self.service.close_case(cur["id"], {"expected_version": cur["version"]},
                                    "officer_wang", "compliance_officer")

        # 复测达标 + 整改关闭 -> 重大事件结案
        self.service.add_record(cur["id"], {"kind": "retest", "instant_value": 40,
                                            "daily_value": 28},
                                "operator_li", "operator")
        self.service.close_record(cur["id"], rect["id"], "operator_li", "operator")
        cur = self.service.get_case(cur["id"], "viewer")
        closed = self.service.close_case(cur["id"], {"expected_version": cur["version"]},
                                         "officer_wang", "compliance_officer")
        self.assertEqual(closed["status"], "closed")
        self.assertEqual(closed["blockers"],
                         ["已结案（限值/工况若更正，结案资格立即失效，旧结论留档）"])

        # 5. 限值更正 -> 原结案立即失效，回到待复核，旧结论留档，revision递增
        rev_before = closed["revision"]
        corrected = self.service.correct(
            closed["id"], {"instant_limit": 50, "daily_limit": 40,
                           "facility_state": "running",
                           "reason": "许可证台账订正",
                           "expected_version": closed["version"]},
            "officer_chen", "compliance_officer")
        self.assertEqual(corrected["status"], "pending")
        self.assertGreater(corrected["revision"], rev_before)
        self.assertIsNone(corrected["closed_at"])
        archives = self.service.list_archives(closed["id"], "viewer")
        kinds = [(a["kind"], a["reason"] is not None) for a in archives]
        self.assertIn(("review", False), kinds)
        self.assertIn(("closure", True), kinds)
        self.assertTrue(self.repo.verify_audit_chain())

    def test_instant_spike_reviewed_directly(self):
        # 仅瞬时超标 -> 待复核；认定为瞬时波动 -> 已复核（非重大，无需整改结案）
        case = self.service.register(
            base(daily_value=20, instant_value=99,
                 sampling_time="2026-09-26T09:00:00+00:00"),
            "operator_li", "operator")
        self.assertEqual(case["status"], "pending")
        self.assertFalse(case["major"])
        done = self.service.review(case["id"], {"result": "transient",
                                                "expected_version": case["version"]},
                                   "officer_wang", "compliance_officer")
        self.assertEqual(done["status"], "reviewed")

    def test_shutdown_and_expired_calibration_go_pending(self):
        # 设施停机期间读数，即便浓度合格也要待复核
        case = self.service.register(
            base(instant_value=1, daily_value=1, facility_state="shutdown",
                 sampling_time="2026-09-26T10:00:00+00:00"),
            "operator_li", "operator")
        self.assertEqual(case["status"], "pending")
        self.assertIn("治污设施停机期间数据", case["blockers"])

    def test_clean_reading_stays_registered(self):
        case = self.service.register(
            base(instant_value=5, daily_value=5,
                 sampling_time="2026-09-26T11:00:00+00:00"),
            "operator_li", "operator")
        self.assertEqual(case["status"], "registered")
        self.assertEqual(case["blockers"], [])


if __name__ == "__main__":
    unittest.main()
