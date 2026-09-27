import unittest

from src import rules
from src.domain import ConflictError, ValidationError


def case(inst=None, daily=None, ilimit=50, dlimit=30, facility="running", cal=None):
    return {
        "instant_value": inst, "daily_value": daily,
        "instant_limit": ilimit, "daily_limit": dlimit,
        "facility_state": facility, "calibrated_until": cal,
    }


class RulesTest(unittest.TestCase):
    def test_flags_keep_instant_and_daily_separate(self):
        # 仅瞬时超标：瞬时波动，不算重大
        flags = rules.evaluate_flags(case(inst=60, daily=20))
        self.assertIn("instantaneous_exceedance", flags)
        self.assertNotIn("daily_exceedance", flags)
        self.assertFalse(rules.is_major(flags))
        # 日均超标：重大事件
        flags = rules.evaluate_flags(case(inst=10, daily=31))
        self.assertIn("daily_exceedance", flags)
        self.assertTrue(rules.is_major(flags))
        # 未超标：无标记
        self.assertEqual(rules.evaluate_flags(case(inst=10, daily=10)), [])

    def test_facility_shutdown_and_calibration(self):
        self.assertIn("facility_shutdown",
                      rules.evaluate_flags(case(inst=1, facility="shutdown")))
        self.assertIn("calibration_expired",
                      rules.evaluate_flags(case(inst=1, cal="2000-01-01T00:00:00+00:00"),
                                           now="2026-01-01T00:00:00+00:00"))
        # 校准未过期不标记
        self.assertNotIn("calibration_expired",
                         rules.evaluate_flags(case(inst=1, cal="2030-01-01T00:00:00+00:00"),
                                              now="2026-01-01T00:00:00+00:00"))

    def test_review_routing(self):
        major = ["daily_exceedance"]
        transient = ["instantaneous_exceedance"]
        self.assertEqual(rules.review_target("exceedance", major), "remediation")
        self.assertEqual(rules.review_target("exceedance", transient), "reviewed")
        self.assertEqual(rules.review_target("transient", transient), "reviewed")
        self.assertEqual(rules.review_target("invalid_data", ["facility_shutdown"]), "reviewed")

    def test_close_blockers(self):
        self.assertEqual(rules.close_blockers(2, True), ["仍有2项整改未关闭"])
        self.assertEqual(rules.close_blockers(0, False), ["尚无达标复测"])
        self.assertEqual(rules.close_blockers(1, False),
                         ["尚无达标复测", "仍有1项整改未关闭"])
        self.assertEqual(rules.close_blockers(0, True), [])

    def test_retest_against_limits(self):
        c = case(ilimit=50, dlimit=30)
        self.assertTrue(rules.retest_passed({"instant_value": 49, "daily_value": 30}, c))
        self.assertFalse(rules.retest_passed({"instant_value": 50.1}, c))
        self.assertIsNone(rules.retest_passed({}, c))

    def test_transition_guards(self):
        self.assertTrue("reviewed" in rules.TRANSITIONS["pending"])
        rules.validate_transition("pending", "remediation")
        with self.assertRaises(ConflictError):
            rules.validate_transition("registered", "closed")
        # 更正后允许从已复核/已结案回退到待复核
        rules.validate_transition("closed", "pending")


if __name__ == "__main__":
    unittest.main()
