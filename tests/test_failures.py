import tempfile
import unittest
from pathlib import Path

from src.domain import (ConflictError, PermissionDenied, ValidationError)
from src.repository import Repository
from src.service import Service


def base(**over):
    p = {
        "outfall": "DW009", "sampling_time": "2026-09-26T08:00:00+00:00",
        "instant_value": 40, "daily_value": 35,
        "instant_limit": 50, "daily_limit": 30,
        "facility_state": "running",
    }
    p.update(over)
    return p


class FailureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.case = self.service.register(base(), "operator_li", "operator")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _to_remediation(self, version):
        return self.service.review(self.case["id"],
                                   {"result": "exceedance", "expected_version": version},
                                   "officer_wang", "compliance_officer")

    def test_role_guards(self):
        # viewer 不能登记/复核
        with self.assertRaises(PermissionDenied):
            self.service.register(base(sampling_time="2026-09-27T00:00:00+00:00"),
                                  "x", "viewer")
        with self.assertRaises(PermissionDenied):
            self.service.review(self.case["id"],
                                {"result": "exceedance",
                                 "expected_version": self.case["version"]},
                                "x", "operator")
        # operator 不能更正限值
        with self.assertRaises(PermissionDenied):
            self.service.correct(self.case["id"],
                                 {"instant_limit": 1, "daily_limit": 1,
                                  "facility_state": "running",
                                  "expected_version": self.case["version"]},
                                 "x", "operator")

    def test_version_conflict(self):
        with self.assertRaises(ConflictError):
            self.service.review(self.case["id"],
                                {"result": "exceedance", "expected_version": 999},
                                "officer_wang", "compliance_officer")

    def test_validation(self):
        with self.assertRaises(ValidationError):
            self.service.register(base(outfall="  "), "x", "operator")
        with self.assertRaises(ValidationError):
            self.service.register(
                base(sampling_time="not-a-time",
                     external_ref=None), "x", "operator")
        with self.assertRaises(ValidationError):
            self.service.register(
                base(instant_value=None, daily_value=None,
                     sampling_time="2026-09-26T12:00:00+00:00"),
                "x", "operator")

    def test_close_invariant_and_recheck_after_correction(self):
        rem = self._to_remediation(self.case["version"])
        # 非整改中状态拒绝复测/整改登记
        pending = self.service.register(
            base(outfall="DW010", daily_value=10, instant_value=1,
                 sampling_time="2026-09-26T13:00:00+00:00"),
            "operator_li", "operator")
        self.assertEqual(pending["status"], "registered")
        with self.assertRaises(ConflictError):
            self.service.add_record(pending["id"], {"kind": "retest", "daily_value": 1},
                                    "operator_li", "operator")

        # 不满足门槛不能结案
        with self.assertRaises(ConflictError):
            self.service.close_case(rem["id"], {"expected_version": rem["version"]},
                                    "officer_wang", "compliance_officer")

        # 满足门槛结案
        self.service.add_record(rem["id"], {"kind": "retest", "daily_value": 10},
                                "operator_li", "operator")
        cur = self.service.get_case(rem["id"], "viewer")
        closed = self.service.close_case(cur["id"], {"expected_version": cur["version"]},
                                         "officer_wang", "compliance_officer")

        # 更正后必须重新由另一名合规人员复核；旧复核/结案字段清空
        corrected = self.service.correct(
            closed["id"], {"instant_limit": 50, "daily_limit": 40,
                           "facility_state": "running",
                           "expected_version": closed["version"]},
            "officer_chen", "compliance_officer")
        self.assertEqual(corrected["status"], "pending")
        self.assertIsNone(corrected["reviewed_by"])
        self.assertIsNone(corrected["review_result"])
        self.assertIsNone(corrected["closed_by"])
        # 不能跳过复核直接结案
        with self.assertRaises(ConflictError):
            self.service.close_case(corrected["id"],
                                    {"expected_version": corrected["version"]},
                                    "officer_chen", "compliance_officer")


if __name__ == "__main__":
    unittest.main()
