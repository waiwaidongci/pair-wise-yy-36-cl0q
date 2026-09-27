import tempfile
import unittest
from pathlib import Path

from src.domain import (ConflictError, PermissionDenied, ValidationError)
from src.repository import Repository
from src.service import Service

SAMPLED = "2026-09-26T08:00:00+00:00"
CAL_FRESH = "2026-09-20T00:00:00+00:00"


def payload(**ov):
    base = {
        "outfall": "DW009", "sampled_at": SAMPLED, "instant_concentration": 30,
        "daily_avg_concentration": None, "permit_limit_instant": 10,
        "permit_limit_daily": 8, "facility_status": "running",
        "calibrated_at": CAL_FRESH, "external_ref": "F-1",
    }
    base.update(ov)
    return base


class FailureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.check = self.service.create_check(payload(), "op1", "operator")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def test_permissions(self):
        # viewer 不能登记
        with self.assertRaises(PermissionDenied):
            self.service.create_check(payload(outfall="DW010"), "v", "viewer")
        # operator 不能复核；director 不能代替合规人员复核
        with self.assertRaises(PermissionDenied):
            self.service.review_check(
                self.check["id"], {"expected_version": self.check["version"],
                                   "conclusion": "confirmed"},
                "op1", "operator")
        # 登记人本人即便是合规角色也受四眼限制
        with self.assertRaises(ConflictError):
            self.service.review_check(
                self.check["id"], {"expected_version": self.check["version"],
                                   "conclusion": "confirmed"},
                "op1", "compliance_officer")
        # 合规人员不能结案
        confirmed = self.service.review_check(
            self.check["id"], {"expected_version": self.check["version"],
                               "conclusion": "confirmed"},
            "co2", "compliance_officer")
        with self.assertRaises(PermissionDenied):
            self.service.close_check(
                self.check["id"], {"expected_version": confirmed["version"]},
                "co2", "compliance_officer")

    def test_optimistic_version_conflict(self):
        with self.assertRaises(ConflictError):
            self.service.review_check(
                self.check["id"], {"expected_version": 999,
                                   "conclusion": "confirmed"},
                "co2", "compliance_officer")

    def test_review_only_in_pending_and_close_only_after_review(self):
        # 再登记一条干净数据（registered），不能直接复核
        clean = self.service.create_check(
            payload(outfall="DW011", external_ref="F-2", instant_concentration=1),
            "op2", "operator")
        self.assertEqual(clean["status"], "registered")
        with self.assertRaises(ConflictError):
            self.service.review_check(
                clean["id"], {"expected_version": clean["version"],
                              "conclusion": "rejected"},
                "co2", "compliance_officer")
        # 待复核的重大事件未经复核不能结案
        with self.assertRaises(ConflictError):
            self.service.close_check(
                self.check["id"], {"expected_version": self.check["version"]},
                "dir1", "director")

    def test_major_close_requires_retest_and_closed_rectifications(self):
        confirmed = self.service.review_check(
            self.check["id"], {"expected_version": self.check["version"],
                               "conclusion": "confirmed"},
            "co2", "compliance_officer")
        # 复测未达标不算数
        self.service.add_record(
            self.check["id"], {"kind": "retest", "detail": "首次复测仍超",
                               "result": "fail",
                               "sampled_at": "2026-09-27T08:00:00+00:00"},
            "co2", "compliance_officer")
        rect = self.service.add_record(
            self.check["id"], {"kind": "rectification", "detail": "检修",
                               "status": "open"}, "op1", "operator")
        with self.assertRaises(ConflictError):
            self.service.close_check(
                self.check["id"], {"expected_version": confirmed["version"]},
                "dir1", "director")
        # 达标复测（复核合格之后）+ 整改关闭后可以结案
        self.service.add_record(
            self.check["id"], {"kind": "retest", "detail": "复测达标",
                               "result": "pass",
                               "sampled_at": "2026-09-27T10:00:00+00:00"},
            "co2", "compliance_officer")
        with self.assertRaises(ConflictError):
            self.service.close_check(
                self.check["id"], {"expected_version": confirmed["version"]},
                "dir1", "director")  # 整改仍未关闭
        self.service.close_rectification(self.check["id"], rect["id"],
                                         "op1", "operator")
        cur = self.service.get_check(self.check["id"], "viewer")
        closed = self.service.close_check(
            self.check["id"], {"expected_version": cur["version"]},
            "dir1", "director")
        self.assertEqual(closed["status"], "closed")

    def test_validation_errors(self):
        with self.assertRaises(ValidationError):
            self.service.create_check(payload(outfall="  "), "op1", "operator")
        with self.assertRaises(ValidationError):
            self.service.create_check(
                payload(outfall="DW020", instant_concentration=-1),
                "op1", "operator")
        with self.assertRaises(ValidationError):
            self.service.create_check(
                payload(outfall="DW021", sampled_at="not-a-time"),
                "op1", "operator")
        with self.assertRaises(ValidationError):
            self.service.add_record(
                self.check["id"], {"kind": "retest", "detail": "x",
                                   "result": "bogus"}, "op1", "operator")
        with self.assertRaises(ValidationError):
            self.service.correct_check(
                self.check["id"], {"expected_version": self.check["version"],
                                   "external_ref": "X"},
                "co2", "compliance_officer")

    def test_correction_collides_with_existing_outfall_time(self):
        # 同一排放口 DW009 的另一个时刻已有记录
        self.service.create_check(
            payload(outfall="DW009", external_ref="F-3",
                    sampled_at="2026-09-26T09:00:00+00:00"),
            "op1", "operator")
        # 把已有记录（DW009 @08:00）的采样时刻改成 09:00 → 唯一冲突
        with self.assertRaises(ConflictError):
            self.service.correct_check(
                self.check["id"],
                {"expected_version": self.check["version"],
                 "sampled_at": "2026-09-26T09:00:00+00:00"},
                "co2", "compliance_officer")

    def test_duplicate_rectification_close(self):
        rect = self.service.add_record(
            self.check["id"], {"kind": "rectification", "detail": "检修",
                               "status": "open"}, "op1", "operator")
        self.service.close_rectification(self.check["id"], rect["id"],
                                         "op1", "operator")
        with self.assertRaises(ConflictError):
            self.service.close_rectification(self.check["id"], rect["id"],
                                             "op1", "operator")


if __name__ == "__main__":
    unittest.main()
