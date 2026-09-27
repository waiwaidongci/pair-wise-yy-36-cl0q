import unittest
from src import rules
from src.domain import ConflictError, ValidationError


class RulesTest(unittest.TestCase):
    def test_instant_and_daily_evaluated_separately(self):
        # 瞬时超标但日均未超：只产生瞬时阻断，不得当成日均超标结论
        ev = rules.evaluate_reading(
            facility_status='running', sampled_at='2026-09-26T08:00:00+00:00',
            calibrated_at='2026-09-01T00:00:00+00:00', instant=12, daily=5,
            limit_instant=10, limit_daily=8)
        self.assertTrue(ev['instant_exceeded'])
        self.assertFalse(ev['daily_exceeded'])
        self.assertIn('instant_exceeded', ev['blockers'])
        self.assertNotIn('daily_exceeded', ev['blockers'])
        self.assertEqual(ev['severity'], 'exceedance')

        # 日均超标、瞬时未超同样独立成立
        ev2 = rules.evaluate_reading(
            facility_status='running', sampled_at='2026-09-26T08:00:00+00:00',
            calibrated_at='2026-09-01T00:00:00+00:00', instant=9, daily=9,
            limit_instant=10, limit_daily=8)
        self.assertFalse(ev2['instant_exceeded'])
        self.assertTrue(ev2['daily_exceeded'])

    def test_stopped_and_expired_calibration_trigger_review(self):
        ev = rules.evaluate_reading(
            facility_status='stopped', sampled_at='2026-09-26T08:00:00+00:00',
            calibrated_at=None, instant=1, daily=1,
            limit_instant=10, limit_daily=8)
        self.assertEqual(ev['severity'], 'watch')
        self.assertIn('facility_stopped', ev['blockers'])
        self.assertIn('calibration_expired', ev['blockers'])
        self.assertEqual(rules.initial_status(ev['blockers']), 'pending_review')

        # 校准早于采样但超过30天 → 过期；采样前30天内 → 有效
        self.assertFalse(rules.calibration_ok(
            '2026-09-26T08:00:00+00:00', '2026-08-01T00:00:00+00:00'))
        self.assertTrue(rules.calibration_ok(
            '2026-09-26T08:00:00+00:00', '2026-08-27T08:00:00+00:00'))
        # 校准晚于采样时刻不接受
        self.assertFalse(rules.calibration_ok(
            '2026-09-26T08:00:00+00:00', '2026-09-27T00:00:00+00:00'))

    def test_clean_reading_registers_without_review(self):
        ev = rules.evaluate_reading(
            facility_status='running', sampled_at='2026-09-26T08:00:00+00:00',
            calibrated_at='2026-09-20T00:00:00+00:00', instant=1, daily=1,
            limit_instant=10, limit_daily=8)
        self.assertEqual(ev['blockers'], [])
        self.assertEqual(ev['severity'], 'normal')
        self.assertEqual(rules.initial_status([]), 'registered')

    def test_major_severity_ratio(self):
        ev = rules.evaluate_reading(
            facility_status='running', sampled_at='2026-09-26T08:00:00+00:00',
            calibrated_at='2026-09-20T00:00:00+00:00', instant=30, daily=None,
            limit_instant=10, limit_daily=8)
        self.assertEqual(ev['severity'], 'major')

    def test_reviewer_must_be_another_person(self):
        self.assertTrue(rules.reviewer_must_be_another('op1', None, 'co2'))
        self.assertFalse(rules.reviewer_must_be_another('op1', None, 'op1'))
        check = {'status': 'pending_review', 'created_by': 'op1',
                 'last_corrected_by': 'co2'}
        self.assertFalse(rules.reviewer_must_be_another('op1', 'co2', 'co2'))
        self.assertIn('另一名合规人员', '；'.join(rules.review_eligible(check, 'co2')))
        check['status'] = 'confirmed'
        self.assertTrue(any('待复核' in p for p in rules.review_eligible(check, 'co3')))

    def test_closure_blockers_for_major(self):
        major = {'status': 'confirmed', 'severity': 'major',
                 'review_conclusion': 'confirmed'}
        self.assertEqual(
            rules.closure_blockers(major, 1, False),
            ['重大事件缺少复核合格后的复测达标记录', '仍有未关闭的整改事项'])
        self.assertEqual(rules.closure_blockers(major, 0, True), [])
        # 复核已排除（波动）的不受重大事件门槛限制
        rejected = dict(major, review_conclusion='rejected')
        self.assertEqual(rules.closure_blockers(rejected, 2, False), [])
        # 未复核不能结案
        self.assertTrue(rules.closure_blockers(
            {'status': 'pending_review'}, 0, False))

    def test_transition_guards(self):
        with self.assertRaises(ConflictError):
            rules.validate_transition('registered', 'closed')
        rules.validate_transition('confirmed', 'closed')
        with self.assertRaises(ValidationError):
            rules.evaluate_reading(
                facility_status='bogus', sampled_at='2026-09-26T08:00:00+00:00',
                calibrated_at=None, instant=1, daily=None,
                limit_instant=10, limit_daily=8)


if __name__ == "__main__":
    unittest.main()
